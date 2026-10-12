"""
Последовательные запуски приложения в одном процессе. Так работает сторож лаунчера
(backend/launcher.py): при сбое он поднимает новый uvicorn с новым event loop, а модули backend
остаются загруженными. Каждый harness.run() — отдельный asyncio.run(), то есть новый event loop.
"""
from __future__ import annotations

import asyncio

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


@pytest.mark.xfail(strict=True, reason="M1: модульный asyncio.Semaphore резолвера привязан к первому event loop; "
                                       "после перезапуска uvicorn конкурентные резолвы падают с RuntimeError")
def test_restart_after_contention_keeps_resolving(h):
    h.resolver.delay = 0.05
    assert h.run(_burst(IDS)) == [206] * len(IDS)
    stream._cache.clear()                       # новые ссылки: резолвер снова нужен
    assert h.run(_burst(IDS)) == [206] * len(IDS)


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
