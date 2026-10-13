"""Избранное и история (backend/userdata.py) на временной SQLite-базе."""
from __future__ import annotations

import json
import sqlite3
import time

import pytest

import userdata
from wavetest.fakes import track_payload

A, B, C = "AAAAAAAAAA1", "BBBBBBBBBB1", "CCCCCCCCCC1"


def _write(profile, sql: str, rows: list[tuple]) -> None:
    """Запись в тестовую базу мимо API (отдельное соединение, с commit) — данные «от прошлой версии»."""
    con = sqlite3.connect(profile.db_path)
    try:
        con.executemany(sql, rows)
        con.commit()
    finally:
        con.close()


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
    # S6 (M6): снимок содержит все поля Track, в том числе explicit, plays, released
    assert stored == {**{k: body[k] for k in ("id", "source", "title", "artist", "artists", "album", "duration", "cover",
                                              "explicit", "stream_url", "artist_id", "artist_ids", "url")},
                      "plays": None, "released": None}
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


def test_favorite_keeps_all_track_fields(api):
    """M6 (исправлено в S6): explicit, plays, released сохраняются и читаются обратно."""
    body = track_payload("sc", "1000000001", explicit=True, plays=12345, released="2025-03-01T10:00:00Z")
    api.post("/api/favorites", json=body)
    item = api.get("/api/favorites").json()["items"][0]
    assert (item.get("explicit"), item.get("plays"), item.get("released")) == (True, 12345, "2025-03-01T10:00:00Z")
    assert userdata.favorite_tracks()[0]["plays"] == 12345                 # то же видят рекомендации


@pytest.mark.parametrize("field, value", [("explicit", "да"), ("plays", "много"), ("released", 5)])
def test_new_track_fields_are_validated(api, field, value):
    assert api.post("/api/favorites", json=track_payload(**{field: value})).status_code == 422
    assert api.profile.sql("SELECT COUNT(*) FROM favorites")[0][0] == 0


def test_favorite_saved_before_s6_is_read_as_is(api):
    """Старый снимок без explicit/plays/released читается без изменений, ответ не падает."""
    old = {k: v for k, v in track_payload(id_=A).items() if k != "explicit"}
    _write(api.profile, "INSERT INTO favorites (key, data, added_at) VALUES (?, ?, 1)", [(f"yt:{A}", json.dumps(old))])
    assert api.get("/api/favorites").json()["items"] == [old]


def test_saved_favorite_can_always_be_removed(api):
    """M6 (исправлено в S6): запись с «/» в id удаляется — и как шлёт фронтенд (%2F), и как есть."""
    for path in ("/api/favorites/yt/a%2Fb", "/api/favorites/yt/a/b"):
        assert api.post("/api/favorites", json=track_payload(id_="a/b")).status_code == 200
        assert api.delete(path).json() == {"ok": True}
        assert api.get("/api/favorites").json()["items"] == []


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


# ---------------------------------------------------------------------------
# Ретеншн истории (S6)
# ---------------------------------------------------------------------------
def test_history_limit_setting_defaults_and_validation(api):
    assert api.get("/api/settings").json()["values"]["history_limit"] == 10_000
    for good in (1000, 25_000, 100_000, "5000"):
        r = api.put("/api/settings", json={"history_limit": good})
        assert r.status_code == 200 and r.json()["values"]["history_limit"] == int(good)
    for bad in (999, 100_001, 0, -5, 1.5, True, "", "много", "5000²", "²", "+5000", None, [1000]):
        assert api.put("/api/settings", json={"history_limit": bad}).status_code == 400, bad
    assert api.get("/api/settings").json()["values"]["history_limit"] == 5000


def test_history_keeps_only_last_records(api, monkeypatch):
    monkeypatch.setattr(userdata, "HISTORY_LIMIT_MIN", 1)          # маленький лимит, чтобы не писать 10 000 строк
    api.post("/api/favorites", json=track_payload(id_=A))
    pid = api.post("/api/playlists", json={"name": "P"}).json()["id"]
    api.post(f"/api/playlists/{pid}/tracks", json=track_payload(id_=A))
    assert api.put("/api/settings", json={"history_limit": 3}).status_code == 200
    for i in range(6):
        _play(api, f"EEEEEEEEEE{i}")
    rows = api.profile.sql("SELECT key FROM history ORDER BY id")
    assert rows == [(f"yt:EEEEEEEEEE{i}",) for i in (3, 4, 5)]
    assert [t["id"] for t in api.get("/api/history").json()["tracks"]] == ["EEEEEEEEEE5", "EEEEEEEEEE4", "EEEEEEEEEE3"]
    # другие данные не тронуты
    assert len(api.get("/api/favorites").json()["items"]) == 1
    assert api.get(f"/api/playlists/{pid}").json()["playlist"]["count"] == 1
    assert api.get("/api/settings").json()["values"]["history_limit"] == 3


def test_lower_limit_trims_immediately_and_at_startup(harness, monkeypatch):
    monkeypatch.setattr(userdata, "HISTORY_LIMIT_MIN", 1)
    with harness.client() as h:
        for i in range(5):
            _play(h, f"FFFFFFFFFF{i}")
        h.put("/api/settings", json={"history_limit": 2})
        assert h.profile.sql("SELECT key FROM history ORDER BY id") == [("yt:FFFFFFFFFF3",), ("yt:FFFFFFFFFF4",)]
    # лишние строки появились мимо API (как в базе версии без ретеншна)
    _write(harness.profile, "INSERT INTO history (key, data, played_at) VALUES (?, ?, ?)",
           [(f"yt:GGGGGGGGGG{i}", "{}", float(i)) for i in range(4)])
    with harness.client() as h:                                     # init() применяет лимит при запуске
        assert h.profile.sql("SELECT key FROM history ORDER BY id") == [("yt:GGGGGGGGGG2",), ("yt:GGGGGGGGGG3",)]


def test_corrupt_history_limit_falls_back_to_default(api):
    _write(api.profile, "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", [("history_limit", "мусор")])
    assert api.get("/api/settings").json()["values"]["history_limit"] == 10_000
    _play(api, A)                                                    # запись не падает и ничего лишнего не удаляет
    assert api.profile.sql("SELECT COUNT(*) FROM history")[0][0] == 1


def test_recommendations_still_see_recent_plays_after_trim(api, monkeypatch):
    monkeypatch.setattr(userdata, "HISTORY_LIMIT_MIN", 1)
    api.put("/api/settings", json={"history_limit": 2})
    for i in range(4):
        _play(api, f"HHHHHHHHHH{i}")
    assert [p["key"] for p in userdata.recent_plays()] == ["yt:HHHHHHHHHH3", "yt:HHHHHHHHHH2"]


def test_history_and_favorites_share_one_database_file(api):
    _play(api, A)
    api.post("/api/favorites", json=track_payload(id_=A))
    tables = {r[0] for r in api.profile.sql("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"favorites", "history", "settings", "downloads", "playlists", "playlist_tracks"} <= tables
