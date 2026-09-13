"""Persistencia local (SQLite) para la biblioteca de audio, la playlist de
cada usuario, la popularidad (/top) y los ajustes por usuario.

El free tier de Render borra el disco en cada deploy: los datos son para uso
personal y se pueden volver a subir los audios si se pierden.
"""

import logging
import os
import sqlite3
import tempfile
import threading
import time

logger = logging.getLogger(__name__)

DB_PATH = os.environ.get("MUSICBOT_DB", os.path.join(tempfile.gettempdir(), "musicbot.db"))
_lock = threading.Lock()

_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS library (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        title TEXT NOT NULL,
        artist TEXT NOT NULL DEFAULT '',
        file_id TEXT NOT NULL,
        mime TEXT NOT NULL DEFAULT '',
        duration INTEGER,
        file_size INTEGER,
        added_at INTEGER NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS playlist (
        key TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL,
        title TEXT NOT NULL,
        artist TEXT NOT NULL DEFAULT '',
        quality TEXT NOT NULL DEFAULT 'm4a',
        source TEXT NOT NULL DEFAULT '',
        file_id TEXT NOT NULL DEFAULT '',
        added_at INTEGER NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS stats (
        key TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        artist TEXT NOT NULL DEFAULT '',
        plays INTEGER NOT NULL DEFAULT 0
    )""",
    """CREATE TABLE IF NOT EXISTS settings (
        user_id INTEGER PRIMARY KEY,
        quality TEXT NOT NULL DEFAULT 'm4a',
        thumb INTEGER NOT NULL DEFAULT 1,
        autosave INTEGER NOT NULL DEFAULT 1
    )""",
]


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init() -> None:
    with _lock:
        conn = _connect()
        try:
            for statement in _SCHEMA:
                conn.execute(statement)
            conn.commit()
        finally:
            conn.close()


def _pl_key(user_id: int, title: str, artist: str) -> str:
    return f"{user_id}|{title.strip().lower()}|{artist.strip().lower()}"


# ------------------------------------------------------------------ biblioteca

def add_library(
    user_id: int,
    title: str,
    artist: str,
    file_id: str,
    mime: str = "",
    duration: int | None = None,
    file_size: int | None = None,
) -> int:
    with _lock:
        conn = _connect()
        try:
            cur = conn.execute(
                "INSERT INTO library (user_id,title,artist,file_id,mime,duration,file_size,added_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (user_id, title or "Sin título", artist or "", file_id, mime,
                 duration, file_size, int(time.time())),
            )
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()


def search_library(query: str, limit: int = 8) -> list[dict]:
    pattern = f"%{query}%"
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT * FROM library WHERE title LIKE ? OR artist LIKE ?"
                " ORDER BY added_at DESC LIMIT ?",
                (pattern, pattern, limit),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()


# ------------------------------------------------------------------- playlist

def add_playlist(
    user_id: int,
    title: str,
    artist: str,
    quality: str = "m4a",
    source: str = "",
    file_id: str = "",
) -> bool:
    """Inserta en la playlist. Devuelve True si se agregó, False si ya existía."""
    with _lock:
        conn = _connect()
        try:
            cur = conn.execute(
                "INSERT OR IGNORE INTO playlist (key,user_id,title,artist,quality,source,file_id,added_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (_pl_key(user_id, title, artist), user_id, title or "♪", artist or "",
                 quality, source, file_id, int(time.time())),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()


def get_playlist(user_id: int, limit: int = 100) -> list[dict]:
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT * FROM playlist WHERE user_id=? ORDER BY added_at DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()


def remove_playlist(user_id: int, title: str, artist: str) -> None:
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                "DELETE FROM playlist WHERE key=?", (_pl_key(user_id, title, artist),)
            )
            conn.commit()
        finally:
            conn.close()


# ---------------------------------------------------------------------- /top

def bump_song(title: str, artist: str, plays: int = 1) -> None:
    key = f"{title.strip().lower()}|{artist.strip().lower()}"
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                "INSERT INTO stats (key,title,artist,plays) VALUES (?,?,?,?)"
                " ON CONFLICT(key) DO UPDATE SET plays=plays+excluded.plays",
                (key, title or "♪", artist or "", plays),
            )
            conn.commit()
        finally:
            conn.close()


def top_songs(limit: int = 20) -> list[dict]:
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT title, artist, plays FROM stats ORDER BY plays DESC, key LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()


# ------------------------------------------------------------------ ajustes

_DEFAULTS = {"quality": "m4a", "thumb": 1, "autosave": 1}


def get_settings(user_id: int) -> dict:
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT quality, thumb, autosave FROM settings WHERE user_id=?",
                (user_id,),
            ).fetchone()
        finally:
            conn.close()
    if not row:
        return dict(_DEFAULTS)
    return {
        "quality": row["quality"] if row["quality"] in ("m4a", "mp3") else "m4a",
        "thumb": bool(row["thumb"]),
        "autosave": bool(row["autosave"]),
    }


def _upsert_setting(user_id: int, field: str, value) -> None:
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                f"INSERT INTO settings (user_id,{field}) VALUES (?,?)"
                f" ON CONFLICT(user_id) DO UPDATE SET {field}=excluded.{field}",
                (user_id, value),
            )
            conn.commit()
        finally:
            conn.close()


def set_quality(user_id: int, quality: str) -> None:
    if quality in ("m4a", "mp3"):
        _upsert_setting(user_id, "quality", quality)


def set_thumb(user_id: int, enabled: bool) -> None:
    _upsert_setting(user_id, "thumb", int(enabled))


def set_autosave(user_id: int, enabled: bool) -> None:
    _upsert_setting(user_id, "autosave", int(enabled))