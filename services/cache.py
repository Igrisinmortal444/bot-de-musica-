"""Caché en memoria con expiración, para guardar resultados de búsqueda
entre los botones del menú (callback_data tiene límite de 64 bytes)."""

import time

TTL = 15 * 60

_CACHE: dict[str, tuple[float, object]] = {}


def set_token(token: str, payload: object) -> None:
    _CACHE[token] = (time.time(), payload)


def get_token(token: str):
    item = _CACHE.get(token)
    if not item:
        return None
    ts, payload = item
    if time.time() - ts > TTL:
        _CACHE.pop(token, None)
        return None
    return payload