"""Respuestas a los botones del menú (callback queries)."""

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from handlers.commands import (
    HELP_TEXT,
    HOME_TEXT,
    _cleanup,
    _make_progress,
    _send_audio,
    ht,
    _send_page,
)
from services import cache, downloader
from services import search as search_api

import state

logger = logging.getLogger(__name__)


async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    data = q.data or ""
    await q.answer()

    try:
        if data == "home":
            await q.edit_message_text(HOME_TEXT, parse_mode="HTML", disable_web_page_preview=True, reply_markup=_help_kb())
        elif data == "ask_help":
            await q.edit_message_text(HELP_TEXT, parse_mode="HTML", disable_web_page_preview=True)
        elif data == "ask_search":
            await q.edit_message_text(
                "🎵 Escribe el <b>artista</b> y/o la <b>canción</b> que quieres.\n"
                "Ejemplo: <code>Quevedo Hotel Arizona</code>",
                parse_mode="HTML",
            )
        elif data == "ask_link":
            await q.edit_message_text(
                "🔗 Pásame el enlace de YouTube (o Spotify, SoundCloud…):\n"
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
    except Exception:  # noqa: BLE001
        logger.exception("Error procesando callback %s", data)


def _help_kb():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🎵 Buscar música", callback_data="ask_search"),
             InlineKeyboardButton("🔗 Pasar enlace", callback_data="ask_link")],
            [InlineKeyboardButton("❓ Ayuda", callback_data="ask_help")],
        ]
    )


def _get_item(token: str, idx: int) -> dict | None:
    results = cache.get_token(token)
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
    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔊 Mejor calidad · M4A", callback_data=f"dl:{token}:{idx}:m4a")],
            [InlineKeyboardButton("💿 MP3 320 kbps", callback_data=f"dl:{token}:{idx}:mp3")],
            [InlineKeyboardButton("▶️ Vista previa 30 s", callback_data=f"pv:{token}:{idx}")],
            [InlineKeyboardButton("🔙 Volver a resultados", callback_data=f"back:{token}")],
        ]
    )
    text = (
        f"🎧 <b>{ht(item['title'])}</b>\n"
        f"👤 {ht(item['artist'])}\n"
        f"💿 {ht(item.get('album') or 'Sencillo')}\n\n"
        f"<b>Elige la calidad:</b>"
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
    query = f"ytsearch1:{item['artist']} - {item['title']}"

    await context.bot.send_chat_action(chat_id, ChatAction.UPLOAD_DOCUMENT)
    try:
        res = await downloader.download(query, quality, _make_progress(status))
    except downloader.DownloadError as exc:
        logger.warning("Descarga fallida: %s", exc)
        await status.edit_text("❌ No pude descargar esa canción.\nInténtalo de nuevo o prueba con el enlace de YouTube.")
        return

    artwork = None
    try:
        if item.get("artwork"):
            artwork = await search_api.download_artwork(item["artwork"])
        await context.bot.send_chat_action(chat_id, ChatAction.UPLOAD_DOCUMENT)
        await _send_audio(context.bot, chat_id, res, extra_thumb=artwork)
        await status.edit_text("✅ ¡Enviado! Disfrútala 🎧\n¿Buscamos otra? /search")
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
            caption="▶️ <b>Vista previa</b> · 30 segundos",
            parse_mode="HTML",
        )
    except TelegramError:
        await context.bot.send_message(
            q.message.chat_id,
            "☁️ La vista previa de Apple no está disponible en tu región.\n"
            "Descárgala completa con 🔊 Mejor calidad.",
        )