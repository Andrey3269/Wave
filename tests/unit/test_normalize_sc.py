"""Нормализация ответов SoundCloud API v2 в модель Track: backend/main.py:_normalize_sc."""
from __future__ import annotations

import pytest

import main
from wavetest.fakes import load_fixture


def _items() -> list[dict]:
    return load_fixture("soundcloud/search_tracks.json")["collection"]


def test_full_track_item():
    t = main._normalize_sc(_items()[0])
    assert t.model_dump() == {
        "id": "1000000001",                      # id всегда строка
        "source": "sc",
        "title": "Indie Morning",
        "artist": "Indie Artist",                # publisher_metadata.artist приоритетнее username
        "artists": ["Indie Artist"],
        "album": "Mornings EP",
        "duration": 188,                         # full_duration 187 500 мс, а не 30-секундный duration
        "cover": "https://i1.sndcdn.com/artworks-abc-t500x500.jpg",
        "explicit": True,
        "stream_url": "/api/stream/sc/1000000001",
        "artist_id": "555001",                   # страница артиста = страница загрузившего
        "artist_ids": ["555001"],
        "plays": 12345,
        "released": "2025-03-01T10:00:00Z",      # display_date приоритетнее created_at
        "url": "https://soundcloud.com/indie-uploader/indie-morning",
    }


def test_uploader_fallbacks():
    t = main._normalize_sc(_items()[1])
    assert t.artist == "bedroom-producer"
    assert t.duration == 200                     # 200 400 мс -> 200 с
    assert t.cover == "https://i1.sndcdn.com/avatars-u2-t500x500.jpg"   # нет artwork -> аватар
    assert t.released == "2024-01-01T00:00:00Z"
    assert t.album is None and t.plays is None and t.explicit is False


@pytest.mark.parametrize("index, why", [
    (2, "policy=SNIP: только 30-секундное превью"),
    (3, "policy=BLOCK: недоступен в регионе"),
    (4, "streamable=false"),
    (5, "kind=playlist"),
    (6, "нет id"),
])
def test_unplayable_or_foreign_items_are_skipped(index, why):
    assert main._normalize_sc(_items()[index]) is None, why


def test_minimal_item_gets_placeholders():
    t = main._normalize_sc(_items()[7])
    assert (t.id, t.title, t.artist) == ("1000000006", "Без названия", "Неизвестный исполнитель")
    assert t.artist_id is None and t.artist_ids == []
    assert t.duration is None and t.cover is None


def test_zero_duration_is_unknown():
    assert main._normalize_sc({"id": 1, "duration": 0}).duration is None


def test_artwork_not_large_is_kept_as_is():
    t = main._normalize_sc({"id": 1, "artwork_url": "https://i1.sndcdn.com/artworks-x-t300x300.jpg"})
    assert t.cover == "https://i1.sndcdn.com/artworks-x-t300x300.jpg"
