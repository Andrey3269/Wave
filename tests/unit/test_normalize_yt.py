"""Нормализация ответов YouTube Music (ytmusicapi) в модель Track: backend/main.py:_normalize_yt."""
from __future__ import annotations

import pytest

import main
import recs
from wavetest.fakes import FakeYTMusic, load_fixture


def _songs() -> list[dict]:
    return load_fixture("ytmusic/search_songs.json")


def test_full_song_item():
    t = main._normalize_yt(_songs()[0])
    assert t is not None
    assert t.model_dump() == {
        "id": "AAAAAAAAAA1",
        "source": "yt",
        "title": "Northern Lights",
        "artist": "Aurora Wave, Guest Singer, Third Feat",
        "artists": ["Aurora Wave", "Guest Singer", "Third Feat"],
        "album": "Polar Nights",
        "album_id": "MPREb_alb00000001",             # S10: browseId альбома из {"name", "id"}
        "duration": 225,
        "cover": "https://lh3.googleusercontent.com/song1=w544-h544-l90-rj",
        "explicit": True,
        "stream_url": "/api/stream/yt/AAAAAAAAAA1",
        "artist_id": "UCaurorawave00000000001",
        # id КАЖДОГО артиста в том же порядке, что и artists; у второго id нет
        "artist_ids": ["UCaurorawave00000000001", None, "UCthirdfeat000000000003"],
        "plays": None,
        "released": None,
        "url": "https://music.youtube.com/watch?v=AAAAAAAAAA1",
    }


def test_item_without_video_id_is_skipped():
    assert main._normalize_yt(_songs()[2]) is None
    assert main._normalize_yt({"title": "x", "videoId": ""}) is None


def test_empty_fields_get_placeholders():
    t = main._normalize_yt(_songs()[3])
    assert t.title == "Без названия"
    assert t.artist == "Неизвестный исполнитель"
    assert t.artists == [] and t.artist_ids == [] and t.artist_id is None
    assert t.album == "Singles"
    assert t.duration is None            # "bad:duration" не разбирается
    assert t.cover is None               # thumbnails = null
    assert t.explicit is False


def test_cover_without_size_suffix_is_kept():
    t = main._normalize_yt(_songs()[1])
    assert t.cover == "https://i.ytimg.com/vi/AAAAAAAAAA2/hqdefault.jpg"
    assert t.album is None               # album = null


@pytest.mark.parametrize("item, expected", [
    ({"duration_seconds": 225, "duration": "9:99"}, 225),       # duration_seconds приоритетнее
    ({"duration": "3:45"}, 225),
    ({"duration": "1:02:10"}, 3730),
    ({"duration": "45"}, 45),
    ({"duration": "abc"}, None),
    ({"duration": ""}, None),
    ({}, None),
    ({"duration_seconds": "225", "duration": "0:10"}, 10),      # строка в duration_seconds игнорируется
])
def test_parse_duration(item, expected):
    assert main._parse_duration(item) == expected


@pytest.mark.parametrize("album, expected", [
    ({"name": "Dict Album", "id": "MPREb_x"}, "Dict Album"),   # search()
    ("String Album", "String Album"),                           # get_album(): название строкой
    (None, None),
    (["unexpected"], None),
])
def test_album_shapes(album, expected):
    t = main._normalize_yt({"videoId": "AAAAAAAAAA1", "title": "t", "album": album})
    assert t.album == expected


def test_artists_without_names_are_dropped():
    t = main._normalize_yt({"videoId": "AAAAAAAAAA1", "title": "t",
                            "artists": [{"name": None, "id": "UCx"}, {"name": "Real", "id": None}]})
    assert t.artists == ["Real"]
    assert t.artist_ids == [None]
    assert t.artist_id is None           # первого артиста с именем И id нет


def test_best_cover_takes_last_thumbnail():
    thumbs = [{"url": "https://lh3.googleusercontent.com/a=w60-h60"}, {"url": "https://lh3.googleusercontent.com/a=w226-h226"}]
    assert main._best_cover(thumbs) == "https://lh3.googleusercontent.com/a=w544-h544"
    assert main._best_cover([]) is None
    assert main._best_cover(None) is None
    assert main._best_cover([{"width": 1}]) is None


def test_search_sync_filters_and_respects_limit():
    ytm = FakeYTMusic()
    ytm.songs["q"] = _songs()
    tracks = main._search_yt_sync(ytm, "q", 500)
    assert [t.id for t in tracks] == ["AAAAAAAAAA1", "AAAAAAAAAA2", "AAAAAAAAAA3"]
    assert ytm.calls == [("search", "q", "songs", main.MAX_RESULTS)]   # глубже MAX_RESULTS не просим


def test_watch_playlist_radio_items_normalize():
    """«Радио» (get_watch_playlist) отдаёт length/thumbnail вместо duration/thumbnails: recs._yt_radio их переносит."""
    ytm = FakeYTMusic()
    ytm.watch["AAAAAAAAAA1"] = load_fixture("ytmusic/watch_playlist.json")
    tracks = [main._normalize_yt(it) for it in recs._yt_radio(ytm, "AAAAAAAAAA1", 25)]
    assert [t.id for t in tracks] == ["AAAAAAAAAA1", "RRRRRRRRRR1", "RRRRRRRRRR2"]
    assert tracks[0].duration == 225 and tracks[1].duration == 241
    assert tracks[0].cover == "https://lh3.googleusercontent.com/song1=w544-h544-l90-rj"
    assert tracks[1].album is None



@pytest.mark.parametrize("album, expected", [
    ({"name": "A", "id": "MPREb_alb00000001"}, "MPREb_alb00000001"),
    ({"name": "A", "id": None}, None),                                 # «Singles» без id
    ({"name": "A"}, None),
    ({"name": "A", "id": "OLAK5uy_polar"}, None),                       # плейлист альбома — не id альбома
    ({"name": "A", "id": "VLPLxxxx"}, None),
    ({"name": "A", "id": "MPRE../x"}, None),
    ({"name": "A", "id": 12}, None),
    ("String Album", None),                                            # get_album(): только название
])
def test_album_id_only_for_album_browse_ids(album, expected):
    """S10: id альбома берётся, только если источник его даёт и это id альбома (MPRE…), иначе None."""
    t = main._normalize_yt({"videoId": "AAAAAAAAAA1", "title": "t", "album": album})
    assert t.album_id == expected
