"""Каталог (backend/catalog.py): поиск артистов и альбомов, страницы артиста и альбома, изоляция сбоев источников."""
from __future__ import annotations

import pytest

import catalog
from wavetest.fakes import load_fixture

ART = "UCaurorawave00000000001"
SONGS = "VLOLAKsongs000000000000001"


@pytest.fixture
def yt_artist(api):
    api.ytm.artists[ART] = load_fixture("ytmusic/artist.json")
    api.ytm.artist_albums[ART] = load_fixture("ytmusic/artist_albums.json")
    api.ytm.playlists[SONGS] = {"tracks": load_fixture("ytmusic/artist.json")["songs"]["results"]}
    return api


# ---------------------------------------------------------------------------
# Поиск артистов и альбомов
# ---------------------------------------------------------------------------
def test_search_artists_yt(api):
    api.ytm.search_results[("artists", "*")] = load_fixture("ytmusic/search_artists.json")
    r = api.get("/api/search/artists", params={"q": "aurora"})
    assert r.status_code == 200
    assert [a["id"] for a in r.json()["items"]] == [ART, "UCnoname0000000000000001"]
    assert r.json()["has_more"] is False


def test_search_albums_yt_pagination(api):
    api.ytm.search_results[("albums", "*")] = [
        {"browseId": f"MPREb_{i}", "title": f"Album {i}", "artists": [], "thumbnails": []} for i in range(30)]
    r = api.get("/api/search/albums", params={"q": "x", "limit": 10, "offset": 10})
    assert [a["id"] for a in r.json()["items"]] == [f"MPREb_{i}" for i in range(10, 20)]
    assert r.json()["has_more"] is True
    assert ("search", "x", "albums", 20) in api.ytm.calls


def test_search_albums_sc(api):
    api.sc.responses["search/albums"] = load_fixture("soundcloud/search_albums.json")
    r = api.get("/api/search/albums", params={"q": "mornings", "source": "sc", "limit": 5, "offset": 5})
    assert r.status_code == 200
    assert [a["id"] for a in r.json()["items"]] == ["3000000001", "3000000002"]
    assert r.json()["has_more"] is True
    assert ("api_get", "search/albums", {"q": "mornings", "limit": 5, "offset": 5, "linked_partitioning": 1}) in api.sc.calls


@pytest.mark.parametrize("path, params", [
    ("/api/search/tracks", {"q": "x"}),
    ("/api/search/artists", {"q": "x", "source": "spotify"}),
])
def test_search_cards_validation(api, path, params):
    assert api.get(path, params=params).status_code == 400


def test_soundcloud_catalog_failure_is_isolated(api):
    """
    Сбой SoundCloud даёт понятную 502 на его запрос, а YouTube Music продолжает работать.
    S7: `detail` называет источник (было «Источник временно недоступен…»), `code` и `source` — аддитивно.
    """
    api.sc.responses["search/users"] = RuntimeError("client_id expired")
    api.ytm.search_results[("artists", "*")] = load_fixture("ytmusic/search_artists.json")
    api.ytm.songs["*"] = load_fixture("ytmusic/search_songs.json")
    r = api.get("/api/search/artists", params={"q": "x", "source": "sc"})
    assert r.status_code == 502
    assert r.json() == {"detail": "SoundCloud временно недоступен. Попробуйте позже.", "code": "internal", "source": "sc"}
    assert api.get("/api/search/artists", params={"q": "x", "source": "yt"}).status_code == 200
    assert api.get("/api/search", params={"q": "x", "sources": "yt"}).status_code == 200


# ---------------------------------------------------------------------------
# Страница артиста
# ---------------------------------------------------------------------------
def test_artist_page_yt(yt_artist):
    r = yt_artist.get(f"/api/artist/yt/{ART}")
    assert r.status_code == 200
    body = r.json()
    assert body["artist"] == {"name": "Aurora Wave", "cover": "https://lh3.googleusercontent.com/artist=w544-h544-p-l90-rj",
                              "subtitle": "1,2 млн подписчиков"}
    # альбомы и синглы с главной + полный список, без дублей и без элементов без browseId
    assert [a["id"] for a in body["albums"]] == ["MPREb_alb00000001", "MPREb_sgl00000001", "MPREb_alb00000002"]
    assert body["albums"][0]["playlist_id"] == "OLAK5uy_polar"
    assert [a["id"] for a in body["related"]] == ["UCneighbour000000000001"]
    assert [t["id"] for t in body["tracks"]] == ["AAAAAAAAAA1", "AAAAAAAAAA4"]
    assert body["has_more"] is False


def test_artist_page_is_cached(yt_artist):
    yt_artist.get(f"/api/artist/yt/{ART}")
    yt_artist.get(f"/api/artist/yt/{ART}")
    assert yt_artist.ytm.count("get_artist") == 1


def test_artist_full_release_list_failure_is_tolerated(yt_artist):
    yt_artist.ytm.fail("get_artist_albums")
    r = yt_artist.get(f"/api/artist/yt/{ART}")
    assert r.status_code == 200
    assert [a["id"] for a in r.json()["albums"]] == ["MPREb_alb00000001", "MPREb_sgl00000001"]


def test_artist_tracks_pagination_grows_cache(yt_artist):
    many = [{"videoId": f"EEEEEEEEE{i:02d}", "title": f"s{i}", "artists": [{"name": "Aurora Wave", "id": ART}]}
            for i in range(100)]
    yt_artist.ytm.playlists[SONGS] = {"tracks": many}
    p1 = yt_artist.get(f"/api/artist/yt/{ART}/tracks", params={"offset": 0, "limit": 20}).json()
    assert [t["id"] for t in p1["tracks"]] == [f"EEEEEEEEE{i:02d}" for i in range(20)] and p1["has_more"] is True
    p3 = yt_artist.get(f"/api/artist/yt/{ART}/tracks", params={"offset": 40, "limit": 20}).json()
    assert [t["id"] for t in p3["tracks"]] == [f"EEEEEEEEE{i:02d}" for i in range(40, 60)] and p3["has_more"] is True
    limits = [c[2] for c in yt_artist.ytm.calls if c[0] == "get_playlist"]
    assert limits == [40, 120]                                  # кэш песен растёт по мере прокрутки


def test_artist_page_sc(api):
    api.sc.responses["users/555001"] = load_fixture("soundcloud/user.json")
    api.sc.responses["users/555001/albums"] = RuntimeError("no albums endpoint")    # у многих артистов альбомов нет
    api.sc.responses["users/555001/tracks"] = {"collection": load_fixture("soundcloud/search_tracks.json")["collection"],
                                                "next_href": "https://api-v2.soundcloud.com/next"}
    r = api.get("/api/artist/sc/555001")
    assert r.status_code == 200
    body = r.json()
    assert body["artist"]["name"] == "indie-uploader" and body["albums"] == [] and body["related"] == []
    assert [t["id"] for t in body["tracks"]] == ["1000000001", "1000000002", "1000000006"]
    assert body["has_more"] is True


@pytest.mark.parametrize("path", ["/api/artist/yt/UC%2E%2E%2Fx", "/api/artist/spotify/abc", f"/api/artist/yt/{'a' * 65}"])
def test_artist_validation(api, path):
    assert api.get(path).status_code in (400, 404)
    assert not api.ytm.calls and not api.sc.calls


def test_artist_source_failure_gives_502(api):
    api.ytm.fail("get_artist")
    r = api.get(f"/api/artist/yt/{ART}")
    assert r.status_code == 502                     # S7: источник в тексте, плюс `code` и `source`
    assert r.json() == {"detail": "YouTube Music временно недоступен. Попробуйте позже.", "code": "internal",
                        "source": "yt"}
    assert not catalog._artist_cache


# ---------------------------------------------------------------------------
# Страница альбома
# ---------------------------------------------------------------------------
def test_album_page_yt(api):
    api.ytm.albums["MPREb_alb00000001"] = load_fixture("ytmusic/album.json")
    r = api.get("/api/album/yt/MPREb_alb00000001")
    assert r.status_code == 200
    body = r.json()
    assert body["album"]["title"] == "Polar Nights" and body["album"]["year"] == "2024"
    assert body["album"]["track_count"] == 3 and body["album"]["playlist_id"] == "OLAK5uy_polar"
    assert [t["id"] for t in body["tracks"]] == ["AAAAAAAAAA1", "AAAAAAAAAA5"]
    assert body["unavailable"] == 1                                       # трек без videoId
    cover = "https://lh3.googleusercontent.com/alb1=w544-h544-l90-rj"
    assert all(t["cover"] == cover and t["album"] == "Polar Nights" for t in body["tracks"])
    assert body["tracks"][1]["artist"] == "Aurora Wave"                  # у трека нет артистов -> артист альбома


def test_album_page_yt_falls_back_to_playlist(api):
    api.ytm.fail("get_album")
    api.ytm.playlists["OLAK5uy_polar"] = {"title": "Polar Nights", "author": {"name": "Aurora Wave"},
                                          "thumbnails": [], "trackCount": 1,
                                          "tracks": load_fixture("ytmusic/album.json")["tracks"][:1]}
    r = api.get("/api/album/yt/MPREb_alb00000001", params={"pl": "OLAK5uy_polar"})
    assert r.status_code == 200
    assert [t["id"] for t in r.json()["tracks"]] == ["AAAAAAAAAA1"]
    assert r.json()["album"]["artist"] == "Aurora Wave"


def test_album_page_yt_failure_without_playlist(api):
    api.ytm.fail("get_album")
    assert api.get("/api/album/yt/MPREb_alb00000001").status_code == 502


def test_album_page_sc_fills_stub_tracks(api):
    api.sc.responses["playlists/3000000001"] = load_fixture("soundcloud/playlist.json")
    api.sc.responses["tracks"] = load_fixture("soundcloud/tracks_batch.json")
    r = api.get("/api/album/sc/3000000001")
    assert r.status_code == 200
    assert [t["id"] for t in r.json()["tracks"]] == ["1000000001", "1000000011"]
    assert r.json()["unavailable"] == 2                                   # превью Go+ и трек, не пришедший из API
    assert ("api_get", "tracks", {"ids": "1000000011,1000000012,1000000013"}) in api.sc.calls


def test_album_invalid_playlist_param(api):
    assert api.get("/api/album/yt/MPREb_alb00000001", params={"pl": "../x"}).status_code == 400
