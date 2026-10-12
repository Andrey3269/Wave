"""
Шаг 1: каркас FastAPI + поиск по YouTube Music.

Запуск:
    pip install -r requirements.txt
    uvicorn main:app --reload --port 8000

Проверка:
    http://localhost:8000/api/search?q=daft+punk
    http://localhost:8000/docs
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Awaitable, Callable, Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from ytmusicapi import YTMusic

import catalog
import library
import paths
import playlists
import recs
import userdata
from downloads import cleanup_partial, router as downloads_router
from lyrics import router as lyrics_router
from soundcloud import search_tracks as sc_search_raw
from stream import make_http_client, router as stream_router

log = logging.getLogger("musicmeta")
logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)  # иначе в консоль летят все URL googlevideo

# ---------------------------------------------------------------------------
# Конфигурация (через переменные окружения)
# ---------------------------------------------------------------------------
# Фронтенд позже будет отдаваться этим же сервером (same-origin), поэтому CORS
# нужен в основном на время разработки (например, Live Server на :5500).
ALLOWED_ORIGINS = os.getenv(
    "ALLOWED_ORIGINS",
    "http://localhost:8000,http://127.0.0.1:8000,"
    "http://localhost:5500,http://127.0.0.1:5500",
).split(",")

CACHE_TTL_SEC = int(os.getenv("SEARCH_CACHE_TTL", "300"))  # 5 минут
CACHE_MAX_ITEMS = 256
MAX_RESULTS = 100   # глубже одного запроса не листаем (дальше выдача теряет релевантность)


# ---------------------------------------------------------------------------
# Единая модель трека (общая для YouTube / SoundCloud)
# ---------------------------------------------------------------------------
class Track(BaseModel):
    id: str                                  # ID внутри источника (videoId и т.п.)
    source: Literal["yt", "sc"]        # источник
    title: str
    artist: str                              # для отображения: "A, B"
    artists: list[str]
    album: str | None = None
    duration: int | None = None              # секунды
    cover: str | None = None                 # URL обложки (крупная версия)
    explicit: bool = False
    stream_url: str                          # относительный URL нашего прокси
    artist_id: str | None = None             # id первого артиста (страница артиста)
    artist_ids: list[str | None] = []        # id КАЖДОГО артиста, в том же порядке, что и artists
    plays: int | None = None                 # прослушивания (есть только у SoundCloud)
    released: str | None = None              # дата выхода ISO (есть только у SoundCloud)
    url: str | None = None                   # публичная ссылка на трек (для «Поделиться»)


class SearchResponse(BaseModel):
    query: str
    count: int
    tracks: list[Track]
    failed_sources: list[str] = []           # источники, которые не ответили ("yt", "sc")
    has_more: bool = False                   # есть ли смысл просить следующую страницу


# ---------------------------------------------------------------------------
# Нормализация данных YouTube Music
# ---------------------------------------------------------------------------
_SIZE_RE = re.compile(r"=w\d+-h\d+")


def _best_cover(thumbnails: list[dict] | None) -> str | None:
    """
    ytmusicapi отдаёт миниатюры в порядке возрастания размера (обычно 60px и 120px).
    Для крупной обложки ("Моя Волна") берём последнюю и, если это ссылка
    googleusercontent, подменяем размер на 544x544.
    """
    if not thumbnails:
        return None
    url = thumbnails[-1].get("url")
    return _SIZE_RE.sub("=w544-h544", url) if url else None


def _parse_duration(item: dict) -> int | None:
    if isinstance(item.get("duration_seconds"), int):
        return item["duration_seconds"]
    raw = item.get("duration")  # формат "3:45" или "1:02:10"
    if not raw:
        return None
    try:
        parts = [int(p) for p in raw.split(":")]
    except ValueError:
        return None
    seconds = 0
    for p in parts:
        seconds = seconds * 60 + p
    return seconds


def _normalize_yt(item: dict) -> Track | None:
    video_id = item.get("videoId")
    if not video_id:  # в выдаче бывают элементы без видео (альбомы, плейлисты)
        return None

    artists = [a["name"] for a in (item.get("artists") or []) if a.get("name")]
    artist_id = next((a["id"] for a in (item.get("artists") or []) if a.get("name") and a.get("id")), None)
    artist_ids = [a.get("id") for a in (item.get("artists") or []) if a.get("name")]
    raw_album = item.get("album")       # в поиске это {"name": ...}, а в get_album() просто строка с названием
    album = raw_album.get("name") if isinstance(raw_album, dict) else (raw_album if isinstance(raw_album, str) else None)

    return Track(
        id=video_id,
        source="yt",
        title=item.get("title") or "Без названия",
        artist=", ".join(artists) or "Неизвестный исполнитель",
        artists=artists,
        album=album,
        duration=_parse_duration(item),
        cover=_best_cover(item.get("thumbnails")),
        explicit=bool(item.get("isExplicit")),
        # Этот URL реализуем на Шаге 2. Фронтенд просто подставит его в <audio>.
        stream_url=f"/api/stream/yt/{video_id}",
        artist_id=artist_id,
        artist_ids=artist_ids,
        url=f"https://music.youtube.com/watch?v={video_id}",
    )


# ---------------------------------------------------------------------------
# Провайдеры поиска
# ---------------------------------------------------------------------------
def _search_yt_sync(ytm: YTMusic, query: str, limit: int) -> list[Track]:
    """Синхронный вызов ytmusicapi (библиотека блокирующая, использует requests)."""
    # ytmusicapi сам догружает страницы; пагинация сделана срезом [offset:offset+limit]
    raw = ytm.search(query, filter="songs", limit=min(limit, MAX_RESULTS))
    tracks = (_normalize_yt(item) for item in raw)
    return [t for t in tracks if t is not None]


async def search_youtube(request: Request, query: str, limit: int) -> list[Track]:
    # Выносим блокирующий вызов в поток, чтобы не вешать event loop.
    return await asyncio.to_thread(_search_yt_sync, request.app.state.ytm, query, limit)


_SC_ART_RE = re.compile(r"-large\.")


def _normalize_sc(item: dict) -> Track | None:
    if item.get("kind", "track") != "track" or not item.get("id"):
        return None
    # BLOCK = недоступен в регионе, SNIP = только 30-секундный превью (Go+): пропускаем,
    # иначе трек оборвётся на 30-й секунде.
    if item.get("policy") in ("BLOCK", "SNIP") or item.get("streamable") is False:
        return None

    track_id = str(item["id"])
    user = item.get("user") or {}
    meta = item.get("publisher_metadata") or {}
    artist = meta.get("artist") or user.get("username") or "Неизвестный исполнитель"

    millis = item.get("full_duration") or item.get("duration")
    art = item.get("artwork_url") or user.get("avatar_url")

    return Track(
        id=track_id,
        source="sc",
        title=item.get("title") or "Без названия",
        artist=artist,
        artists=[artist],
        album=meta.get("album_title"),
        duration=round(millis / 1000) if millis else None,
        # SoundCloud отдаёт 100x100 ("-large"); просим 500x500
        cover=_SC_ART_RE.sub("-t500x500.", art) if art else None,
        explicit=bool(meta.get("explicit")),
        stream_url=f"/api/stream/sc/{track_id}",
        artist_id=str(user["id"]) if user.get("id") else None,   # страница артиста = страница загрузившего
        artist_ids=[str(user["id"])] if user.get("id") else [],
        plays=item.get("playback_count"),
        released=item.get("display_date") or item.get("created_at"),
        url=item.get("permalink_url"),
    )


async def search_soundcloud(request: Request, query: str, limit: int) -> list[Track]:
    raw = await asyncio.to_thread(sc_search_raw, query, limit)
    tracks = (_normalize_sc(item) for item in raw)
    return [t for t in tracks if t is not None]


# Реестр провайдеров. Чтобы добавить источник, достаточно написать async-функцию
# с той же сигнатурой, возвращающую list[Track], и зарегистрировать её здесь.
Provider = Callable[[Request, str, int], Awaitable[list[Track]]]
_ALL_PROVIDERS: dict[str, Provider] = {
    "yt": search_youtube,
    "sc": search_soundcloud,
}
# Какие источники включены. Чтобы совсем отключить SoundCloud: SEARCH_SOURCES=yt
_ENABLED = {s.strip() for s in os.getenv("SEARCH_SOURCES", "yt,sc").split(",")}
PROVIDERS: dict[str, Provider] = {k: v for k, v in _ALL_PROVIDERS.items() if k in _ENABLED}

# Один медленный источник не должен тормозить весь поиск.
PROVIDER_TIMEOUT = float(os.getenv("SEARCH_TIMEOUT", "6"))
# «Автомат» для необязательных источников: после сбоя не опрашиваем их N секунд
# (иначе каждый поиск ждал бы таймаут). YouTube сюда не входит: он основной.
OPTIONAL_SOURCES = {"sc"}
SOURCE_COOLDOWN = float(os.getenv("SOURCE_COOLDOWN", "300"))
_down_until: dict[str, float] = {}


async def _timed(name: str, request: Request, query: str, limit: int) -> list[Track]:
    """Запрос к одному источнику с таймаутом и замером времени (видно в консоли)."""
    t0 = time.monotonic()
    tracks = await asyncio.wait_for(PROVIDERS[name](request, query, limit), PROVIDER_TIMEOUT)
    log.info("Поиск %s: %d треков за %.1f с", name, len(tracks), time.monotonic() - t0)
    return tracks


def _interleave(groups: list[list[Track]], limit: int) -> list[Track]:
    """Чередуем источники (YT, SC, YT, SC…), чтобы в начале выдачи были оба."""
    merged: list[Track] = []
    for i in range(max((len(g) for g in groups), default=0)):
        for g in groups:
            if i < len(g):
                merged.append(g[i])
    return merged[:limit]


# ---------------------------------------------------------------------------
# Простой in-memory кэш (TTL), чтобы не дёргать YouTube на каждый повтор запроса
# ---------------------------------------------------------------------------
_cache: dict[tuple[str, int], tuple[float, list[Track]]] = {}


def _cache_get(key: tuple[str, int]) -> list[Track] | None:
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < CACHE_TTL_SEC:
        return hit[1]
    _cache.pop(key, None)
    return None


def _cache_set(key: tuple[str, int], value: list[Track]) -> None:
    if len(_cache) >= CACHE_MAX_ITEMS:
        _cache.pop(next(iter(_cache)))  # выкидываем самую старую запись
    _cache[key] = (time.monotonic(), value)


# ---------------------------------------------------------------------------
# Приложение
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Клиент без авторизации: для поиска по каталогу этого достаточно.
    app.state.ytm = YTMusic()
    log.info("YTMusic инициализирован")
    # Один httpx-клиент на всё приложение: пул соединений и keep-alive к googlevideo
    app.state.http = make_http_client()
    library.init()  # локальный индекс загрузок (SQLite)
    userdata.init()  # избранное, история, настройки
    playlists.init()  # локальные плейлисты
    cleanup_partial()  # недокачанные загрузки прошлого запуска
    yield
    await app.state.http.aclose()
    _cache.clear()


app = FastAPI(title="Music Meta Client", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in ALLOWED_ORIGINS if o.strip()],
    allow_methods=["GET", "OPTIONS"],
    # Range нужен для <audio> при кросс-доменной работе (Шаг 2);
    # Content-Range и др. должны быть видны клиенту.
    allow_headers=["Range", "Content-Type"],
    expose_headers=["Content-Range", "Accept-Ranges", "Content-Length"],
)
# Сервер слушает только 127.0.0.1; запросы с чужим именем хоста (атака через DNS rebinding) отклоняем
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])


app.include_router(stream_router)  # /api/stream/{source}/{id}
app.include_router(lyrics_router)  # /api/lyrics
app.include_router(downloads_router)  # /api/downloads, /api/local
catalog.configure(_normalize_yt, _normalize_sc)
app.include_router(userdata.router)  # /api/favorites, /api/history, /api/settings
app.include_router(playlists.router)  # /api/playlists
app.include_router(recs.router)  # /api/recommendations
app.include_router(catalog.router)  # /api/search/{artists|albums}, /api/artist, /api/album


@app.get("/api/health")
async def health():
    return {"status": "ok"}


@app.get("/api/search", response_model=SearchResponse)
async def search(
    request: Request,
    q: str = Query(..., min_length=1, max_length=200, description="Поисковый запрос"),
    limit: int = Query(20, ge=1, le=50, description="Максимум треков"),
    sources: str | None = Query(None, description="Источники через запятую, например: yt или yt,sc"),
    offset: int = Query(0, ge=0, le=MAX_RESULTS, description="Сколько треков пропустить (пагинация)"),
):
    query = q.strip()
    if not query:
        raise HTTPException(status_code=400, detail="Пустой поисковый запрос")

    # Какие источники опрашивать (фронтенд запрашивает каждый отдельно, чтобы быстрый
    # источник не ждал медленный)
    wanted = {s.strip() for s in sources.split(",")} if sources else None
    selected = [n for n in PROVIDERS if wanted is None or n in wanted]
    if not selected:
        raise HTTPException(status_code=400, detail="Неизвестные или отключённые источники")

    key = (query.lower(), limit, offset, ",".join(selected))
    cached = _cache_get(key)
    if cached is not None:
        return SearchResponse(query=query, count=len(cached), tracks=cached,
                              has_more=bool(cached) and offset + limit < MAX_RESULTS)

    # Источники, которые недавно упали, пропускаем сразу (без ожидания таймаута)
    now = time.monotonic()
    failed_sources: list[str] = [n for n in selected if _down_until.get(n, 0) > now]
    names = [n for n in selected if n not in failed_sources]
    if not names:
        raise HTTPException(status_code=502, detail="Источники поиска недоступны")

    # Опрашиваем провайдеры параллельно, у каждого свой таймаут; сбой одного не роняет остальных.
    results = await asyncio.gather(
        *(_timed(n, request, query, min(offset + limit, MAX_RESULTS)) for n in names),
        return_exceptions=True,
    )

    groups: list[list[Track]] = []
    for name, res in zip(names, results):
        if isinstance(res, BaseException):
            failed_sources.append(name)
            log.warning("Провайдер %s не ответил: %r", name, res)
            if name in OPTIONAL_SOURCES:
                _down_until[name] = time.monotonic() + SOURCE_COOLDOWN
                log.warning("Источник %s отключён на %d с (после сбоя)", name, SOURCE_COOLDOWN)
        else:
            _down_until.pop(name, None)
            groups.append(res)

    if not groups:
        raise HTTPException(status_code=502, detail="Источники поиска недоступны")

    tracks = _interleave(groups, offset + limit)[offset:]
    if not failed_sources:  # неполные результаты не кэшируем, чтобы источник мог «выздороветь»
        _cache_set(key, tracks)
    return SearchResponse(query=query, count=len(tracks), tracks=tracks, failed_sources=failed_sources,
                          has_more=bool(tracks) and offset + limit < MAX_RESULTS)


# Раздача фронтенда (Шаги 3–4). Монтируем В САМОМ КОНЦЕ, после всех /api-роутов,
# иначе "/" перехватит их. Один адрес для API и UI = никаких проблем с CORS.
FRONTEND_DIR = paths.resource_dir() / "frontend"   # и в разработке, и внутри собранного приложения
if FRONTEND_DIR.is_dir():
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
else:
    log.warning("Папка фронтенда не найдена: %s", FRONTEND_DIR)

# «Очистить кэш» в настройках сбрасывает эти кэши в памяти (загрузки и история не затрагиваются)
import lyrics as _lyrics  # noqa: E402
import stream as _stream  # noqa: E402

for _fn in (_cache.clear, _lyrics._cache.clear, catalog._artist_cache.clear, _stream._heads.clear, _stream._cache.clear):
    userdata.register_cache_clear(_fn)