"""
Ограниченные кэши в памяти (S7, находка M9): поиск, страницы артистов, тексты песен.

Каждый кэш — LRU+TTL (providers/cache.py): лимит записей держится при любом числе разных запросов,
устаревшие записи не отдаются. Прежняя семантика сохранена: регистр и пробелы запроса не важны,
неполные результаты не кэшируются, «Очистить кэш» в настройках сбрасывает всё.
"""
from __future__ import annotations

import catalog
import lyrics
import main
from wavetest.fakes import load_fixture

ART = "UCaurorawave00000000001"
SONGS = "VLOLAKsongs000000000000001"


def yt_items(n: int) -> list[dict]:
    return [{"videoId": f"YYYYYYYYY{i:02d}", "title": f"yt {i}", "artists": [{"name": "A", "id": "UCa"}]}
            for i in range(n)]


# ---------------------------------------------------------------------------
# Поиск
# ---------------------------------------------------------------------------
def test_search_cache_limits_are_set():
    assert main._cache.maxsize == main.CACHE_MAX_ITEMS == 256 and main._cache.ttl == main.CACHE_TTL_SEC == 300


def test_search_cache_stays_bounded_with_many_distinct_queries(api, monkeypatch):
    monkeypatch.setattr(main._cache, "maxsize", 10)
    api.ytm.songs["*"] = yt_items(3)
    for i in range(40):
        assert api.get("/api/search", params={"q": f"запрос {i}", "sources": "yt"}).status_code == 200
    assert len(main._cache) == 10
    calls = api.ytm.count("search")
    api.get("/api/search", params={"q": "запрос 39", "sources": "yt"})          # свежий — из кэша
    api.get("/api/search", params={"q": "запрос 0", "sources": "yt"})           # вытеснен — снова к источнику
    assert api.ytm.count("search") == calls + 1


def test_search_cache_normalizes_query_and_skips_partial_results(api):
    api.ytm.songs["*"] = yt_items(3)
    api.sc.search_error = RuntimeError("down")
    api.get("/api/search", params={"q": "  Mixed Case "})
    assert not main._cache                                       # SoundCloud не ответил — не кэшируем
    r1 = api.get("/api/search", params={"q": "  Mixed Case ", "sources": "yt"})
    r2 = api.get("/api/search", params={"q": "mixed case", "sources": "yt"})
    assert r1.json()["tracks"] == r2.json()["tracks"] and len(main._cache) == 1


# ---------------------------------------------------------------------------
# Страницы артистов
# ---------------------------------------------------------------------------
def artists(api, n: int) -> list[str]:
    ids = [f"UCartist{i:016d}" for i in range(n)]
    for a in ids:
        data = load_fixture("ytmusic/artist.json")
        data["songs"]["browseId"] = None                         # без отдельного плейлиста песен
        data["albums"].pop("params", None)                       # без догрузки полного списка релизов
        data["singles"].pop("params", None)
        api.ytm.artists[a] = data
    return ids


def test_artist_cache_limits_are_set():
    assert catalog._artist_cache.maxsize == catalog.ARTIST_CACHE_ITEMS == 32
    assert catalog._artist_cache.ttl == catalog.ARTIST_CACHE_TTL == 600


def test_artist_cache_is_bounded_and_evicts_least_recent(api, monkeypatch):
    monkeypatch.setattr(catalog._artist_cache, "maxsize", 3)
    ids = artists(api, 5)
    for a in ids:
        assert api.get(f"/api/artist/yt/{a}").status_code == 200
    assert len(catalog._artist_cache) == 3
    api.get(f"/api/artist/yt/{ids[-1]}")                         # в кэше
    assert api.ytm.count("get_artist") == 5
    api.get(f"/api/artist/yt/{ids[0]}")                          # вытеснен — загружается заново
    assert api.ytm.count("get_artist") == 6


def test_artist_cache_expires(api, monkeypatch):
    (a,) = artists(api, 1)
    api.get(f"/api/artist/yt/{a}")
    monkeypatch.setattr(catalog._artist_cache, "ttl", 0)
    api.get(f"/api/artist/yt/{a}")
    assert api.ytm.count("get_artist") == 2


def test_incomplete_artist_page_is_not_cached(api):
    """Полный список релизов не получен: страница показывается, но не кэшируется — источник может «выздороветь»."""
    api.ytm.artists[ART] = load_fixture("ytmusic/artist.json")
    api.ytm.playlists[SONGS] = {"tracks": load_fixture("ytmusic/artist.json")["songs"]["results"]}
    api.ytm.fail("get_artist_albums")
    assert api.get(f"/api/artist/yt/{ART}").status_code == 200
    assert api.ytm.count("get_artist") == 1                      # одна загрузка на открытие страницы
    assert not catalog._artist_cache
    del api.ytm.failures["get_artist_albums"]
    api.ytm.artist_albums[ART] = load_fixture("ytmusic/artist_albums.json")
    assert api.get(f"/api/artist/yt/{ART}").status_code == 200
    assert api.ytm.count("get_artist") == 2 and len(catalog._artist_cache) == 1


# ---------------------------------------------------------------------------
# Тексты песен
# ---------------------------------------------------------------------------
def test_lyrics_cache_is_bounded_and_expires(api, monkeypatch):
    assert lyrics._cache.maxsize == 512 and lyrics._cache.ttl == 24 * 3600
    monkeypatch.setattr(lyrics._cache, "maxsize", 2)
    api.net.lyrics = (200, {"syncedLyrics": "[00:01.00] строка", "plainLyrics": "строка"})
    for i in range(4):
        assert api.get("/api/lyrics", params={"artist": "A", "title": f"Песня {i}"}).status_code == 200
    assert len(lyrics._cache) == 2 and api.net.lyrics_calls == 4
    api.get("/api/lyrics", params={"artist": "a", "title": "песня 3 "})          # регистр и пробелы не важны
    assert api.net.lyrics_calls == 4
    monkeypatch.setattr(lyrics._cache, "ttl", 0)
    api.get("/api/lyrics", params={"artist": "A", "title": "Песня 3"})
    assert api.net.lyrics_calls == 5


def test_lyrics_temporary_failure_is_not_cached(api):
    api.net.lyrics = (503, "maintenance")
    api.get("/api/lyrics", params={"artist": "A", "title": "T"})
    assert not lyrics._cache
    api.net.lyrics = (404, {"message": "not found"})
    api.get("/api/lyrics", params={"artist": "A", "title": "T"})
    assert len(lyrics._cache) == 1                               # «текста нет» — окончательный ответ, кэшируется


def test_clear_cache_empties_bounded_caches(api):
    api.ytm.songs["*"] = yt_items(2)
    api.net.lyrics = (404, {"message": "not found"})
    (a,) = artists(api, 1)
    api.get("/api/search", params={"q": "x", "sources": "yt"})
    api.get(f"/api/artist/yt/{a}")
    api.get("/api/lyrics", params={"artist": "A", "title": "T"})
    assert main._cache and catalog._artist_cache and lyrics._cache
    assert api.post("/api/settings/clear-cache").json() == {"ok": True}
    assert not main._cache and not catalog._artist_cache and not lyrics._cache
