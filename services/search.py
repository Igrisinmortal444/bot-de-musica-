"""Búsqueda global de música.

1) iTunes Store Search API sobre varios países (la mayor catálogo comercial).
2) Si no hay resultados, cae en MusicBrainz (la base de datos musical más
   abierta del mundo, cubre hasta los artistas más locales/underground).

Todo lo que una persona pueda escribir — cualquier país, idioma o género —
acabará encontrándose aquí y luego se baja desde YouTube vía yt-dlp.
"""

import logging
import os
import tempfile

import aiohttp

from config import ITUNES_COUNTRIES

logger = logging.getLogger(__name__)

ITUNES_ENDPOINT = "https://itunes.apple.com/search"
MUSICBRAINZ_ENDPOINT = "https://musicbrainz.org/ws/2/recording"

USER_AGENT = "MusicPowerBot/1.0 (+https://t.me/MusicPowerBot)"
TIMEOUT = aiohttp.ClientTimeout(total=25)


async def _get_json(session: aiohttp.ClientSession, url: str, params: dict, headers: dict | None = None):
    try:
        async with session.get(url, params=params, headers=headers, timeout=TIMEOUT) as resp:
            if resp.status != 200:
                logger.warning("HTTP %s desde %s", resp.status, url)
                return None
            return await resp.json(content_type=None)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Error consultando %s: %s", url, exc)
        return None


async def itunes_search(query: str, limit: int = 12) -> list[dict]:
    """Busca en la iTunes Store de varios países y deduplica resultados."""
    per = max(1, limit // 2)
    seen: set[tuple[str, str]] = set()
    results: list[dict] = []

    async with aiohttp.ClientSession() as session:
        for country in ITUNES_COUNTRIES:
            params = {
                "term": query,
                "media": "music",
                "entity": "song",
                "limit": per,
                "country": country,
            }
            data = await _get_json(session, ITUNES_ENDPOINT, params)
            if not data:
                continue
            for row in data.get("results", []):
                artist = (row.get("artistName") or "").strip()
                title = (row.get("trackName") or "").strip()
                if not artist or not title:
                    continue
                key = (artist.lower(), title.lower())
                if key in seen:
                    continue
                seen.add(key)
                length = row.get("trackTimeMillis")
                artwork = (row.get("artworkUrl100") or "").replace("100x100bb", "600x600bb")
                results.append(
                    {
                        "artist": artist,
                        "title": title,
                        "album": (row.get("collectionName") or "").strip(),
                        "year": (row.get("releaseDate") or "")[:4],
                        "duration": int(length / 1000) if length else None,
                        "artwork": artwork or None,
                        "preview": (row.get("previewUrl") or "").strip() or None,
                        "region": country,
                    }
                )
            if len(results) >= limit:
                break

    return results[:limit]


async def musicbrainz_search(query: str, limit: int = 12) -> list[dict]:
    """Fallback: busca grabaciones en MusicBrainz (catálogo verdaderamente global)."""
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    params = {"query": query, "fmt": "json", "limit": str(limit)}
    results: list[dict] = []

    async with aiohttp.ClientSession() as session:
        data = await _get_json(session, MUSICBRAINZ_ENDPOINT, params, headers)
    if not data:
        return results

    for row in data.get("recordings", []):
        title = (row.get("title") or "").strip()
        segments = row.get("artist-credit") or []
        artist = "".join((seg.get("name") or "") + (seg.get("joinphrase") or "") for seg in segments).strip()
        if not title or not artist:
            continue

        releases = row.get("releases") or []
        album = releases[0].get("title") if releases else None
        year = (releases[0].get("date") or "")[:4] if releases and releases[0].get("date") else None
        length = row.get("length")

        results.append(
            {
                "artist": artist,
                "title": title,
                "album": album,
                "year": year,
                "duration": int(length / 1000) if length else None,
                "artwork": None,
                "preview": None,
                "region": "musicbrainz",
            }
        )

    return results


async def search_global(query: str, limit: int = 12):
    """Busca en iTunes (varios países) y, si no hay resultados, en MusicBrainz.

    Devuelve (resultados, origen) donde origen es "itunes", "musicbrainz" o "none".
    """
    try:
        results = await itunes_search(query, limit)
        if results:
            return results, "itunes"
    except Exception:  # noqa: BLE001
        logger.exception("Falló la búsqueda en iTunes")

    try:
        results = await musicbrainz_search(query, limit)
        if results:
            return results, "musicbrainz"
    except Exception:  # noqa: BLE001
        logger.exception("Falló la búsqueda en MusicBrainz")

    return [], "none"


async def download_artwork(url: str) -> str | None:
    """Descarga una carátula a un archivo temporal. Devuelve su ruta (o None)."""
    if not url:
        return None
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=TIMEOUT) as resp:
                if resp.status != 200:
                    return None
                fd, path = tempfile.mkstemp(prefix="musicbot_art_", suffix=".jpg")
                with os.fdopen(fd, "wb") as fh:
                    async for chunk in resp.content.iter_chunked(1024 * 64):
                        fh.write(chunk)
                return path
    except Exception as exc:  # noqa: BLE001
        logger.warning("No se pudo bajar la carátula %s: %s", url, exc)
        return None