"""Descarga de audio con la mejor calidad posible vía yt-dlp.

Funciona con YouTube y con miles de sitios más (Spotify, SoundCloud, Deezer,
Bandcamp, Vimeo, etc.). Prefiere el formato M4A (AAC) nativo para no perder
calidad con recodificaciones; te re-encode y recodifica solo si es necesario.
"""

import asyncio
import base64
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

# Las IPs de datacenter (Render) suelen ser bloqueadas por YouTube con
# "Sign in to confirm you're not a bot" o "The page needs to be reloaded".
# Con estas cookies (sesión autenticada, exportada desde un navegador y
# codificada en base64 como variable de entorno) las descargas funcionan.
YOUTUBE_RE = re.compile(r"(youtube\.com|youtu\.be)/", re.IGNORECASE)
YOUTUBE_SEARCH_RE = re.compile(r"^(yt|ym)search\d*:", re.IGNORECASE)

# Clientes de respaldo en cascada por si el cliente por defecto falla.
# ("web" genera su token POT (proof-of-origin) vía bgutil cuando está activo).
# Se prueban varios porque desde IPs de datacenter (Render) YouTube da
# bot-check con unos clientes y con otros no:
#   - tv_embedded / tv_simply: suelen dejar pasar sin PO token.
#   - android / ios: formato limitado pero casi nunca bloquean.
#   - mweb: útil cuando web falla.
YOUTUBE_CLIENTS = ["web", "tv_embedded", "tv_simply", "mweb", "ios"]
YOUTUBE_COOKIES_B64 = os.environ.get("YOUTUBE_COOKIES_B64", "") or ""
YOUTUBE_COOKIES_PATH = os.path.join(tempfile.gettempdir(), "youtube_cookies.txt")

USER_AGENT = "MusicPowerBot/1.0 (+https://t.me/MusicPowerBot)"

# APIs públicas de búsqueda para esquivar el bot-check de YouTube desde IPs
# de datacenter (Render): Piped y, en su defecto, Invidious.
PIPED_INSTANCES = [
    "https://pipedapi.kavin.rocks",
    "https://pipedapi.moomoo.me",
]
INVIDIOUS_INSTANCES = [
    "https://yewtu.be",
    "https://invidious.nerdvpn.de",
]


def _write_youtube_cookies() -> Optional[str]:
    if not YOUTUBE_COOKIES_B64:
        return None
    try:
        with open(YOUTUBE_COOKIES_PATH, "wb") as fh:
            fh.write(base64.b64decode(YOUTUBE_COOKIES_B64))
        return YOUTUBE_COOKIES_PATH
    except Exception:  # noqa: BLE001
        logger.exception("No se pudo escribir las cookies de YouTube")
        return None


def _is_youtube(source: str) -> bool:
    return bool(YOUTUBE_RE.search(source) or YOUTUBE_SEARCH_RE.search(source))


def _external_search(query: str) -> list[str]:
    """Busca la canción en APIs públicas (Piped y, en su defecto, Invidious)
    para esquivar el bot-check de YouTube desde IPs de datacenter (Render).
    Devuelve hasta 3 URLs de video ordenadas de mayor a menor duración."""
    import json
    import urllib.parse
    import urllib.request

    quoted = urllib.parse.quote(query)

    for base in PIPED_INSTANCES:
        try:
            req = urllib.request.Request(
                f"{base}/search?q={quoted}&filter=music_songs",
                headers={"User-Agent": USER_AGENT},
            )
            with urllib.request.urlopen(req, timeout=6) as resp:
                data = json.load(resp)
            items = []
            for it in data.get("items") or []:
                url = it.get("url") or ""
                dur = int(it.get("duration") or 0)
                if not url or (dur and dur < 30):
                    continue
                items.append((dur, url))
            items.sort(key=lambda x: x[0], reverse=True)
            out = []
            for _, url in items[:3]:
                out.append("https://www.youtube.com" + url if url.startswith("/") else url)
            if out:
                logger.info("Búsqueda externa Piped: %d resultados", len(out))
                return out
        except Exception:  # noqa: BLE001
            continue

    for base in INVIDIOUS_INSTANCES:
        try:
            req = urllib.request.Request(
                f"{base}/api/v1/search?q={quoted}&type=video",
                headers={"User-Agent": USER_AGENT},
            )
            with urllib.request.urlopen(req, timeout=6) as resp:
                data = json.load(resp)
            items = []
            for it in data or []:
                vid = it.get("videoId") or ""
                dur = int(it.get("lengthSeconds") or 0)
                if not vid or (dur and dur < 30):
                    continue
                items.append((dur, vid))
            items.sort(key=lambda x: x[0], reverse=True)
            if items:
                out = [f"https://www.youtube.com/watch?v={vid}" for _, vid in items[:3]]
                logger.info("Búsqueda externa Invidious: %d resultados", len(out))
                return out
        except Exception:  # noqa: BLE001
            continue

    return []


def _resolve_source(source: str) -> list[str]:
    """Para búsquedas 'ytsearch…:'/'ymsearch…:'/'scsearch…:' devuelve hasta 3
    candidatos ordenados por duración (el más largo primero), para no bajar
    teasers, shorts o vistas previas de 30 segundos. También permite reintentar
    con otro video si el primero está bloqueado desde la IP del datacenter.
    La resolución usa las cookies de YouTube; si el buscador de YouTube está
    bloqueado (típico en datacenters), cae a las APIs públicas de Piped."""
    m = re.match(r"^(yt|ym|sc)search(\d*):(.*)$", source, re.IGNORECASE)
    if not m:
        return [source]
    prefix, qn, query = m.group(1), m.group(2), m.group(3)
    n = int(qn) if qn.isdigit() and int(qn) > 1 else 5
    url = f"{prefix}search{n}:{query}"
    try:
        opts = {
            "quiet": True,
            "no_warnings": True,
            "simulate": True,
            "noplaylist": True,
            "skip_download": True,
        }
        cookies_path = _write_youtube_cookies()
        if cookies_path:
            opts["cookiefile"] = cookies_path
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False) or {}
        entries = [e for e in (info.get("entries") or []) if e and e.get("duration")]
        entries.sort(key=lambda e: e["duration"], reverse=True)
        out = []
        for e in entries:
            cand = e.get("webpage_url") or e.get("url")
            if cand and len(out) < 3:
                out.append(cand)
        if out:
            return out
        logger.warning("La búsqueda %s devolvió 0 resultados", url)
    except Exception as exc:  # noqa: BLE001
        logger.warning("No se pudo resolver la búsqueda %s: %s", url, exc)
    if prefix in ("yt", "ym"):
        ext = _external_search(query)
        if ext:
            return ext
    return [source]


def _opts(outdir: str, quality: str, source: str) -> dict:
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
        "socket_timeout": 15,
        "retries": 2,
        "fragment_retries": 3,
        "concurrent_fragment_downloads": 8,
        "buffersize": 1024 * 64,
        "max_filesize": MAX_UPLOAD_MB * 1024 * 1024,
        # Solver de challenges JS de YouTube (firma/n-challenge). yt-dlp 2026 usa
        # un runtime JS (deno) + un script de solver que se baja de GitHub la
        # primera vez. Sin él, web da 'Some formats may be missing' / bot-check.
        "remote_components": {"ejs:github", "ejs:npm"},
    }

    if quality == "m4a":
        opts["format"] = "bestaudio[ext=m4a]/bestaudio/best"
    else:
        opts["format"] = "bestaudio[abr>=160]/bestaudio[abr>=128]/bestaudio/best"

    if _is_youtube(source):
        cookies_path = _write_youtube_cookies()
        if cookies_path:
            opts["cookiefile"] = cookies_path

    return opts


def _newest_audio(outdir: str) -> Optional[str]:
    files = []
    for f in glob.glob(os.path.join(outdir, "*")):
        if os.path.splitext(f)[1].lower() in AUDIO_EXTS:
            files.append(f)
    return max(files, key=os.path.getmtime) if files else None


def _clean_title(title: str) -> str:
    """Quita lo de "(Official Video)", "(Audio)", "(Lyrics)"… y espacios sueltos."""
    t = title or ""
    t = re.sub(
        r"(?i)\s*[\(\[]\s*(official\s+)?(music\s+)?(video|audio|lyric(s)?|vídeo|audio oficial)\s*(hd|4k|8k)?\s*[\)\]]",
        " ",
        t,
    )
    return re.sub(r"\s+", " ", t).strip(" \t-–—|")


def _guess_artist(title: str) -> Optional[str]:
    """Si el título es 'Artista - Canción', devuelve la primera parte limpia."""
    if not title or " - " not in title:
        return None
    prefix = title.split(" - ")[0].strip()
    if not (0 < len(prefix) <= 40):
        return None
    if re.search(r"[\(\[\d:\-–]", prefix):
        return None
    return prefix


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
    original = source
    candidates = _resolve_source(source)
    opts = _opts(outdir, quality, original)

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

    attempts = _retry_attempts(opts, original, candidates)
    for i, attempt in enumerate(attempts, 1):
        logger.info("Intento %d/%d de descarga", i, len(attempts))
        try:
            with yt_dlp.YoutubeDL(attempt) as ydl:
                info = ydl.extract_info(attempt.pop("_source", candidates[0]), download=True) or {}
            break
        except Exception as exc:  # noqa: BLE001
            logger.warning("Intento %d falló: %s", i, exc)
            if i == len(attempts):
                raise DownloadError(str(exc)) from exc
            _flush_outdir(outdir)

    if info.get("_type") == "playlist":
        entries = info.get("entries") or [None]
        info = next((e for e in entries if e), info)

    audio_path = _newest_audio(outdir)
    if not audio_path:
        raise DownloadError("No se obtuvo ningún archivo de audio.")

    thumb = _thumb_for(os.path.splitext(audio_path)[0], outdir)

    artist = info.get("artist") or info.get("channel") or info.get("uploader")
    title = _clean_title(info.get("title") or "Música")
    prefix = _guess_artist(title)
    if prefix and (not artist or artist == info.get("channel") or artist == info.get("uploader")):
        artist = prefix
    if not artist and " - " in title:
        artist = title.split(" - ")[0].strip()
    if artist and artist.lower().endswith((" - topic", "- topic")):
        artist = re.sub(r"(?i)\s*-\s*topic\s*$", "", artist).strip()

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


def _retry_attempts(base: dict, original: str, candidates: list[str]) -> list[dict]:
    """Para YouTube: prueba varios videos candidatos (los 3 más largos de la
    búsqueda) y, para cada uno, varios clientes (web/mweb/ios…), ya que las IPs
    de datacenter suelen provocar 'Sign in…'/'The page needs to be reloaded' con
    un video y/o cliente concreto. El token POT (Proof of Origin) de bgutil se
    usa automáticamente cuando el servidor local está activo. Como último
    recurso se busca la misma canción en YouTube Music y en SoundCloud."""
    if not _is_youtube(candidates[0]):
        return [base]

    reencode = None
    if "m4a" in base.get("format", ""):
        reencode = dict(base)
        reencode["format"] = "bestaudio/best"
        reencode["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "m4a",
                "preferredquality": "192",
            },
        ] + list(base.get("postprocessors", []))

    attempts: list[dict] = []

    def _push(item: dict) -> None:
        # Los combos de cliente comparten el mismo _source (no se colapsan a
        # propósito: cada uno lleva su propio extractor_args). Solo se deduplica
        # explícitamente lo que se añade en los fallbacks de más abajo.
        attempts.append(item)

    # Cascada principal: 2 videos candidatos × varias clientes extra + un
    # intento re-encodificado por si el nativo da "not available".
    for ci, cand in enumerate(candidates[:2]):
        combos = [None] + YOUTUBE_CLIENTS if ci == 0 else ["mweb", "tv_embedded"]
        for cl in combos:
            alt = dict(base)
            if cl:
                alt["extractor_args"] = {
                    "youtube": {"player_client": [cl]}
                }
            alt["_source"] = cand
            _push(alt)
        if reencode is not None:
            fallback = dict(reencode)
            fallback["_source"] = cand
            _push(fallback)

    # Fallbacks: buscar la misma canción en YouTube Music y SoundCloud.
    # Aquí sí se deduplica, para no repetir el mismo source recuperado con
    # la resolución interna (que ya probó esos clientes arriba).
    m = re.match(r"^(yt|ym)search(\d*):(.*)$", original, re.IGNORECASE)
    if m:
        query = m.group(3)
        qn = int(m.group(2)) if m.group(2).isdigit() and int(m.group(2)) > 1 else 1
        fb_tpl = dict(reencode) if reencode else dict(base)
        seen: set[str] = set()
        for prefix in ("scsearch",):
            for cand in _resolve_source(f"{prefix}{qn}:{query}")[:2]:
                if re.match(rf"^{prefix}\d*:", cand):
                    continue  # no añadir búsquedas sin resolver (dan 404/scheme error)
                if cand in seen:
                    continue
                seen.add(cand)
                fb = dict(fb_tpl)
                fb["_source"] = cand
                fb.pop("extractor_args", None)
                _push(fb)

    return attempts


def _flush_outdir(outdir: str) -> None:
    for f in glob.glob(os.path.join(outdir, "*")):
        try:
            if os.path.isfile(f):
                os.remove(f)
            else:
                shutil.rmtree(f, ignore_errors=True)
        except OSError:
            pass


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