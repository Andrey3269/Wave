"""Проверка снимков трека и адресов (backend/domain/snapshot.py, S13)."""
from __future__ import annotations

import json

import pytest

from domain import snapshot
from domain.models import TRACK_ID_RE
from streaming import resolver

A = "AAAAAAAAAA1"


@pytest.mark.parametrize("source, tid, ok", [("yt", A, True), ("yt", "a-b_c123456", True), ("yt", "short", False),
                                             ("yt", A + "1", False), ("yt", "a/b", False), ("sc", "1", True),
                                             ("sc", "1" * 15, True), ("sc", "1" * 16, False), ("sc", "12a", False),
                                             ("spotify", A, False), (None, A, False), ("yt", None, False),
                                             ("yt", f"{A}\n", False)])
def test_valid_track(source, tid, ok):
    assert snapshot.valid_track(source, tid) is ok


def test_one_definition_of_track_id_format():
    """Резолвер потоков проверяет id тем же выражением, что и снимки (поток и снимок не разойдутся)."""
    assert resolver.VIDEO_ID_RE is TRACK_ID_RE["yt"] and resolver.SC_ID_RE is TRACK_ID_RE["sc"]
    assert resolver.SOURCES["yt"].id_re is TRACK_ID_RE["yt"] and resolver.SOURCES["sc"].id_re is TRACK_ID_RE["sc"]


@pytest.mark.parametrize("host, ok", [("lh3.googleusercontent.com", True), ("googleusercontent.com", True),
                                      ("i.ytimg.com", True), ("yt3.ggpht.com", True), ("i1.sndcdn.com", True),
                                      ("LH3.GoogleUserContent.com", True), ("lh3.googleusercontent.com.", True),
                                      ("evilgoogleusercontent.com", False), ("googleusercontent.com.evil.example", False),
                                      ("sndcdn.com.evil", False), ("", False), (None, False)])
def test_cover_host_allowed(host, ok):
    assert snapshot.cover_host_allowed(host) is ok


@pytest.mark.parametrize("url, ok", [
    ("https://lh3.googleusercontent.com/x=w544-h544", True),
    ("https://lh3.googleusercontent.com:443/x", True),
    (f"/api/local/yt/{A}/cover", True),
    ("/api/local/sc/1000000001/cover", True),
    ("http://lh3.googleusercontent.com/x", False),
    ("https://lh3.googleusercontent.com:8443/x", False),
    ("https://a:b@lh3.googleusercontent.com/x", False),
    ("https://lh3.googleusercontent.com/a b", False),
    ("https://lh3.googleusercontent.com/a\x00", False),
    ("https://lh3.googleusercontent.com\\@evil.example/x", False),
    ("https://[::1]/x", False),
    ("https://lh3.googleusercontent.com:99999/x", False),
    ("https://lh3.googleusercontent.com/" + "a" * 2048, False),
    ("/api/local/yt/../cover", False),
    ("/api/local/yt/short/cover", False),
    (f"/api/local/yt/{A}/cover?x=1", False),
    (f"/api/local/yt/{A}/audio", False),
    ("javascript:alert(1)", False),
    ("data:image/png;base64,AA", False),
    (None, False), (5, False), ("", False),
])
def test_safe_cover(url, ok):
    assert snapshot.safe_cover(url) == (url if ok else None)


def test_remote_only_cover_for_downloader():
    assert snapshot.safe_cover(f"/api/local/yt/{A}/cover", local=False) is None
    assert snapshot.safe_cover("https://i1.sndcdn.com/a.jpg", local=False) == "https://i1.sndcdn.com/a.jpg"


@pytest.mark.parametrize("url, ok", [("https://music.youtube.com/watch?v=1", True), ("https://youtu.be/x", True),
                                     ("https://soundcloud.com/a/b", True), ("http://soundcloud.com/a", False),
                                     ("https://soundcloud.com.evil.example/a", False), ("javascript:x", False),
                                     ("https://u@soundcloud.com/a", False), (None, False)])
def test_safe_link(url, ok):
    assert snapshot.safe_link(url) == (url if ok else None)


def test_read_snapshot_recomputes_and_rechecks_but_keeps_other_fields():
    raw = json.dumps({"id": A, "source": "yt", "title": "T", "stream_url": "https://evil.example/a",
                      "cover": "https://evil.example/c", "url": "javascript:x", "custom": 1})
    assert snapshot.read_snapshot(raw) == {"id": A, "source": "yt", "title": "T", "stream_url": f"/api/stream/yt/{A}",
                                           "cover": None, "url": None, "custom": 1}


def test_read_snapshot_does_not_add_missing_optional_fields():
    """Старый снимок без cover/url читается без новых ключей (контракт «читается как есть»)."""
    assert snapshot.read_snapshot({"id": A, "source": "yt", "stream_url": "x"}) == {
        "id": A, "source": "yt", "stream_url": f"/api/stream/yt/{A}"}


@pytest.mark.parametrize("raw", ["", "not json", "[]", "null", "5", '{"id": "a/b", "source": "yt"}',
                                 '{"id": "1", "source": "local"}', "[" * 100_000])
def test_read_snapshot_rejects_bad_rows(raw):
    assert snapshot.read_snapshot(raw, "favorites", "k") is None
