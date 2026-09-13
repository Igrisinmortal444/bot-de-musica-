"""Biblioteca musical: /song, /artist, /my, /top, /settings, reconocimiento
de voz (Shazam) y guardado de archivos de audio para búsqueda."""

import base64
import logging
import os
import secrets
import subprocess
import tempfile
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import ContextTypes

from handlers.commands import (
    SEP,
    SIGN,
    _cleanup,
    _do_search,
    _make_progress,
    _send_audio,
    ht,
)
from services import cache, downloader, recognize, search as search_api, storage

import state

logger = logging.getLogger(__name__)

PAGE = 5


def b64e(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode()


def b64d(text: str) -> str:
    try:
        return base64.urlsafe_b64decode(text.encode()).decode()
    except Exception:  # noqa: BLE001
        return ""


def _build_query(artist: str, title: str) -> str:
    artist = (artist or "").strip()
    title = (title or "").strip()
    q = f"{artist} - {title}" if artist else title
    return f"ytsearch1:{q}" if q else ""


# ------------------------------------------------------------------ comandos

async def song(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    parts = (update.message.text or "").split(maxsplit=1)
    query = parts[1].strip() if len(parts) > 1 else None
    if not query:
        await update.message.reply_text(
            "🎵 <b>/song — buscar por título</b>\n\n"
            "Ejemplo: <code>/song Despacito</code>\n"
            "o escríbeme el nombre directamente. 🚀",
            parse_mode="HTML",
        )
        return
    await _do_search(update, query)
    await _suggest_library(update, query)


async def artist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    parts = (update.message.text or "").split(maxsplit=1)
    query = parts[1].strip() if len(parts) > 1 else None
    if not query:
        await update.message.reply_text(
            "🎤 <b>/artist — buscar por cantante</b>\n\n"
            "Ejemplo: <code>/artist Bad Bunny</code>",
            parse_mode="HTML",
        )
        return
    await _do_search(update, query)
    await _suggest_library(update, query)


async def _suggest_library(update: Update, query: str) -> None:
    rows = storage.search_library(query)
    if not rows:
        return
    token = secrets.token_hex(4)
    cache.set_token(token, {"kind": "lib", "rows": rows})
    buttons = [
        [
            InlineKeyboardButton(
                f"🎧 {ht(r['title'])} — {ht(r['artist']) or 'desconocido'}",
                callback_data=f"lib:{token}:{i}",
            )
        ]
        for i, r in enumerate(rows[:5])
    ]
    buttons.append([InlineKeyboardButton("🏠 Inicio", callback_data="home")])
    await update.message.reply_text(
        f"📚 <b>También en tu biblioteca</b> ({len(rows)}):",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def my_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _send_my_page(update.effective_chat.id, context.bot, update.effective_user.id, 0)


async def top_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _send_top_page(update.effective_chat.id, context.bot, 0)


async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _send_settings(update.effective_chat.id, context.bot, update.effective_user.id)


# ------------------------------------------------------------------- voz/audio

def _download_telegram_file(file, path: str) -> None:
    if hasattr(file, "download_to_drive"):
        return file.download_to_drive(custom_path=path)
    return file.download(custom_path=path)


def _to_wav(src: str, dst: str) -> bool:
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", src, "-ar", "16000", "-ac", "1", dst],
            capture_output=True,
            timeout=60,
        )
        return os.path.isfile(dst) and os.path.getsize(dst) > 0
    except Exception as exc:  # noqa: BLE001
        logger.warning("ffmpeg falló al convertir %s: %s", src, exc)
        return False


async def on_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    user_id = msg.from_user.id
    status = await msg.reply_text("🎤 <b>Reconociendo la canción…</b>", parse_mode="HTML")

    fd, path = tempfile.mkstemp(prefix="voice_", suffix=".ogg")
    os.close(fd)
    try:
        file = await msg.voice.get_file()
        await _download_telegram_file(file, path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("No pude leer el mensaje de voz: %s", exc)
        await status.edit_text("❌ No pude leer tu mensaje de voz.")
        return

    result = await recognize.recognize_file(path)
    if not result:
        wav = path.rsplit(".", 1)[0] + ".wav"
        if _to_wav(path, wav):
            result = await recognize.recognize_file(wav)
            try:
                os.remove(wav)
            except OSError:
                pass

    if not result:
        await status.edit_text(
            "😕 <b>No reconocí la canción.</b>\n"
            "Prueba con un mensaje de voz más claro o, si tienes el archivo, "
            "envíamelo como audio y lo guardo en la biblioteca.",
            parse_mode="HTML",
        )
        try:
            os.remove(path)
        except OSError:
            pass
        return

    storage.bump_song(result["title"], result["artist"])
    my_settings = storage.get_settings(user_id)
    if my_settings["autosave"]:
        storage.add_playlist(user_id, result["title"], result["artist"],
                             my_settings["quality"], _build_query(result["artist"], result["title"]))

    query = _build_query(result["artist"], result["title"])
    card_title, card_artist = result["title"], result["artist"]
    text = (
        f"🎤 <b>Reconocida con Shazam</b>\n" + SEP + "\n\n"
        f"🎧 <b>{ht(result['title'])}</b>\n"
        f"👤 {ht(result['artist']) or 'Desconocido'}\n\n"
        "Descárgala en la calidad que prefieras 👇"
    )
    buttons = [
        [
            InlineKeyboardButton("🔊 Descargar M4A", callback_data=f"qdl:m4a:{b64e(query)}"),
            InlineKeyboardButton("💿 MP3 320", callback_data=f"qdl:mp3:{b64e(query)}"),
        ],
        [InlineKeyboardButton("➕ A mi playlist", callback_data=f"save:{b64e(card_title + '||' + (card_artist or ''))}")],
        [InlineKeyboardButton("🏠 Inicio", callback_data="home")],
    ]
    kb = InlineKeyboardMarkup(buttons)

    artwork = None
    try:
        if result.get("artwork"):
            artwork = await search_api.download_artwork(result["artwork"])
        if artwork:
            await context.bot.send_photo(
                msg.chat_id, photo=artwork, caption=text,
                parse_mode="HTML", reply_markup=kb,
            )
        else:
            await context.bot.send_message(
                msg.chat_id, text, parse_mode="HTML", reply_markup=kb,
            )
        await status.edit_text("✅ Listo · puedes descargar desde la tarjeta 👆")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Fallo al mostrar el resultado de Shazam: %s", exc)
        await status.edit_text("😕 Reconocí la canción pero falló el envío.")
    finally:
        if artwork:
            try:
                os.remove(artwork)
            except OSError:
                pass
        try:
            os.remove(path)
        except OSError:
            pass


async def on_audio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    user_id = msg.from_user.id

    if msg.audio is not None:
        audio = msg.audio
        title = (audio.title or "").strip() or (audio.file_name or "").strip() or "Sin título"
        artist = (audio.performer or "").strip()
        mime = audio.mime_type or "audio/mpeg"
        duration = audio.duration
        size = audio.file_size
        file_id = audio.file_id
    else:
        doc = msg.document
        name = (doc.file_name or "audio").rsplit(".", 1)[0]
        title = (msg.caption or "").strip() or name or "Sin título"
        artist = ""
        mime = doc.mime_type or ""
        duration = None
        size = doc.file_size
        file_id = doc.file_id

    storage.add_library(user_id, title, artist, file_id, mime, duration, size)
    storage.bump_song(title, artist)

    text = (
        f"📚 <b>Guardado en la biblioteca</b>\n" + SEP + "\n\n"
        f"🎧 <b>{ht(title)}</b>\n"
        f"👤 {ht(artist) or 'Desconocido'}\n\n"
        "Ya está <b>disponible para búsqueda</b> con /song y /artist."
    )
    buttons = [
        [InlineKeyboardButton("➕ A mi playlist", callback_data=f"save:{b64e((title or '♪') + '||' + (artist or '') + '||' + file_id)}")],
        [InlineKeyboardButton("🏠 Inicio", callback_data="home")],
    ]
    await msg.reply_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))


# ------------------------------------------------------------------- accioneS

async def qdl(q, context: ContextTypes.DEFAULT_TYPE, quality: str, query: str) -> None:
    """Descarga por búsqueda (usada por reconocimiento, /top y /my)."""
    if not query:
        await q.answer("Sin datos para descargar.")
        return
    chat_id = q.message.chat_id
    user_id = q.from_user.id
    settings = storage.get_settings(user_id)
    quality = quality or settings["quality"]
    state.downloads += 1

    status = await context.bot.send_message(chat_id, "⏳ Preparando la descarga…")
    await status.edit_text("🔊 Buscando la canción en <b>YouTube</b>…", parse_mode="HTML", reply_markup=None)
    await context.bot.send_chat_action(chat_id, ChatAction.UPLOAD_DOCUMENT)
    try:
        res = await downloader.download(query, quality, _make_progress(status))
    except downloader.DownloadError as exc:
        logger.warning("Descarga fallida: %s", exc)
        await status.edit_text("❌ <b>No pude descargar esa canción.</b>\nInténtalo de nuevo.", parse_mode="HTML")
        return

    storage.bump_song(res["title"], res["artist"])
    if settings["autosave"]:
        storage.add_playlist(user_id, res["title"], res["artist"], quality, query)

    try:
        await _send_audio(context.bot, chat_id, res)
        label = "💿 MP3 320" if quality == "mp3" else "🔊 M4A"
        await status.edit_text(
            f"✅ <b>¡Enviado! Disfruta tu música 🎧</b>\n{label} · {ht(res['artist'])} — {ht(res['title'])}\n\n{SIGN}",
            parse_mode="HTML",
        )
    except Exception:  # noqa: BLE001
        logger.exception("Fallo al enviar el audio")
        try:
            await status.edit_text("❌ Falló el envío del audio.")
        except Exception:  # noqa: BLE001
            pass
    finally:
        _cleanup(res)


async def save_to_playlist(q, context: ContextTypes.DEFAULT_TYPE, payload: str) -> None:
    parts = payload.split("||")
    title = parts[0] or "♪"
    artist = parts[1] if len(parts) > 1 else ""
    file_id = parts[2] if len(parts) > 2 else ""
    user_id = q.from_user.id
    settings = storage.get_settings(user_id)
    source = _build_query(artist, title) or ""
    if storage.add_playlist(user_id, title, artist, settings["quality"], source, file_id):
        await q.answer("➕ Agregada a tu playlist ✔")
        await context.bot.send_message(
            q.message.chat_id,
            f"➕ <b>Agregada a tu playlist</b>\n🎧 {ht(title)} — {ht(artist) or '—'}\n\n"
            f"Luego puedes pedirla con /my.",
            parse_mode="HTML",
        )
    else:
        await q.answer("Ya estaba en tu playlist 🙂")


async def lib_item(q, context: ContextTypes.DEFAULT_TYPE, token: str, idx: int) -> None:
    payload = cache.get_token(token)
    if not payload or payload.get("kind") != "lib":
        await q.answer("⌛️ Resultados caducados.")
        return
    rows = payload.get("rows") or []
    try:
        row = rows[idx]
    except (IndexError, TypeError):
        await q.answer("No encontrado.")
        return
    await _resend_audio(context.bot, q.message.chat_id, row)


async def lib_send(q, context: ContextTypes.DEFAULT_TYPE, payload: str) -> None:
    parts = payload.split("||")
    row = {
        "title": parts[0] if parts else "♪",
        "artist": parts[1] if len(parts) > 1 else "",
        "file_id": parts[2] if len(parts) > 2 else "",
    }
    if not row["file_id"]:
        await q.answer("Sin audio almacenado.")
        return
    await _resend_audio(context.bot, q.message.chat_id, row)


async def _resend_audio(bot, chat_id: int, row: dict) -> None:
    title = row.get("title") or "♪"
    artist = row.get("artist") or "Desconocido"
    storage.bump_song(title, artist)
    await bot.send_audio(
        chat_id,
        audio=row["file_id"],
        title=title[:120],
        performer=artist[:120],
        caption=f"📚 <b>Desde la biblioteca</b>\n🎧 {ht(title)} — {ht(artist)}\n\n{SIGN}",
        parse_mode="HTML",
    )


# ----------------------------------------------------------------------- /my

async def my_page(q, context: ContextTypes.DEFAULT_TYPE, page: int) -> None:
    await _send_my_page(q.message.chat_id, context.bot, q.from_user.id, page, q.message.message_id)


async def _send_my_page(chat_id: int, bot, user_id: int, page: int, message_id: int | None = None) -> None:
    rows = storage.get_playlist(user_id)
    if not rows:
        text = (
            "📃 <b>Tu lista de reproducción</b>\n" + SEP + "\n\n"
            "Todavía está <b>vacía</b>.\n\n"
            "Descarga canciones con /song y se guardarán aquí, o sube un audio "
            "para guardarlo en la biblioteca."
        )
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Inicio", callback_data="home")]])
        if message_id:
            await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id,
                                        parse_mode="HTML", reply_markup=kb)
        else:
            await bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=kb)
        return

    pages = max(1, -(-len(rows) // PAGE))
    page = max(0, min(page, pages - 1))
    chunk = rows[page * PAGE : page * PAGE + PAGE]

    lines = [f"📃 <b>Tu lista de reproducción</b> ({len(rows)})", SEP, ""]
    for i, r in enumerate(chunk, page * PAGE + 1):
        lines.append(f"{i}. 🎧 <b>{ht(r['title'])}</b> — <i>{ht(r['artist']) or '—'}</i>")
    lines.append("\n" + SEP)

    buttons = []
    for r in chunk:
        title = r["title"][:24]
        artist = (r["artist"] or "—")[:14]
        if r["file_id"]:
            r_title, r_artist, r_file = r["title"], r["artist"], r["file_id"]
            buttons.append([InlineKeyboardButton(
                f"🎧 {title} — {artist}",
                callback_data=f"libsend:{b64e(r_title + '||' + (r_artist or '') + '||' + r_file)}",
            )])
        else:
            buttons.append([InlineKeyboardButton(
                f"🔊 {title} — {artist}",
                callback_data=f"qdl::{b64e(r['source'] or _build_query(r['artist'], r['title']))}",
            )])
        buttons.append([InlineKeyboardButton(
            f"🗑 Quitar",
            callback_data=f"myrm:{b64e((r['title'] or '') + '||' + (r['artist'] or ''))}",
        )])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"my:{page - 1}"))
    nav.append(InlineKeyboardButton(f"📄 {page + 1}/{pages}", callback_data="noop"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"my:{page + 1}"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton("🏠 Inicio", callback_data="home")])

    text = "\n".join(lines)
    if message_id:
        await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id,
                                    parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))
    else:
        await bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))


async def my_remove(q, context: ContextTypes.DEFAULT_TYPE, payload: str) -> None:
    parts = payload.split("||")
    title = parts[0] if parts else ""
    artist = parts[1] if len(parts) > 1 else ""
    storage.remove_playlist(q.from_user.id, title, artist)
    await q.answer("🗑 Eliminada de tu playlist.")
    await _send_my_page(q.message.chat_id, context.bot, q.from_user.id, 0, q.message.message_id)


# ---------------------------------------------------------------------- /top

async def top_page(q, context: ContextTypes.DEFAULT_TYPE, page: int) -> None:
    await _send_top_page(q.message.chat_id, context.bot, page, q.message.message_id)


async def _send_top_page(chat_id: int, bot, page: int, message_id: int | None = None) -> None:
    rows = storage.top_songs(50)
    if not rows:
        text = "🔥 <b>Canciones populares</b>\n\nSin datos todavía. Descarga canciones y aparecerán aquí."
        if message_id:
            await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, parse_mode="HTML")
        else:
            await bot.send_message(chat_id, text, parse_mode="HTML")
        return

    pages = max(1, -(-len(rows) // PAGE))
    page = max(0, min(page, pages - 1))
    chunk = rows[page * PAGE : page * PAGE + PAGE]

    labels = ["🥇", "🥈", "🥉"] + ["🏅"] * (PAGE - 3)
    lines = [f"🔥 <b>Canciones populares</b>", SEP, ""]
    buttons = []
    for i, r in enumerate(chunk, page * PAGE):
        medal = labels[i - page * PAGE]
        lines.append(f"{medal} <b>{ht(r['title'])}</b> — <i>{ht(r['artist']) or '—'}</i> · {r['plays']}×")
        buttons.append([InlineKeyboardButton(
            f"🔊 {r['title'][:26]} — {(r['artist'] or '—')[:14]}",
            callback_data=f"qdl::{b64e(_build_query(r['artist'], r['title']))}",
        )])
    lines.append("\n" + SEP)

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"top:{page - 1}"))
    nav.append(InlineKeyboardButton(f"📄 {page + 1}/{pages}", callback_data="noop"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"top:{page + 1}"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton("🏠 Inicio", callback_data="home")])

    text = "\n".join(lines)
    if message_id:
        await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id,
                                    parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))
    else:
        await bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))


# ------------------------------------------------------------------ ajustes

async def _send_settings(chat_id: int, bot, user_id: int, message_id: int | None = None) -> None:
    s = storage.get_settings(user_id)
    quality = "🔊 M4A (nativa)" if s["quality"] == "m4a" else "💿 MP3 320"
    thumb = "✅ Sí" if s["thumb"] else "❌ No"
    autosave = "✅ Sí" if s["autosave"] else "❌ No"

    text = (
        "⚙️ <b>Ajustes</b>\n" + SEP + "\n\n"
        f"🔊 Calidad por defecto: <b>{quality}</b>\n"
        f"🖼 Carátulas: <b>{thumb}</b>\n"
        f"📚 Guardar en playlist al descargar/reconocer: <b>{autosave}</b>\n\n"
        "Toca una opción para cambiarla."
    )
    q_btn = ("💿 MP3 320" if s["quality"] == "m4a" else "🔊 M4A nativa")
    buttons = [
        [InlineKeyboardButton(f"🎚 Calidad → {q_btn}", callback_data="set:q")],
        [InlineKeyboardButton(f"🖼 Carátulas → {'❌ No' if s['thumb'] else '✅ Sí'}", callback_data="set:t")],
        [InlineKeyboardButton(f"📚 Auto-guardar → {'❌ No' if s['autosave'] else '✅ Sí'}", callback_data="set:s")],
        [InlineKeyboardButton("🏠 Inicio", callback_data="home")],
    ]
    if message_id:
        await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id,
                                    parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))
    else:
        await bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))


async def settings_toggle(q, context: ContextTypes.DEFAULT_TYPE, field: str) -> None:
    user_id = q.from_user.id
    s = storage.get_settings(user_id)
    if field == "q":
        storage.set_quality(user_id, "mp3" if s["quality"] == "m4a" else "m4a")
    elif field == "t":
        storage.set_thumb(user_id, not s["thumb"])
    elif field == "s":
        storage.set_autosave(user_id, not s["autosave"])
    await q.answer("Ajuste guardado ✔")
    await _send_settings(q.message.chat_id, context.bot, user_id, q.message.message_id)