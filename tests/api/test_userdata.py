"""Избранное и история (backend/userdata.py) на временной SQLite-базе."""
from __future__ import annotations

import json
import time

import pytest

import userdata
from wavetest.fakes import track_payload

A, B, C = "AAAAAAAAAA1", "BBBBBBBBBB1", "CCCCCCCCCC1"


# ---------------------------------------------------------------------------
# Избранное
# ---------------------------------------------------------------------------
def test_favorites_add_list_remove(api):
    assert api.get("/api/favorites").json() == {"items": []}
    assert api.post("/api/favorites", json=track_payload(id_=A)).json() == {"ok": True}
    time.sleep(0.01)
    assert api.post("/api/favorites", json=track_payload("sc", "1000000001", title="SC")).status_code == 200
    items = api.get("/api/favorites").json()["items"]
    assert [(t["source"], t["id"]) for t in items] == [("sc", "1000000001"), ("yt", A)]     # новые сверху
    assert api.delete(f"/api/favorites/yt/{A}").json() == {"ok": True}
    assert [t["id"] for t in api.get("/api/favorites").json()["items"]] == ["1000000001"]


def test_favorite_repeat_add_keeps_one_and_moves_up(api):
    api.post("/api/favorites", json=track_payload(id_=A, title="Старое название"))
    time.sleep(0.01)
    api.post("/api/favorites", json=track_payload(id_=B))
    time.sleep(0.01)
    api.post("/api/favorites", json=track_payload(id_=A, title="Новое название"))
    items = api.get("/api/favorites").json()["items"]
    assert [t["id"] for t in items] == [A, B]
    assert items[0]["title"] == "Новое название"
    assert api.profile.sql("SELECT COUNT(*) FROM favorites")[0][0] == 2


def test_favorite_stored_snapshot_fields(api):
    body = track_payload(id_=A, title="Песня «ВОЛНА»", artists=["Артист", "Гость"], artist_ids=["UCa", None])
    api.post("/api/favorites", json=body)
    stored = json.loads(api.profile.sql("SELECT data FROM favorites")[0][0])
    assert stored == {k: body[k] for k in ("id", "source", "title", "artist", "artists", "album", "duration", "cover",
                                           "stream_url", "artist_id", "artist_ids", "url")}
    assert api.profile.sql("SELECT key FROM favorites") == [(f"yt:{A}",)]


def test_remove_missing_favorite_is_ok(api):
    assert api.delete("/api/favorites/yt/ZZZZZZZZZZZ").status_code == 200


@pytest.mark.parametrize("bad", [
    {"source": "local"},
    {"source": "spotify"},
    {"stream_url": None},
    {"title": None},
])
def test_favorite_validation(api, bad):
    body = track_payload(id_=A)
    body.update(bad)
    assert api.post("/api/favorites", json=body).status_code == 422
    assert api.profile.sql("SELECT COUNT(*) FROM favorites")[0][0] == 0


@pytest.mark.xfail(strict=True, reason="M6: TrackIn не содержит explicit, plays, released — при сохранении они теряются")
def test_favorite_keeps_all_track_fields(api):
    body = track_payload("sc", "1000000001", explicit=True, plays=12345, released="2025-03-01T10:00:00Z")
    api.post("/api/favorites", json=body)
    item = api.get("/api/favorites").json()["items"][0]
    assert (item.get("explicit"), item.get("plays"), item.get("released")) == (True, 12345, "2025-03-01T10:00:00Z")


@pytest.mark.xfail(strict=True, reason="M6: id не проверяется по формату источника; запись с '/' в id нельзя удалить")
def test_saved_favorite_can_always_be_removed(api):
    r = api.post("/api/favorites", json=track_payload(id_="a/b"))
    if r.status_code == 200:
        api.delete("/api/favorites/yt/a/b")
        assert api.get("/api/favorites").json()["items"] == []
    else:
        assert r.status_code == 422


def test_favorites_survive_restart(harness):
    with harness.client() as h:
        h.post("/api/favorites", json=track_payload(id_=A))
    with harness.client() as h:
        assert [t["id"] for t in h.get("/api/favorites").json()["items"]] == [A]


# ---------------------------------------------------------------------------
# История
# ---------------------------------------------------------------------------
def _play(api, id_, **over):
    r = api.post("/api/history", json=track_payload(id_=id_, **over))
    assert r.status_code == 200 and r.json() == {"ok": True}
    time.sleep(0.005)


def test_history_groups_by_track_latest_first(api):
    _play(api, A, title="A v1")
    _play(api, B)
    _play(api, A, title="A v2")
    _play(api, C)
    body = api.get("/api/history").json()
    assert [t["id"] for t in body["tracks"]] == [C, A, B]
    assert body["tracks"][1]["title"] == "A v2"            # снимок последнего прослушивания
    assert body["has_more"] is False
    assert api.profile.sql("SELECT COUNT(*) FROM history")[0][0] == 4     # все прослушивания хранятся


def test_history_pagination(api):
    ids = [f"DDDDDDDDDD{i}" for i in range(5)]
    for i in ids:
        _play(api, i)
    p1 = api.get("/api/history", params={"limit": 2}).json()
    p2 = api.get("/api/history", params={"limit": 2, "offset": 2}).json()
    p3 = api.get("/api/history", params={"limit": 2, "offset": 4}).json()
    assert [t["id"] for t in p1["tracks"] + p2["tracks"] + p3["tracks"]] == ids[::-1]
    assert (p1["has_more"], p2["has_more"], p3["has_more"]) == (True, True, False)


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 51}, {"offset": -1}, {"offset": 2001}])
def test_history_query_validation(api, params):
    assert api.get("/api/history", params=params).status_code == 422


def test_skip_marks_only_latest_play(api):
    _play(api, A)
    _play(api, B)
    _play(api, A)
    assert api.post("/api/history/skip", json={"source": "yt", "id": A}).json() == {"ok": True}
    rows = api.profile.sql("SELECT key, skipped FROM history ORDER BY id")
    assert rows == [(f"yt:{A}", 0), (f"yt:{B}", 0), (f"yt:{A}", 1)]
    plays = userdata.recent_plays()
    assert [(p["key"], p["skipped"]) for p in plays] == [(f"yt:{A}", True), (f"yt:{B}", False), (f"yt:{A}", False)]


def test_skip_unknown_track_is_noop(api):
    _play(api, A)
    assert api.post("/api/history/skip", json={"source": "yt", "id": "ZZZZZZZZZZZ"}).status_code == 200
    assert api.profile.sql("SELECT skipped FROM history") == [(0,)]


@pytest.mark.parametrize("method, url", [("delete", "/api/history"), ("post", "/api/settings/clear-history")])
def test_clear_history(api, method, url):
    _play(api, A)
    api.post("/api/favorites", json=track_payload(id_=B))
    r = getattr(api, method)(url)
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert api.get("/api/history").json() == {"tracks": [], "has_more": False}
    assert len(api.get("/api/favorites").json()["items"]) == 1          # избранное не тронуто


def test_history_and_favorites_share_one_database_file(api):
    _play(api, A)
    api.post("/api/favorites", json=track_payload(id_=A))
    tables = {r[0] for r in api.profile.sql("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"favorites", "history", "settings", "downloads", "playlists", "playlist_tracks"} <= tables
