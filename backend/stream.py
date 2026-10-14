"""
Фасад совместимости для прежнего модуля стриминга (до S8 весь код жил здесь).

Сам движок — пакет backend/streaming/ (ranges, resolver, proxy, local, router и др.). Этот модуль даёт те же
имена, что и раньше (`stream._extract_sync`, `stream._cache`, `stream.STREAM_CHUNK_BYTES`, `stream.router` …),
чтобы существующие вызовы и тесты работали без изменений. Чтение имени возвращает объект из модуля-владельца,
а присваивание (в том числе monkeypatch в тестах) меняет значение в модуле-владельце — поэтому подмена,
например, `stream._extract_sync` действует на настоящий резолвер.

Новый код импортирует модули streaming.* напрямую.
"""
from __future__ import annotations

import sys
import types

from streaming import config, errors, proxy, ranges, resolver, router as _router

_EXPORTS: dict[types.ModuleType, tuple[str, ...]] = {
    config: ("YTDLP_COOKIES", "YTDLP_PROXY"),
    errors: ("ERROR_HEADER", "DownloadError", "StreamUnavailable", "_err", "_resolve_error", "_runtime_error",
             "_RUNTIME_KINDS"),
    ranges: ("RANGE_RE", "CONTENT_RANGE_RE"),
    resolver: ("VIDEO_ID_RE", "SC_ID_RE", "DEFAULT_TTL", "EXPIRY_MARGIN", "MAX_PARALLEL_RESOLVES", "FORWARD_HEADERS",
               "Source", "SOURCES", "Resolved", "ResolverState", "_cache", "_url_expiry", "ydl_opts", "_resolve_pool",
               "_tls", "_get_ydl", "_extract_sync", "_resolve_uncached", "_resolve", "_invalidate"),
    proxy: ("CHUNK_SIZE", "STREAM_CHUNK_BYTES", "HEAD_CACHE_ITEMS", "HEAD_TTL", "STALE_STATUSES", "make_http_client",
            "_connect", "_body", "Head", "_heads", "_head_get", "_head_put", "_serve_from_head", "_prefetch_head",
            "_start_warm", "_cap_range"),
    _router: ("router", "stream_track"),
}
_OWNERS: dict[str, types.ModuleType] = {name: mod for mod, names in _EXPORTS.items() for name in names}


class _Facade(types.ModuleType):
    def __getattr__(self, name: str):
        owner = _OWNERS.get(name)
        if owner is None:
            raise AttributeError(f"module 'stream' has no attribute {name!r}")
        return getattr(owner, name)

    def __setattr__(self, name: str, value) -> None:
        owner = _OWNERS.get(name)
        if owner is not None:
            setattr(owner, name, value)
        else:
            super().__setattr__(name, value)

    def __delattr__(self, name: str) -> None:
        owner = _OWNERS.get(name)
        if owner is not None:
            delattr(owner, name)
        else:
            super().__delattr__(name)

    def __dir__(self):
        return sorted(set(super().__dir__()) | set(_OWNERS))


sys.modules[__name__].__class__ = _Facade
