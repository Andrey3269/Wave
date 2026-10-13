"""
Общий слой музыкальных источников (S7, TARGET_ARCHITECTURE §5; находки M2, M3).

Что здесь есть:
  * ProviderError — единый контракт ошибки источника: тип (kind), источник, понятный текст на русском;
  * Capabilities — что источник фактически умеет в Wave (не обещания платформы);
  * CircuitBreaker — cooldown: после сбоя источник N секунд не опрашивается, запрос отклоняется сразу;
  * BoundedExecutor — СВОЙ ограниченный пул потоков на источник с пределом ожидающих задач;
  * Provider — базовый класс адаптера: `call()` выполняет блокирующий вызов библиотеки в пуле источника
    с таймаутом, переводит исключение в ProviderError и ведёт cooldown.

Почему свой пул. Библиотеки источников (ytmusicapi на requests, yt-dlp) блокирующие. Раньше их вызывали
через asyncio.to_thread — общий пул event loop: зависший SoundCloud занимал потоки, нужные YouTube Music и
другим задачам приложения (M2). Теперь у каждого источника свои потоки и жёсткий предел
«работает + ждёт очереди»; сверх него запрос отклоняется сразу понятной ошибкой, ничего не накапливается.

Чего Python не умеет: остановить уже работающий поток. asyncio.wait_for только перестаёт ждать результат,
а поток продолжает висеть в сетевом вызове (M2). Поэтому:
  * настоящий предел времени задаёт HTTP-клиент библиотеки (адаптеры: тайм-аут сессии requests для
    ytmusicapi, socket_timeout yt-dlp для SoundCloud) — по нему поток рано или поздно завершается сам;
  * таймаут `call()` — предел ожидания для пользователя; задача из очереди отменяется по-настоящему,
    а уже работающая помечается «брошенной» и до своего завершения продолжает занимать место в пуле.
    Так брошенные задачи не копятся без предела: их не больше, чем потоков пула.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass
from typing import Any, Callable, Literal

log = logging.getLogger("musicmeta.providers")

ErrorKind = Literal["unavailable", "timeout", "rate_limited", "not_found",
                    "restricted", "auth_required", "unsupported", "internal"]
ERROR_KINDS: tuple[str, ...] = ("unavailable", "timeout", "rate_limited", "not_found",
                                "restricted", "auth_required", "unsupported", "internal")

# Статус источника в ответах API (поле source_status):
#   ok       — источник ответил;
#   degraded — источник ответил не полностью (часть данных страницы не получена) или работает с перебоями
#              (запрос отклонён из-за переполнения пула, в пуле висят брошенные вызовы);
#   cooldown — после недавнего сбоя источник не опрашивается до конца паузы;
#   disabled — источник выключен настройкой (SEARCH_SOURCES, только поиск треков).
SourceStatus = Literal["ok", "degraded", "cooldown", "disabled"]


def user_message(kind: str, label: str) -> str:
    """Понятный текст ошибки источника. Без технических подробностей: они — только в журнале."""
    return {
        "unavailable": f"{label} сейчас недоступен. Проверьте подключение к интернету и попробуйте позже.",
        "timeout": f"{label} не ответил вовремя. Попробуйте позже.",
        "rate_limited": f"{label} временно ограничил число запросов. Попробуйте через несколько минут.",
        "not_found": f"{label}: страница не найдена или удалена.",
        "restricted": f"{label} ограничил доступ к этим данным (например, в вашем регионе).",
        "auth_required": f"{label} отклонил запрос без входа в аккаунт. Обычно помогает обновление приложения.",
        "unsupported": f"{label} не поддерживает этот запрос.",
        "internal": f"{label} временно недоступен. Попробуйте позже.",
    }[kind]


class ProviderError(Exception):
    """
    Ошибка музыкального источника.

    kind         — машинный тип (ERROR_KINDS), уходит в ответ API полем `code`;
    source       — "yt" | "sc", уходит полем `source`;
    user_message — текст для пользователя на русском, уходит полем `detail` (на него опирается фронтенд);
    cooldown     — запрос не отправлялся: источник на паузе после сбоя (статус `cooldown`);
    busy         — запрос не отправлялся: пул источника переполнен (статус `degraded`);
    retry_after  — через сколько секунд источник снова будут опрашивать (для cooldown).
    """

    def __init__(self, kind: str, source: str, user_message: str, *, cooldown: bool = False, busy: bool = False,
                 retry_after: float | None = None):
        if kind not in ERROR_KINDS:
            raise ValueError(f"неизвестный тип ошибки источника: {kind}")
        super().__init__(user_message)
        self.kind = kind
        self.source = source
        self.user_message = user_message
        self.cooldown = cooldown
        self.busy = busy
        self.retry_after = retry_after
        self.tripped = False              # эта ошибка перевела источник в cooldown (выставляет Provider)

    @property
    def code(self) -> str:
        return self.kind

    @property
    def status(self) -> str:
        """Статус источника для source_status: cooldown — источник на паузе, иначе degraded."""
        return "cooldown" if self.cooldown or self.tripped else "degraded"

    def __repr__(self) -> str:
        return f"ProviderError({self.kind!r}, {self.source!r})"


@dataclass(frozen=True)
class Capabilities:
    """
    Что источник ФАКТИЧЕСКИ умеет в текущем Wave. Это описание поведения приложения, а не разрешение
    платформы: например, download=True значит «Wave сохраняет поток, который играет плеер» (ADR-10,
    backend/download_policy.py), а не то, что условия источника это разрешают (TARGET_ARCHITECTURE §6).
    """
    search_tracks: bool
    search_artists: bool
    search_albums: bool
    artist_page: bool
    album_page: bool
    radio: bool
    stream: bool
    download: bool
    attribution_required: bool     # условия источника требуют указывать автора и источник
    mixing_allowed: bool           # Wave смешивает треки этого источника с другими в выдаче и очереди

    def as_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Cooldown
# ---------------------------------------------------------------------------
# Какие ошибки переводят источник в cooldown. Сеть, таймаут, лимит запросов, отказ в доступе к API —
# всегда: источник в целом сейчас не работает. Внутренняя ошибка и ограничение доступа — только для поиска
# (поиск — основная операция, её сбой означает сбой источника). Для страниц артиста, альбома и радио такие
# ошибки обычно относятся к одной странице (её не удалось разобрать, она закрыта в регионе) — источник
# из-за этого не выключаем. «Не найдено» и «не поддерживается» не выключают источник никогда.
TRIP_ALWAYS = frozenset({"unavailable", "timeout", "rate_limited", "auth_required"})
TRIP_ON_SEARCH = TRIP_ALWAYS | {"internal", "restricted"}
SEARCH_OPS = frozenset({"search_tracks", "search_cards"})


class CircuitBreaker:
    """Пауза после сбоя. cooldown <= 0 — пауза выключена (источник опрашивается всегда)."""

    def __init__(self, cooldown: float, clock: Callable[[], float] = time.monotonic):
        self.cooldown = cooldown
        self._clock = clock
        self.until = 0.0                    # до какого момента (clock) источник не опрашивается
        self.last_kind: str | None = None   # тип последней ошибки, открывшей паузу
        self._lock = threading.Lock()

    def remaining(self) -> float:
        return max(0.0, self.until - self._clock())

    def is_open(self) -> bool:
        return self.remaining() > 0

    def trip(self, kind: str) -> bool:
        """Открывает паузу. False — пауза выключена (cooldown <= 0)."""
        if self.cooldown <= 0:
            return False
        with self._lock:
            self.until = self._clock() + self.cooldown
            self.last_kind = kind
        return True

    def record_success(self) -> None:
        with self._lock:
            self.until = 0.0
            self.last_kind = None

    def reset(self) -> None:
        self.record_success()


# ---------------------------------------------------------------------------
# Ограниченный пул потоков источника
# ---------------------------------------------------------------------------
class ExecutorBusy(Exception):
    """Пул источника заполнен: работает и ждёт столько задач, сколько разрешено."""


class BoundedExecutor:
    """
    ThreadPoolExecutor с жёстким пределом задач «в работе + в очереди» (capacity = workers + max_pending).

    Задача считается занимающей место, пока её поток действительно не завершился, — даже если вызывающий
    уже перестал ждать (таймаут, клиент ушёл). Такие задачи считаются «брошенными» (abandoned).
    Пул создаётся при первой задаче; shutdown_pending() отменяет ожидающие задачи (при остановке backend),
    работающие доживают сами, следующий вызов создаёт новый пул.
    """

    def __init__(self, name: str, workers: int, max_pending: int):
        if workers < 1 or max_pending < 0:
            raise ValueError("workers >= 1, max_pending >= 0")
        self.name = name
        self.workers = workers
        self.max_pending = max_pending
        self._pool: ThreadPoolExecutor | None = None
        self._lock = threading.Lock()
        self._outstanding = 0              # отправлено и ещё не завершилось (очередь + работа + брошенные)
        self._abandoned: set[Future] = set()
        self.rejected = 0                  # сколько задач отклонено из-за переполнения (для диагностики)

    @property
    def capacity(self) -> int:
        return self.workers + self.max_pending

    def submit(self, fn: Callable, *args) -> Future:
        with self._lock:
            if self._outstanding >= self.capacity:
                self.rejected += 1
                raise ExecutorBusy(self.name)
            self._outstanding += 1
            if self._pool is None:
                self._pool = ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix=f"provider-{self.name}")
            pool = self._pool
        try:
            fut = pool.submit(fn, *args)
        except BaseException:
            with self._lock:
                self._outstanding -= 1
            raise
        fut.add_done_callback(self._done)
        return fut

    def _done(self, fut: Future) -> None:
        with self._lock:
            self._outstanding -= 1
            self._abandoned.discard(fut)

    def abandon(self, fut: Future) -> None:
        """Вызывающий больше не ждёт: задача из очереди отменяется, работающая доживает и считается брошенной."""
        if fut.cancel():
            return                           # ещё не начиналась: место освобождено (_done уже вызван)
        with self._lock:
            if not fut.done():
                self._abandoned.add(fut)

    def shutdown_pending(self) -> None:
        """Отменить задачи в очереди (остановка backend). Работающие потоки завершаются сами по таймауту HTTP."""
        with self._lock:
            pool, self._pool = self._pool, None
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)

    def stats(self) -> dict:
        with self._lock:
            out = self._outstanding
            return {"workers": self.workers, "max_pending": self.max_pending, "outstanding": out,
                    "running": min(out, self.workers), "queued": max(0, out - self.workers),
                    "abandoned": len(self._abandoned), "rejected": self.rejected}


# ---------------------------------------------------------------------------
# Базовый адаптер
# ---------------------------------------------------------------------------
class Provider:
    """
    Базовый класс адаптера источника. Подкласс задаёт id, label, capabilities, параметры пула и cooldown
    и переопределяет classify() — перевод исключений своей библиотеки в ProviderError.
    """

    id: str = ""
    label: str = ""
    capabilities: Capabilities
    workers: int = 2
    max_pending: int = 4
    cooldown: float = 0.0

    def __init__(self):
        self.search_enabled = True                         # SEARCH_SOURCES (main.py) — только поиск треков
        self.breaker = CircuitBreaker(self.cooldown)
        self.executor = BoundedExecutor(self.id, self.workers, self.max_pending)

    # ---- ошибки ----
    def error(self, kind: str, message: str | None = None, **kw) -> ProviderError:
        return ProviderError(kind, self.id, message or user_message(kind, self.label), **kw)

    def classify(self, exc: BaseException) -> ProviderError:
        """Исключение библиотеки -> ProviderError. Базовый вариант: всё неизвестное — internal."""
        if isinstance(exc, ProviderError):
            return exc
        if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
            return self.error("timeout")
        return self.error("internal")

    def cooldown_error(self) -> ProviderError:
        left = max(1, round(self.breaker.remaining()))
        return self.error("unavailable", f"{self.label} временно недоступен после сбоя. "
                                         f"Повторная попытка — примерно через {left} с.",
                          cooldown=True, retry_after=float(left))

    # ---- выполнение ----
    def check_available(self) -> None:
        """Источник на паузе после сбоя — ProviderError сразу, без запроса к источнику."""
        if self.breaker.is_open():
            raise self.cooldown_error()

    async def call(self, op: str, fn: Callable[..., Any], *args, timeout: float, optional: bool = False) -> Any:
        """
        Блокирующий вызов библиотеки `fn(*args)` в пуле этого источника, не дольше `timeout` секунд ожидания.

        Успех сбрасывает cooldown. Сбой переводится в ProviderError; подходящие сбои (TRIP_*) открывают
        cooldown. В cooldown запрос не отправляется. При переполнении пула — ProviderError(busy) сразу.
        Отмена ожидающего (клиент ушёл) отменяет задачу в очереди; работающая доживает как брошенная.

        optional=True — необязательная часть страницы (полный список релизов, альбомы артиста): страница
        показывается и без неё, поэтому её сбой паузу не открывает — иначе пауза оборвала бы остальные
        запросы этой же страницы.
        """
        self.check_available()
        try:
            fut = self.executor.submit(fn, *args)
        except ExecutorBusy:
            log.warning("provider.busy source=%s op=%s %s", self.id, op, self.executor.stats())
            raise self.error("unavailable", f"{self.label} не успевает отвечать: слишком много запросов ждут "
                                            "ответа. Попробуйте чуть позже.", busy=True) from None
        t0 = time.monotonic()
        try:
            result = await asyncio.wait_for(asyncio.wrap_future(fut), timeout)
        except asyncio.CancelledError:
            self.executor.abandon(fut)
            raise
        except Exception as exc:
            if fut.cancelled() or not fut.done():
                # Мы перестали ждать (таймаут wait_for): задача из очереди отменена, работающая брошена
                self.executor.abandon(fut)
                err, cause = self.error("timeout"), None
            else:
                err, cause = self._failure(op, exc), exc        # исключение самой библиотеки
            self._record(op, err, exc, t0, optional)
            raise err from cause
        self.breaker.record_success()
        return result

    def _failure(self, op: str, exc: BaseException) -> ProviderError:
        try:
            err = self.classify(exc)
        except Exception:                                  # классификатор не должен ронять запрос
            log.exception("provider.classify source=%s op=%s", self.id, op)
            err = self.error("internal")
        if err.source != self.id:                          # ошибка чужого источника не должна сюда попасть
            err = self.error(err.kind)
        return err

    def _record(self, op: str, err: ProviderError, exc: BaseException, t0: float, optional: bool = False) -> None:
        kinds = TRIP_ON_SEARCH if op in SEARCH_OPS else TRIP_ALWAYS
        tripped = not optional and err.kind in kinds and self.breaker.trip(err.kind)
        err.tripped = tripped
        log.warning("provider.fail source=%s op=%s kind=%s %.1f с%s: %r", self.id, op, err.kind,
                    time.monotonic() - t0, f" -> cooldown {self.breaker.cooldown:.0f} с" if tripped else "", exc)

    # ---- состояние ----
    def health(self) -> str:
        """Состояние источника сейчас: cooldown | degraded (в пуле висят брошенные вызовы) | ok."""
        if self.breaker.is_open():
            return "cooldown"
        if self.executor.stats()["abandoned"]:
            return "degraded"
        return "ok"

    def snapshot(self) -> dict:
        return {"label": self.label, "status": self.health(), "search_enabled": self.search_enabled,
                "cooldown_seconds": self.breaker.cooldown, "cooldown_remaining": round(self.breaker.remaining(), 1),
                "last_error": self.breaker.last_kind if self.breaker.is_open() else None,
                "pool": self.executor.stats(), "capabilities": self.capabilities.as_dict()}

    def shutdown_pending(self) -> None:
        self.executor.shutdown_pending()

    def reset(self) -> None:
        """Чистое состояние (тесты): пауза снята, новый пул; потоки старого пула доживают сами."""
        self.executor.shutdown_pending()
        self.breaker = CircuitBreaker(self.cooldown)
        self.executor = BoundedExecutor(self.id, self.workers, self.max_pending)
