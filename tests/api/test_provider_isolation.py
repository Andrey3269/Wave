"""
Изоляция источников на уровне API (S7, находка M2; критерий приёмки 1 в ROADMAP S7).

Источник «зависает» на 30 секунд (вызов библиотеки ждёт события, тест отпускает его в конце — сами тесты
не ждут 30 с). Пока он висит, второй источник, каталог, рекомендации и SQLite-эндпоинты отвечают сразу,
а число задач зависшего источника не превышает предел его пула.
"""
from __future__ import annotations

import asyncio
import threading
import time

import pytest

import catalog
import main
import providers
from wavetest.fakes import load_fixture, track_payload

HANG = 30.0                         # сколько «висит» источник (столько же готов ждать запрос к нему)
FAST = 0.75                         # предел для ответов, которые не должны ждать зависший источник


def yt_items(n: int) -> list[dict]:
    return [{"videoId": f"YYYYYYYYY{i:02d}", "title": f"yt {i}", "artists": [{"name": "YT Artist", "id": "UCy"}]}
            for i in range(n)]


def sc_items(n: int) -> list[dict]:
    return [{"kind": "track", "id": 2000000000 + i, "title": f"sc {i}", "user": {"id": 1, "username": "sc user"}}
            for i in range(n)]


@pytest.fixture
def world(harness, monkeypatch):
    harness.ytm.songs["*"] = yt_items(5)
    harness.ytm.search_results[("artists", "*")] = load_fixture("ytmusic/search_artists.json")
    harness.sc.tracks["*"] = sc_items(5)
    monkeypatch.setattr(main, "PROVIDER_TIMEOUT", HANG)     # запрос к зависшему источнику ждёт всё время зависания
    return harness


async def timed(coro):
    t0 = time.monotonic()
    r = await coro
    return r, time.monotonic() - t0


def test_hung_soundcloud_does_not_delay_youtube_catalog_or_sqlite(world):
    gate = threading.Event()
    world.sc.gate = gate                                       # SoundCloud висит до gate.set() (не дольше 30 с)

    async def go(c):
        await c.post("/api/favorites", json=track_payload())
        # Больше запросов к SoundCloud, чем вмещает его пул (2 + 4): лишние должны отклоняться сразу
        hung = [asyncio.create_task(c.get("/api/search", params={"q": f"sc {i}", "sources": "sc"})) for i in range(10)]
        await asyncio.sleep(0.2)
        sc_pool = providers.sc.executor.stats()
        checks = {
            "search yt": await timed(c.get("/api/search", params={"q": "x", "sources": "yt"})),
            "artists yt": await timed(c.get("/api/search/artists", params={"q": "x", "source": "yt"})),
            "favorites": await timed(c.get("/api/favorites")),
            "settings": await timed(c.get("/api/settings")),
            "history": await timed(c.post("/api/history", json=track_payload(id_="AAAAAAAAAA2"))),
            "playlists": await timed(c.post("/api/playlists", json={"name": "Во время зависания"})),
        }
        busy = [t for t in hung if t.done()]                   # отклонены сразу: пул SoundCloud заполнен
        gate.set()
        rest = await asyncio.gather(*hung)
        return sc_pool, checks, len(busy), rest

    sc_pool, checks, busy_early, rest = world.run(go)
    for name, (r, took) in checks.items():
        assert r.status_code == (201 if name == "playlists" else 200), (name, r.status_code, r.text)
        assert took < FAST, f"{name} ждал зависший SoundCloud: {took:.2f} с"
    assert [t["source"] for t in checks["search yt"][0].json()["tracks"]] == ["yt"] * 5
    # Пул SoundCloud не вышел за предел, лишние запросы не встали в бесконечную очередь
    assert sc_pool["outstanding"] <= providers.sc.executor.capacity == 6
    assert sc_pool["running"] <= providers.sc.workers == 2
    assert busy_early >= 10 - providers.sc.executor.capacity
    busy = [r for r in rest if r.status_code == 502 and "не успевает" in r.json()["detail"]]
    assert len(busy) >= 4 and all(r.json()["code"] == "unavailable" and r.json()["source"] == "sc" for r in busy)
    assert any(r.status_code == 200 for r in rest)            # после «выздоровления» висевшие запросы ответили


def test_hung_youtube_does_not_delay_soundcloud_or_sqlite(world):
    gate = threading.Event()
    world.ytm.gates["search"] = gate

    async def go(c):
        hung = asyncio.create_task(c.get("/api/search", params={"q": "x", "sources": "yt"}))
        await asyncio.sleep(0.2)
        checks = {
            "search sc": await timed(c.get("/api/search", params={"q": "x", "sources": "sc"})),
            "favorites": await timed(c.get("/api/favorites")),
            "settings": await timed(c.get("/api/settings")),
        }
        done_early = hung.done()
        gate.set()
        return checks, done_early, await hung

    checks, done_early, hung = world.run(go)
    for name, (r, took) in checks.items():
        assert r.status_code == 200 and took < FAST, (name, r.status_code, took)
    assert [t["source"] for t in checks["search sc"][0].json()["tracks"]] == ["sc"] * 5
    assert not done_early and hung.status_code == 200          # запрос YouTube честно дождался ответа


def test_hung_soundcloud_does_not_delay_youtube_recommendations(world):
    gate = threading.Event()
    world.sc.gate = gate
    world.sc.responses["tracks/1000000001/related"] = {"collection": []}

    async def go(c):
        hung = asyncio.create_task(c.get("/api/recommendations", params={"source": "sc", "id": "1000000001"}))
        await asyncio.sleep(0.2)
        yt = await timed(c.get("/api/recommendations", params={"source": "yt", "id": "AAAAAAAAAA1"}))
        gate.set()
        await hung
        return yt

    (r, took) = world.run(go)
    assert r.status_code == 200 and took < FAST and r.json()["source_status"] == {"yt": "ok"}


def test_source_calls_run_in_their_own_pools(api):
    """Ни один вызов ytmusicapi или API SoundCloud не идёт в общий пул event loop (asyncio.to_thread)."""
    api.ytm.songs["*"] = yt_items(3)
    api.ytm.search_results[("albums", "*")] = []
    api.sc.tracks["*"] = sc_items(3)
    api.sc.responses["search/users"] = {"collection": []}
    api.get("/api/search", params={"q": "x"})
    api.get("/api/search/albums", params={"q": "x"})
    api.get("/api/search/artists", params={"q": "x", "source": "sc"})
    api.get("/api/recommendations")
    assert api.ytm.threads and all(n.startswith("provider-yt") for n in api.ytm.threads), api.ytm.threads
    assert api.sc.threads and all(n.startswith("provider-sc") for n in api.sc.threads), api.sc.threads


def test_catalog_timeout_is_a_clear_error(api, monkeypatch):
    gate = threading.Event()
    api.ytm.gates["get_artist"] = gate
    monkeypatch.setattr(catalog, "CATALOG_TIMEOUT", 0.2)
    try:
        t0 = time.monotonic()
        r = api.get("/api/artist/yt/UCaurorawave00000000001")
        assert time.monotonic() - t0 < 2
    finally:
        gate.set()
    assert r.status_code == 502
    assert r.json() == {"detail": "YouTube Music не ответил вовремя. Попробуйте позже.", "code": "timeout",
                        "source": "yt", "source_status": {"yt": "cooldown"}}


def test_broken_answer_inside_adapter_is_an_internal_error_of_that_source(api):
    """Ответ источника неожиданной формы ломает разбор в адаптере: 502 этого источника, а не 500."""
    api.ytm.artists["UCbroken0000000000000001"] = {"name": "X", "albums": {"results": [{"browseId": "MPREb_x",
                                                                                       "thumbnails": [{}]}]}}
    r = api.get("/api/artist/yt/UCbroken0000000000000001")
    assert r.status_code == 502 and r.json()["code"] == "internal" and r.json()["source"] == "yt"


def test_single_source_failure_explains_itself(api):
    """Фронтенд ищет по каждому источнику отдельно: при сбое единственного источника `detail` — его причина."""
    api.sc.search_error = RuntimeError("down")
    r = api.get("/api/search", params={"q": "x", "sources": "sc"})
    assert r.status_code == 502
    assert r.json() == {"detail": "SoundCloud временно недоступен. Попробуйте позже.", "code": "internal",
                        "source": "sc", "failed_sources": ["sc"], "source_status": {"sc": "cooldown"}}
    r = api.get("/api/search", params={"q": "y", "sources": "sc"})          # теперь — пауза после сбоя
    assert r.status_code == 502 and r.json()["code"] == "unavailable" and "Повторная попытка" in r.json()["detail"]
    assert api.sc.count("search_tracks") == 1
