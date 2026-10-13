"""Локальные плейлисты /api/playlists (backend/playlists.py) на временной SQLite-базе."""
from __future__ import annotations

import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor

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


def test_concurrent_add_of_same_track(db, monkeypatch):
    """
    M5 (исправлено в S6): два одновременных добавления одного трека. Барьер выпускает оба потока к записи
    одновременно, когда оба уже прошли проверку запроса. Раньше проверка дубля и вставка были разными
    запросами, и второй поток получал IntegrityError (через API — HTTP 500).
    """
    pid = playlists.create_playlist(playlists.NameIn(name="race"))["id"]
    barrier = threading.Barrier(2, timeout=5)
    real_tx = library.transaction

    def racing_tx():
        try:
            barrier.wait()
        except threading.BrokenBarrierError:
            pass
        return real_tx()

    monkeypatch.setattr(library, "transaction", racing_tx)
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
    assert db.sql("SELECT key, position FROM playlist_tracks") == [(f"yt:{A}", 0)]


def test_concurrent_api_adds_keep_order_and_no_duplicates(api):
    """Много параллельных POST: дубль одного трека — один раз, разные треки — без потерь и с разными позициями."""
    pid = _create(api)
    same = [track_payload(id_=A)] * 8
    distinct = [track_payload(id_=f"DDDDDDDDDD{i}") for i in range(8)]

    def post(body):
        return api.post(f"/api/playlists/{pid}/tracks", json=body)

    with ThreadPoolExecutor(max_workers=8) as pool:
        answers = list(pool.map(post, same + distinct))
    assert all(r.status_code == 200 for r in answers), [r.status_code for r in answers]
    assert [r.json()["added"] for r in answers[:8]].count(True) == 1
    assert all(r.json()["added"] for r in answers[8:])
    rows = api.profile.sql("SELECT key, position FROM playlist_tracks ORDER BY position")
    assert len(rows) == 9 and sorted(k for k, _ in rows) == sorted({f"yt:{A}"} | {f"yt:DDDDDDDDDD{i}" for i in range(8)})
    assert [pos for _, pos in rows] == list(range(9))                       # позиции без пропусков и повторов


def test_add_to_deleted_playlist_is_404_not_500(api):
    """Плейлист удалили между открытием и добавлением: 404, «висячая» строка не появляется (foreign_keys=ON)."""
    pid = _create(api)
    api.delete(f"/api/playlists/{pid}")
    r = api.post(f"/api/playlists/{pid}/tracks", json=track_payload(id_=A))
    assert r.status_code == 404
    assert api.profile.sql("SELECT COUNT(*) FROM playlist_tracks") == [(0,)]


def _fail_playlist_updates(profile) -> None:
    """Триггер в тестовой базе: любое изменение playlists падает — проверяем, что операция откатывается целиком."""
    con = sqlite3.connect(profile.db_path)
    con.execute("CREATE TRIGGER fail_touch BEFORE UPDATE ON playlists BEGIN SELECT RAISE(ABORT, 'сбой записи'); END")
    con.commit()
    con.close()


def test_add_track_is_atomic(db):
    pid = playlists.create_playlist(playlists.NameIn(name="atomic"))["id"]
    _fail_playlist_updates(db)
    with pytest.raises(sqlite3.IntegrityError):
        playlists.add_track(pid, userdata.TrackIn(**track_payload(id_=A)))
    assert db.sql("SELECT COUNT(*) FROM playlist_tracks") == [(0,)]           # вставка откатилась вместе с updated_at


def test_remove_track_is_atomic(db):
    pid = playlists.create_playlist(playlists.NameIn(name="atomic"))["id"]
    playlists.add_track(pid, userdata.TrackIn(**track_payload(id_=A)))
    _fail_playlist_updates(db)
    with pytest.raises(sqlite3.IntegrityError):
        playlists.remove_track(pid, "yt", A)
    assert db.sql("SELECT key FROM playlist_tracks") == [(f"yt:{A}",)]        # трек на месте


def test_delete_playlist_is_atomic(db):
    pid = playlists.create_playlist(playlists.NameIn(name="atomic"))["id"]
    playlists.add_track(pid, userdata.TrackIn(**track_payload(id_=A)))
    con = sqlite3.connect(db.db_path)
    con.execute("CREATE TRIGGER fail_delete BEFORE DELETE ON playlists BEGIN SELECT RAISE(ABORT, 'сбой'); END")
    con.commit()
    con.close()
    with pytest.raises(sqlite3.IntegrityError):
        playlists.delete_playlist(pid)
    assert db.sql("SELECT COUNT(*) FROM playlist_tracks") == [(1,)]          # треки не потеряны


def test_unique_constraint_backs_duplicate_check(db):
    """Даже при гонке дубль не попадёт в базу: UNIQUE(playlist_id, key) на уровне схемы."""
    pid = playlists.create_playlist(playlists.NameIn(name="x"))["id"]
    library._q("INSERT INTO playlist_tracks (playlist_id, position, key, data, added_at) VALUES (?, 0, 'yt:x', '{}', 0)", (pid,))
    with pytest.raises(sqlite3.IntegrityError):
        library._q("INSERT INTO playlist_tracks (playlist_id, position, key, data, added_at) VALUES (?, 1, 'yt:x', '{}', 0)", (pid,))
