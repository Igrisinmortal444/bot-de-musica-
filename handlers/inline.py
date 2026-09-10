"""Búsqueda inline: escribe @MusicPowerBot <canción> en cualquier chat
y manda vistas previas de 30 s al instante. Creado por Igris Inmortal."""

import logging

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InlineQueryResultAudio,
    InputTextMessageContent,
    Update,
)
from telegram.ext import ContextTypes

from services import search as search_api

logger = logging.getLogger(__name__)

MAX_INLINE = 30


async def inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = (update.inline_query.query or "").strip()
    results: list = []

    if not query:
        results.append(
            InlineQueryResultArticle(
                id="help",
                title="🎧 Music Power Bot",
                description="Escribe aquí artista o canción. Ej: Bad Bunny Monaco",
                input_message_content=InputTextMessageContent(
                    "🎧 <b>Music Power Bot</b>\n\n"
                    "Escribe el nombre de una canción después de @Familyaudiox_bot "
                    "y te la envío en un toque.\n\n"
                    "👑 Creado por Igris Inmortal",
                    parse_mode="HTML",
                ),
                reply_markup=InlineKeyboardMarkup(
                    [
                        [InlineKeyboardButton("🎵 Abrir en privado", url="https://t.me/Familyaudiox_bot")]
                    ]
                ),
            )
        )
        await update.inline_query.answer(results, cache_time=60, is_personal=True)
        return

    try:
        found, _ = await search_api.search_global(query, limit=20)
    except Exception:  # noqa: BLE001
        logger.exception("Fallo la búsqueda inline")
        found = []

    for i, item in enumerate(found[:MAX_INLINE]):
        preview = item.get("preview")
        base_id = f"mpb{i}"
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("⬇️ Descargar completa", url="https://t.me/Familyaudiox_bot")]]
        )
        if preview:
            results.append(
                InlineQueryResultAudio(
                    id=base_id,
                    audio_url=preview,
                    title=item["title"][:120],
                    performer=item["artist"][:120],
                    audio_duration=item.get("duration"),
                    reply_markup=keyboard,
                )
            )
        else:
            results.append(
                InlineQueryResultArticle(
                    id=base_id,
                    title=item["title"][:120],
                    description=f"👤 {item['artist']} · 💿 {item.get('album') or '—'}",
                    input_message_content=InputTextMessageContent(
                        f"🎧 <b>{item['title']}</b> — {item['artist']}\n"
                        f"💿 {item.get('album') or 'Sencillo'}\n\n"
                        f"📥 Mándame el nombre al MD y te la descargo: "
                        f"<a href=\"https://t.me/Familyaudiox_bot\">@Familyaudiox_bot</a>",
                        parse_mode="HTML",
                        disable_web_page_preview=True,
                    ),
                    reply_markup=keyboard,
                )
            )

    if not results:
        results.append(
            InlineQueryResultArticle(
                id="none",
                title="😕 Sin resultados",
                description="No encontré '" + query[:40] + "' en mi catálogo. Prueba otro nombre.",
                input_message_content=InputTextMessageContent("😕 No encontré esa canción."),
            )
        )

    await update.inline_query.answer(results, cache_time=60, is_personal=True)