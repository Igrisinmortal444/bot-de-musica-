"""Comandos del bot + lógica compartida de envío y paginación."""

import logging
import math
import os
import re
import secrets
import shutil
import time
from urllib.parse import urlparse

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import ContextTypes

from services import cache, downloader
from services import search as search_api

import state

logger = logging.getLogger(__name__)

PAGE_SIZE = 5
EMOJI = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"]


def ht(text) -> str:
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


HOME_TEXT = (
    "<b>🎧 MusicPowerBot</b>\n\n"
    "El buscador de música más potente, con catálogo de <b>todo el mundo</b> 🌍\n\n"
    "🎵 Búsqueda por <b>artista</b> o <b>canción</b>\n"
    "🔗 Descarga desde <b>YouTube</b> (y miles de webs más)\n"
    "⚡ La <b>mejor calidad de audio</b> disponible\n"
    "▶️ Vista previa de 30 segundos\n\n"
    "<b>¿Cómo funciona?</b>\n"
    "1️⃣ /search <i>artista - canción</i>\n"
    "2️⃣ /link <i>https://…</i>\n"
    "3️⃣ O simplemente escribe el nombre aquí 🚀"
)

HELP_TEXT = (
    "<b>❓ Ayuda</b>\n\n"
    "<b>Buscar música</b>\n"
    "→ /search <i>artista - canción</i>\n"
    "→ o escribe directamente: <i>Bad Bunny Monaco</i>\n\n"
    "<b>Descargar un enlace</b>\n"
    "→ /link <i>https://www.youtube.com/watch?v=…</i>\n\n"
    "En la lista de resultados elige la canción y púlsala:\n"
    "💿 <b>MP3 320 kbps</b> = el clásico compatible con todo\n\n"
    "<b>Tip:</b> también acepto enlaces de Spotify, SoundCloud, Deezer, Vimeo…\n"
    "Más info con /stats"
)


def home_keyboard():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🎵 Buscar música", callback_data="ask_search"),
             InlineKeyboardButton("🔗 Pasar enlace", callback_data="ask_link")],
            [InlineKeyboardButton("❓ Ayuda", callback_data="ask_help")],
        ]
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HOME_TEXT, parse_mode="HTML", disable_web_page_preview=True, reply_markup=home_keyboard())


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT, parse_mode="HTML", disable_web_page_preview=True)


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uptime = int(time.time() - state.started_at)
    h, rem = divmod(uptime, 3600)
    m, s = divmod(rem, 60)
    text = (
        f"📊 <b>Estadísticas</b>\n\n"
        f"🔎 Búsquedas: <b>{state.searches}</b>\n"
        f"⬇️ Descargas: <b>{state.downloads}</b>\n"
        f"⏱ Activo: <b>{h}h {m}m {s}s</b>\n\n"
        f"<i>Hecho con 🎵 por ti, usando yt-dlp · yt-dlp de código abierto.</i>"
    )
    await update.message.reply_text(text, parse_mode="HTML")


# ------------------------------------------------------------------ búsqueda

async def search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    parts = (update.message.text or "").split(maxsplit=1)
    query = parts[1].strip() if len(parts) > 1 else None
    if not query:
        await update.message.reply_text(
            "🎵 Ejemplo de uso:\n<code>/search Bad Bunny Monaco</code>\n\n"
            "También puedes escribirme el nombre directamente.",
            parse_mode="HTML",
        )
        return
    await _do_search(update, query)


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Mensajes de texto en chat privado: un enlace se descarga, lo demás se busca."""
    text = (update.message.text or "").strip()
    if not text:
        return
    if text.lower().startswith(("http://", "https://")):
        await _handle_link(update, text[:500])
    else:
        await _do_search(update, text[:200])


async def _do_search(update: Update, raw_query: str) -> None:
    query = re.sub(r"\s+", " ", raw_query).strip()
    state.searches += 1

    status = await update.message.reply_text("🔍 Buscando en todo el mundo…")
    try:
        results, source = await search_api.search_global(query)
    except Exception:  # noqa: BLE001
        logger.exception("Error buscando")
        await status.edit_text("❌ Falló la búsqueda. Inténtalo de nuevo en un momento.")
        return

    if not results:
        await status.edit_text(
            "😕 No encontré eso en mi base de datos mundial.\n\n"
            "Pásame el enlace de YouTube directamente:\n<code>/link https://youtu.be/…</code>",
            parse_mode="HTML",
        )
        return

    token = secrets.token_hex(4)
    cache.set_token(token, results)
    await _send_page(update.effective_chat.id, update.get_bot(), token, 0, message_id=status.message_id)


def _fmt_item(item: dict, idx: int) -> str:
    line = f"{EMOJI[idx]} <b>{ht(item['title'])}</b> — <i>{ht(item['artist'])}</i>"
    extra = []
    if item.get("album"):
        extra.append(ht(item["album"]))
    if item.get("year"):
        extra.append(ht(item["year"]))
    if item.get("duration"):
        extra.append(f"{item['duration'] // 60}:{item['duration'] % 60:02d}")
    if extra:
        line += f"\n   💿 {', '.join(extra)}"
    return line


async def _send_page(chat_id: int, bot, token: str, page: int, message_id: int | None = None) -> None:
    results = cache.get_token(token)
    if not results:
        await bot.send_message(chat_id, "⌛️ Los resultados caducaron. Vuelve a buscar.")
        return

    total = len(results)
    pages = max(1, math.ceil(total / PAGE_SIZE))
    page = max(0, min(page, pages - 1))
    start = page * PAGE_SIZE
    chunk = results[start : start + PAGE_SIZE]

    lines = [f"🎵 <b>Resultados</b> ({total})", ""]
    lines += [_fmt_item(item, i) for i, item in enumerate(chunk)]
    text = "\n".join(lines)

    buttons = []
    for i, item in enumerate(chunk):
        label = f"{i + 1}. {item['title'][:28]} - {item['artist'][:18]}"
        buttons.append([InlineKeyboardButton(label, callback_data=f"s:{token}:{start + i}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"pg:{token}:{page - 1}"))
    nav.append(InlineKeyboardButton(f"📄 {page + 1}/{pages}", callback_data="noop"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"pg:{token}:{page + 1}"))
    buttons.append(nav)
    buttons.append([InlineKeyboardButton("🏠 Inicio", callback_data="home")])

    kb = InlineKeyboardMarkup(buttons)
    if message_id:
        await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, parse_mode="HTML", reply_markup=kb)
    else:
        await bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=kb)


# ---------------------------------------------------------------------- links

async def link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    parts = (update.message.text or "").split(maxsplit=1)
    url = parts[1].strip() if len(parts) > 1 else None
    if not url or not re.match(r"^https?://", url):
        await update.message.reply_text(
            "🔗 Pásame un enlace válido:\n<code>/link https://www.youtube.com/watch?v=…</code>",
            parse_mode="HTML",
        )
        return
    await _handle_link(update, url[:500])


async def _handle_link(update: Update, url: str) -> None:
    state.downloads += 1
    host = urlparse(url).netloc or "enlace"
    status = await update.message.reply_text(
        f"🔗 Procesando <code>{ht(host)}</code>…\n⏳ Buscando la mejor calidad de audio",
        parse_mode="HTML",
    )

    await update.effective_chat.send_action(ChatAction.UPLOAD_DOCUMENT)
    try:
        res = await downloader.download(url, "mp3", _make_progress(status))
    except downloader.DownloadError as exc:
        logger.warning("Descarga fallida: %s", exc)
        await status.edit_text(
            "❌ No pude descargar ese enlace.\n"
            "🔎 Verifica que el video exista y no esté restringido por edad o país."
        )
        return

    try:
        await _send_audio(update.get_bot(), update.effective_chat.id, res)
        await status.edit_text("✅ ¡Listo! Disfruta tu música 🎧\n¿Quieres otra? Busca /search o pásame otro enlace.")
    except Exception:  # noqa: BLE001
        logger.exception("Fallo al enviar el audio")
        try:
            await status.edit_text("❌ Falló el envío (mira que el archivo no pese más de 45 MB).")
        except Exception:  # noqa: BLE001
            pass
    finally:
        _cleanup(res)


def _make_progress(status_msg):
    last_percent = [0]
    last_time = [0.0]

    async def cb(percent: int) -> None:
        now = time.time()
        if percent - last_percent[0] < 5 and now - last_time[0] < 4:
            return
        last_percent[0], last_time[0] = percent, now
        try:
            await status_msg.edit_text(f"⬇️ <b>Descargando…</b> {percent}%\n⚡ Buscando la mejor calidad")
        except Exception:  # noqa: BLE001
            pass

    return cb


# ---------------------------------------------------------------------- envío

async def _send_audio(bot, chat_id: int, res: dict, extra_thumb: str | None = None) -> None:
    kwargs = {}
    if res.get("title"):
        kwargs["title"] = res["title"][:120]
    if res.get("artist"):
        kwargs["performer"] = res["artist"][:120]
    if res.get("duration"):
        kwargs["duration"] = int(res["duration"])

    thumb = extra_thumb or res.get("thumbnail")
    caption = f"🎵 <b>{ht(res.get('title'))}</b>\n👤 {ht(res.get('artist') or 'Desconocido')}\n\n⚡ Audio de alta calidad · {ht(res.get('ext', '').upper())}"

    await bot.send_audio(
        chat_id,
        audio=res["file_path"],
        thumbnail=thumb,
        caption=caption,
        parse_mode="HTML",
        **kwargs,
    )


def _cleanup(res: dict, extra_thumb: str | None = None) -> None:
    for path in (res.get("file_path"), res.get("thumbnail"), extra_thumb):
        if path and os.path.isfile(path):
            try:
                os.remove(path)
            except OSError:
                pass
    outdir = res.get("outdir")
    if outdir:
        shutil.rmtree(outdir, ignore_errors=True)