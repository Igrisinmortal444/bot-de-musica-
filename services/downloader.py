"""Descarga de audio con la mejor calidad posible vía yt-dlp.

Funciona con YouTube y con miles de sitios más (Spotify, SoundCloud, Deezer,
Bandcamp, Vimeo, etc.). Prefiere el formato M4A (AAC) nativo para no perder
calidad con recodificaciones; te re-encode y recodifica solo si es necesario.
"""

import asyncio
import glob
import logging
import os
import re
import shutil
import tempfile
from typing import Callable, Awaitable, Optional

import yt_dlp

from config import MAX_UPLOAD_MB, WORKERS

logger = logging.getLogger(__name__)


class DownloadError(Exception):
    pass


_sem: Optional[asyncio.Semaphore] = None


def _get_sem() -> asyncio.Semaphore:
    global _sem
    if _sem is None:
        _sem = asyncio.Semaphore(WORKERS)
    return _sem


AUDIO_EXTS = {".m4a", ".mp3", ".opus", ".ogg", ".oga", ".aac", ".wav", ".flac", ".webm"}
ALLOWED_QUALITY = {"m4a", "mp3"}


def _opts(outdir: str, quality: str) -> dict:
    postprocessors = []
    if quality == "mp3":
        postprocessors.append(
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "320",
            }
        )
    postprocessors += [
        {"key": "FFmpegMetadata"},
        {"key": "FFmpegThumbnailsConvertor", "format": "jpg"},
    ]

    opts = {
        "format": "bestaudio/best",
        "outtmpl": os.path.join(outdir, "%(title).80s [%(id)s].%(ext)s"),
        "postprocessors": postprocessors,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": True,
        "writethumbnail": True,
        "nocheckcertificate": True,
        "socket_timeout": 25,
        "retries": 3,
        "fragment_retries": 3,
        "concurrent_fragment_downloads": 8,
        "buffersize": 1024 * 64,
        "max_filesize": MAX_UPLOAD_MB * 1024 * 1024,
    }

    if quality == "m4a":
        opts["format"] = "bestaudio[ext=m4a]/bestaudio/best"

    return opts


def _newest_audio(outdir: str) -> Optional[str]:
    files = []
    for f in glob.glob(os.path.join(outdir, "*")):
        if os.path.splitext(f)[1].lower() in AUDIO_EXTS:
            files.append(f)
    return max(files, key=os.path.getmtime) if files else None


def _thumb_for(stem: str, outdir: str) -> Optional[str]:
    for c in (stem + ".jpg", stem + ".png", stem + ".webp"):
        if os.path.isfile(c):
            return c
    for f in glob.glob(os.path.join(outdir, "*.jpg")):
        return f
    return None


def _worker(
    source: str,
    quality: str,
    outdir: str,
    progress_cb: Optional[Callable[[int], Awaitable[None]]],
    loop: Optional[asyncio.AbstractEventLoop],
) -> dict:
    opts = _opts(outdir, quality)

    if progress_cb is not None:
        def hook(data: dict):
            if data.get("status") not in ("downloading", "finished"):
                return
            match = re.search(r"([\d.]+)%", data.get("_percent_str") or "")
            if not match or loop is None:
                return
            percent = int(float(match.group(1)))
            try:
                asyncio.run_coroutine_threadsafe(progress_cb(percent), loop)
            except RuntimeError:
                pass

        opts["progress_hooks"] = [hook]

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(source, download=True) or {}

    if info.get("_type") == "playlist":
        entries = info.get("entries") or [None]
        info = next((e for e in entries if e), info)

    audio_path = _newest_audio(outdir)
    if not audio_path:
        raise DownloadError("No se obtuvo ningún archivo de audio.")

    thumb = _thumb_for(os.path.splitext(audio_path)[0], outdir)

    artist = info.get("artist") or info.get("channel") or info.get("uploader")
    title = info.get("title") or "Música"
    if not artist and title and " - " in title:
        artist = title.split(" - ")[0].strip()

    return {
        "outdir": outdir,
        "file_path": audio_path,
        "title": title,
        "artist": artist,
        "duration": info.get("duration"),
        "thumbnail": thumb,
        "ext": os.path.splitext(audio_path)[1].lstrip("."),
        "source": info.get("webpage_url") or source,
    }


async def download(
    source: str,
    quality: str = "m4a",
    progress_cb: Optional[Callable[[int], Awaitable[None]]] = None,
) -> dict:
    """Baja el audio de `source` (URL o búsqueda "ytsearch1:...") con calidad `quality`.

    Devuelve un dict con file_path, title, artist, duration, thumbnail, outdir.
    El llamador es responsable de limpiar la carpeta `outdir`.
    """
    if quality not in ALLOWED_QUALITY:
        quality = "m4a"

    loop = asyncio.get_running_loop()
    outdir = tempfile.mkdtemp(prefix="musicbot_")

    try:
        async with _get_sem():
            result = await asyncio.to_thread(_worker, source, quality, outdir, progress_cb, loop)
        return result
    except yt_dlp.utils.DownloadError as exc:
        shutil.rmtree(outdir, ignore_errors=True)
        raise DownloadError(str(exc)) from exc
    except DownloadError:
        shutil.rmtree(outdir, ignore_errors=True)
        raise
    except Exception as exc:  # noqa: BLE001
        shutil.rmtree(outdir, ignore_errors=True)
        logger.exception("Error inesperado al descargar")
        raise DownloadError("Error interno al descargar.") from exc