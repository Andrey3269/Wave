"""
`source_status` в ответах поиска и каталога, `GET /api/sources` (S7, TARGET_ARCHITECTURE §5).

Значения: ok — источник ответил; degraded — ответил не полностью или работает с перебоями; cooldown —
пауза после сбоя; disabled — выключен для поиска треков (SEARCH_SOURCES). Поля ответов до S7 сохранены.
"""
from __future__ import annotations

import io
import json
import threading

import pytest
from yt_dlp.networking import Response
from yt_dlp.networking.exceptions import HTTPError as YdlHTTPError
from yt_dlp.utils import ExtractorError

import main
import providers
from wavetest.fakes import load_fixture

ART = "UCaurorawave00000000001"
SONGS = "VLOLAKsongs000000000000001"


def yt_items(n):
    return [{"videoId": f"YYYYYYYYY{i:02d}", "title": f"yt {i}", "artists": [{"name": "A", "id": "UCa"}]} for i in range(n)]


def sc_items(n):
    return [{"kind": "track", "id": 2000000000 + i, "title": f"sc {i}", "user": {"id": 1, "username": "u"}}
            for i in range(n)]


@pytest.fixture
def both(api):
    api.ytm.songs["*"] = yt_items(4)
    api.sc.tracks["*"] = sc_items(4)
    return api


# ---------------------------------------------------------------------------
# Поиск треков
# ---------------------------------------------------------------------------
def test_search_all_ok(both):
    body = both.get("/api/search", params={"q": "x"}).json()
    assert body["source_status"] == {"yt": "ok", "sc": "ok"} and body["failed_sources"] == []


def test_search_partial_failure(both):
    both.sc.search_error = RuntimeError("down")
    body = both.get("/api/search", params={"q": "x"}).json()
    assert body["source_status"] == {"yt": "ok", "sc": "cooldown"} and body["failed_sources"] == ["sc"]
    assert {t["source"] for t in body["tracks"]} == {"yt"}
    again = both.get("/api/search", params={"q": "y"}).json()          # в паузе: не опрашивается
    assert again["source_status"] == {"yt": "ok", "sc": "cooldown"} and both.sc.count("search_tracks") == 1


def test_search_busy_source_is_degraded_not_paused(both):
    gate = threading.Event()
    ex = providers.sc.executor
    held = [ex.submit(gate.wait, 5) for _ in range(ex.capacity)]
    try:
        body = both.get("/api/search", params={"q": "x"}).json()
    finally:
        gate.set()
    for f in held:
        f.result(3)
    assert body["source_status"] == {"yt": "ok", "sc": "degraded"} and body["failed_sources"] == ["sc"]
    assert not providers.sc.breaker.is_open()


def test_search_full_failure(both):
    both.ytm.fail("search")
    both.sc.search_error = RuntimeError("down")
    r = both.get("/api/search", params={"q": "x"})
    assert r.status_code == 502
    assert r.json() == {"detail": "Источники поиска недоступны", "code": "unavailable", "failed_sources": ["yt", "sc"],
                        "source_status": {"yt": "cooldown", "sc": "cooldown"}}


def test_search_full_failure_while_paused(both):
    providers.yt.breaker.trip("timeout")
    providers.sc.breaker.trip("timeout")
    r = both.get("/api/search", params={"q": "x"})
    assert r.status_code == 502 and r.json()["source_status"] == {"yt": "cooldown", "sc": "cooldown"}
    assert not both.ytm.calls and not both.sc.calls


def test_cached_search_reports_ok(both):
    both.get("/api/search", params={"q": "x"})
    body = both.get("/api/search", params={"q": "x"}).json()
    assert both.ytm.count("search") == 1 and body["source_status"] == {"yt": "ok", "sc": "ok"}


def test_disabled_source(both, monkeypatch):
    monkeypatch.setattr(providers.sc, "search_enabled", False)
    monkeypatch.setattr(main, "PROVIDERS", {"yt": providers.yt})
    body = both.get("/api/search", params={"q": "x"}).json()
    assert body["source_status"] == {"yt": "ok", "sc": "disabled"} and body["failed_sources"] == []
    assert both.sc.count("search_tracks") == 0
    body = both.get("/api/search", params={"q": "z", "sources": "yt"}).json()
    assert body["source_status"] == {"yt": "ok"}
    r = both.get("/api/search", params={"q": "x", "sources": "sc"})
    assert r.status_code == 400 and r.json()["detail"] == "Неизвестные или отключённые источники"


# ---------------------------------------------------------------------------
# Каталог
# ---------------------------------------------------------------------------
def test_cards_ok_and_paused(api):
    api.ytm.search_results[("artists", "*")] = load_fixture("ytmusic/search_artists.json")
    body = api.get("/api/search/artists", params={"q": "x"}).json()
    assert body["source_status"] == {"yt": "ok"} and set(body) == {"items", "has_more", "source_status"}
    providers.yt.breaker.trip("rate_limited")
    r = api.get("/api/search/artists", params={"q": "y"})
    assert r.status_code == 502 and r.json()["source_status"] == {"yt": "cooldown"}


def test_artist_page_ok_and_degraded(api):
    api.ytm.artists[ART] = load_fixture("ytmusic/artist.json")
    api.ytm.artist_albums[ART] = load_fixture("ytmusic/artist_albums.json")
    api.ytm.playlists[SONGS] = {"tracks": load_fixture("ytmusic/artist.json")["songs"]["results"]}
    body = api.get(f"/api/artist/yt/{ART}").json()
    assert body["source_status"] == {"yt": "ok"}
    assert api.get(f"/api/artist/yt/{ART}/tracks").json()["source_status"] == {"yt": "ok"}
    main._cache.clear()
    api.post("/api/settings/clear-cache")
    api.ytm.fail("get_artist_albums")
    body = api.get(f"/api/artist/yt/{ART}").json()
    assert body["source_status"] == {"yt": "degraded"}            # полный список релизов не получен
    assert [a["id"] for a in body["albums"]] == ["MPREb_alb00000001", "MPREb_sgl00000001"]


def http_error(status: int) -> ExtractorError:
    resp = Response(io.BytesIO(b""), "https://api-v2.soundcloud.com/users/555001/albums", {}, status=status)
    return ExtractorError("HTTP Error", cause=YdlHTTPError(resp))


@pytest.mark.parametrize("albums, status", [
    ({"collection": []}, "ok"),
    (http_error(404), "ok"),                                       # альбомов нет — это не сбой
    (http_error(503), "degraded"),
    (RuntimeError("неожиданный ответ"), "degraded"),
])
def test_soundcloud_artist_albums_status(api, albums, status):
    api.sc.responses["users/555001"] = load_fixture("soundcloud/user.json")
    api.sc.responses["users/555001/albums"] = albums
    api.sc.responses["users/555001/tracks"] = {"collection": []}
    body = api.get("/api/artist/sc/555001").json()
    assert body["source_status"] == {"sc": status} and body["albums"] == []
    assert not providers.sc.breaker.is_open()                   # необязательная часть страницы паузу не открывает


def test_album_page_ok_and_fallback_is_degraded(api):
    api.ytm.albums["MPREb_alb00000001"] = load_fixture("ytmusic/album.json")
    assert api.get("/api/album/yt/MPREb_alb00000001").json()["source_status"] == {"yt": "ok"}
    api.ytm.fail("get_album")
    api.ytm.playlists["OLAK5uy_polar"] = {"title": "Polar Nights", "author": {"name": "Aurora Wave"}, "thumbnails": [],
                                          "tracks": load_fixture("ytmusic/album.json")["tracks"][:1]}
    body = api.get("/api/album/yt/MPREb_alb00000001", params={"pl": "OLAK5uy_polar"}).json()
    assert body["source_status"] == {"yt": "degraded"} and [t["id"] for t in body["tracks"]] == ["AAAAAAAAAA1"]


def test_album_page_sc_ok(api):
    api.sc.responses["playlists/3000000001"] = load_fixture("soundcloud/playlist.json")
    api.sc.responses["tracks"] = load_fixture("soundcloud/tracks_batch.json")
    assert api.get("/api/album/sc/3000000001").json()["source_status"] == {"sc": "ok"}


# ---------------------------------------------------------------------------
# Сводка состояния источников
# ---------------------------------------------------------------------------
def test_sources_report(api):
    body = api.get("/api/sources").json()
    assert set(body["sources"]) == {"yt", "sc"}
    yt, sc = body["sources"]["yt"], body["sources"]["sc"]
    assert yt["status"] == sc["status"] == "ok" and yt["search_enabled"] and sc["search_enabled"]
    assert yt["cooldown_seconds"] == 45 and sc["cooldown_seconds"] == 300
    assert yt["pool"]["workers"] == 4 and yt["pool"]["max_pending"] == 8
    assert sc["pool"]["workers"] == 2 and sc["pool"]["max_pending"] == 4
    assert sc["capabilities"]["attribution_required"] is True and yt["capabilities"]["radio"] is True
    api.sc.search_error = RuntimeError("down")
    api.get("/api/search", params={"q": "x", "sources": "sc"})
    sc = api.get("/api/sources").json()["sources"]["sc"]
    assert sc["status"] == "cooldown" and sc["last_error"] == "internal" and 290 < sc["cooldown_remaining"] <= 300
    text = json.dumps(api.get("/api/sources").json())
    assert "client_id" not in text and "http" not in text
