"""
Базовый слой источников (backend/providers/base.py, S7): контракт ProviderError, cooldown,
ограниченный пул на источник, таймауты, отмена, брошенные задачи (находки M2, M3).
"""
from __future__ import annotations

import asyncio
import threading
import time

import pytest

from providers.base import (
    ERROR_KINDS, BoundedExecutor, Capabilities, CircuitBreaker, ExecutorBusy, Provider, ProviderError, user_message,
)

CAPS = Capabilities(search_tracks=True, search_artists=True, search_albums=True, artist_page=True, album_page=True,
                    radio=True, stream=True, download=True, attribution_required=False, mixing_allowed=True)


class LibError(Exception):
    """Ошибка «библиотеки» источника."""


class Dummy(Provider):
    id = "yt"
    label = "Тестовый источник"
    capabilities = CAPS
    workers = 2
    max_pending = 1
    cooldown = 30.0

    def classify(self, exc):
        if isinstance(exc, LibError):
            return self.error("rate_limited" if "429" in str(exc) else "not_found")
        return super().classify(exc)


@pytest.fixture
def prov():
    p = Dummy()
    yield p
    p.reset()


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# ProviderError
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("kind", ERROR_KINDS)
def test_every_kind_has_a_russian_message_with_the_source_name(kind):
    msg = user_message(kind, "SoundCloud")
    assert "SoundCloud" in msg and msg.endswith(".")
    err = ProviderError(kind, "sc", msg)
    assert err.code == kind and err.source == "sc" and err.user_message == msg and str(err) == msg


def test_unknown_kind_is_rejected():
    with pytest.raises(ValueError):
        ProviderError("broken", "yt", "x")


def test_error_status():
    assert ProviderError("timeout", "yt", "x").status == "degraded"
    assert ProviderError("unavailable", "yt", "x", cooldown=True).status == "cooldown"
    tripped = ProviderError("timeout", "yt", "x")
    tripped.tripped = True
    assert tripped.status == "cooldown"


# ---------------------------------------------------------------------------
# Выполнение и классификация
# ---------------------------------------------------------------------------
def test_call_runs_in_the_providers_own_threads(prov):
    out = run(prov.call("search_tracks", lambda x: (x * 2, threading.current_thread().name), 21, timeout=2))
    assert out[0] == 42 and out[1].startswith("provider-yt")


def test_library_error_is_classified(prov):
    def boom():
        raise LibError("HTTP 404")

    with pytest.raises(ProviderError) as e:
        run(prov.call("artist_page", boom, timeout=2))
    assert e.value.kind == "not_found" and e.value.source == "yt" and isinstance(e.value.__cause__, LibError)
    assert not prov.breaker.is_open()                    # «не найдено» источник не выключает


def test_unknown_error_is_internal(prov):
    def boom():
        raise KeyError("parser")

    with pytest.raises(ProviderError) as e:
        run(prov.call("album_page", boom, timeout=2))
    assert e.value.kind == "internal" and e.value.user_message == user_message("internal", prov.label)


def test_classifier_crash_becomes_internal(prov, monkeypatch):
    monkeypatch.setattr(prov, "classify", lambda exc: 1 / 0)

    def boom():
        raise LibError("x")

    with pytest.raises(ProviderError) as e:
        run(prov.call("search_tracks", boom, timeout=2))
    assert e.value.kind == "internal"


# ---------------------------------------------------------------------------
# Cooldown
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("op, exc, trips", [
    ("search_tracks", LibError("429"), True),            # лимит запросов — всегда
    ("artist_page", LibError("429"), True),
    ("search_tracks", RuntimeError("?"), True),          # внутренняя ошибка поиска — сбой источника
    ("search_cards", RuntimeError("?"), True),
    ("artist_page", RuntimeError("?"), False),           # ...а одной страницы — нет
    ("radio", RuntimeError("?"), False),
    ("search_tracks", LibError("404"), False),           # «не найдено» — никогда
])
def test_which_failures_open_cooldown(prov, op, exc, trips):
    def boom():
        raise exc

    with pytest.raises(ProviderError) as e:
        run(prov.call(op, boom, timeout=2))
    assert prov.breaker.is_open() is trips and e.value.tripped is trips
    assert prov.health() == ("cooldown" if trips else "ok")


def test_cooldown_rejects_without_calling_the_source(prov):
    calls = []
    prov.breaker.trip("timeout")
    with pytest.raises(ProviderError) as e:
        run(prov.call("search_tracks", lambda: calls.append(1), timeout=2))
    assert not calls
    assert e.value.cooldown and e.value.status == "cooldown" and e.value.kind == "unavailable"
    assert 1 <= e.value.retry_after <= 30 and "через" in e.value.user_message


def test_success_after_cooldown_resets_it(prov):
    prov.breaker.trip("timeout")
    prov.breaker.until = time.monotonic() - 0.01           # пауза истекла
    assert run(prov.call("search_tracks", lambda: "ok", timeout=2)) == "ok"
    assert not prov.breaker.is_open() and prov.breaker.last_kind is None


def test_timeout_opens_cooldown_even_for_pages(prov):
    gate = threading.Event()
    try:
        with pytest.raises(ProviderError) as e:
            run(prov.call("artist_page", gate.wait, 5, timeout=0.05))
        assert e.value.kind == "timeout" and e.value.tripped and prov.breaker.is_open()
    finally:
        gate.set()


def test_zero_cooldown_never_opens():
    b = CircuitBreaker(0)
    assert b.trip("timeout") is False and not b.is_open()


# ---------------------------------------------------------------------------
# Пул: пределы, таймауты, отмена, брошенные задачи
# ---------------------------------------------------------------------------
def wait_until(cond, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


def test_executor_capacity_is_hard():
    ex = BoundedExecutor("t", workers=2, max_pending=3)
    gate = threading.Event()
    futs = [ex.submit(gate.wait, 5) for _ in range(5)]
    with pytest.raises(ExecutorBusy):
        ex.submit(gate.wait, 5)
    st = ex.stats()
    assert st["outstanding"] == 5 and st["running"] == 2 and st["queued"] == 3 and st["rejected"] == 1
    gate.set()
    for f in futs:
        f.result(3)
    assert wait_until(lambda: ex.stats()["outstanding"] == 0)
    ex.shutdown_pending()


def test_timeout_does_not_wait_for_a_hung_thread(prov):
    gate = threading.Event()
    try:
        t0 = time.monotonic()
        with pytest.raises(ProviderError) as e:
            run(prov.call("search_tracks", gate.wait, 30, timeout=0.1))
        assert time.monotonic() - t0 < 1.0 and e.value.kind == "timeout"
        st = prov.executor.stats()
        assert st["outstanding"] == 1 and st["abandoned"] == 1       # поток жив и занимает место
        assert prov.health() == "cooldown"
    finally:
        gate.set()
    assert wait_until(lambda: prov.executor.stats()["outstanding"] == 0 and prov.executor.stats()["abandoned"] == 0)


def test_repeated_timeouts_do_not_pile_up_abandoned_work():
    """Без cooldown (худший случай): 40 таймаутов подряд — потоков и задач не больше, чем вмещает пул."""
    class NoPause(Dummy):
        cooldown = 0.0

    p = NoPause()
    gate = threading.Event()
    started = []

    def hang():
        started.append(threading.current_thread().name)
        gate.wait(10)

    async def go():
        kinds = []
        for _ in range(40):
            try:
                await p.call("search_tracks", hang, timeout=0.02)
            except ProviderError as exc:
                kinds.append("busy" if exc.busy else exc.kind)
            st = p.executor.stats()
            assert st["outstanding"] <= p.executor.capacity and st["running"] <= p.workers
        return kinds

    try:
        kinds = run(go())
        st = p.executor.stats()
        alive = [t for t in threading.enumerate() if t.name.startswith("provider-yt")]
        assert st["outstanding"] <= p.executor.capacity and st["abandoned"] == p.workers
        assert len(alive) <= p.workers
        # Запустились только первые `workers` вызовов; остальные ждали в очереди и при таймауте отменены
        # до старта — висящих потоков не прибавилось
        assert len(started) == p.workers
        assert len(kinds) == 40 and set(kinds) <= {"timeout", "busy"}
    finally:
        gate.set()
    assert wait_until(lambda: p.executor.stats()["outstanding"] == 0 and p.executor.stats()["abandoned"] == 0)
    p.reset()


def test_busy_pool_rejects_immediately_and_does_not_open_cooldown(prov):
    gate = threading.Event()
    try:
        futs = [prov.executor.submit(gate.wait, 5) for _ in range(prov.executor.capacity)]
        calls = []
        t0 = time.monotonic()
        with pytest.raises(ProviderError) as e:
            run(prov.call("search_tracks", lambda: calls.append(1), timeout=5))
        assert time.monotonic() - t0 < 0.5 and not calls
        assert e.value.busy and e.value.status == "degraded" and not prov.breaker.is_open()
    finally:
        gate.set()
    for f in futs:
        f.result(3)


def test_cancelled_request_drops_queued_work(prov):
    """Клиент ушёл, пока задача ждала очереди: функция так и не запускается, место освобождается."""
    gate = threading.Event()
    ran = []

    async def go():
        blockers = [prov.executor.submit(gate.wait, 5) for _ in range(prov.workers)]
        task = asyncio.create_task(prov.call("search_tracks", lambda: ran.append(1), timeout=5))
        await asyncio.sleep(0.05)
        assert prov.executor.stats()["queued"] == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return blockers

    try:
        blockers = run(go())
        assert prov.executor.stats()["outstanding"] == prov.workers   # отменённая задача место не держит
    finally:
        gate.set()
    for f in blockers:
        f.result(3)
    time.sleep(0.05)
    assert not ran and not prov.breaker.is_open()


def test_hung_provider_does_not_delay_another_provider():
    class Other(Dummy):
        id = "sc"

    hung, other = Dummy(), Other()
    gate = threading.Event()

    async def go():
        stuck = asyncio.create_task(hung.call("search_tracks", gate.wait, 30, timeout=30))
        await asyncio.sleep(0.05)
        t0 = time.monotonic()
        out = await other.call("search_tracks", lambda: "быстро", timeout=5)
        took = time.monotonic() - t0
        gate.set()
        await stuck
        return out, took

    try:
        out, took = run(go())
        assert out == "быстро" and took < 0.5
    finally:
        gate.set()
        hung.reset()
        other.reset()


def test_shutdown_cancels_queued_work(prov):
    gate = threading.Event()
    running = [prov.executor.submit(gate.wait, 5) for _ in range(prov.workers)]
    queued = prov.executor.submit(gate.wait, 5)
    prov.shutdown_pending()
    assert queued.cancelled()
    gate.set()
    for f in running:
        f.result(3)
    assert wait_until(lambda: prov.executor.stats()["outstanding"] == 0)
    assert run(prov.call("search_tracks", lambda: "новый пул", timeout=2)) == "новый пул"


def test_snapshot_has_no_secrets_and_reports_state(prov):
    prov.breaker.trip("timeout")
    s = prov.snapshot()
    assert s["status"] == "cooldown" and s["last_error"] == "timeout" and s["cooldown_remaining"] > 0
    assert set(s["pool"]) >= {"workers", "max_pending", "outstanding", "abandoned"}
    assert s["capabilities"]["search_tracks"] is True
