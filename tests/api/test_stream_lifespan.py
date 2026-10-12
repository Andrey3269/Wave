"""
Последовательные запуски приложения в одном процессе. Так работает сторож лаунчера
(backend/launcher.py): при сбое он поднимает новый uvicorn с новым event loop, а модули backend
остаются загруженными. Каждый harness.run() — отдельный asyncio.run(), то есть новый event loop.

С S2b асинхронное состояние резолвера (семафор, задачи резолва и прогрева) создаёт и закрывает lifespan:
app.state.resolver = stream.ResolverState(). Ссылки (_cache) и «головы» (_heads) — данные уровня процесса.
"""
from __future__ import annotations

import asyncio
import threading

import pytest

import stream
from wavetest.fakes import audio_bytes

IDS = [f"CCCCCCCCCC{i}" for i in range(stream.MAX_PARALLEL_RESOLVES + 2)]   # больше, чем разрешений семафора
DATA = audio_bytes(300)


@pytest.fixture
def h(harness):
    for i in IDS:
        harness.net.add_file(f"/yt/{i}", DATA)
    return harness


def _burst(ids):
    async def go(c):
        rs = await asyncio.gather(*(c.get(f"/api/stream/yt/{i}", headers={"Range": "bytes=0-"}) for i in ids))
        return [r.status_code for r in rs]
    return go


def _sequential(ids):
    async def go(c):
        return [(await c.get(f"/api/stream/yt/{i}", headers={"Range": "bytes=0-"})).status_code for i in ids]
    return go


def test_restart_without_contention_works(h):
    """Перезапуск без одновременных резолвов работает: семафор ещё не привязан к циклу."""
    assert h.run(_sequential(IDS[:2])) == [206, 206]
    stream._cache.clear()
    assert h.run(_sequential(IDS[2:4])) == [206, 206]
    assert h.lifespans == 2


def test_first_run_with_contention_works(h):
    h.resolver.delay = 0.05
    assert h.run(_burst(IDS)) == [206] * len(IDS)


def test_restart_after_contention_keeps_resolving(h):
    """M1: после перезапуска (новый event loop) конкурентные резолвы работают. До S2b: [206]*4 + [500, 500]."""
    h.resolver.delay = 0.05
    assert h.run(_burst(IDS)) == [206] * len(IDS)
    stream._cache.clear()                       # новые ссылки: резолвер снова нужен
    assert h.run(_burst(IDS)) == [206] * len(IDS)


def test_many_restarts_with_contention(h):
    """
    Несколько запусков подряд, в каждом — больше одновременных резолвов, чем разрешений семафора.
    У каждого запуска своё состояние резолвера; после остановки оно закрыто и пусто.
    """
    h.resolver.delay = 0.03
    states = []

    async def burst_and_grab(c):
        states.append(h.app.state.resolver)
        rs = await asyncio.gather(*(c.get(f"/api/stream/yt/{i}", headers={"Range": "bytes=0-"}) for i in IDS))
        return [r.status_code for r in rs], states[-1].loop is asyncio.get_running_loop()

    for run in range(4):
        stream._cache.clear()
        codes, same_loop = h.run(burst_and_grab)
        assert codes == [206] * len(IDS), f"запуск {run + 1}: {codes}"
        assert same_loop
    assert len({id(s) for s in states}) == 4 and len({id(s.sem) for s in states}) == 4
    assert all(s.closed and not s.inflight and not s.background and not s.prefetching for s in states)
    assert h.resolver.total() == 4 * len(IDS)
    assert h.lifespans == 4


def test_shutdown_cancels_pending_work_and_next_run_is_clean(h):
    """
    Остановка посреди прогрева: задачи прогрева и резолва этого запуска отменяются, наборы очищаются.
    Следующий запуск не видит «вечного» прогрева старого цикла (раньше ключ оставался в _prefetching).
    """
    key = f"yt:{IDS[0]}"
    gate = threading.Event()
    h.resolver.gate = gate
    seen = {}

    async def start_warm_and_stop(c):
        rs = h.app.state.resolver
        r = await c.get(f"/api/stream/yt/{IDS[0]}", params={"warm": 1})
        while key not in rs.inflight:                       # резолв реально начался и ждёт
            await asyncio.sleep(0.01)
        seen.update(code=r.status_code, rs=rs, pending=len(rs.background))
        return None                                         # выходим: lifespan останавливается

    try:
        h.run(start_warm_and_stop)
    finally:
        gate.set()                                          # отпускаем поток yt-dlp старого запуска
        h.resolver.gate = None
    rs = seen["rs"]
    assert seen["code"] == 202 and seen["pending"] == 1
    assert rs.closed and not rs.inflight and not rs.background and not rs.prefetching
    assert key not in stream._heads

    async def warm_again(c):
        r = await c.get(f"/api/stream/yt/{IDS[0]}", params={"warm": 1})
        await asyncio.gather(*list(h.app.state.resolver.background))
        again = await c.get(f"/api/stream/yt/{IDS[0]}", params={"warm": 1})
        return r.status_code, again.status_code

    assert h.run(warm_again) == (202, 204)                  # прогрев заново и готов
    assert key in stream._heads


def test_state_refuses_foreign_loop_and_closed_use():
    """Ошибка использования видна сразу и понятно, а не как зависание или чужой RuntimeError asyncio."""
    async def make():
        return stream.ResolverState()

    rs = asyncio.run(make())

    async def use():
        rs.check()

    with pytest.raises(RuntimeError, match="другом event loop"):
        asyncio.run(use())

    async def close_and_use():
        own = stream.ResolverState()
        await own.aclose()
        own.check()

    with pytest.raises(RuntimeError, match="закрыто"):
        asyncio.run(close_and_use())


def test_restart_reuses_resolved_links(h):
    """Кэш резолвера переживает перезапуск: живая ссылка не резолвится заново."""
    assert h.run(_sequential(IDS[:1])) == [206]
    assert h.run(_sequential(IDS[:1])) == [206]
    assert h.resolver.calls[f"yt:{IDS[0]}"] == 1


def test_restart_closes_old_http_client_and_creates_new(h):
    clients = []

    async def grab(c):
        clients.append(h.app.state.http)
        return (await c.get(f"/api/stream/yt/{IDS[0]}", headers={"Range": "bytes=0-"})).status_code

    assert h.run(grab) == 206
    stream._cache.clear()
    assert h.run(grab) == 206
    assert clients[0] is not clients[1]
    assert clients[0].is_closed and clients[1].is_closed      # lifespan закрывает клиент при остановке


def test_search_cache_cleared_on_shutdown(h):
    import main
    h.ytm.songs["*"] = []

    async def search(c):
        return (await c.get("/api/search", params={"q": "x", "sources": "yt"})).status_code

    assert h.run(search) == 200
    assert not main._cache                     # lifespan очищает кэш поиска при остановке
