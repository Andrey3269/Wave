"""
S16: кэши процесса ограничены по размеру и сроку, устаревшее вытесняется, «Очистить кэш» сбрасывает всё.

Раньше кэш ссылок резолвера был dict без предела живых записей (чистились только протухшие после 512 штук), и за
долгий сеанс он рос вместе с числом прослушанных треков. Здесь проверяется, что у каждого кэша уровня процесса есть
потолок и что вытеснение не ломает работу резолвера.
"""
from __future__ import annotations

import asyncio
import time

import catalog
import lyrics
import main
import stream
from providers.cache import TTLCache
from streaming import hls_proxy, proxy, resolver


def _resolve(source: str, track_id: str):
    async def go():
        return await stream._resolve(stream.ResolverState(), source, track_id)
    return asyncio.run(go())


def test_every_process_cache_has_a_size_and_age_limit():
    caches = {"search": main._cache, "lyrics": lyrics._cache, "artist": catalog._artist_cache, "resolver": resolver._cache}
    for name, c in caches.items():
        assert isinstance(c, TTLCache), name
        assert 1 <= c.maxsize <= 1024, (name, c.maxsize)
        assert 0 < c.ttl <= 24 * 3600, (name, c.ttl)
    assert 1 <= proxy.HEAD_CACHE_ITEMS <= 64 and proxy.HEAD_TTL <= 3600
    assert 1 <= hls_proxy.MAX_SESSIONS <= 32 and hls_proxy.MAX_AGE <= 6 * 3600
    assert stream._cache is resolver._cache                   # старое имя смотрит на тот же объект


def test_resolver_cache_evicts_least_recently_used(monkeypatch):
    monkeypatch.setattr(resolver._cache, "maxsize", 3)
    calls = []

    def extract(source, track_id):
        calls.append(track_id)
        return stream.Resolved(f"https://cdn.test/{track_id}", {}, time.time() + 3600)

    monkeypatch.setattr(stream, "_extract_sync", extract)
    ids = [f"AAAAAAAAAA{i}" for i in range(4)]
    for i in ids[:3]:
        _resolve("yt", i)
    _resolve("yt", ids[0])                                     # из кэша: становится «недавно использованным»
    assert calls == ids[:3]
    _resolve("yt", ids[3])                                     # переполнение: вытесняется ids[1], а не ids[0]
    assert len(resolver._cache) == 3
    assert f"yt:{ids[1]}" not in resolver._cache and f"yt:{ids[0]}" in resolver._cache
    _resolve("yt", ids[1])                                     # вытесненный трек снова резолвится и играет
    assert calls == [*ids[:3], ids[3], ids[1]]


def test_resolver_cache_drops_entries_older_than_ceiling(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(resolver._cache, "_clock", lambda: clock[0])
    monkeypatch.setattr(stream, "_extract_sync",
                        lambda s, i: stream.Resolved("https://cdn.test/x", {}, time.time() + 10 * 3600))
    _resolve("yt", "AAAAAAAAAA1")
    assert "yt:AAAAAAAAAA1" in resolver._cache
    clock[0] += resolver.RESOLVE_CACHE_TTL + 1                # ссылка «жива» по expire, но старше потолка
    assert "yt:AAAAAAAAAA1" not in resolver._cache


def test_expired_link_is_resolved_again(monkeypatch):
    n = [0]

    def extract(source, track_id):
        n[0] += 1
        return stream.Resolved("https://cdn.test/x", {}, time.time() + (-1 if n[0] == 1 else 3600))

    monkeypatch.setattr(stream, "_extract_sync", extract)
    _resolve("yt", "AAAAAAAAAA1")
    _resolve("yt", "AAAAAAAAAA1")                              # первая ссылка уже истекла
    assert n[0] == 2


def test_cache_growth_stays_bounded_over_a_long_session(monkeypatch):
    monkeypatch.setattr(stream, "_extract_sync",
                        lambda s, i: stream.Resolved(f"https://cdn.test/{i}", {}, time.time() + 3600))
    for i in range(resolver.RESOLVE_CACHE_ITEMS * 2):
        _resolve("yt", f"B{i:010d}")
    assert len(resolver._cache) == resolver.RESOLVE_CACHE_ITEMS


def test_clear_cache_setting_empties_resolver_cache(api):
    resolver._cache.set("yt:AAAAAAAAAA1", stream.Resolved("u", {}, time.time() + 60))
    assert api.post("/api/settings/clear-cache").status_code == 200
    assert len(resolver._cache) == 0
