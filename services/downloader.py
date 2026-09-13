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
import random
import re
import shutil
import tempfile
import time
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
    "https://api.piped.private.coffee",
    "https://pipedapi.adminforge.de",
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


# yt-dlp 2026 bloquea Spotify por DRM, así que los enlaces de canción se
# resuelven a una búsqueda en YouTube/YouTube Music y se descargan de ahí.
SPOTIFY_RE = re.compile(
    r"(?:open\.spotify\.com(?:/[^/\s\"']+)?/|spotify:)(track|album|playlist)[:/]([A-Za-z0-9]{10,})",
    re.IGNORECASE,
)


def _spotify_track_info(source: str) -> Optional[dict]:
    """Extrae canción/artista de un enlace de Spotify (track) usando la página
    embed pública, sin API keys. Devuelve None si no hay datos para no romper
    el flujo de descarga."""
    import json
    import urllib.request

    m = SPOTIFY_RE.search(source)
    if not m:
        return None
    kind, spot_id = m.group(1).lower(), m.group(2)
    try:
        req = urllib.request.Request(
            f"https://open.spotify.com/embed/{kind}/{spot_id}",
            headers={"User-Agent": USER_AGENT},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            html = resp.read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return None

    i = html.find('"entity"')
    if i == -1:
        return None
    entity = html[i:i + 4000]
    if not re.search(r'"type":\s*"(track)"', entity):
        logger.warning("Spotify: solo se soportan enlaces de canción (track)")
        return None
    info: dict = {}
    m_title = re.search(r'"title":\s*"([^"]+)"', entity)
    m_name = re.search(r'"name":\s*"([^"]+)"', entity)
    m_dur = re.search(r'"duration":\s*(\d+)', entity)
    m_art = re.search(r'"artists":\s*(\[[^]]*\])', entity)
    info["title"] = m_title.group(1) if m_title else (m_name.group(1) if m_name else "")
    if m_art:
        try:
            artists = json.loads(m_art.group(1))
            info["artist"] = ", ".join(a.get("name", "") for a in artists if a.get("name"))
        except Exception:  # noqa: BLE001
            pass
    if m_dur:
        info["duration"] = int(m_dur.group(1)) // 1000
    if not info.get("title") and not info.get("artist"):
        return None
    return info


# Estado del bloqueo temporal de YouTube desde la IP del datacenter: cuando
# YouTube devuelve "Sign in to confirm you're not a bot", se evita martillear
# con más peticiones y se salta directamente a SoundCloud durante un rato.
_YT_GATED_UNTIL = 0.0

BOT_CHECK_MARKERS = (
    "Sign in to confirm you're not a bot",
    "not a bot",
    "The request cannot be completed",
    "reload the page",
    "Please sign in",
)


def _is_bot_check_message(msg: str) -> bool:
    return any(m.lower() in (msg or "").lower() for m in BOT_CHECK_MARKERS)


def _yt_gated() -> bool:
    return time.monotonic() < _YT_GATED_UNTIL


def _mark_yt_gated() -> None:
    global _YT_GATED_UNTIL
    _YT_GATED_UNTIL = time.monotonic() + 120


def _resolve_source(source: str) -> list[str]:
    """Para búsquedas 'ytsearch…:'/'ymsearch…:'/'scsearch…:' devuelve hasta 3
    candidatos ordenados por duración (el más largo primero), para no bajar
    teasers, shorts o vistas previas de 30 segundos. También permite reintentar
    con otro video si el primero está bloqueado desde la IP del datacenter.
    Usa 'extract_flat' para no extraer cada resultado (más rápido y con menos
    peticiones: los resultados DRM de SoundCloud no abortan la búsqueda)."""
    m = re.match(r"^(yt|ym|sc)search(\d*):(.*)$", source, re.IGNORECASE)
    if not m:
        return [source]
    prefix, qn, query = m.group(1), m.group(2), m.group(3)
    n = min(int(qn) if qn.isdigit() and int(qn) > 1 else 5, 10)
    url = f"{prefix}search{n}:{query}"

    for attempt in range(2):
        try:
            opts: dict = {
                "quiet": True,
                "no_warnings": True,
                "simulate": True,
                "noplaylist": True,
                "skip_download": True,
                "extract_flat": "in_playlist",
                # El solver JS (deno en el contenedor) da el token POT para 'web';
                # sin él, la búsqueda de YouTube desde IPs de datacenter da bot-check.
                "remote_components": {"ejs:github", "ejs:npm"},
            }
            cookies_path = _write_youtube_cookies()
            if cookies_path:
                opts["cookiefile"] = cookies_path
            if prefix in ("yt", "ym"):
                opts["extractor_args"] = {
                    "youtube": {"player_client": ["web", "tv_embedded"]}
                }
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False) or {}
            entries = [e for e in (info.get("entries") or []) if e]
            out: list[tuple[int, str]] = []
            for e in entries:
                dur = int(e.get("duration") or 0)
                if dur and dur < 30:
                    continue
                cand = e.get("webpage_url") or e.get("url")
                if not cand or re.match(rf"^{prefix}search\d*:", cand, re.IGNORECASE):
                    continue
                out.append((dur, cand))
            out.sort(key=lambda x: x[0], reverse=True)
            cands = [c for _, c in out[:3]]
            if cands:
                return cands
            logger.warning("La búsqueda %s devolvió 0 candidatos", url)
        except Exception as exc:  # noqa: BLE001
            if attempt == 0:
                logger.warning("No se pudo resolver la búsqueda %s: %s", url, exc)
                if _is_bot_check_message(str(exc)) and prefix in ("yt", "ym"):
                    _mark_yt_gated()

    if prefix in ("yt", "ym"):
        ext = _external_search(query)
        if ext:
            return ext
        # YouTube caído/bloqueado: probar la misma búsqueda en SoundCloud.
        return _resolve_sc(query)
    return [source]


def _resolve_sc(query: str, take: int = 3) -> list[str]:
    """Busca `query` en SoundCloud y devuelve hasta `take` URLs de pistas.
    No extrae cada pista (extract_flat), así que las pistas DRM no abortan la
    búsqueda ni el plan de descarga."""
    try:
        opts: dict = {
            "quiet": True,
            "no_warnings": True,
            "simulate": True,
            "noplaylist": True,
            "skip_download": True,
            "extract_flat": "in_playlist",
            "remote_components": {"ejs:github", "ejs:npm"},
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"scsearch5:{query}", download=False) or {}
        out: list[tuple[int, str]] = []
        for e in info.get("entries") or []:
            if not e:
                continue
            cand = e.get("webpage_url") or e.get("url")
            dur = int(e.get("duration") or 0)
            if not cand or dur and dur < 30:
                continue
            out.append((dur, cand))
        out.sort(key=lambda x: x[0], reverse=True)
        cands = [c for _, c in out[:take]]
        if cands:
            logger.info("SoundCloud: %d candidatos para %r", len(cands), query)
            return cands
    except Exception as exc:  # noqa: BLE001
        logger.warning("No se pudo buscar en SoundCloud %r: %s", query, exc)
    return []


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
        "concurrent_fragment_downloads": 4,
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
    if SPOTIFY_RE.search(source):
        spot = _spotify_track_info(source)
        if spot:
            title_q = (spot.get("title") or "").strip()
            artist_q = (spot.get("artist") or "").strip()
            if title_q and artist_q:
                original = f"ytsearch1:{artist_q} - {title_q}"
            else:
                original = f"ytsearch1:{title_q or artist_q}"
            logger.info("Spotify resuelto a descarga de YouTube: %s", original)
    candidates = _resolve_source(original)
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
            if _is_bot_check_message(str(exc)):
                _mark_yt_gated()
            if i == len(attempts):
                raise DownloadError(str(exc)) from exc
            _flush_outdir(outdir)
            # Pequeña pausa entre intentos: una ráfaga de peticiones rápidas
            # empeora el bot-check de YouTube desde IPs de datacenter.
            if _is_youtube(source):
                time.sleep(1.5 + random.random() * 1.5)

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


def _youtube_title(source: str, base: dict) -> Optional[str]:
    """Obtiene el título de un video de YouTube (solo metadatos) para poder
    buscarlo en otra plataforma si su descarga falla desde el datacenter."""
    if not YOUTUBE_RE.search(source):
        return None
    try:
        opts = {
            "quiet": True,
            "no_warnings": True,
            "simulate": True,
            "noplaylist": True,
            "skip_download": True,
            "remote_components": {"ejs:github", "ejs:npm"},
            "extractor_args": {"youtube": {"player_client": ["tv_embedded", "android"]}},
        }
        if base.get("cookiefile"):
            opts["cookiefile"] = base["cookiefile"]
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(source, download=False) or {}
        title = info.get("title")
        return title if isinstance(title, str) and title.strip() else None
    except Exception:  # noqa: BLE001
        return None


def _retry_attempts(base: dict, original: str, candidates: list[str]) -> list[dict]:
    """Para YouTube: prueba varios videos candidatos (los más largos de la
    búsqueda) y, para cada uno, varios clientes (web/mweb/ios…), ya que las IPs
    de datacenter suelen provocar 'Sign in…'/'The page needs to be reloaded' con
    un video y/o cliente concreto. El token POT (Proof of Origin) de bgutil se
    usa automáticamente cuando el servidor local está activo. Cuando YouTube
    está bloqueado temporalmente (o como último recurso), se busca la misma
    canción en SoundCloud."""
    is_youtube = _is_youtube(candidates[0]) or bool(
        re.match(r"^(yt|ym)search\d*:", original, re.IGNORECASE)
    )
    if not is_youtube:
        return [base]

    # YouTube bloqueado en este momento: ir directo a SoundCloud sin martillar.
    if re.match(r"^(yt|ym)search\d*:", candidates[0], re.IGNORECASE) or _yt_gated():
        sc = _resolve_sc(_search_query(original), take=4)
        return [dict(base, _source=c) for c in sc] or [base]

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
        attempts.append(item)

    # Cascada principal: 2 videos candidatos × varios clientes extra.
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
        if reencode is not None and ci == 0:
            fallback = dict(reencode)
            fallback["_source"] = cand
            _push(fallback)

    # Fallbacks: buscar la misma canción en otra plataforma (SoundCloud) al
    # agotar los intentos de YouTube. También cubre URLs directas de YouTube:
    # se obtiene el título y se reintenta en SoundCloud.
    fb_query = _search_query(original) or _youtube_title(original, base)
    if fb_query and fb_query.strip():
        fb_tpl = dict(reencode) if reencode else dict(base)
        seen: set[str] = set()
        for cand in _resolve_sc(fb_query, take=3):
            if cand in seen:
                continue
            seen.add(cand)
            fb = dict(fb_tpl)
            fb["_source"] = cand
            fb.pop("extractor_args", None)
            _push(fb)

    return attempts


def _search_query(source: str) -> str:
    m = re.match(r"^(yt|ym|sc)search\d*:(.*)$", source, re.IGNORECASE)
    return m.group(2).strip() if m else ""


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