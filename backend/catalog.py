"""
Каталог: поиск артистов и альбомов, страницы артиста и альбома.

С S7 запросы к источникам и разбор их ответов — в адаптерах (backend/providers/youtube_music.py,
backend/providers/soundcloud.py): свой пул потоков, таймаут, cooldown, единая ошибка ProviderError.
Здесь — маршруты, проверка входных данных и кэш страниц артистов. Формат карточек прежний:
artist {id, source, kind, name, cover, subtitle} и album {id, source, kind, title, artist, year, cover,
track_count, playlist_id}. Ошибка источника отдаётся как 502 с `detail`, `code`, `source` (main.py).
"""
from __future__ import annotations

import logging
import re
import time

from fastapi import APIRouter, HTTPException, Query, Request

import providers
from domain.models import album_card, artist_card
from providers import soundcloud as sc_provider, youtube_music as yt_provider
from providers.base import Provider, ProviderError

log = logging.getLogger("musicmeta.catalog")
router = APIRouter(prefix="/api", tags=["catalog"])

MAX_RESULTS = 100
YT_ARTIST_MAX = yt_provider.ARTIST_SONGS_MAX     # сколько песен артиста YouTube Music подгружаем максимум
CATALOG_TIMEOUT = 25           # сколько секунд ждём ответа источника на один вызов каталога (как до S7)
_ID_RE = re.compile(r"^[\w-]{1,64}$")
_artist_cache: dict[tuple[str, str], tuple[float, dict]] = {}

# Прежние имена помощников (тесты карточек, live-тесты) — теперь это функции адаптеров и domain/models.py
_artist_card = artist_card
_album_card = album_card
_yt_img = yt_provider.card_image
_sc_img = sc_provider.card_image
_yt_search = yt_provider.search_cards_sync
_sc_artist = sc_provider.artist_from_user
_sc_album = sc_provider.album_from_playlist


def _check(source: str, id_: str) -> None:
    if source not in ("yt", "sc") or not _ID_RE.fullmatch(id_):
        raise HTTPException(400, "Некорректный запрос")


def _fail(p: Provider, exc: Exception) -> Exception:
    """Ошибка источника — как есть; любая другая (сбой разбора ответа в адаптере) — internal этого источника."""
    if isinstance(exc, (HTTPException, ProviderError)):
        return exc
    log.warning("Каталог %s: %r", p.id, exc)
    return p.error("internal")


# ---------- поиск артистов и альбомов ----------
@router.get("/search/{kind}")
async def search_cards(request: Request, kind: str, q: str = Query(..., min_length=1, max_length=200),
                       source: str = "yt", offset: int = Query(0, ge=0, le=MAX_RESULTS),
                       limit: int = Query(20, ge=1, le=50)):
    if kind not in ("artists", "albums") or source not in ("yt", "sc"):
        raise HTTPException(400, "Некорректный запрос")
    p = providers.get(source)
    try:
        items, more = await p.search_cards(request.app.state, kind, q.strip(), limit, offset, timeout=CATALOG_TIMEOUT)
    except Exception as exc:
        raise _fail(p, exc) from exc
    return {"items": items, "has_more": more}


# ---------- страница артиста ----------
async def _artist_info(request: Request, source: str, id_: str) -> dict:
    hit = _artist_cache.get((source, id_))
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    info, _degraded = await providers.get(source).artist_info(request.app.state, id_, timeout=CATALOG_TIMEOUT)
    _artist_cache[(source, id_)] = (time.time(), info)
    return info


async def _artist_tracks(request: Request, source: str, id_: str, offset: int, limit: int):
    info = await _artist_info(request, source, id_)
    return await providers.get(source).artist_tracks(request.app.state, id_, info, offset, limit,
                                                     timeout=CATALOG_TIMEOUT)


@router.get("/artist/{source}/{artist_id}")
async def artist_page(request: Request, source: str, artist_id: str):
    _check(source, artist_id)
    try:
        info = await _artist_info(request, source, artist_id)
        tracks, more = await _artist_tracks(request, source, artist_id, 0, 20)
    except Exception as exc:
        raise _fail(providers.get(source), exc) from exc
    head = {k: info[k] for k in ("name", "cover", "subtitle")}
    return {"artist": head, "albums": info["albums"], "related": info.get("related", []), "tracks": tracks, "has_more": more}


@router.get("/artist/{source}/{artist_id}/tracks")
async def artist_tracks(request: Request, source: str, artist_id: str,
                        offset: int = Query(0, ge=0, le=5000), limit: int = Query(20, ge=1, le=50)):
    _check(source, artist_id)
    try:
        tracks, more = await _artist_tracks(request, source, artist_id, offset, limit)
    except Exception as exc:
        raise _fail(providers.get(source), exc) from exc
    return {"tracks": tracks, "has_more": more}


# ---------- страница альбома ----------
@router.get("/album/{source}/{album_id}")
async def album_page(request: Request, source: str, album_id: str, pl: str | None = Query(None)):
    _check(source, album_id)
    if pl is not None and not _ID_RE.fullmatch(pl):
        raise HTTPException(400, "Некорректный запрос")
    p = providers.get(source)
    try:
        card, tracks, skipped, _degraded = await p.album(request.app.state, album_id, pl if source == "yt" else None,
                                                         timeout=CATALOG_TIMEOUT)
    except Exception as exc:
        raise _fail(p, exc) from exc
    return {"album": card, "tracks": tracks, "unavailable": max(0, skipped)}
