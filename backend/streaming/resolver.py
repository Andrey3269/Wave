"""
Резолвер: yt-dlp -> прямая ссылка на аудиопоток, с кэшем и защитой от дублей.

Ссылки googlevideo привязаны к IP того, кто их получил, поэтому и резолвим, и скачиваем с ОДНОГО
компьютера и по одному IP-семейству (принудительно IPv4, см. proxy.make_http_client).
"""
from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable
from urllib.parse import parse_qs, urlparse

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

import jsruntime
from streaming import config, errors

log = logging.getLogger("musicmeta.stream")

# Дочерние процессы yt-dlp (JS-runtime Deno): без окна консоли, с таймаутом и завершением при выходе
jsruntime.install_process_guard()

VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")      # videoId YouTube
SC_ID_RE = re.compile(r"^\d{1,15}$")                  # числовой id трека SoundCloud

DEFAULT_TTL = 30 * 60             # TTL ссылки, если в ней нет параметра expire
EXPIRY_MARGIN = 120               # обновляем ссылку за 2 минуты до истечения
MAX_PARALLEL_RESOLVES = 4         # одновременных запусков yt-dlp
HLS_PARALLEL = 6                  # одновременных соединений HLS-прокси с источником

# Из заголовков yt-dlp оставляем только безопасные. Accept-Encoding задаём сами:
# сжатие вместе с Range ломает подсчёт байтов.
FORWARD_HEADERS = {"user-agent", "accept", "accept-language", "referer", "origin"}


@dataclass(frozen=True)
class Source:
    """
    Описание источника: как строить URL для yt-dlp и какой формат брать.

    hls=False — источник играет только прогрессивный файл (выбор формата делает yt-dlp по `fmt`, HLS/DASH
    YouTube отключены в ydl_opts); hls=True — формат выбирает choose_format: прогрессивный, если есть,
    иначе HLS (S8, N4). Фронтенд держит тот же признак в PROGRESSIVE_ONLY (frontend/js/player/controller.js).
    """
    label: str
    page_url: Callable[[str], str]
    id_re: re.Pattern
    fmt: str
    hls: bool = False


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
        # Любой формат, лишь бы yt-dlp не падал с «Requested format is not available», когда SoundCloud
        # не предложил прогрессивный mp3 (N4); сам поток выбирает choose_format по полному списку форматов.
        fmt="bestaudio/best",
        hls=True,
    ),
}

PROGRESSIVE = "progressive"
HLS = "hls"


@dataclass(slots=True)
class Resolved:
    url: str
    headers: dict[str, str]
    expires_at: float             # unix-время
    # Источник отдал только фрагмент (SoundCloud Go+: 30-секундный превью, format_id yt-dlp с «preview»).
    # Загрузка такой трек не сохраняет (download_policy, S5); воспроизведение этим полем не управляется.
    preview: bool = False
    # S8: тип потока. progressive — обычный файл (Range-прокси, <audio src>), hls — плейлист m3u8 (только
    # HLS-прокси и hls.js; через Range-прокси не отдаётся никогда). codec — mp3 | aac | None (неизвестен).
    kind: str = PROGRESSIVE
    codec: str | None = None

    def alive(self) -> bool:
        return time.time() < self.expires_at


class HlsEncrypted(Exception):
    """Источник предложил только зашифрованные (AES/DRM) варианты."""


class NoPlayableFormat(Exception):
    """Ни один формат источника Wave воспроизвести не может (например, только Opus-HLS)."""


# Ссылки — просто данные: они не привязаны к event loop и переживают перезапуск uvicorn сторожем
# лаунчера, поэтому живут на уровне модуля.
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
        # S8: одновременных соединений HLS-прокси с источником (плейлисты и сегменты; ждём только заголовки)
        self.hls_sem = asyncio.Semaphore(HLS_PARALLEL)
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
    """
    У googlevideo в ссылке есть ?expire=<unix>, у плейлистов SoundCloud — ?expires=<unix>. Если нет — берём
    TTL по умолчанию.
    """
    q = parse_qs(urlparse(url).query)
    for name in ("expire", "expires"):
        try:
            return int(q.get(name, [""])[0]) - EXPIRY_MARGIN
        except ValueError:
            continue
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
        # Манифесты DASH/HLS YouTube нам не нужны (через Range проксируется только обычный файл),
        # а их загрузка добавляет лишние запросы к YouTube.
        "extractor_args": {"youtube": {"skip": ["dash", "hls", "translated_subs"]}},
        # Кэш yt-dlp на диске включён: в нём лежит client_id SoundCloud и кэш
        # расшифровки YouTube, без него каждый резолв был бы заметно дольше.
    }
    if config.YTDLP_COOKIES:
        opts["cookiefile"] = config.YTDLP_COOKIES
    if config.YTDLP_PROXY:
        opts["proxy"] = config.YTDLP_PROXY
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


# ---------------------------------------------------------------------------
# Выбор формата (S8, N4)
# ---------------------------------------------------------------------------
_HLS_PROTOCOLS = ("m3u8", "m3u8_native")
_PLAYABLE_HLS_CODECS = ("mp3", "aac", None)   # то, что демультиплексирует hls.js (MP3, AAC в fMP4/ADTS/TS)


def _codec(f: dict) -> str | None:
    acodec = str(f.get("acodec") or "").lower()
    ext = str(f.get("ext") or "").lower()
    if acodec.startswith("mp3") or (acodec in ("", "none") and ext == "mp3"):
        return "mp3"
    if acodec.startswith(("mp4a", "aac")) or (acodec in ("", "none") and ext in ("m4a", "aac", "mp4")):
        return "aac"
    if acodec in ("", "none"):
        return None
    return acodec.split(".", 1)[0]               # opus, vorbis, flac … — для HLS не подходят


def classify(f: dict) -> tuple[str, str | None] | None:
    """
    (тип, кодек) формата yt-dlp: ("progressive" | "hls" | "encrypted", кодек) или None — не аудиопоток.
    Тип определяется по протоколу yt-dlp И адресу: .m3u8 по http(s) — тоже HLS, а не файл.
    """
    url = f.get("url")
    if not url:
        return None
    proto = str(f.get("protocol") or "").lower()
    fid = str(f.get("format_id") or "").lower()
    if f.get("has_drm") or fid.startswith("hls-aes") or "encrypted" in fid or proto.startswith(("ctr-", "cbc-")):
        return ("encrypted", None)
    path = urlparse(url).path.lower()
    if proto in _HLS_PROTOCOLS or path.endswith(".m3u8"):
        return (HLS, _codec(f))
    if proto in ("http", "https"):
        return (PROGRESSIVE, _codec(f))
    return None                                  # DASH, f4m, ism … — Wave их не проксирует


def choose_format(info: dict) -> tuple[dict, str, str | None]:
    """
    Формат для воспроизведения и загрузки (один и тот же — правило S5): прогрессивный, если есть; иначе
    HLS с mp3, затем AAC, затем с неизвестным кодеком. Превью (30-секундные фрагменты) — только если ничего
    другого нет, как и раньше у yt-dlp (preference -10). Внутри группы — больший битрейт.
    HlsEncrypted / NoPlayableFormat — если подходящего формата нет.
    """
    formats = info.get("formats") or info.get("requested_formats") or [info]
    best: tuple | None = None
    encrypted = False
    for f in formats:
        kind_codec = classify(f)
        if kind_codec is None:
            continue
        kind, codec = kind_codec
        if kind == "encrypted":
            encrypted = True
            continue
        if kind == HLS and codec not in _PLAYABLE_HLS_CODECS:
            continue
        preview = "preview" in str(f.get("format_id") or "").lower()
        rank = (preview, 0 if kind == PROGRESSIVE else 1, {"mp3": 0, "aac": 1}.get(codec, 2) if kind == HLS else 0,
                -(f.get("abr") or f.get("tbr") or 0))
        if best is None or rank < best[0]:
            best = (rank, f, kind, codec)
    if best is None:
        if encrypted:
            raise HlsEncrypted("только зашифрованные варианты")
        raise NoPlayableFormat(", ".join(sorted({str(f.get("format_id")) for f in formats})) or "нет форматов")
    return best[1], best[2], best[3]


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

    if cfg.hls:
        fmt, kind, codec = choose_format(info)
    else:
        # Формат выбрал yt-dlp (fmt). При выборе одного формата url лежит на верхнем уровне; на случай
        # слияния берём первый. Источник без HLS играет только прогрессивный файл.
        fmt = (info.get("requested_formats") or [info])[0]
        kind_codec = classify(fmt) if fmt.get("url") else None
        kind, codec = kind_codec if kind_codec else (PROGRESSIVE, None)
        if kind != PROGRESSIVE:
            raise errors.StreamUnavailable(f"{cfg.label}: не прогрессивный поток ({fmt.get('protocol')})")
    url = fmt.get("url")
    if not url:
        raise RuntimeError("yt-dlp не вернул прямой URL")

    raw_headers = fmt.get("http_headers") or info.get("http_headers") or {}
    headers = {k: v for k, v in raw_headers.items() if k.lower() in FORWARD_HEADERS}
    return Resolved(url=url, headers=headers, expires_at=_url_expiry(url),
                    preview="preview" in str(fmt.get("format_id") or "").lower(), kind=kind, codec=codec)


async def _resolve_uncached(rs: ResolverState, source: str, track_id: str) -> Resolved:
    key = f"{source}:{track_id}"
    async with rs.sem:
        t0 = time.monotonic()
        try:
            res = await asyncio.get_running_loop().run_in_executor(
                _resolve_pool, _extract_sync, source, track_id
            )
            log.info("Резолв %s занял %.1f с (%s%s)", key, time.monotonic() - t0, res.kind,
                     f", {res.codec}" if res.codec else "")
        except errors.StreamUnavailable as exc:
            log.warning("Нет прямого потока %s: %s", key, exc)
            raise errors._err(404, "no_direct_stream", "Для этого трека нет прямого аудиопотока") from exc
        except HlsEncrypted as exc:
            log.warning("Только зашифрованный поток %s", key)
            raise errors._err(403, "hls_encrypted", f"{SOURCES[source].label} передаёт этот трек только в "
                                                    "зашифрованном виде — Wave такие потоки не воспроизводит.") from exc
        except NoPlayableFormat as exc:
            log.warning("Нет воспроизводимого формата %s: %s", key, exc)
            raise errors._err(404, "no_playable_format", f"{SOURCES[source].label} не предложил формат, который "
                                                         "Wave умеет воспроизводить.") from exc
        except DownloadError as exc:
            err = errors._runtime_error(source, errors._resolve_error(source, exc),
                                        getattr(exc, "wave_js_issue", None))
            log.warning("yt-dlp: %s -> %s [%s]", key, exc, err.headers[errors.ERROR_HEADER])
            raise err from exc
        except Exception as exc:
            log.exception("Сбой резолвера для %s", key)
            raise errors._runtime_error(source, errors._err(502, "internal", "Внутренняя ошибка резолвера"),
                                        None) from exc

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
