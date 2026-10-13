"""
Фасад для совместимости (S7). Клиент API v2 SoundCloud переехал в адаптер backend/providers/soundcloud.py.

Функции ниже вызывают адаптерный клиент НАПРЯМУЮ, в потоке вызывающего — без пула, таймаута ожидания и
cooldown источника. В коде приложения используйте адаптер: `providers.sc` (search_tracks, search_cards,
artist_info, album, radio …). Модуль остаётся, пока на имя `soundcloud` есть ссылки (tests/conftest.py).
"""
from __future__ import annotations

from providers import soundcloud as _adapter

API_BASE = _adapter.API_BASE
API_SEARCH = _adapter.API_SEARCH


def search_tracks(query: str, limit: int) -> list[dict]:
    """Блокирующий поиск треков: «сырые» объекты API (providers.soundcloud.search_raw)."""
    return _adapter.search_raw(query, limit)


def api_get(path: str, query: dict | None = None):
    """Блокирующий GET к API v2 SoundCloud (providers.soundcloud.api_get)."""
    return _adapter.api_get(path, query)
