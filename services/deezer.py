"""Descarga en alta calidad (MP3 320) directamente desde Deezer vía deemix.

Se activa solo si existe la variable de entorno DEEZER_ARL (la cookie `arl`
de una sesión iniciada en deezer.com). Sin ARL, ninguna función descarga:
todo devuelve None/[] y el bot sigue con SoundCloud. Creado por Igris Inmortal.
"""

import glob
import logging
import os
import subprocess
import sys
import tempfile

log = logging.getLogger(__name__)

DEEZER_ARL = os.environ.get("DEEZER_ARL", "").strip()

# Calidad pedida a deemix; FLAC requiere cuenta HiFi y muchas canciones no lo
# permiten desde free, así que el estándar es MP3_320 con fallback a 128.
DEEMIX_BITRATE = "MP3_320"


def active() -> bool:
    """¿Está configurado el ARL de Deezer?"""
    return bool(DEEZER_ARL)


def _config_dir() -> str:
    """Carpeta de configuración de deemix (donde guarda el .arl)."""
    from deemix.utils import localpaths

    return str(localpaths.getConfigFolder())


def _write_arl() -> bool:
    """Escribe el ARL en el .arl de deemix para que la CLI no pregunte."""
    try:
        dirpath = _config_dir()
        os.makedirs(dirpath, exist_ok=True)
        with open(os.path.join(dirpath, ".arl"), "w", encoding="utf-8") as fh:
            fh.write(DEEZER_ARL.strip() + "\n")
        return True
    except Exception:  # noqa: BLE001
        log.exception("No se pudo escribir el .arl de deemix")
        return False


def _find_mp3(outdir: str) -> str | None:
    hits = [
        f for f in glob.glob(os.path.join(outdir, "**", "*.mp3"), recursive=True)
        if os.path.isfile(f)
    ]
    return max(hits, key=os.path.getmtime) if hits else None


def search_track_id(artist: str, title: str) -> str | None:
    """Busca el ID de Deezer de una canción (para bajar la copia exacta).

    Devuelve el ID del resultado de mejor coincidencia o None si no hay ARL
    o no se encontró nada."""
    if not active():
        return None
    try:
        import requests

        resp = requests.get(
            "https://api.deezer.com/search",
            params={"q": f"{artist} {title}", "limit": 10, "order": "RANKING"},
            timeout=15,
        )
        resp.raise_for_status()
    except Exception:  # noqa: BLE001
        log.warning("[deezer] No se pudo consultar la API de Deezer")
        return None

    best = None
    al = (artist or "").lower().strip()
    tl = (title or "").lower().strip()
    for row in resp.json().get("data") or []:
        row_artist = ((row.get("artist") or {}).get("name") or "").lower().strip()
        row_title = (row.get("title") or "").lower().strip()
        if best is None:
            best = row
        if row_artist == al and row_title.startswith(tl[:40]):
            return str(row.get("id"))
    return str(best.get("id")) if best else None


def download_track(outdir: str, track_id: str | int, progress=None) -> dict | None:
    """Baja una canción de Deezer (por su ID) en MP3 320 con metadatos y carátula.

    Devuelve dict con file_path/title/artist/album/duration/thumbnail o None si
    no hay ARL o algo salió mal.
    """
    if not active():
        return None
    if not _write_arl():
        return None

    url = f"https://www.deezer.com/track/{int(track_id)}"
    args = [
        sys.executable, "-m", "deemix",
        "-b", DEEMIX_BITRATE,
        "-p", outdir,
        url,
    ]
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        log.info("[deezer] Bajar %s (MP3 320)", url)
        subprocess.run(
            args,
            check=False,
            capture_output=True,
            timeout=180,
            env=env,
            stdin=subprocess.DEVNULL,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("[deezer] Falló al ejecutar deemix: %s", exc)
        return None

    file_path = _find_mp3(outdir)
    if not file_path:
        log.warning("[deezer] No quedó ningún .mp3 para %s", url)
        return None

    import mutagen.mp3

    try:
        meta = mutagen.mp3.MP3(file_path)
        duration = int(meta.info.length) or None
    except Exception:  # noqa: BLE001
        duration = None

    base = os.path.basename(file_path)
    stem = os.path.splitext(base)[0]
    parts = [p for p in stem.split(" - ", 1)] or [base]
    title = parts[-1].strip() if len(parts) == 2 else base
    artist = parts[0].strip() if len(parts) == 2 else None

    return {
        "file_path": file_path,
        "title": title,
        "artist": artist,
        "album": None,
        "duration": duration,
        "thumbnail": None,
        "ext": "mp3",
        "source": url,
    }