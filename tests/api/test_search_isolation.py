"""
Поиск треков /api/search (backend/main.py): параллельный опрос источников, изоляция сбоев, cooldown,
таймауты, кэш и пагинация. YouTube Music и SoundCloud подменены (FakeYTMusic, FakeSoundCloud) на границе
адаптеров; пулы, таймауты и cooldown адаптеров (backend/providers/*) работают по-настоящему.
"""
from __future__ import annotations

import asyncio
import threading
import time

import pytest

import main
import providers
from wavetest.fakes import load_fixture, track_payload


def yt_items(n: int, prefix: str = "Y") -> list[dict]:
    return [{"videoId": f"{prefix * 9}{i:02d}", "title": f"yt {i}", "artists": [{"name": "YT Artist", "id": "UCy"}]}
            for i in range(n)]


def sc_items(n: int) -> list[dict]:
    return [{"kind": "track", "id": 2000000000 + i, "title": f"sc {i}", "user": {"id": 1, "username": "sc user"}}
            for i in range(n)]


@pytest.fixture
def both(api):
    api.ytm.songs["*"] = yt_items(30)
    api.sc.tracks["*"] = sc_items(30)
    return api


def ids(r) -> list[str]:
    return [f"{t['source']}:{t['title']}" for t in r.json()["tracks"]]


def test_sources_interleaved(both):
    r = both.get("/api/search", params={"q": "test", "limit": 4})
    assert r.status_code == 200
    assert ids(r) == ["yt:yt 0", "sc:sc 0", "yt:yt 1", "sc:sc 1"]
    assert r.json()["failed_sources"] == [] and r.json()["has_more"] is True
    assert r.json()["query"] == "test" and r.json()["count"] == 4


def test_single_source_selection(both):
    r = both.get("/api/search", params={"q": "test", "sources": "yt", "limit": 3})
    assert ids(r) == ["yt:yt 0", "yt:yt 1", "yt:yt 2"]
    assert both.sc.count("search_tracks") == 0
    r = both.get("/api/search", params={"q": "test", "sources": "sc", "limit": 2})
    assert ids(r) == ["sc:sc 0", "sc:sc 1"]


@pytest.mark.parametrize("sources", ["spotify", "yandex", " ", "local"])
def test_unknown_sources(both, sources):
    r = both.get("/api/search", params={"q": "test", "sources": sources})
    assert r.status_code == 400 and r.json()["detail"] == "Неизвестные или отключённые источники"
    assert not both.ytm.calls and not both.sc.calls


# ---------------------------------------------------------------------------
# Изоляция сбоев
# ---------------------------------------------------------------------------
def test_soundcloud_failure_does_not_break_youtube(both):
    both.sc.search_error = RuntimeError("SoundCloud API changed")
    r = both.get("/api/search", params={"q": "test", "limit": 3})
    assert r.status_code == 200
    assert ids(r) == ["yt:yt 0", "yt:yt 1", "yt:yt 2"]
    assert r.json()["failed_sources"] == ["sc"]
    assert not main._cache                                     # неполный результат не кэшируется


def test_failed_optional_source_enters_cooldown(both):
    both.sc.search_error = RuntimeError("down")
    both.get("/api/search", params={"q": "one"})
    assert both.sc.count("search_tracks") == 1
    r = both.get("/api/search", params={"q": "two"})
    assert r.status_code == 200 and r.json()["failed_sources"] == ["sc"]
    assert both.sc.count("search_tracks") == 1                 # в cooldown не опрашивается
    assert providers.sc.cooldown == 300 and providers.sc.breaker.remaining() > providers.sc.cooldown - 5


def test_source_recovers_after_cooldown(both):
    both.sc.search_error = RuntimeError("down")
    both.get("/api/search", params={"q": "one"})
    providers.sc.breaker.until = time.monotonic() - 1          # cooldown истёк
    both.sc.search_error = None
    r = both.get("/api/search", params={"q": "two", "limit": 2})
    assert r.json()["failed_sources"] == [] and ids(r) == ["yt:yt 0", "sc:sc 0"]
    assert not providers.sc.breaker.is_open() and providers.sc.breaker.last_kind is None


def test_youtube_failure_keeps_soundcloud(both):
    both.ytm.fail("search")
    r = both.get("/api/search", params={"q": "test", "limit": 2})
    assert r.status_code == 200
    assert ids(r) == ["sc:sc 0", "sc:sc 1"] and r.json()["failed_sources"] == ["yt"]


def test_youtube_cooldown_is_short(both):
    """
    M3 (исправлено в S7): YouTube Music после сбоя тоже уходит в паузу, но короткую (30–60 с), а SoundCloud —
    на 300 с. Заменяет тест «как есть» test_youtube_is_not_put_in_cooldown (YouTube опрашивался при каждом поиске).
    """
    both.ytm.fail("search")
    r = both.get("/api/search", params={"q": "one"})
    assert r.status_code == 200 and r.json()["failed_sources"] == ["yt"]
    assert 30 <= providers.yt.cooldown <= 60 < providers.sc.cooldown == 300
    assert providers.yt.cooldown - 5 < providers.yt.breaker.remaining() <= providers.yt.cooldown
    assert providers.yt.breaker.last_kind == "internal"


def test_youtube_failure_enters_cooldown(both):
    both.ytm.fail("search")
    both.get("/api/search", params={"q": "one"})
    both.get("/api/search", params={"q": "two"})
    assert both.ytm.count("search") == 1


def test_youtube_recovers_after_cooldown(both):
    both.ytm.fail("search")
    both.get("/api/search", params={"q": "one"})
    providers.yt.breaker.until = time.monotonic() - 1          # пауза истекла
    del both.ytm.failures["search"]
    r = both.get("/api/search", params={"q": "two", "limit": 2})
    assert r.json()["failed_sources"] == [] and ids(r) == ["yt:yt 0", "sc:sc 0"]
    assert both.ytm.count("search") == 2 and not providers.yt.breaker.is_open()


def test_youtube_still_failing_after_cooldown_pauses_again(both):
    both.ytm.fail("search")
    both.get("/api/search", params={"q": "one"})
    providers.yt.breaker.until = time.monotonic() - 1
    both.get("/api/search", params={"q": "two"})               # снова опрошен и снова упал
    both.get("/api/search", params={"q": "three"})             # новая пауза
    assert both.ytm.count("search") == 2 and providers.yt.breaker.is_open()


def test_empty_result_is_not_a_failure(both):
    """Пустая выдача — нормальный ответ: источник не уходит в паузу и опрашивается дальше."""
    both.ytm.songs["*"] = []
    both.sc.tracks["*"] = []
    r = both.get("/api/search", params={"q": "nothing"})
    assert r.status_code == 200 and r.json()["failed_sources"] == []
    both.get("/api/search", params={"q": "nothing else"})
    assert both.ytm.count("search") == 2 and both.sc.count("search_tracks") == 2
    assert not providers.yt.breaker.is_open() and not providers.sc.breaker.is_open()


def test_hanging_youtube_costs_one_timeout_not_one_per_search(both, monkeypatch):
    """M3: при «висящей» сети ждём таймаут один раз, дальше поиск отвечает сразу результатами SoundCloud."""
    monkeypatch.setattr(main, "PROVIDER_TIMEOUT", 0.3)
    gate = threading.Event()
    both.ytm.gates["search"] = gate
    try:
        t0 = time.monotonic()
        first = both.get("/api/search", params={"q": "one", "limit": 2})
        first_took = time.monotonic() - t0
        t0 = time.monotonic()
        later = [both.get("/api/search", params={"q": f"q{i}", "limit": 2}) for i in range(5)]
        later_took = time.monotonic() - t0
    finally:
        gate.set()
    assert 0.25 < first_took < 1.5 and first.json()["failed_sources"] == ["yt"]
    assert later_took < 0.5, f"каждый поиск ждал таймаут YouTube: {later_took:.2f} с на 5 запросов"
    assert all(r.status_code == 200 and r.json()["failed_sources"] == ["yt"] and ids(r) == ["sc:sc 0", "sc:sc 1"]
               for r in later)
    assert both.ytm.count("search") == 1 and providers.yt.breaker.last_kind == "timeout"


def test_all_sources_failed_gives_502(both):
    both.ytm.fail("search")
    both.sc.search_error = RuntimeError("down")
    r = both.get("/api/search", params={"q": "test"})
    assert r.status_code == 502 and r.json()["detail"] == "Источники поиска недоступны"


def test_all_selected_sources_in_cooldown_gives_502_without_calls(both):
    providers.sc.breaker.trip("timeout")
    r = both.get("/api/search", params={"q": "test", "sources": "sc"})
    assert r.status_code == 502
    assert both.sc.count("search_tracks") == 0


def test_provider_timeout_is_isolated(both, monkeypatch):
    monkeypatch.setattr(main, "PROVIDER_TIMEOUT", 0.3)
    both.sc.search_sleep = 1.5
    t0 = time.monotonic()
    r = both.get("/api/search", params={"q": "test", "limit": 2})
    elapsed = time.monotonic() - t0
    assert r.status_code == 200 and r.json()["failed_sources"] == ["sc"]
    assert ids(r) == ["yt:yt 0", "yt:yt 1"]
    assert elapsed < 1.2, f"ответ ждал медленный источник: {elapsed:.2f} с"


def test_slow_provider_does_not_block_local_endpoints(harness, monkeypatch):
    """Пока SoundCloud «висит», избранное и настройки (SQLite) отвечают сразу."""
    harness.ytm.songs["*"] = yt_items(3)
    harness.sc.tracks["*"] = sc_items(3)
    harness.sc.search_sleep = 1.0
    monkeypatch.setattr(main, "PROVIDER_TIMEOUT", 3)

    async def go(c):
        await c.post("/api/favorites", json=track_payload())
        search = asyncio.create_task(c.get("/api/search", params={"q": "slow"}))
        await asyncio.sleep(0.1)
        t0 = time.monotonic()
        fav = await c.get("/api/favorites")
        settings = await c.get("/api/settings")
        local_time = time.monotonic() - t0
        return fav, settings, local_time, search.done(), await search

    fav, settings, local_time, search_done_early, search = harness.run(go)
    assert fav.status_code == 200 and len(fav.json()["items"]) == 1 and settings.status_code == 200
    assert local_time < 0.5 and not search_done_early
    assert search.status_code == 200


# ---------------------------------------------------------------------------
# Кэш и пагинация
# ---------------------------------------------------------------------------
def test_identical_search_is_cached(both):
    a = both.get("/api/search", params={"q": "Test ", "limit": 5})
    b = both.get("/api/search", params={"q": "test", "limit": 5})        # регистр и пробелы не важны
    assert a.json()["tracks"] == b.json()["tracks"]
    assert both.ytm.count("search") == 1 and both.sc.count("search_tracks") == 1
    both.get("/api/search", params={"q": "test", "limit": 6})            # другой limit — другой ключ
    assert both.ytm.count("search") == 2


def test_cache_expires(both, monkeypatch):
    monkeypatch.setattr(main._cache, "ttl", 0)                  # S7: срок — у кэша LRU+TTL (было main.CACHE_TTL_SEC)
    both.get("/api/search", params={"q": "test"})
    both.get("/api/search", params={"q": "test"})
    assert both.ytm.count("search") == 2


def test_cache_is_bounded():
    for i in range(main.CACHE_MAX_ITEMS + 5):
        main._cache_set((f"q{i}", 20, 0, "yt"), [])
    assert len(main._cache) == main.CACHE_MAX_ITEMS
    assert ("q0", 20, 0, "yt") not in main._cache


def test_pagination_offset(both):
    r = both.get("/api/search", params={"q": "test", "limit": 4, "offset": 4})
    assert ids(r) == ["yt:yt 2", "sc:sc 2", "yt:yt 3", "sc:sc 3"]
    assert ("search", "test", "songs", 8) in both.ytm.calls               # источник просят offset + limit
    assert ("search_tracks", "test", 8) in both.sc.calls


def test_pagination_stops_at_max_results(both):
    r = both.get("/api/search", params={"q": "test", "limit": 20, "offset": 90})
    assert ("search", "test", "songs", main.MAX_RESULTS) in both.ytm.calls
    assert r.json()["has_more"] is False


def test_empty_result_has_no_more(api):
    api.ytm.songs["*"] = []
    api.sc.tracks["*"] = []
    r = api.get("/api/search", params={"q": "nothing"})
    assert r.status_code == 200 and r.json()["tracks"] == [] and r.json()["has_more"] is False


def test_normalization_applied_to_provider_results(api):
    api.ytm.songs["*"] = load_fixture("ytmusic/search_songs.json")
    api.sc.tracks["*"] = load_fixture("soundcloud/search_tracks.json")["collection"]
    r = api.get("/api/search", params={"q": "x", "limit": 50})
    got = {(t["source"], t["id"]) for t in r.json()["tracks"]}
    assert got == {("yt", "AAAAAAAAAA1"), ("yt", "AAAAAAAAAA2"), ("yt", "AAAAAAAAAA3"),
                   ("sc", "1000000001"), ("sc", "1000000002"), ("sc", "1000000006")}


@pytest.mark.parametrize("params, status", [
    ({}, 422),
    ({"q": ""}, 422),
    ({"q": "   "}, 400),
    ({"q": "x" * 201}, 422),
    ({"q": "x", "limit": 0}, 422),
    ({"q": "x", "limit": 51}, 422),
    ({"q": "x", "offset": -1}, 422),
    ({"q": "x", "offset": 101}, 422),
])
def test_query_validation(both, params, status):
    assert both.get("/api/search", params=params).status_code == status
    assert not both.ytm.calls and not both.sc.calls
