"""Comandos del bot + lógica compartida de envío, búsqueda y paginación."""

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
SEP = "━━━━━━━━━━━━━━━━━━━━"
SIGN = "👑 <b>Igris Inmortal</b>"

ORIGIN_LABEL = {
    "itunes": "🍏 iTunes",
    "deezer": "🎶 Deezer",
    "musicbrainz": "🌍 MusicBrainz",
    "none": "global",
}


def ht(text) -> str:
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _bar(percent: int) -> str:
    filled = max(0, min(10, round(percent / 10)))
    return "▰" * filled + "▱" * (10 - filled)


HOME_TEXT = (
    "🎧 <b>MUSIC POWER BOT</b> 🎧\n" + SEP + "\n\n"
    "⚡ El buscador de música <b>más potente del mundo</b>\n"
    "🌍 Catálogo global · cualquier artista, país y género\n\n"
    "🎵 Busca por <b>nombre</b> o 🎤 <b>artista</b>\n"
    "🔗 Descarga por <b>enlace</b> (YouTube, Spotify, SoundCloud…)\n"
    "💎 <b>Mejor calidad de audio</b> con carátulas en HD\n"
    "▶️ Vista previa de 30 segundos\n"
    "📋 Envía vistas previas desde cualquier chat (modo inline)\n\n"
    + SEP + "\n\n"
    "👑 Creado por " + SIGN
)

HELP_TEXT = (
    "❓ <b>AYUDA · Music Power Bot</b>\n" + SEP + "\n\n"
    "🎵 <b>Buscar por nombre</b>\n"
    "→ escribe directo: <code>Bad Bunny Monaco</code>\n"
    "→ o con el comando: <code>/search Bad Bunny Monaco</code>\n\n"
    "🔗 <b>Descargar por enlace</b>\n"
    "→ <code>/link https://www.youtube.com/watch?v=…</code>\n"
    "→ acepto YouTube, Spotify, SoundCloud, Deezer, Vimeo y más\n\n"
    "🧰 <b>Calidad disponible</b>\n"
    "🔊 M4A · calidad nativa (mejor bitrate)\n"
    "💿 MP3 320 · el clásico compatible con todo\n"
    "▶️ Vista previa 30 s antes de descargar\n\n"
    "💡 <b>Tip:</b> en cualquier chat escribe\n"
    "<code>@Familyaudiox_bot canción</code> para enviar vistas previas.\n\n"
    + SEP + "\n\n"
    "👑 Creado por " + SIGN
)

STATS_TEXT = (
    "📊 <b>Music Power Bot · Stats</b>\n" + SEP + "\n\n"
    "🔎 Búsquedas: <b>{searches}</b>\n"
    "⬇️ Descargas: <b>{downloads}</b>\n"
    "⏱ Activo: <b>{up}</b>\n\n"
    "⚡ Fuentes: 🍏 iTunes · 🎶 Deezer · 🌍 MusicBrainz · ▶️ YouTube\n\n"
    + SEP + "\n\n"
    "👑 Creado por " + SIGN
)


def home_keyboard():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🎵 Buscar música", callback_data="ask_search"),
                InlineKeyboardButton("🔗 Pasar enlace", callback_data="ask_link"),
            ],
            [
                InlineKeyboardButton("📊 Estadísticas", callback_data="ask_stats"),
                InlineKeyboardButton("❓ Ayuda", callback_data="ask_help"),
            ],
        ]
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        HOME_TEXT, parse_mode="HTML", disable_web_page_preview=True, reply_markup=home_keyboard()
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT, parse_mode="HTML", disable_web_page_preview=True)


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uptime = int(time.time() - state.started_at)
    h, rem = divmod(uptime, 3600)
    m, s = divmod(rem, 60)
    up = f"{h}h {m}m {s}s"
    await update.message.reply_text(
        STATS_TEXT.format(searches=state.searches, downloads=state.downloads, up=up),
        parse_mode="HTML",
    )


# ------------------------------------------------------------------ búsqueda

async def search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    parts = (update.message.text or "").split(maxsplit=1)
    query = parts[1].strip() if len(parts) > 1 else None
    if not query:
        await update.message.reply_text(
            "🎵 <b>Búsqueda musical</b>\n\n"
            "Usa así: <code>/search Bad Bunny Monaco</code>\n\n"
            "También puedes escribirme el nombre directamente. 🚀",
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
        results, origin = await search_api.search_global(query)
    except Exception:  # noqa: BLE001
        logger.exception("Error buscando")
        await status.edit_text("❌ Falló la búsqueda. Inténtalo de nuevo en un momento.")
        return

    if not results:
        await status.edit_text(
            "😕 <b>No encontré eso en mi catálogo mundial.</b>\n\n"
            "Pásame el enlace de YouTube directamente:\n"
            "<code>/link https://youtu.be/…</code>",
            parse_mode="HTML",
        )
        return

    token = secrets.token_hex(4)
    cache.set_token(token, {"results": results, "origin": origin})
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
        line += f"\n    💿 {', '.join(extra)}"
    return line


async def _send_page(chat_id: int, bot, token: str, page: int, message_id: int | None = None) -> None:
    from handlers.callbacks import _load_token

    payload = cache.get_token(token)
    if not payload:
        await bot.send_message(chat_id, "⌛️ Los resultados caducaron. Vuelve a buscar.")
        return

    results = payload["results"]
    origin = ORIGIN_LABEL.get(payload.get("origin", ""), "global")
    total = len(results)
    pages = max(1, math.ceil(total / PAGE_SIZE))
    page = max(0, min(page, pages - 1))
    start_i = page * PAGE_SIZE
    chunk = results[start_i : start_i + PAGE_SIZE]

    lines = [
        f"🎵 <b>Resultados · {origin}</b> ({total})",
        SEP,
        "",
    ]
    lines += [_fmt_item(item, i) for i, item in enumerate(chunk)]
    lines.append("\n" + SEP)
    text = "\n".join(lines)

    buttons = []
    for i, item in enumerate(chunk):
        label = f"{EMOJI[i]} {item['title'][:26]} — {item['artist'][:16]}"
        buttons.append([InlineKeyboardButton(label, callback_data=f"s:{token}:{start_i + i}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"pg:{token}:{page - 1}"))
    nav.append(InlineKeyboardButton(f"📄 {page + 1}/{pages}", callback_data="noop"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"pg:{token}:{page + 1}"))
    if nav:
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
    host = urlparse(url).netloc.replace("www.", "") or "enlace"
    status = await update.message.reply_text(
        f"🔗 <b>Descargando · {ht(host)}</b>\n"
        "⏳ Buscando la mejor calidad de audio…",
        parse_mode="HTML",
    )

    await update.effective_chat.send_action(ChatAction.UPLOAD_DOCUMENT)
    try:
        res = await downloader.download(url, "m4a", _make_progress(status))
    except downloader.DownloadError as exc:
        logger.warning("Descarga fallida: %s", exc)
        await status.edit_text(
            "❌ <b>No pude descargar ese enlace.</b>\n"
            "🔎 Verifica que el vídeo exista y no esté restringido por edad o país.",
            parse_mode="HTML",
        )
        return

    try:
        await _send_audio(update.get_bot(), update.effective_chat.id, res)
        await status.edit_text(
            "✅ <b>¡Listo! Disfruta tu música 🎧</b>\n"
            "¿Quieres otra? Busca /search o pásame otro enlace.",
            parse_mode="HTML",
        )
    except Exception:  # noqa: BLE001
        logger.exception("Fallo al enviar el audio")
        try:
            await status.edit_text(
                "❌ Falló el envío (mira que el archivo no pese más de 45 MB).",
                parse_mode="HTML",
            )
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
            await status_msg.edit_text(
                f"⬇️ <b>Descargando…</b> {percent}%\n"
                f"{_bar(percent)}\n"
                "⚡ Buscando la mejor calidad de audio",
                parse_mode="HTML",
            )
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
    caption = (
        f"🎧 <b>{ht(res.get('title') or '♪')}</b>\n"
        f"👤 {ht(res.get('artist') or 'Desconocido')}\n\n"
        f"⚡ Audio de alta calidad · <code>{ht(res.get('ext', '').upper())}</code>\n\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"👑 Creado por Igris Inmortal"
    )

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