"""Reconocimiento de canciones con Shazam (vía shazamio, sin API key).

Accepta una ruta a un archivo de audio (ogg/m4a/wav…) y devuelve un dict con
title/artist/artwork, o None si no se pudo reconocer.
"""

import logging

logger = logging.getLogger(__name__)

_ARTWORK_FIELDS = ("coverarthq", "coverartf", "coverart")


def _extract(out) -> dict | None:
    track = (out or {}).get("track")
    if not isinstance(track, dict):
        return None
    title = (track.get("title") or "").strip()
    artist = (track.get("subtitle") or "").strip()
    if not title:
        return None
    images = track.get("images") or {}
    artwork = next((images.get(f) for f in _ARTWORK_FIELDS if images.get(f)), None)
    return {"title": title, "artist": artist, "artwork": artwork, "raw": track}


async def recognize_file(path: str) -> dict | None:
    try:
        from shazamio import Shazam
    except Exception as exc:  # noqa: BLE001
        logger.warning("shazamio no está instalado: %s", exc)
        return None
    try:
        shazam = Shazam()
        func = getattr(shazam, "recognize", None) or shazam.recognize_song
        out = await func(path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Shazam no pudo reconocer %s: %s", path, exc)
        return None
    return _extract(out) if isinstance(out, dict) else None