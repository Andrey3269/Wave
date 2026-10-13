"""
Проксирование аудиопотока с поддержкой Range (HTTP 206) для YouTube и SoundCloud.

Схема:
    <audio> --Range--> /api/stream/{source}/{id} --Range--> CDN (прямая ссылка от yt-dlp)
                       <--206 + чанки------                 <--206 + чанки--

Почему именно так:
  * ссылки googlevideo привязаны к IP того, кто их получил -> и резолвим, и
    скачиваем с ОДНОГО сервера и по одному IP-семейству (принудительно IPv4);
  * браузер сам шлёт `Range: bytes=N-` при старте и перемотке, мы просто
    прозрачно пробрасываем диапазон и возвращаем 206 + Content-Range.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import ssl
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import AsyncIterator, Callable
from urllib.parse import parse_qs, urlparse

import certifi
import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, Response, StreamingResponse

import jsruntime
import library
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

log = logging.getLogger("musicmeta.stream")
router = APIRouter(prefix="/api/stream", tags=["stream"])

# Дочерние процессы yt-dlp (JS-runtime Deno): без окна консоли, с таймаутом и завершением при выходе
jsruntime.install_process_guard()

# ---------------------------------------------------------------------------
# Константы и настройки
# ---------------------------------------------------------------------------
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")      # videoId YouTube
SC_ID_RE = re.compile(r"^\d{1,15}$")                  # числовой id трека SoundCloud
RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")         # одиночный диапазон (без multipart)

CHUNK_SIZE = 64 * 1024            # размер куска при перекачке внутри одного ответа
# Максимальный размер одной порции трека. Браузер просит "bytes=0-" (весь файл), а мы
# отвечаем только первой порцией (206 + Content-Range), и плеер сам докачивает
# следующие по мере надобности. Так же работает YouTube Music: быстрый старт и
# перемотка, и CDN не режет скорость на длинных запросах.
STREAM_CHUNK_BYTES = int(os.getenv("STREAM_CHUNK_KB", "1024")) * 1024

# Кэш «голов»: первая порция прогретых треков лежит в памяти (24 x 1 МБ ~ 24 МБ).
HEAD_CACHE_ITEMS = 24
HEAD_TTL = 30 * 60
CONTENT_RANGE_RE = re.compile(r"^bytes (\d+)-(\d+)/(\d+|\*)$")
DEFAULT_TTL = 30 * 60             # TTL ссылки, если в ней нет параметра expire
EXPIRY_MARGIN = 120               # обновляем ссылку за 2 минуты до истечения
MAX_PARALLEL_RESOLVES = 4         # одновременных запусков yt-dlp
STALE_STATUSES = {401, 403, 404, 410}  # ответы CDN при протухшей ссылке

# Опционально: куки (если YouTube просит подтвердить, что вы не бот) и прокси.
# ВАЖНО: если резолвим через прокси, то и качать надо через него же.
YTDLP_COOKIES = os.getenv("YTDLP_COOKIES")   # путь к cookies.txt
YTDLP_PROXY = os.getenv("YTDLP_PROXY")       # например socks5://user:pass@host:1080

# Из заголовков yt-dlp оставляем только безопасные. Accept-Encoding задаём сами:
# сжатие вместе с Range ломает подсчёт байтов.
FORWARD_HEADERS = {"user-agent", "accept", "accept-language", "referer", "origin"}


@dataclass(frozen=True)
class Source:
    """Описание источника: как строить URL для yt-dlp и какой формат брать."""
    label: str
    page_url: Callable[[str], str]
    id_re: re.Pattern
    fmt: str
    progressive_only: bool = False   # нужен обычный HTTP-файл (HLS через Range не проксируется)


SOURCES: dict[str, Source] = {
    "yt": Source(
        label="YouTube Music",
        page_url=lambda i: f"https://www.youtube.com/watch?v={i}",
        id_re=VIDEO_ID_RE,
        # m4a (AAC) играет везде, включая Safari/iOS; иначе — лучший аудиопоток (Opus)
        fmt="bestaudio[ext=m4a]/bestaudio/best",
    ),
    "sc": Source(
        label="SoundCloud",
        page_url=lambda i: f"https://api.soundcloud.com/tracks/{i}",
        id_re=SC_ID_RE,
        # только прогрессивный mp3 по http(s); HLS-варианты (m3u8) пропускаем
        fmt="bestaudio[protocol=https]/bestaudio[protocol=http]",
        progressive_only=True,
    ),
}


def make_http_client() -> httpx.AsyncClient:
    """Общий HTTP-клиент для всего приложения (keep-alive, пул соединений)."""
    # Явный контекст на сертификатах certifi (он ставится вместе с httpx). Так мы не
    # зависим от переменных окружения SSL_CERT_FILE / SSL_CERT_DIR: если они на
    # компьютере указывают на несуществующий файл, httpx иначе падает при старте.
    ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    # Ссылки googlevideo привязаны к IP, с которого их получил yt-dlp. Если httpx уйдёт
    # по IPv6, Google ответит 403. local_address="0.0.0.0" = только IPv4 (при прокси не нужно).
    transport = httpx.AsyncHTTPTransport(
        verify=ssl_ctx,
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
        local_address=None if YTDLP_PROXY else "0.0.0.0",
        proxy=YTDLP_PROXY or None,
        retries=1,  # повтор при ошибке установки соединения
    )
    return httpx.AsyncClient(
        transport=transport,
        timeout=httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0),
        follow_redirects=True,
    )


# ---------------------------------------------------------------------------
# Резолвер: yt-dlp -> прямая ссылка, с кэшем и защитой от дублей
# ---------------------------------------------------------------------------
class StreamUnavailable(Exception):
    """У трека нет подходящего прямого потока."""


# ---------------------------------------------------------------------------
# Понятные ошибки (находка L6). Пользователь видит короткий текст без технических подробностей:
# ни текста исключения, ни URL, ни путей и значений из настроек. Подробности — только в журнале.
# Тип ошибки уходит в заголовке X-Wave-Error (для тестов и будущего интерфейса).
# ---------------------------------------------------------------------------
ERROR_HEADER = "X-Wave-Error"

_AGE = ("confirm your age", "age-restricted", "age restricted", "inappropriate for some users")
_BOT = ("not a bot", "not a robot", "sign in to confirm", "--cookies", "cookies-from-browser")
_RATE = ("http error 429", "too many requests")
_PROXY = ("proxy", "socks")
_NETWORK = (
    "urlopen error", "name resolution", "name or service not known", "nodename nor servname", "getaddrinfo",
    "failed to resolve", "errno 11001", "errno 11002", "network is unreachable", "no route to host",
    "connection refused", "connection reset", "connection aborted", "remote end closed", "timed out", "timeout",
    "errno 10051", "errno 10054", "errno 10060", "errno 10061", "errno 10065", "ssl:", "eof occurred",
)
_FORMAT = ("requested format is not available", "no video formats", "nsig", "signature extraction",
           "unable to extract", "javascript runtime", "js runtime", "player response")
_GONE = ("video unavailable", "this video is unavailable", "private video", "is private", "not available",
         "removed", "has been terminated", "does not exist", "unavailable", "private", "404")


def _err(status: int, kind: str, detail: str) -> HTTPException:
    return HTTPException(status, detail, headers={ERROR_HEADER: kind})


def _resolve_error(source: str, exc: DownloadError) -> HTTPException:
    """Сообщение yt-dlp -> понятная ошибка. Про cookies говорим только когда YouTube действительно просит вход."""
    label = SOURCES[source].label
    msg = str(exc).lower()
    if any(s in msg for s in _AGE):
        return _err(403, "age_restricted", "Трек ограничен YouTube по возрасту и недоступен без входа в аккаунт.")
    if any(s in msg for s in _BOT):
        hint = ("Возможно, файл cookies из YTDLP_COOKIES устарел." if YTDLP_COOKIES
                else "Если повтор не помогает, можно указать файл cookies в переменной YTDLP_COOKIES.")
        return _err(503, "bot_check", f"{label} просит подтвердить, что запрос не от бота. Повторите позже. {hint}")
    if any(s in msg for s in _RATE):
        return _err(503, "rate_limited", f"{label} временно ограничил число запросов. Повторите через несколько минут.")
    if YTDLP_PROXY and any(s in msg for s in _PROXY):
        return _err(502, "proxy", "Не удалось подключиться через прокси из настройки YTDLP_PROXY. Проверьте прокси.")
    if any(s in msg for s in _NETWORK):
        return _err(503, "network", f"Нет связи с {label}. Проверьте подключение к интернету.")
    if any(s in msg for s in _FORMAT):
        return _err(502, "extractor", f"Не удалось получить аудиопоток: {label} изменил формат ответа или компонент "
                                      "yt-dlp устарел. Обычно помогает обновление приложения.")
    if any(s in msg for s in _GONE):
        return _err(404, "unavailable", "Трек недоступен")
    return _err(502, "resolve_failed", f"Не удалось получить аудиопоток от {label}. Попробуйте позже.")


# Ошибки, за которыми может стоять JS-runtime. Возраст, проверка на бота, лимит запросов, сеть и
# «трек недоступен» — настоящие причины, их не подменяем (S3, требование 13).
_RUNTIME_KINDS = {"extractor", "resolve_failed", "internal"}


def _runtime_error(source: str, err: HTTPException, js_issue: str | None) -> HTTPException:
    """
    YouTube без JS-runtime работает в урезанном режиме и падает с общей ошибкой формата. Если runtime
    недоступен — говорим об этом (а не «трек недоступен»); если доступен, но yt-dlp сообщил о сбое
    решения JS-задачи — тоже отдельный текст. Подробности — в журнале (jsruntime.YtDlpLogger).
    """
    if source != "yt" or err.headers.get(ERROR_HEADER) not in _RUNTIME_KINDS:
        return err
    st = jsruntime.status()
    if not st.ok:
        return _err(503, "js_runtime", jsruntime.user_message(st))
    if js_issue:
        return _err(502, "js_runtime_failed", "Компонент воспроизведения YouTube не смог обработать ответ YouTube. "
                                              "Обычно помогает обновление приложения; подробности — в журнале.")
    return err


@dataclass(slots=True)
class Resolved:
    url: str
    headers: dict[str, str]
    expires_at: float             # unix-время

    def alive(self) -> bool:
        return time.time() < self.expires_at


# Ссылки и «головы» (ниже) — просто данные: они не привязаны к event loop и переживают перезапуск
# uvicorn сторожем лаунчера, поэтому живут на уровне модуля.
_cache: dict[str, Resolved] = {}


class ResolverState:
    """
    Асинхронное состояние резолвера на время ОДНОГО запуска приложения (FastAPI lifespan):
    семафор параллельных резолвов, задачи резолва в работе, фоновые задачи прогрева.

    Всё это принадлежит конкретному event loop. Раньше оно жило на уровне модуля и после перезапуска
    uvicorn (новый event loop в том же процессе, launcher.py) семафор оставался привязанным к старому
    циклу: конкурентные резолвы падали с RuntimeError, а задачи старого цикла висели в _inflight
    (находка M1). Теперь lifespan создаёт новое состояние при запуске (app.state.resolver) и закрывает
    его при остановке: незавершённые задачи отменяются и дожидаются, наборы очищаются.
    """

    def __init__(self, max_parallel: int = MAX_PARALLEL_RESOLVES):
        self.loop = asyncio.get_running_loop()            # создаётся только внутри работающего цикла
        self.sem = asyncio.Semaphore(max_parallel)
        self.inflight: dict[str, asyncio.Task[Resolved]] = {}
        self.prefetching: set[str] = set()
        self.background: set[asyncio.Task] = set()
        self.closed = False

    def check(self) -> None:
        """Использовать состояние можно только в том цикле, где оно создано, и только до закрытия."""
        if self.closed:
            raise RuntimeError("Состояние резолвера уже закрыто (приложение остановлено)")
        if asyncio.get_running_loop() is not self.loop:
            raise RuntimeError("Состояние резолвера создано в другом event loop")

    def spawn(self, coro) -> asyncio.Task:
        """Фоновая задача этого запуска: ссылка хранится до завершения, при остановке задача отменяется."""
        task = asyncio.create_task(coro)
        self.background.add(task)
        task.add_done_callback(self.background.discard)
        return task

    async def aclose(self) -> None:
        self.closed = True
        tasks = [t for t in (*self.background, *self.inflight.values()) if not t.done()]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.inflight.clear()
        self.prefetching.clear()
        self.background.clear()


def _url_expiry(url: str) -> float:
    """У googlevideo в ссылке есть ?expire=<unix>. Если нет — берём TTL по умолчанию."""
    try:
        exp = int(parse_qs(urlparse(url).query).get("expire", [""])[0])
        return exp - EXPIRY_MARGIN
    except ValueError:
        return time.time() + DEFAULT_TTL


def ydl_opts(fmt: str = "bestaudio/best") -> dict:
    """Общие настройки yt-dlp (используются и резолвером, и поиском SoundCloud)."""
    opts: dict = {
        "format": fmt,
        "quiet": True,
        # Предупреждения yt-dlp (в т.ч. о JS-runtime и решении JS-задач) — в журнал Wave, не в консоль
        "logger": jsruntime.YDL_LOGGER,
        "noplaylist": True,
        "skip_download": True,
        # Нормальный резолв ~3 с. Если запрос завис, лучше оборвать его за 7 с и повторить,
        # чем ждать 15 с (в логах были резолвы по 18-32 с = таймаут + повтор).
        "socket_timeout": 7,
        "retries": 3,
        # Манифесты DASH/HLS нам не нужны (через Range проксируется только обычный файл),
        # а их загрузка добавляет лишние запросы к YouTube.
        "extractor_args": {"youtube": {"skip": ["dash", "hls", "translated_subs"]}},
        # Кэш yt-dlp на диске включён: в нём лежит client_id SoundCloud и кэш
        # расшифровки YouTube, без него каждый резолв был бы заметно дольше.
    }
    if YTDLP_COOKIES:
        opts["cookiefile"] = YTDLP_COOKIES
    if YTDLP_PROXY:
        opts["proxy"] = YTDLP_PROXY
    else:
        opts["source_address"] = "0.0.0.0"  # только IPv4: тот же IP, что и у httpx
    # JS-runtime для задач плеера YouTube: всегда явный путь (или пустой словарь, если runtime нет)
    opts.update(jsruntime.ydl_options())
    return opts


# Отдельный пул потоков для резолва. В каждом потоке живёт СВОЙ постоянный экземпляр
# YoutubeDL на источник: он держит открытые соединения (без новых TLS-рукопожатий,
# а они при обходе блокировок дорогие) и не тратит время на инициализацию.
_resolve_pool = ThreadPoolExecutor(max_workers=MAX_PARALLEL_RESOLVES, thread_name_prefix="resolve")
_tls = threading.local()


def _get_ydl(source: str) -> YoutubeDL:
    # Экземпляр пересоздаётся, если сменился JS-runtime (например, его нашли при повторной проверке)
    key = jsruntime.config_key()
    ydls: dict[str, tuple[YoutubeDL, str]] = _tls.__dict__.setdefault("ydls", {})
    cached = ydls.get(source)
    if cached is None or cached[1] != key:
        if cached is not None:
            cached[0].close()
        ydls[source] = (YoutubeDL(ydl_opts(SOURCES[source].fmt)), key)
    return ydls[source][0]


def _extract_sync(source: str, track_id: str) -> Resolved:
    """Блокирующий вызов yt-dlp (выполняется в потоке пула _resolve_pool)."""
    cfg = SOURCES[source]
    ydl = _get_ydl(source)
    jsruntime.YDL_LOGGER.begin()
    try:
        info = ydl.extract_info(cfg.page_url(track_id), download=False)
    except DownloadError as exc:
        # Было ли предупреждение о JS-задачах в этом извлечении: по нему ошибка отличается от «трек недоступен»
        exc.wave_js_issue = jsruntime.YDL_LOGGER.take_js_issue()
        raise

    # При выборе одного формата url лежит на верхнем уровне; на случай слияния берём первый.
    fmt = (info.get("requested_formats") or [info])[0]
    url = fmt.get("url")
    if not url:
        raise RuntimeError("yt-dlp не вернул прямой URL")
    if cfg.progressive_only and fmt.get("protocol") not in ("http", "https"):
        raise StreamUnavailable(f"{cfg.label}: только HLS-поток ({fmt.get('protocol')})")

    raw_headers = fmt.get("http_headers") or info.get("http_headers") or {}
    headers = {k: v for k, v in raw_headers.items() if k.lower() in FORWARD_HEADERS}
    return Resolved(url=url, headers=headers, expires_at=_url_expiry(url))


async def _resolve_uncached(rs: ResolverState, source: str, track_id: str) -> Resolved:
    key = f"{source}:{track_id}"
    async with rs.sem:
        t0 = time.monotonic()
        try:
            res = await asyncio.get_running_loop().run_in_executor(
                _resolve_pool, _extract_sync, source, track_id
            )
            log.info("Резолв %s занял %.1f с", key, time.monotonic() - t0)
        except StreamUnavailable as exc:
            log.warning("Нет прямого потока %s: %s", key, exc)
            raise _err(404, "no_direct_stream", "Для этого трека нет прямого аудиопотока") from exc
        except DownloadError as exc:
            err = _runtime_error(source, _resolve_error(source, exc), getattr(exc, "wave_js_issue", None))
            log.warning("yt-dlp: %s -> %s [%s]", key, exc, err.headers[ERROR_HEADER])
            raise err from exc
        except Exception as exc:
            log.exception("Сбой резолвера для %s", key)
            raise _runtime_error(source, _err(502, "internal", "Внутренняя ошибка резолвера"), None) from exc

    _cache[key] = res
    if len(_cache) > 512:  # чистим протухшие записи
        for k in [k for k, v in _cache.items() if not v.alive()]:
            _cache.pop(k, None)
    return res


async def _resolve(rs: ResolverState, source: str, track_id: str) -> Resolved:
    """
    Отдаёт ссылку из кэша или резолвит новую. Браузер шлёт несколько параллельных
    Range-запросов на один трек — rs.inflight гарантирует ровно один запуск yt-dlp.
    """
    key = f"{source}:{track_id}"
    hit = _cache.get(key)
    if hit and hit.alive():
        return hit

    rs.check()
    task = rs.inflight.get(key)
    if task is None:
        task = asyncio.create_task(_resolve_uncached(rs, source, track_id))
        rs.inflight[key] = task
        task.add_done_callback(lambda _t: rs.inflight.pop(key, None))
    # shield: отмена одного клиента не должна убивать резолв для остальных
    return await asyncio.shield(task)


def _invalidate(source: str, track_id: str) -> None:
    _cache.pop(f"{source}:{track_id}", None)


# ---------------------------------------------------------------------------
# Прокси
# ---------------------------------------------------------------------------
async def _connect(
    client: httpx.AsyncClient, rs: ResolverState, source: str, track_id: str, upstream_range: str
) -> httpx.Response:
    """
    Открывает потоковое соединение с CDN. Если ссылка протухла
    (401/403/404/410) или соединение не удалось — один раз перерезолвим и повторим.
    """
    last: int | None = None                       # HTTP-статус последней неудачи или None (нет соединения)
    for _attempt in range(2):
        res = await _resolve(rs, source, track_id)
        headers = {**res.headers, "Accept-Encoding": "identity", "Range": upstream_range}
        try:
            upstream = await client.send(
                client.build_request("GET", res.url, headers=headers), stream=True
            )
        except httpx.HTTPError as exc:
            log.warning("Сбой соединения с источником (%s:%s): %r", source, track_id, exc)
            _invalidate(source, track_id)
            last = None
            continue

        if upstream.status_code in STALE_STATUSES:
            log.info("Ссылка протухла (%s:%s): HTTP %s", source, track_id, upstream.status_code)
            await upstream.aclose()
            _invalidate(source, track_id)
            last = upstream.status_code
            continue
        return upstream

    label = SOURCES[source].label
    if last is None:
        raise _err(503, "network", f"Нет связи с сервером аудио {label}. Проверьте подключение к интернету.")
    raise _err(502, "link_unavailable", f"{label} не отдаёт аудио по ссылке даже после её обновления "
                                        f"(HTTP {last}). Попробуйте позже.")


async def _body(upstream: httpx.Response) -> AsyncIterator[bytes]:
    """Перекачивает чанки клиенту; при разрыве/отмене гарантированно закрывает источник."""
    try:
        async for chunk in upstream.aiter_raw(CHUNK_SIZE):
            yield chunk
    except httpx.HTTPError as exc:
        log.info("Поток оборван источником: %r", exc)
    finally:
        await upstream.aclose()


# ---------------------------------------------------------------------------
# Прогрев: ссылка + первая порция трека заранее лежат на сервере
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class Head:
    data: bytes            # первая порция трека
    total: str             # полный размер файла из Content-Range ("*" если неизвестен)
    content_type: str
    stored_at: float


_heads: OrderedDict[str, Head] = OrderedDict()   # LRU


def _head_get(key: str) -> Head | None:
    head = _heads.get(key)
    if head and time.time() - head.stored_at < HEAD_TTL:
        _heads.move_to_end(key)
        return head
    _heads.pop(key, None)
    return None


def _head_put(key: str, head: Head) -> None:
    _heads[key] = head
    _heads.move_to_end(key)
    while len(_heads) > HEAD_CACHE_ITEMS:
        _heads.popitem(last=False)


def _serve_from_head(head: Head, raw_range: str) -> Response | None:
    """Отвечает на Range из памяти, если начало диапазона попадает в сохранённую порцию."""
    m = RANGE_RE.fullmatch(raw_range)
    if not m or not m.group(1):
        return None
    start, size = int(m.group(1)), len(head.data)
    if start >= size:
        return None
    end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
    if end < start:
        return None
    return Response(
        content=head.data[start : end + 1],
        status_code=206,
        headers={
            "Content-Type": head.content_type,
            "Accept-Ranges": "bytes",
            "Content-Range": f"bytes {start}-{end}/{head.total}",
            "Cache-Control": "private, max-age=3600",
        },
    )


async def _prefetch_head(client: httpx.AsyncClient, rs: ResolverState, source: str, track_id: str) -> None:
    key = f"{source}:{track_id}"
    t0 = time.monotonic()
    try:
        upstream = await _connect(client, rs, source, track_id, f"bytes=0-{STREAM_CHUNK_BYTES - 1}")
        try:
            m = CONTENT_RANGE_RE.fullmatch(upstream.headers.get("content-range", ""))
            if upstream.status_code != 206 or not m:
                return
            data = await upstream.aread()
            ctype = upstream.headers.get("content-type", "audio/mpeg" if source == "sc" else "audio/mp4")
        finally:
            await upstream.aclose()
        _head_put(key, Head(data=data, total=m.group(3), content_type=ctype, stored_at=time.time()))
        log.info("Прогрет %s: %d КБ за %.1f с", key, len(data) // 1024, time.monotonic() - t0)
    except Exception as exc:
        log.info("Прогрев %s не удался: %r", key, exc)
    finally:
        rs.prefetching.discard(key)


def _start_warm(client: httpx.AsyncClient, rs: ResolverState, source: str, track_id: str) -> Response:
    """
    Ответ на ?warm=1: 204 уже готово, 202 готовится, 503 сервер занят (фронтенд повторит позже).
    Сам ответ уходит сразу, прогрев идёт в фоне.
    """
    key = f"{source}:{track_id}"
    if _head_get(key) is not None:
        return Response(status_code=204)
    if key in rs.prefetching:
        return Response(status_code=202)
    if len(rs.inflight) >= 2 or len(rs.prefetching) >= 3:
        return Response(status_code=503)
    rs.check()
    rs.prefetching.add(key)
    rs.spawn(_prefetch_head(client, rs, source, track_id))
    return Response(status_code=202)


def _cap_range(raw: str) -> str:
    """
    Обрезает запрошенный диапазон до одной порции:
      bytes=a-   ->  bytes=a-(a+N-1)
      bytes=a-b  ->  bytes=a-min(b, a+N-1)
    Хвостовой диапазон (bytes=-n) оставляем как есть: плееры используют его для
    маленького кусочка в конце файла.
    """
    m = RANGE_RE.fullmatch(raw)
    if not m or not m.group(1):
        return raw
    start = int(m.group(1))
    last_allowed = start + STREAM_CHUNK_BYTES - 1
    end = min(int(m.group(2)), last_allowed) if m.group(2) else last_allowed
    return f"bytes={start}-{end}"


@router.get("/{source}/{track_id}")
async def stream_track(source: str, track_id: str, request: Request, warm: int = 0):
    cfg = SOURCES.get(source)
    if cfg is None:
        raise HTTPException(404, "Неизвестный источник")
    if not cfg.id_re.fullmatch(track_id):
        raise HTTPException(400, "Некорректный идентификатор трека")

    # Скачанный трек играем с диска: работает без интернета и мгновенно (Range поддерживает FileResponse)
    local = library.audio_path(source, track_id)
    if local:
        if warm:
            return Response(status_code=204)
        mime = {".m4a": "audio/mp4", ".mp3": "audio/mpeg", ".webm": "audio/webm", ".ogg": "audio/ogg"}
        return FileResponse(local, media_type=mime.get(local.suffix, "audio/mp4"))

    # Прогрев (?warm=1): заранее получить ссылку и первую порцию. Ответ уходит сразу.
    if warm:
        return _start_warm(request.app.state.http, request.app.state.resolver, source, track_id)

    # --- Разбираем Range клиента. Поддерживаем один диапазон: bytes=a-b | bytes=a- | bytes=-n
    raw_range = (request.headers.get("range") or "").strip()
    client_wants_range = bool(RANGE_RE.fullmatch(raw_range))
    # Диапазон клиента режем до одной порции: ответ будет 206 с честным Content-Range,
    # а плеер сам запросит продолжение. Если Range не прислан (curl, прямая ссылка),
    # отдаём файл целиком обычным 200.
    upstream_range = _cap_range(raw_range) if client_wants_range else "bytes=0-"

    # Трек прогрет: начало лежит в памяти, отвечаем мгновенно, без обращения к источнику
    if client_wants_range:
        head = _head_get(f"{source}:{track_id}")
        if head:
            from_memory = _serve_from_head(head, raw_range)
            if from_memory is not None:
                return from_memory

    upstream = await _connect(request.app.state.http, request.app.state.resolver, source, track_id, upstream_range)
    status = upstream.status_code

    # Диапазон за пределами файла: пробрасываем 416 как есть
    if status == 416:
        content_range = upstream.headers.get("content-range", "bytes */0")
        await upstream.aclose()
        return Response(status_code=416, headers={"Content-Range": content_range, "Accept-Ranges": "bytes"})

    if status not in (200, 206):
        await upstream.aclose()
        raise _err(502, "upstream_status", f"Сервер аудио {cfg.label} ответил ошибкой HTTP {status}. Попробуйте позже.")

    headers = {
        "Content-Type": upstream.headers.get("content-type", "audio/mpeg" if source == "sc" else "audio/mp4"),
        "Accept-Ranges": "bytes",
        "Cache-Control": "private, max-age=3600",
    }
    if length := upstream.headers.get("content-length"):
        headers["Content-Length"] = length

    # 206 отдаём, только если клиент просил диапазон И источник его выдал.
    # Если диапазон не просили — это обычный 200 (Content-Length = размер всего файла).
    content_range = upstream.headers.get("content-range")
    if client_wants_range and status == 206 and content_range:
        headers["Content-Range"] = content_range
        code = 206
    else:
        code = 200

    return StreamingResponse(_body(upstream), status_code=code, headers=headers)