"""Respuestas a los botones del menú (callback queries)."""

import asyncio
import logging
import shutil
import tempfile

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from handlers.commands import (
    HELP_TEXT,
    HOME_TEXT,
    SEP,
    SIGN,
    STATS_TEXT,
    _cleanup,
    _make_progress,
    _send_audio,
    ht,
    _send_page,
)
from services import cache, deezer, downloader
from services import search as search_api

import state

logger = logging.getLogger(__name__)


def _load_token(token: str) -> tuple[list | None, str]:
    """Devuelve (resultados, origen) o (None, '') si caducó."""
    payload = cache.get_token(token)
    if not payload:
        return None, ""
    return payload["results"], payload.get("origin", "")


async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    data = q.data or ""
    await q.answer()

    try:
        if data == "home":
            await q.edit_message_text(HOME_TEXT, parse_mode="HTML", disable_web_page_preview=True, reply_markup=_help_kb())
        elif data == "ask_help":
            await q.edit_message_text(HELP_TEXT, parse_mode="HTML", disable_web_page_preview=True)
        elif data == "ask_stats":
            await q.edit_message_text(_stats_text(), parse_mode="HTML")
        elif data == "ask_search":
            await q.edit_message_text(
                "🎵 <b>Buscar música</b>\n\n"
                "Escribe el <b>artista</b> y/o la <b>canción</b> que quieres.\n"
                "Ejemplo: <code>Quevedo Hotel Arizona</code>",
                parse_mode="HTML",
            )
        elif data == "ask_link":
            await q.edit_message_text(
                "🔗 <b>Pasar enlace</b>\n\n"
                "Pásame el enlace de YouTube (o Spotify, SoundCloud…):\n"
                "<code>/link https://www.youtube.com/watch?v=…</code>",
                parse_mode="HTML",
            )
        elif data == "noop":
            pass
        elif data.startswith("pg:"):
            _, token, page = data.split(":", 2)
            await _send_page(q.message.chat_id, context.bot, token, int(page), q.message.message_id)
        elif data.startswith("back:"):
            token = data.split(":", 1)[1]
            await _send_page(q.message.chat_id, context.bot, token, 0, q.message.message_id)
        elif data.startswith("s:"):
            _, token, idx = data.split(":")
            await _pick_quality(q, token, int(idx))
        elif data.startswith("dl:"):
            _, token, idx, quality = data.split(":")
            await _start_download(q, context, token, int(idx), quality)
        elif data.startswith("pv:"):
            _, token, idx = data.split(":")
            await _preview(q, context, token, int(idx))
    except TelegramError as exc:
        logger.warning("Callback fallido (%s): %s", data, exc)
    except ValueError:
        logger.warning("Callback malformado: %s", data)
    except Exception:  # noqa: BLE001
        logger.exception("Error procesando callback %s", data)


def _help_kb():
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


def _stats_text() -> str:
    import time
    uptime = int(time.time() - state.started_at)
    h, rem = divmod(uptime, 3600)
    m, s = divmod(rem, 60)
    return STATS_TEXT.format(
        searches=state.searches, downloads=state.downloads, up=f"{h}h {m}m {s}s"
    )


def _get_item(token: str, idx: int) -> dict | None:
    results, _ = _load_token(token)
    if not results:
        return None
    try:
        return results[idx]
    except (IndexError, TypeError):
        return None


async def _pick_quality(q, token: str, idx: int) -> None:
    item = _get_item(token, idx)
    if not item:
        await q.edit_message_text("⌛️ Los resultados caducaron. Vuelve a buscar.")
        return

    extra = []
    if item.get("album"):
        extra.append(ht(item["album"]))
    if item.get("year"):
        extra.append(f"🗓 {ht(item['year'])}")
    if item.get("duration"):
        extra.append(f"⏱ {item['duration'] // 60}:{item['duration'] % 60:02d}")

    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🔊 Mejor calidad · M4A", callback_data=f"dl:{token}:{idx}:m4a"),
                InlineKeyboardButton("💿 MP3 320 kbps", callback_data=f"dl:{token}:{idx}:mp3"),
            ],
            [InlineKeyboardButton("▶️ Vista previa 30 s", callback_data=f"pv:{token}:{idx}")],
            [
                InlineKeyboardButton("🔙 Resultados", callback_data=f"back:{token}"),
                InlineKeyboardButton("🏠 Inicio", callback_data="home"),
            ],
        ]
    )
    text = (
        f"🎧 <b>{ht(item['title'])}</b>\n"
        f"👤 {ht(item['artist'])}\n"
        + (f"💿 {', '.join(extra)}\n\n" if extra else "\n")
        + "━━━━━━━━━━━━━━━━\n"
        + "<b>Elige la calidad:</b>\n"
        "🔊 M4A · calidad nativa (máxima)\n"
        "💿 MP3 320 · el clásico universal"
    )
    await q.edit_message_text(text, parse_mode="HTML", reply_markup=kb)


async def _start_download(q, context: ContextTypes.DEFAULT_TYPE, token: str, idx: int, quality: str) -> None:
    item = _get_item(token, idx)
    if not item:
        await q.edit_message_text("⌛️ Los resultados caducaron. Vuelve a buscar.")
        return

    state.downloads += 1
    chat_id = q.message.chat_id
    status = await context.bot.send_message(chat_id, "⏳ Preparando la descarga…")
    res = None
    via = None

    # 1) Deezer en MP3 320 (HQ con metadatos y carátula) si hay ARL. Usa el
    #    ID de Deezer del resultado o lo busca si no viene.
    dz_id = item.get("deezer_id")
    if deezer.active() and (dz_id or item.get("artist")):
        if not dz_id:
            dz_id = await asyncio.to_thread(
                deezer.search_track_id, item.get("artist", ""), item.get("title", "")
            )
        if dz_id:
            deezer_out = tempfile.mkdtemp(prefix="musicbot_dz_")
            try:
                await status.edit_text("🎧 Descargando desde <b>Deezer</b> (MP3 320)…", parse_mode="HTML")
                await context.bot.send_chat_action(chat_id, ChatAction.UPLOAD_DOCUMENT)
                res = await asyncio.to_thread(deezer.download_track, deezer_out, dz_id)
                if res:
                    via = "deezer"
                else:
                    shutil.rmtree(deezer_out, ignore_errors=True)
            except Exception:  # noqa: BLE001
                logger.exception("Fallo en Deezer; se intenta SoundCloud")
                shutil.rmtree(deezer_out, ignore_errors=True)

    # 2) SoundCloud (independiente, música real de artistas y remixes).
    if res is None:
        query = f"scsearch1:{item['artist']} - {item['title']}"
        await status.edit_text("🔊 Buscando la canción en <b>SoundCloud</b>…", parse_mode="HTML")
        await context.bot.send_chat_action(chat_id, ChatAction.UPLOAD_DOCUMENT)
        try:
            res = await downloader.download(query, quality, _make_progress(status))
            via = "soundcloud"
        except downloader.DownloadError as exc:
            logger.warning("Descarga fallida: %s", exc)
            await status.edit_text(
                "❌ <b>No pude descargar esa canción.</b>\n"
                "Inténtalo de nuevo o pásame el enlace directo.",
                parse_mode="HTML",
            )
            return

    artwork = None
    try:
        if item.get("artwork"):
            artwork = await search_api.download_artwork(item["artwork"])
        await context.bot.send_chat_action(chat_id, ChatAction.UPLOAD_DOCUMENT)
        await _send_audio(context.bot, chat_id, res, extra_thumb=artwork)
        label = "💿 MP3 320" if (res.get("ext") or "").lower() == "mp3" else "🔊 M4A"
        source = "Deezer" if via == "deezer" else "SoundCloud"
        await status.edit_text(
            f"✅ <b>¡Enviado! Disfrútala 🎧</b>\n{label} · {ht(item['artist'])} — {ht(item['title'])}\n"
            f"Fuente: {source}\n{SIGN}",
            parse_mode="HTML",
        )
    except Exception:  # noqa: BLE001
        logger.exception("Fallo al enviar el audio")
        try:
            await status.edit_text("❌ Falló el envío del audio. Inténtalo de nuevo.")
        except Exception:  # noqa: BLE001
            pass
    finally:
        _cleanup(res, artwork)


async def _preview(q, context: ContextTypes.DEFAULT_TYPE, token: str, idx: int) -> None:
    item = _get_item(token, idx)
    if not item:
        await q.edit_message_text("⌛️ Los resultados caducaron. Vuelve a buscar.")
        return
    if not item.get("preview"):
        await q.edit_message_text("😕 Esta canción no tiene vista previa disponible.")
        return
    try:
        await context.bot.send_audio(
            q.message.chat_id,
            audio=item["preview"],
            title=item["title"][:120],
            performer=item["artist"][:120],
            duration=item.get("duration"),
            caption=f"▶️ <b>Vista previa</b> · 30 segundos\n\n{SIGN}",
            parse_mode="HTML",
        )
    except TelegramError:
        await context.bot.send_message(
            q.message.chat_id,
            "☁️ La vista previa de Apple no está disponible en tu región.\n"
            "Descárgala completa con 🔊 Mejor calidad.",
        )