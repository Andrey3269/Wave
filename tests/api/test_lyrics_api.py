"""/api/lyrics: кэш, временные сбои LRCLIB, текст скачанного трека с диска."""
from __future__ import annotations

import json

import pytest

import library
from wavetest.fakes import load_fixture

PARAMS = {"artist": "Aurora Wave", "title": "Northern Lights", "duration": 225}


def test_found_lyrics_are_cached(api):
    api.net.lyrics = (200, load_fixture("lrclib/get_synced.json"))
    a = api.get("/api/lyrics", params=PARAMS)
    b = api.get("/api/lyrics", params={"artist": " aurora wave ", "title": "NORTHERN LIGHTS"})   # ключ без регистра и пробелов
    assert a.status_code == 200 and a.json()["synced"][0] == {"t": 1.5, "text": "Первая строка"}
    assert b.json() == a.json()
    assert api.net.lyrics_calls == 1


def test_not_found_is_cached_too(api):
    api.get("/api/lyrics", params=PARAMS)
    r = api.get("/api/lyrics", params=PARAMS)
    assert r.json() == {"synced": [], "plain": None, "provider": "lrclib"}
    assert api.net.lyrics_calls == 1


def test_temporary_failure_is_not_cached(api):
    api.net.lyrics = (503, {"error": "busy"})
    r = api.get("/api/lyrics", params=PARAMS)
    assert r.status_code == 200 and r.json() == {"synced": [], "plain": None, "provider": "lrclib"}
    api.net.lyrics = (200, load_fixture("lrclib/get_synced.json"))
    assert api.get("/api/lyrics", params=PARAMS).json()["plain"] == "Первая строка\nВторая строка"
    assert api.net.lyrics_calls == 2


def test_downloaded_track_lyrics_read_from_disk(api):
    folder = api.profile.downloads / "Aurora Wave - Northern Lights [yt-AAAAAAAAAA1]"
    folder.mkdir()
    (folder / "audio.m4a").write_bytes(b"x")
    saved = {"synced": [{"t": 3.0, "text": "с диска"}], "plain": "с диска", "provider": "lrclib"}
    (folder / "lyrics.json").write_text(json.dumps(saved, ensure_ascii=False), encoding="utf-8")
    library.add(source="yt", track_id="AAAAAAAAAA1", title="Northern Lights", artist="Aurora Wave", album=None,
                duration=225, folder=folder.name, audio="audio.m4a", cover=None, lyrics="lyrics.json", size=1,
                created_at=1.0, root=str(api.profile.downloads))
    r = api.get("/api/lyrics", params={**PARAMS, "source": "yt", "id": "AAAAAAAAAA1"})
    assert r.json() == saved
    assert api.net.lyrics_calls == 0                     # офлайн: сеть не нужна


def test_downloaded_track_without_lyrics_asks_service(api):
    folder = api.profile.downloads / "x"
    folder.mkdir()
    (folder / "audio.m4a").write_bytes(b"x")
    library.add(source="yt", track_id="AAAAAAAAAA1", title="t", artist="a", album=None, duration=1, folder="x",
                audio="audio.m4a", cover=None, lyrics=None, size=1, created_at=1.0, root=str(api.profile.downloads))
    api.get("/api/lyrics", params={**PARAMS, "source": "yt", "id": "AAAAAAAAAA1"})
    assert api.net.lyrics_calls == 1


@pytest.mark.parametrize("params", [{}, {"artist": "a"}, {"title": "t"}])
def test_required_params(api, params):
    assert api.get("/api/lyrics", params=params).status_code == 422


@pytest.mark.xfail(strict=True, reason="M8: не-JSON ответ LRCLIB даёт HTTP 500 вместо пустого результата")
def test_non_json_service_response_is_handled(api):
    api.net.lyrics = (200, "<html>maintenance</html>")
    r = api.get("/api/lyrics", params=PARAMS)
    assert r.status_code == 200 and r.json() == {"synced": [], "plain": None, "provider": "lrclib"}
