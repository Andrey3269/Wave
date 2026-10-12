"""Локальные плейлисты /api/playlists (backend/playlists.py) на временной SQLite-базе."""
from __future__ import annotations

import sqlite3
import threading
import time

import pytest

import library
import playlists
import userdata
from wavetest.fakes import track_payload

A, B, C = "AAAAAAAAAA1", "BBBBBBBBBB1", "CCCCCCCCCC1"


def _create(api, name="Мой плейлист") -> int:
    r = api.post("/api/playlists", json={"name": name})
    assert r.status_code == 201
    return r.json()["id"]


def test_create_and_list(api):
    r = api.post("/api/playlists", json={"name": "  Дорога   домой  "})
    assert r.status_code == 201
    pid = r.json()["id"]
    assert r.json() == {"id": pid, "name": "Дорога домой", "count": 0, "cover": None}
    assert api.get("/api/playlists").json() == {"items": [{"id": pid, "name": "Дорога домой", "count": 0, "cover": None}]}


@pytest.mark.parametrize("name", ["", "   ", "\n\t"])
def test_empty_name_rejected(api, name):
    r = api.post("/api/playlists", json={"name": name})
    assert r.status_code == 400 and r.json()["detail"] == "Введите название плейлиста."
    assert api.get("/api/playlists").json() == {"items": []}


def test_long_name_truncated(api):
    pid = _create(api, "Я" * 200)
    assert api.get(f"/api/playlists/{pid}").json()["playlist"]["name"] == "Я" * 80


def test_add_tracks_order_and_duplicates(api):
    pid = _create(api)
    assert api.post(f"/api/playlists/{pid}/tracks", json=track_payload(id_=A, cover=None)).json() == {"added": True}
    assert api.post(f"/api/playlists/{pid}/tracks", json=track_payload(id_=B)).json() == {"added": True}
    assert api.post(f"/api/playlists/{pid}/tracks", json=track_payload(id_=A)).json() == {"added": False}
    assert api.post(f"/api/playlists/{pid}/tracks", json=track_payload("sc", "1000000001")).json() == {"added": True}
    body = api.get(f"/api/playlists/{pid}").json()
    assert [t["id"] for t in body["tracks"]] == [A, B, "1000000001"]
    assert body["playlist"]["count"] == 3
    assert body["playlist"]["cover"] == track_payload(id_=B)["cover"]       # первая обложка, которая есть
    assert api.profile.sql("SELECT position FROM playlist_tracks ORDER BY position") == [(0,), (1,), (2,)]
    listed = api.get("/api/playlists").json()["items"][0]
    assert listed["count"] == 3 and listed["cover"] is None                 # в списке — обложка первого трека


def test_list_orders_by_last_update(api):
    p1 = _create(api, "Первый")
    time.sleep(0.01)
    p2 = _create(api, "Второй")
    time.sleep(0.01)
    api.post(f"/api/playlists/{p1}/tracks", json=track_payload(id_=A))
    assert [p["id"] for p in api.get("/api/playlists").json()["items"]] == [p1, p2]


def test_rename(api):
    pid = _create(api)
    assert api.patch(f"/api/playlists/{pid}", json={"name": "Новое"}).json() == {"ok": True}
    assert api.get(f"/api/playlists/{pid}").json()["playlist"]["name"] == "Новое"
    assert api.patch(f"/api/playlists/{pid}", json={"name": " "}).status_code == 400
    assert api.get(f"/api/playlists/{pid}").json()["playlist"]["name"] == "Новое"


@pytest.mark.parametrize("method, url, body", [
    ("get", "/api/playlists/999", None),
    ("patch", "/api/playlists/999", {"name": "x"}),
    ("post", "/api/playlists/999/tracks", track_payload()),
])
def test_missing_playlist_404(api, method, url, body):
    kw = {"json": body} if body is not None else {}
    r = getattr(api, method)(url, **kw)
    assert r.status_code == 404 and r.json()["detail"] == "Плейлист не найден."


def test_remove_track(api):
    pid = _create(api)
    for i in (A, B, C):
        api.post(f"/api/playlists/{pid}/tracks", json=track_payload(id_=i))
    assert api.delete(f"/api/playlists/{pid}/tracks/yt/{B}").json() == {"ok": True}
    assert [t["id"] for t in api.get(f"/api/playlists/{pid}").json()["tracks"]] == [A, C]
    api.post(f"/api/playlists/{pid}/tracks", json=track_payload(id_=B))
    assert [t["id"] for t in api.get(f"/api/playlists/{pid}").json()["tracks"]] == [A, C, B]   # в конец


def test_delete_playlist_removes_its_tracks_only(api):
    p1, p2 = _create(api, "1"), _create(api, "2")
    api.post(f"/api/playlists/{p1}/tracks", json=track_payload(id_=A))
    api.post(f"/api/playlists/{p2}/tracks", json=track_payload(id_=A))
    assert api.delete(f"/api/playlists/{p1}").json() == {"ok": True}
    assert api.get(f"/api/playlists/{p1}").status_code == 404
    assert api.profile.sql("SELECT playlist_id FROM playlist_tracks") == [(p2,)]
    assert api.delete("/api/playlists/999").status_code == 200              # удаление несуществующего — не ошибка


def test_playlist_survives_restart(harness):
    with harness.client() as h:
        pid = h.post("/api/playlists", json={"name": "Сохранится"}).json()["id"]
        h.post(f"/api/playlists/{pid}/tracks", json=track_payload(id_=A))
    with harness.client() as h:
        body = h.get(f"/api/playlists/{pid}").json()
    assert body["playlist"]["name"] == "Сохранится" and [t["id"] for t in body["tracks"]] == [A]


@pytest.mark.xfail(strict=True, reason="M5: add_track — проверка дубля и вставка разными запросами; гонка даёт IntegrityError (HTTP 500)")
def test_concurrent_add_of_same_track(db, monkeypatch):
    """Два одновременных добавления одного трека. Барьер после проверки дубля делает гонку детерминированной."""
    pid = playlists.create_playlist(playlists.NameIn(name="race"))["id"]
    barrier = threading.Barrier(2, timeout=5)
    real_q = library._q

    def racing_q(sql, args=()):
        rows = real_q(sql, args)
        if sql.startswith("SELECT 1 FROM playlist_tracks"):
            try:
                barrier.wait()
            except threading.BrokenBarrierError:
                pass
        return rows

    monkeypatch.setattr(library, "_q", racing_q)
    results, errors = [], []

    def worker():
        try:
            results.append(playlists.add_track(pid, userdata.TrackIn(**track_payload(id_=A))))
        except Exception as exc:          # noqa: BLE001 — фиксируем, что именно вылетает
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert not errors, [type(e).__name__ for e in errors]
    assert sorted(r["added"] for r in results) == [False, True]


def test_unique_constraint_backs_duplicate_check(db):
    """Даже при гонке дубль не попадёт в базу: UNIQUE(playlist_id, key) на уровне схемы."""
    pid = playlists.create_playlist(playlists.NameIn(name="x"))["id"]
    library._q("INSERT INTO playlist_tracks (playlist_id, position, key, data, added_at) VALUES (?, 0, 'yt:x', '{}', 0)", (pid,))
    with pytest.raises(sqlite3.IntegrityError):
        library._q("INSERT INTO playlist_tracks (playlist_id, position, key, data, added_at) VALUES (?, 1, 'yt:x', '{}', 0)", (pid,))
