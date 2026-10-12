"""
Контракт Track и формы ответов, на которые опирается фронтенд (docs: 07_API_DATA_CONTRACTS, Track).
Если тест падает после изменения backend — это изменение контракта: сначала план миграции
(фронтенд, e2e-фикстуры, SQLite, документация), затем правка ожиданий здесь.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

import library
import main
from wavetest.fakes import load_fixture

TRACK_FIELDS = {"id", "source", "title", "artist", "artists", "album", "duration", "cover", "explicit",
                "stream_url", "artist_id", "artist_ids", "plays", "released", "url"}
REQUIRED = {"id", "source", "title", "artist", "artists", "stream_url"}


def test_track_model_fields():
    assert set(main.Track.model_fields) == TRACK_FIELDS
    required = {name for name, f in main.Track.model_fields.items() if f.is_required()}
    assert required == REQUIRED


@pytest.mark.parametrize("source", ["local", "spotify", "yandex", ""])
def test_track_source_is_closed_set(source):
    with pytest.raises(ValidationError):
        main.Track(id="x", source=source, title="t", artist="a", artists=[], stream_url="/x")


def test_search_response_fields():
    assert set(main.SearchResponse.model_fields) == {"query", "count", "tracks", "failed_sources", "has_more"}


def test_search_api_returns_full_track_objects(api):
    api.ytm.songs["*"] = load_fixture("ytmusic/search_songs.json")
    api.sc.tracks["*"] = load_fixture("soundcloud/search_tracks.json")["collection"]
    r = api.get("/api/search", params={"q": "northern"})
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"query", "count", "tracks", "failed_sources", "has_more"}
    assert body["count"] == len(body["tracks"]) > 0
    for t in body["tracks"]:
        assert set(t) == TRACK_FIELDS, t
        assert t["stream_url"] == f"/api/stream/{t['source']}/{t['id']}"      # относительный URL прокси
        assert isinstance(t["artists"], list) and isinstance(t["artist_ids"], list)


def test_openapi_exposes_track_schema(api):
    r = api.get("/openapi.json")
    assert r.status_code == 200
    props = r.json()["components"]["schemas"]["Track"]["properties"]
    assert set(props) == TRACK_FIELDS
    assert props["source"]["enum"] == ["yt", "sc"]


def test_downloaded_track_shape(db):
    """Скачанный трек отдаётся тем же /api/stream/{source}/{id}, но с другим набором полей (фиксируем как есть)."""
    row = {"source": "yt", "track_id": "AAAAAAAAAA1", "title": "T", "artist": "A", "album": None, "duration": 10,
           "folder": "f", "audio": "audio.m4a", "cover": "cover.jpg", "lyrics": None, "size": 3, "created_at": 1.0}
    library.add(**row)
    t = library.to_track(library._q("SELECT * FROM downloads")[0])
    assert t == {"id": "AAAAAAAAAA1", "source": "yt", "title": "T", "artist": "A", "artists": ["A"], "album": None,
                 "duration": 10, "cover": "/api/local/yt/AAAAAAAAAA1/cover", "explicit": False,
                 "stream_url": "/api/stream/yt/AAAAAAAAAA1", "downloaded": True, "size": 3, "has_lyrics": False}
