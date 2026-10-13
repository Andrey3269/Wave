"""Ограниченный кэш LRU+TTL (backend/providers/cache.py, S7, находка M9)."""
from __future__ import annotations

import threading

import pytest

from providers.cache import TTLCache


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_get_set_and_dict_interface():
    c = TTLCache(maxsize=3, ttl=10, clock=Clock())
    c["a"] = 1
    c.set("b", 2)
    assert c.get("a") == 1 and c["b"] == 2 and "a" in c and "zzz" not in c
    assert len(c) == 2 and bool(c) and set(c) == {"a", "b"}
    assert c.get("zzz") is None and c.get("zzz", 5) == 5
    with pytest.raises(KeyError):
        c["zzz"]
    assert c.pop("a") == 1 and c.pop("a", "нет") == "нет"
    c.clear()
    assert not c and len(c) == 0


def test_size_limit_evicts_least_recently_used():
    c = TTLCache(maxsize=3, ttl=100, clock=Clock())
    for k in "abc":
        c[k] = k
    c.get("a")                                  # «a» использована недавно — вытеснится «b»
    c["d"] = "d"
    assert set(c) == {"a", "c", "d"}
    assert len(c) == 3


def test_ttl_expiry_on_read():
    clock = Clock()
    c = TTLCache(maxsize=10, ttl=5, clock=clock)
    c["a"] = 1
    clock.now += 4.9
    assert c.get("a") == 1
    clock.now += 0.2                            # 5,1 с после записи
    assert c.get("a") is None and "a" not in c and len(c) == 0


def test_rewrite_refreshes_ttl():
    clock = Clock()
    c = TTLCache(maxsize=10, ttl=5, clock=clock)
    c["a"] = 1
    clock.now += 4
    c["a"] = 2
    clock.now += 4
    assert c.get("a") == 2


def test_expired_entries_are_dropped_before_live_ones_on_overflow():
    clock = Clock()
    c = TTLCache(maxsize=3, ttl=5, clock=clock)
    c["old"] = 0
    clock.now += 3
    c["b"], c["c"] = 1, 2
    c.get("old")                                # старая, но недавно прочитанная — по LRU выжила бы
    clock.now += 3                              # «old» устарела (6 с), «b» и «c» живые (3 с)
    c["d"] = 3
    assert set(c) == {"b", "c", "d"}


def test_ttl_change_applies_to_stored_entries():
    c = TTLCache(maxsize=10, ttl=300, clock=Clock())
    c["a"] = 1
    c.ttl = 0
    assert c.get("a") is None


def test_high_cardinality_stays_bounded():
    """Тысячи разных поисковых запросов не раздувают кэш сверх лимита."""
    c = TTLCache(maxsize=64, ttl=300, clock=Clock())
    for i in range(10_000):
        c[(f"запрос {i}", 20, 0, "yt,sc")] = [i]
    assert len(c) == 64
    assert (f"запрос {9999}", 20, 0, "yt,sc") in c and ("запрос 0", 20, 0, "yt,sc") not in c


def test_concurrent_access_is_safe():
    c = TTLCache(maxsize=50, ttl=300)

    def work(n):
        for i in range(2000):
            c[(n, i % 80)] = i
            c.get((n, (i * 7) % 80))

    threads = [threading.Thread(target=work, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(c) <= 50


def test_invalid_size():
    with pytest.raises(ValueError):
        TTLCache(maxsize=0, ttl=1)
