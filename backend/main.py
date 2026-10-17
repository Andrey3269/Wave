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
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import applog
import catalog
import diagnostics
import jsruntime
import library
import paths
import playback
import playlists
import providers
import recs
import userdata
from domain.models import Track
from downloads import DownloadManager, router as downloads_router, startup_cleanup
from lyrics import router as lyrics_router
from origin_guard import OriginGuard
from providers import soundcloud as sc_provider, youtube_music as yt_provider
from providers.base import ProviderError
from providers.cache import TTLCache
from streaming import hls_proxy as _hls_proxy, netguard, proxy as _proxy, resolver as _resolver
from streaming.proxy import make_http_client
from streaming.resolver import ResolverState
from streaming.router import router as stream_router
from version import __version__

log = logging.getLogger("musicmeta")
_handlers_before = list(logging.getLogger().handlers)
logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)  # иначе в консоль летят все URL googlevideo
# S13 (L1): консоль разработки (uvicorn main:app), созданная basicConfig, маскирует секреты так же, как файл журнала
# (applog.setup в лаунчере). Чужие обработчики (pytest, встраивающее приложение) не трогаем
applog.protect([h for h in logging.getLogger().handlers if h not in _handlers_before])

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
# Единая модель трека (общая для YouTube / SoundCloud) — backend/domain/models.py (S7), здесь тот же класс
# ---------------------------------------------------------------------------
class SearchResponse(BaseModel):
    query: str
    count: int
    tracks: list[Track]
    failed_sources: list[str] = []           # источники, которые не ответили ("yt", "sc")
    has_more: bool = False                   # есть ли смысл просить следующую страницу
    # S7 (аддитивно): состояние каждого источника в этом ответе — ok | degraded | cooldown | disabled
    # (providers/base.SourceStatus). failed_sources = источники со статусом cooldown или degraded.
    source_status: dict[str, str] = {}


# ---------------------------------------------------------------------------
# Нормализация — в адаптерах источников (S7): backend/providers/youtube_music.py и soundcloud.py.
# Прежние имена оставлены для совместимости (тесты нормализации, внешние вызовы).
# ---------------------------------------------------------------------------
_best_cover = yt_provider.best_cover
_parse_duration = yt_provider.parse_duration
_normalize_yt = yt_provider.normalize_track
_search_yt_sync = yt_provider.search_songs_sync
_normalize_sc = sc_provider.normalize_track


# ---------------------------------------------------------------------------
# Источники поиска: реестр адаптеров (providers/__init__.py)
# ---------------------------------------------------------------------------
# Какие источники включены в поиск треков. Чтобы совсем отключить SoundCloud: SEARCH_SOURCES=yt
_ENABLED = {s.strip() for s in os.getenv("SEARCH_SOURCES", "yt,sc").split(",")}
for _name, _p in providers.REGISTRY.items():
    _p.search_enabled = _name in _ENABLED
PROVIDERS = {name: p for name, p in providers.REGISTRY.items() if p.search_enabled}

# Один медленный источник не должен тормозить весь поиск: столько секунд ждём ответа каждого.
# Пауза после сбоя (cooldown) и пул потоков — у каждого адаптера свои (providers/base.py).
PROVIDER_TIMEOUT = float(os.getenv("SEARCH_TIMEOUT", "6"))


async def _timed(name: str, request: Request, query: str, limit: int) -> list[Track]:
    """Запрос к одному источнику с таймаутом и замером времени (видно в журнале)."""
    t0 = time.monotonic()
    tracks = await PROVIDERS[name].search_tracks(request.app.state, query, limit, timeout=PROVIDER_TIMEOUT)
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
# Кэш поиска в памяти: LRU + TTL (S7, providers/cache.py), чтобы не дёргать источники на каждый повтор
# запроса. Ключ — нормализованный запрос, limit, offset и набор источников; неполные результаты
# (какой-то источник не ответил) не кэшируются. Число записей ограничено при любом числе разных запросов.
# ---------------------------------------------------------------------------
_cache: TTLCache[list[Track]] = TTLCache(maxsize=CACHE_MAX_ITEMS, ttl=CACHE_TTL_SEC)


def _cache_get(key: tuple) -> list[Track] | None:
    return _cache.get(key)


def _cache_set(key: tuple, value: list[Track]) -> None:
    _cache.set(key, value)


# ---------------------------------------------------------------------------
# Приложение
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Клиент YouTube Music без авторизации (для каталога этого достаточно), с таймаутом HTTP-запросов (S7)
    app.state.ytm = yt_provider.create_client()
    log.info("YTMusic инициализирован")
    # Один httpx-клиент на всё приложение: пул соединений и keep-alive к googlevideo
    app.state.http = make_http_client()
    # S8: отдельный клиент HLS-прокси и HLS-загрузок — соединения только с проверенными IP (streaming/netguard.py)
    app.state.hls_http = netguard.make_client()
    # Асинхронное состояние резолвера (семафор, задачи) принадлежит циклу ЭТОГО запуска: сторож лаунчера
    # поднимает uvicorn заново в том же процессе с новым event loop (находка M1)
    app.state.resolver = ResolverState()
    library.init()  # локальный индекс загрузок (SQLite)
    userdata.init()  # избранное, история, настройки
    playlists.init()  # локальные плейлисты
    # Недокачанные загрузки прошлого запуска — во всех проверенных каталогах загрузок (S11). Обход диска — в потоке;
    # старт ждёт его не дольше downloads.CLEANUP_STARTUP_WAIT (недоступный сетевой диск), остальное — в фоне
    await startup_cleanup()
    # S11: менеджер загрузок этого запуска (лимит, очередь, отмена); объекты asyncio — в цикле этого запуска (M1)
    app.state.downloads = DownloadManager()
    try:
        yield
    finally:
        # S11: сначала загрузки — они пользуются резолвером и обоими HTTP-клиентами. Новые не принимаются, текущие
        # отменяются и дожидаются: запись в потоке закончена, файлы закрыты, папки .part удалены
        await app.state.downloads.aclose()
        await app.state.resolver.aclose()  # отменяем резолвы и прогрев этого запуска до закрытия клиента
        # Потоки резолвера не отменить, но их дочерние процессы (Deno) завершаем: резолвы этого запуска
        # уже никому не нужны, а процесс не должен пережить остановку backend (S3, находка L8)
        await asyncio.to_thread(jsruntime.terminate_children)
        await app.state.http.aclose()
        await app.state.hls_http.aclose()
        providers.shutdown_pending()  # запросы к источникам, ждущие в очереди пулов, этому запуску уже не нужны
        _cache.clear()


app = FastAPI(title="Music Meta Client", version=__version__, lifespan=lifespan)   # версия — backend/version.py

# S13 (M7): изменяющие запросы только с адреса самого сервера — кросс-доменный POST/PUT/PATCH/DELETE получает 403
# (backend/origin_guard.py). Самый внутренний слой: снаружи TrustedHost (Host уже проверен) и CORS (preflight)
app.add_middleware(OriginGuard)
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


@app.exception_handler(ProviderError)
async def provider_error(request: Request, exc: ProviderError):
    """
    Сбой источника (S7). `detail` — понятный текст, как и раньше (на него опирается фронтенд); аддитивно:
    `code` — машинный тип ошибки (providers/base.ERROR_KINDS), `source` — источник, `source_status` — его
    состояние (cooldown — пауза после сбоя, degraded — прочие сбои). Статус 502, как до S7.
    """
    return JSONResponse(status_code=502, content={"detail": exc.user_message, "code": exc.code, "source": exc.source,
                                                  "source_status": {exc.source: exc.status}})


app.include_router(stream_router)  # /api/stream/{source}/{id}
app.include_router(lyrics_router)  # /api/lyrics
app.include_router(downloads_router)  # /api/downloads, /api/local
app.include_router(userdata.router)  # /api/favorites, /api/history, /api/settings
app.include_router(playback.router)  # /api/player/state: очередь и позиция между запусками (S10)
app.include_router(playlists.router)  # /api/playlists
app.include_router(recs.router)  # /api/recommendations
app.include_router(catalog.router)  # /api/search/{artists|albums}, /api/artist, /api/album


@app.get("/api/health")
async def health():
    # version (S4, аддитивно): по ней проверка собранного backend (WIN-13) убеждается, что отвечает
    # именно установленная версия, а не другой запущенный экземпляр
    return {"status": "ok", "version": __version__}


@app.get("/api/diagnostics")
async def diagnostics_report():
    """
    Состояние компонентов воспроизведения YouTube (yt-dlp, yt-dlp-ejs, JS-runtime) без обращения к сети.
    Подтверждает конфигурацию, а не воспроизведение (поле playback_verified=false). Секретов не содержит.
    """
    return await asyncio.to_thread(diagnostics.collect, True)


def _status_of(exc: BaseException) -> str:
    """Статус источника, который не ответил: cooldown (пауза после сбоя) или degraded (прочие сбои)."""
    return exc.status if isinstance(exc, ProviderError) else "degraded"


def _disabled_sources(wanted: set[str] | None) -> dict[str, str]:
    """Источники, выключенные для поиска (SEARCH_SOURCES), которые были запрошены (или все, если не уточняли)."""
    return {n: "disabled" for n, p in providers.REGISTRY.items()
            if not p.search_enabled and (wanted is None or n in wanted)}


def _all_sources_failed(selected: list[str], errors: dict[str, BaseException],
                        status: dict[str, str]) -> JSONResponse:
    """
    Ни один выбранный источник не ответил: 502, как до S7. Если источник был один (так ищет фронтенд —
    по каждому источнику отдельно), `detail` — его понятная причина (пауза после сбоя, таймаут …);
    для нескольких — прежний текст. Аддитивно: `code`, `source` (для одного источника), `failed_sources`,
    `source_status`.
    """
    body: dict = {"detail": "Источники поиска недоступны", "code": "unavailable", "failed_sources": list(errors),
                  "source_status": status}
    if len(selected) == 1:
        err = errors.get(selected[0])
        if isinstance(err, ProviderError):
            body.update(detail=err.user_message, code=err.code, source=err.source)
        else:
            body["source"] = selected[0]
    return JSONResponse(status_code=502, content=body)


@app.get("/api/sources")
async def sources_report():
    """
    Состояние музыкальных источников (S7): статус (ok | degraded | cooldown), сколько осталось до конца паузы,
    тип последней ошибки, загрузка пула потоков, возможности источника, включён ли он в поиск треков.
    Без сети, секретов и ссылок.
    """
    return {"sources": {name: p.snapshot() for name, p in providers.REGISTRY.items()}}


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
    disabled = _disabled_sources(wanted)
    cached = _cache_get(key)
    if cached is not None:                   # в кэше только полные результаты: все выбранные источники ответили
        return SearchResponse(query=query, count=len(cached), tracks=cached,
                              has_more=bool(cached) and offset + limit < MAX_RESULTS,
                              source_status={**{n: "ok" for n in selected}, **disabled})

    # Источники на паузе после сбоя (cooldown) пропускаем сразу, без ожидания таймаута
    errors: dict[str, BaseException] = {}
    for n in selected:
        try:
            PROVIDERS[n].check_available()
        except ProviderError as exc:
            errors[n] = exc
    failed_sources: list[str] = list(errors)
    names = [n for n in selected if n not in errors]

    # Опрашиваем источники параллельно, у каждого свой пул и таймаут; сбой одного не роняет остальных.
    results = await asyncio.gather(
        *(_timed(n, request, query, min(offset + limit, MAX_RESULTS)) for n in names),
        return_exceptions=True,
    )

    groups: list[list[Track]] = []
    for name, res in zip(names, results):
        if isinstance(res, BaseException):
            failed_sources.append(name)
            errors[name] = res
            log.warning("Провайдер %s не ответил: %r", name, res)
        else:
            groups.append(res)

    status = {n: _status_of(errors[n]) if n in errors else "ok" for n in selected}
    status.update(disabled)
    if not groups:
        return _all_sources_failed(selected, errors, status)

    tracks = _interleave(groups, offset + limit)[offset:]
    if not failed_sources:  # неполные результаты не кэшируем, чтобы источник мог «выздороветь»
        _cache_set(key, tracks)
    return SearchResponse(query=query, count=len(tracks), tracks=tracks, failed_sources=failed_sources,
                          has_more=bool(tracks) and offset + limit < MAX_RESULTS, source_status=status)


# Раздача фронтенда (Шаги 3–4). Монтируем В САМОМ КОНЦЕ, после всех /api-роутов,
# иначе "/" перехватит их. Один адрес для API и UI = никаких проблем с CORS.
FRONTEND_DIR = paths.resource_dir() / "frontend"   # и в разработке, и внутри собранного приложения
_JS_SUFFIXES = (".js", ".mjs")


class FrontendFiles(StaticFiles):
    """Статика интерфейса (S9: интерфейс — ES-модули).

    * `.js` всегда отдаётся как JavaScript. Тип файла StaticFiles берёт из модуля `mimetypes`, а он на Windows
      читает реестр, где `.js` бывает записан как `text/plain`. Обычный <script> это терпит, модуль — нет:
      WebView2 отказывается исполнять модуль с таким типом, и окно остаётся пустым.
    * `Cache-Control: no-cache`: браузер перепроверяет файлы по ETag (ответ 304, без повторной передачи).
      Модули ссылаются друг на друга — после обновления приложения нельзя смешать файлы двух версий из кэша.
    """

    def file_response(self, full_path, stat_result, scope, status_code: int = 200):
        response = super().file_response(full_path, stat_result, scope, status_code)
        response.headers["Cache-Control"] = "no-cache"
        if str(full_path).lower().endswith(_JS_SUFFIXES) and response.status_code != 304:
            response.headers["Content-Type"] = "text/javascript; charset=utf-8"
        return response


if FRONTEND_DIR.is_dir():
    app.mount("/", FrontendFiles(directory=FRONTEND_DIR, html=True), name="frontend")
else:
    log.warning("Папка фронтенда не найдена: %s", FRONTEND_DIR)

# «Очистить кэш» в настройках сбрасывает эти кэши в памяти (загрузки и история не затрагиваются)
import lyrics as _lyrics  # noqa: E402
for _fn in (_cache.clear, _lyrics._cache.clear, catalog._artist_cache.clear, _proxy._heads.clear, _resolver._cache.clear,
            _hls_proxy.clear):
    userdata.register_cache_clear(_fn)