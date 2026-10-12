"""Карточки артистов и альбомов каталога (backend/catalog.py): единый формат для YouTube Music и SoundCloud."""
from __future__ import annotations

import pytest

import catalog
from wavetest.fakes import FakeYTMusic, load_fixture

ARTIST_KEYS = {"id", "source", "kind", "name", "cover", "subtitle"}
ALBUM_KEYS = {"id", "source", "kind", "title", "artist", "year", "cover", "track_count", "playlist_id"}


def test_artist_card_shape():
    card = catalog._artist_card("sc", 555001, "Name", None)
    assert set(card) == ARTIST_KEYS
    assert card == {"id": "555001", "source": "sc", "kind": "artist", "name": "Name", "cover": None, "subtitle": None}


@pytest.mark.parametrize("year, expected", [
    ("2024-05-01T00:00:00Z", "2024"),   # дата ISO обрезается до года
    ("2019", "2019"),
    ("", None),
    (None, None),
])
def test_album_card_year(year, expected):
    card = catalog._album_card("yt", "MPREb_x", "T", "A", year, None)
    assert set(card) == ALBUM_KEYS
    assert card["year"] == expected


def test_album_card_placeholder_title_and_str_id():
    card = catalog._album_card("sc", 3000000001, None, None, None, None, count=4, playlist_id=None)
    assert card["id"] == "3000000001" and card["title"] == "Без названия" and card["track_count"] == 4


def test_image_helpers():
    assert catalog._yt_img([{"url": "https://lh3.googleusercontent.com/a=w60-h60-p"}]) == "https://lh3.googleusercontent.com/a=w544-h544-p"
    assert catalog._yt_img([]) is None and catalog._yt_img(None) is None
    assert catalog._sc_img("https://i1.sndcdn.com/a-large.jpg") == "https://i1.sndcdn.com/a-t500x500.jpg"
    assert catalog._sc_img(None) is None


def test_sc_artist_followers_formatting():
    users = load_fixture("soundcloud/search_users.json")["collection"]
    a = catalog._sc_artist(users[0])
    assert a["subtitle"] == "1 234 567 подписчиков"
    assert a["cover"] == "https://i1.sndcdn.com/avatars-u1-t500x500.jpg"
    b = catalog._sc_artist(users[1])
    assert b["name"] == "—" and b["subtitle"] is None and b["cover"] is None


def test_sc_album_dates_and_artwork_fallback():
    albums = load_fixture("soundcloud/search_albums.json")["collection"]
    a = catalog._sc_album(albums[0])
    assert (a["year"], a["artist"], a["track_count"]) == ("2025", "indie-uploader", 4)
    assert a["cover"] == "https://i1.sndcdn.com/avatars-u1-t500x500.jpg"      # нет artwork -> аватар
    b = catalog._sc_album(albums[1])
    assert b["year"] == "2019" and b["cover"] == "https://i1.sndcdn.com/artworks-old-t500x500.png"


def test_yt_search_artists():
    ytm = FakeYTMusic()
    ytm.search_results[("artists", "*")] = load_fixture("ytmusic/search_artists.json")
    cards = catalog._yt_search(ytm, "aurora", "artists", 20)
    assert [c["id"] for c in cards] == ["UCaurorawave00000000001", "UCnoname0000000000000001"]   # без browseId — пропуск
    assert cards[0]["subtitle"] == "1,2 млн подписчиков"
    assert cards[0]["cover"] == "https://lh3.googleusercontent.com/artist=w544-h544-p-l90-rj"
    assert cards[1]["name"] == "—" and cards[1]["subtitle"] is None


def test_yt_search_albums():
    ytm = FakeYTMusic()
    ytm.search_results[("albums", "*")] = load_fixture("ytmusic/search_albums.json")
    cards = catalog._yt_search(ytm, "polar", "albums", 20)
    assert cards[0]["artist"] == "Aurora Wave, Guest"
    assert cards[0]["playlist_id"] == "OLAK5uy_polar" and cards[0]["year"] == "2024"
    assert cards[1]["title"] == "Без названия" and cards[1]["year"] is None
    assert cards[1]["playlist_id"] == "OLAK5uy_untitled"         # audioPlaylistId как запасной вариант


@pytest.mark.parametrize("source, id_, ok", [
    ("yt", "UCaurorawave00000000001", True),
    ("sc", "555001", True),
    ("yt", "a" * 64, True),
    ("yt", "a" * 65, False),
    ("yt", "../etc", False),
    ("yt", "", False),
    ("spotify", "abc", False),
])
def test_check_ids(source, id_, ok):
    from fastapi import HTTPException
    if ok:
        catalog._check(source, id_)
    else:
        with pytest.raises(HTTPException) as e:
            catalog._check(source, id_)
        assert e.value.status_code == 400
