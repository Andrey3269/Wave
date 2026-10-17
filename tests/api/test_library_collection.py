"""
S15: библиотека — избранные альбомы и исполнители (/api/library/albums|artists), порядок треков плейлиста
(PATCH /api/playlists/{pid}/tracks/order), удаление одной записи истории (DELETE /api/history/{source}/{id}).
"""
from __future__ import annotations

import json

import pytest

from wavetest.fakes import track_payload

A, B, C, D = "AAAAAAAAAA1", "BBBBBBBBBB1", "CCCCCCCCCC1", "DDDDDDDDDD1"
COVER = "https://lh3.googleusercontent.com/alb=w544-h544"


def album(i: int = 1, **over) -> dict:
    body = {"id": f"MPREb_album{i}", "source": "yt", "kind": "album", "title": f"Альбом {i}", "artist": "GSPD",
            "year": "2024", "cover": COVER, "track_count": 9, "playlist_id": f"OLAK5uy_pl{i}"}
    body.update(over)
    return body


def artist(i: int = 1, **over) -> dict:
    body = {"id": f"UCartist{i:04d}", "source": "yt", "kind": "artist", "name": f"Исполнитель {i}", "cover": COVER,
            "subtitle": "1 млн подписчиков"}
    body.update(over)
    return body


# ---------------------------------------------------------------------------
# Избранные альбомы и исполнители
# ---------------------------------------------------------------------------
def test_albums_add_list_remove(api):
    assert api.get("/api/library/albums").json() == {"items": []}
    assert api.post("/api/library/albums", json=album(1)).json() == {"ok": True, "added": True}
    assert api.post("/api/library/albums", json=album(2, source="sc", id="123456")).json()["added"] is True
    assert api.post("/api/library/albums", json=album(1, title="Другое")).json() == {"ok": True, "added": False}
    items = api.get("/api/library/albums").json()["items"]
    assert [(c["source"], c["id"]) for c in items] == [("sc", "123456"), ("yt", "MPREb_album1")]     # новые сверху
    assert items[1] == album(1)                                                # повтор не перезаписал карточку
    assert api.delete("/api/library/albums/yt/MPREb_album1").json() == {"ok": True}
    assert [c["id"] for c in api.get("/api/library/albums").json()["items"]] == ["123456"]
    assert api.profile.sql("SELECT kind, source, item_id FROM favorite_items") == [("album", "sc", "123456")]


def test_artists_add_list_remove_and_track_favorites_untouched(api):
    api.post("/api/favorites", json=track_payload(id_=A))
    assert api.post("/api/library/artists", json=artist(1)).json()["added"] is True
    assert api.get("/api/library/artists").json()["items"] == [artist(1)]
    assert api.delete("/api/library/artists/yt/UCartist0001").json() == {"ok": True}
    assert api.get("/api/library/artists").json() == {"items": []}
    assert [t["id"] for t in api.get("/api/favorites").json()["items"]] == [A]          # избранное треков как было


def test_album_and_artist_with_same_id_are_separate(api):
    api.post("/api/library/albums", json=album(1, id="X1"))
    api.post("/api/library/artists", json=artist(1, id="X1"))
    api.delete("/api/library/artists/yt/X1")
    assert [c["id"] for c in api.get("/api/library/albums").json()["items"]] == ["X1"]


@pytest.mark.parametrize("bad", [
    {"id": "../../etc"}, {"id": ""}, {"id": "a" * 65}, {"source": "local"}, {"source": "spotify"},
    {"title": ""}, {"title": "x\u0000y"}, {"title": "я" * 301}, {"year": "20245"}, {"track_count": -1},
    {"playlist_id": "bad id"},
])
def test_bad_album_rejected(api, bad):
    assert api.post("/api/library/albums", json=album(1, **bad)).status_code == 422
    assert api.profile.sql("SELECT COUNT(*) FROM favorite_items") == [(0,)]


def test_untrusted_cover_is_dropped(api):
    api.post("/api/library/artists", json=artist(1, cover="javascript:alert(1)"))
    api.post("/api/library/albums", json=album(1, cover="https://evil.example/c.jpg"))
    assert api.get("/api/library/artists").json()["items"][0]["cover"] is None
    assert api.get("/api/library/albums").json()["items"][0]["cover"] is None


def test_damaged_row_is_skipped_not_deleted(api):
    api.post("/api/library/albums", json=album(1))
    import library
    library._q("INSERT INTO favorite_items (kind, source, item_id, data, added_at) VALUES ('album', 'yt', 'X', 'не json', 1)")
    assert [c["id"] for c in api.get("/api/library/albums").json()["items"]] == ["MPREb_album1"]
    assert api.profile.sql("SELECT COUNT(*) FROM favorite_items") == [(2,)]


# ---------------------------------------------------------------------------
# Порядок треков плейлиста
# ---------------------------------------------------------------------------
def _playlist(api, *ids) -> int:
    pid = api.post("/api/playlists", json={"name": "Порядок"}).json()["id"]
    for i in ids:
        assert api.post(f"/api/playlists/{pid}/tracks", json=track_payload(id_=i)).json() == {"added": True}
    return pid


def _ids(api, pid):
    return [t["id"] for t in api.get(f"/api/playlists/{pid}").json()["tracks"]]


def test_reorder_saves_new_order(api):
    pid = _playlist(api, A, B, C)
    before = api.profile.sql("SELECT updated_at FROM playlists")[0][0]
    r = api.patch(f"/api/playlists/{pid}/tracks/order", json={"keys": [f"yt:{C}", f"yt:{A}", f"yt:{B}"]})
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert _ids(api, pid) == [C, A, B]
    assert api.profile.sql("SELECT position FROM playlist_tracks ORDER BY position") == [(0,), (1,), (2,)]
    assert api.profile.sql("SELECT updated_at FROM playlists")[0][0] >= before
    # новый трек после перестановки — в конец
    api.post(f"/api/playlists/{pid}/tracks", json=track_payload(id_=D))
    assert _ids(api, pid) == [C, A, B, D]


@pytest.mark.parametrize("keys, code", [
    ([f"yt:{A}", f"yt:{A}", f"yt:{B}"], 400),          # дубль
    ([f"yt:{A}", f"yt:{B}", "yt:ZZZZZZZZZZ1"], 400),    # чужой трек
    ([f"yt:{A}", f"yt:{B}"], 409),                      # не все треки (плейлист изменился)
    ([f"yt:{A}", f"yt:{B}", f"yt:{C}", "yt:ZZZZZZZZZZ1"], 400),
    ([], 409),
])
def test_bad_order_rejected_and_nothing_changes(api, keys, code):
    pid = _playlist(api, A, B, C)
    rows = api.profile.sql("SELECT id, key, position FROM playlist_tracks ORDER BY id")
    assert api.patch(f"/api/playlists/{pid}/tracks/order", json={"keys": keys}).status_code == code
    assert api.profile.sql("SELECT id, key, position FROM playlist_tracks ORDER BY id") == rows


def test_reorder_validates_body_and_playlist(api):
    pid = _playlist(api, A)
    assert api.patch("/api/playlists/999/tracks/order", json={"keys": []}).status_code == 404
    assert api.patch(f"/api/playlists/{pid}/tracks/order", json={"keys": "yt:A"}).status_code == 422
    assert api.patch(f"/api/playlists/{pid}/tracks/order", json={"keys": [1]}).status_code == 422
    assert api.patch(f"/api/playlists/{pid}/tracks/order", json={}).status_code == 422
    assert api.patch(f"/api/playlists/{pid}/tracks/order", json={"keys": ["x" * 301]}).status_code == 400


def test_track_of_other_playlist_is_not_accepted(api):
    p1, p2 = _playlist(api, A, B), _playlist(api, C)
    assert api.patch(f"/api/playlists/{p1}/tracks/order", json={"keys": [f"yt:{B}", f"yt:{C}"]}).status_code == 400
    assert _ids(api, p1) == [A, B] and _ids(api, p2) == [C]


def test_hidden_damaged_rows_stay_after_visible_ones(api):
    pid = _playlist(api, A, B)
    import library
    library._q("INSERT INTO playlist_tracks (playlist_id, position, key, data, added_at) VALUES (?, 5, 'yt:bad/id', ?, 1)",
               (pid, json.dumps({"id": "bad/id", "source": "yt"})))
    assert api.patch(f"/api/playlists/{pid}/tracks/order", json={"keys": [f"yt:{B}", f"yt:{A}"]}).status_code == 200
    assert api.profile.sql("SELECT key, position FROM playlist_tracks ORDER BY position") == [
        (f"yt:{B}", 0), (f"yt:{A}", 1), ("yt:bad/id", 2)]
    assert api.patch(f"/api/playlists/{pid}/tracks/order", json={"keys": [f"yt:{A}", f"yt:{B}", "yt:bad/id"]}).status_code == 400


# ---------------------------------------------------------------------------
# История: удаление одной записи
# ---------------------------------------------------------------------------
def test_remove_one_history_entry(api):
    for i in (A, B, A, C):
        api.post("/api/history", json=track_payload(id_=i))
    api.post("/api/history/skip", json={"source": "yt", "id": A, "track": track_payload(id_=A)})
    r = api.delete(f"/api/history/yt/{A}")
    assert r.status_code == 200 and r.json() == {"ok": True, "removed": 3}
    assert [t["id"] for t in api.get("/api/history").json()["tracks"]] == [C, B]
    assert api.profile.sql("SELECT key FROM history ORDER BY id") == [(f"yt:{B}",), (f"yt:{C}",)]
    assert api.delete(f"/api/history/yt/{A}").json() == {"ok": True, "removed": 0}
    api.post("/api/favorites", json=track_payload(id_=A))                     # избранное не затронуто
    assert api.get("/api/favorites").json()["items"][0]["id"] == A


def test_remove_history_entry_rejects_huge_ids(api):
    assert api.delete("/api/history/yt/" + "a" * 300).status_code == 400
