"""
Cooldown источников за пределами поиска треков (S7, находка M3): каталог и рекомендации.

Пауза после сбоя действует на все операции источника: пока она идёт, к источнику не уходит ни одного
запроса, ответ — сразу и с понятной причиной. Какие сбои её открывают — providers/base.py (TRIP_*).
"""
from __future__ import annotations

import threading
import time

from ytmusicapi.exceptions import YTMusicServerError

import catalog
import providers
from wavetest.fakes import load_fixture

ART = "UCaurorawave00000000001"


def test_youtube_in_cooldown_is_not_asked_for_catalog_pages(api):
    api.ytm.artists[ART] = load_fixture("ytmusic/artist.json")
    providers.yt.breaker.trip("timeout")
    r = api.get(f"/api/artist/yt/{ART}")
    assert r.status_code == 502 and r.json()["code"] == "unavailable" and r.json()["source"] == "yt"
    assert r.json()["detail"].startswith("YouTube Music временно недоступен после сбоя. Повторная попытка")
    assert api.get("/api/search/albums", params={"q": "x"}).status_code == 502
    assert api.get("/api/album/yt/MPREb_alb00000001").status_code == 502
    assert not api.ytm.calls


def test_youtube_in_cooldown_fails_recommendations_fast(api):
    providers.yt.breaker.trip("rate_limited")
    t0 = time.monotonic()
    body = api.get("/api/recommendations").json()
    assert time.monotonic() - t0 < 0.5 and not api.ytm.calls
    # Статусы рекомендаций — как в S2 (ok | partial | failed), не меняются
    assert body["tracks"] == [] and body["failed_sources"] == ["yt"] and body["source_status"] == {"yt": "failed"}


def test_soundcloud_search_failure_pauses_its_catalog_but_not_youtube(api):
    api.sc.search_error = RuntimeError("SoundCloud API changed")
    api.ytm.search_results[("artists", "*")] = load_fixture("ytmusic/search_artists.json")
    api.get("/api/search", params={"q": "x", "sources": "sc"})
    r = api.get("/api/search/artists", params={"q": "x", "source": "sc"})
    assert r.status_code == 502 and r.json()["code"] == "unavailable" and "Повторная попытка" in r.json()["detail"]
    assert api.sc.count("api_get") == 0
    assert api.get("/api/search/artists", params={"q": "x", "source": "yt"}).status_code == 200


def test_page_not_found_does_not_pause_the_source(api):
    api.ytm.fail("get_album", YTMusicServerError("Server returned HTTP 404: Not Found.\n"))
    api.ytm.songs["*"] = []
    r = api.get("/api/album/yt/MPREb_gone000000001")
    assert r.status_code == 502 and r.json()["code"] == "not_found"
    assert r.json()["detail"] == "YouTube Music: страница не найдена или удалена."
    assert not providers.yt.breaker.is_open()
    assert api.get("/api/search", params={"q": "x", "sources": "yt"}).status_code == 200
    assert api.ytm.count("search") == 1


def test_broken_single_page_does_not_pause_the_source(api):
    """Внутренняя ошибка на странице артиста (её не удалось разобрать) не выключает источник целиком."""
    api.ytm.fail("get_artist")
    api.ytm.songs["*"] = []
    assert api.get(f"/api/artist/yt/{ART}").json()["code"] == "internal"
    assert not providers.yt.breaker.is_open()
    assert api.get("/api/search", params={"q": "x", "sources": "yt"}).status_code == 200


def test_page_timeout_pauses_the_source(api, monkeypatch):
    gate = threading.Event()
    api.ytm.gates["get_artist"] = gate
    monkeypatch.setattr(catalog, "CATALOG_TIMEOUT", 0.2)
    try:
        assert api.get(f"/api/artist/yt/{ART}").json()["code"] == "timeout"
    finally:
        gate.set()
    assert providers.yt.breaker.is_open() and providers.yt.breaker.last_kind == "timeout"
    assert api.get("/api/search", params={"q": "x", "sources": "yt"}).status_code == 502
    assert api.ytm.count("search") == 0


def test_successful_page_after_pause_clears_it(api):
    api.ytm.artists[ART] = load_fixture("ytmusic/artist.json")
    api.ytm.artist_albums[ART] = load_fixture("ytmusic/artist_albums.json")
    api.ytm.playlists["VLOLAKsongs000000000000001"] = {"tracks": []}
    providers.yt.breaker.trip("timeout")
    providers.yt.breaker.until = time.monotonic() - 1
    assert api.get(f"/api/artist/yt/{ART}").status_code == 200
    assert not providers.yt.breaker.is_open() and providers.yt.breaker.last_kind is None
