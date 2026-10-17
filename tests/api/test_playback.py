"""
Состояние воспроизведения между запусками (backend/playback.py, S10) на временном профиле.

Хранится строкой playback_state в таблице settings; база остаётся схемы v1. Проверяется: запись, чтение,
очистка, обновление позиции, валидация (структура, размеры, типы, значения, URL), испорченная строка,
перезапуск backend на том же профиле, совместимость со старым профилем и сохранность остальных данных.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from wavetest.fakes import track_payload

YT = ["AAAAAAAAAA1", "BBBBBBBBBB1", "CCCCCCCCCC1"]


def _tracks(n: int = 3) -> list[dict]:
    return [track_payload(id_=f"T{i:010d}") if i >= len(YT) else track_payload(id_=YT[i]) for i in range(n)]


def _state(**over) -> dict:
    body = {"tracks": _tracks(), "index": 1, "position": 42.5, "shuffle": True, "repeat": "all", "label": "Альбом 1",
            "history": [0, 2, 1], "cursor": 2, "cycle": [0, 2, 1]}
    body.update(over)
    return body


def _row(api) -> dict | None:
    rows = api.profile.sql("SELECT value FROM settings WHERE key = 'playback_state'")
    return json.loads(rows[0][0]) if rows else None


# ---------------------------------------------------------------- запись, чтение, очистка
def test_empty_profile_has_no_state(api):
    assert api.get("/api/player/state").json() == {"state": None}


def test_put_get_roundtrip(api):
    assert api.put("/api/player/state", json=_state()).json() == {"ok": True}
    st = api.get("/api/player/state").json()["state"]
    assert [t["id"] for t in st["tracks"]] == YT
    assert {k: st[k] for k in ("index", "position", "shuffle", "repeat", "label", "history", "cursor", "cycle")} == \
        {"index": 1, "position": 42.5, "shuffle": True, "repeat": "all", "label": "Альбом 1",
         "history": [0, 2, 1], "cursor": 2, "cycle": [0, 2, 1]}
    assert st["tracks"][0] == {**track_payload(id_=YT[0]), "plays": None, "released": None,
                               "artist_ids": ["UCartist"], "url": None, "album_id": None}
    assert isinstance(st["saved_at"], float)


def test_put_replaces_previous_state(api):
    api.put("/api/player/state", json=_state())
    api.put("/api/player/state", json=_state(tracks=_tracks(1), index=0, history=[0], cursor=0, cycle=[0], repeat="one"))
    st = api.get("/api/player/state").json()["state"]
    assert len(st["tracks"]) == 1 and st["repeat"] == "one"
    assert api.profile.sql("SELECT COUNT(*) FROM settings WHERE key = 'playback_state'")[0][0] == 1


def test_delete_forgets_state(api):
    api.put("/api/player/state", json=_state())
    assert api.delete("/api/player/state").json() == {"ok": True}
    assert api.get("/api/player/state").json() == {"state": None}
    assert api.delete("/api/player/state").status_code == 200          # повторно — тоже без ошибки


def test_defaults_for_optional_fields(api):
    api.put("/api/player/state", json={"tracks": _tracks(2), "index": 0})
    st = api.get("/api/player/state").json()["state"]
    assert (st["position"], st["shuffle"], st["repeat"], st["label"], st["history"], st["cursor"], st["cycle"]) == \
        (0.0, False, "off", "", [], -1, [])


# ---------------------------------------------------------------- позиция
def test_patch_updates_only_position(api):
    api.put("/api/player/state", json=_state())
    assert api.patch("/api/player/state", json={"key": f"yt:{YT[1]}", "index": 1, "position": 77.123}).json() == {"ok": True}
    st = api.get("/api/player/state").json()["state"]
    assert st["position"] == 77.12 and st["index"] == 1 and st["history"] == [0, 2, 1]


@pytest.mark.parametrize("body", [
    {"key": f"yt:{YT[0]}", "index": 1, "position": 5},        # на этом месте другой трек
    {"key": f"yt:{YT[0]}", "index": 0, "position": 5},        # трек есть, но текущий — другой
    {"key": f"yt:{YT[1]}", "index": 7, "position": 5},
])
def test_stale_patch_is_refused(api, body):
    api.put("/api/player/state", json=_state())
    assert api.patch("/api/player/state", json=body).status_code == 409
    assert api.get("/api/player/state").json()["state"]["position"] == 42.5


def test_patch_without_state_is_refused(api):
    assert api.patch("/api/player/state", json={"key": f"yt:{YT[0]}", "index": 0, "position": 1}).status_code == 409
    assert _row(api) is None


@pytest.mark.parametrize("body", [
    {"key": 1, "index": 0, "position": 1}, {"key": "yt:x", "index": True, "position": 1},
    {"key": "yt:x", "index": 0, "position": -1}, {"key": "yt:x", "index": 0, "position": "1"},
    {"key": "yt:x", "index": 0, "position": 1e9}, {"key": "yt:x", "index": 0},
])
def test_bad_patch_is_400(api, body):
    api.put("/api/player/state", json=_state())
    assert api.patch("/api/player/state", json=body).status_code == 400


# ---------------------------------------------------------------- валидация записи
@pytest.mark.parametrize("over", [
    {"tracks": []}, {"tracks": "x"}, {"tracks": None},
    {"index": 3}, {"index": -2}, {"index": "1"}, {"index": True},
    {"position": -1}, {"position": float("inf")}, {"position": "10"}, {"position": 10 ** 6}, {"position": True},
    {"repeat": "loop"}, {"repeat": True}, {"shuffle": "yes"},
    {"history": [0, 5]}, {"history": "0"}, {"history": [0] * 501}, {"cursor": 3}, {"cursor": -2},
    {"cycle": [0, 0]}, {"cycle": [9]}, {"cycle": [0, 1, 2, 0]},
])
def test_invalid_state_is_400_and_keeps_previous(api, over):
    api.put("/api/player/state", json=_state())
    body = _state(**over)
    if over.get("position") == float("inf"):
        r = api.put("/api/player/state", content=json.dumps(body).replace("Infinity", "1e999"),
                    headers={"Content-Type": "application/json"})
    else:
        r = api.put("/api/player/state", json=body)
    assert r.status_code == 400
    assert api.get("/api/player/state").json()["state"]["position"] == 42.5       # прежнее состояние не тронуто


@pytest.mark.parametrize("bad", [
    {"id": "../../etc"}, {"id": "short"}, {"source": "spotify"}, {"title": ""}, {"title": None},
    {"title": "x" * 501}, {"artist": 5}, {"id": "dQw4w9WgXcQ", "source": "sc"},
])
def test_invalid_track_is_400(api, bad):
    tracks = _tracks()
    tracks[2] = {**tracks[2], **bad}
    assert api.put("/api/player/state", json=_state(tracks=tracks)).status_code == 400
    assert _row(api) is None


def test_too_many_tracks_and_too_big_body(api):
    many = [track_payload(id_=f"X{i:010d}") for i in range(501)]
    assert api.put("/api/player/state", json={"tracks": many, "index": 0}).status_code == 400
    huge = json.dumps(_state(label="x" * 1_000_001))
    assert api.put("/api/player/state", content=huge, headers={"Content-Type": "application/json"}).status_code == 413
    assert api.put("/api/player/state", content=b"{not json", headers={"Content-Type": "application/json"}).status_code == 400
    assert _row(api) is None


def test_urls_and_unknown_fields_are_not_trusted(api):
    t = {**track_payload(id_=YT[0]), "stream_url": "https://evil.example/x.mp3", "cover": "javascript:alert(1)",
         "url": "https://evil.example/watch", "extra": "drop me", "downloaded": True}
    t2 = {**track_payload("sc", "1000000001"), "stream_url": "/api/stream/yt/AAAAAAAAAA1",
          "cover": "/api/local/sc/1000000001/cover", "url": "https://soundcloud.com/u/t"}
    t3 = {**track_payload(id_=YT[1]), "cover": "http://insecure.example/c.jpg", "url": "http://music.youtube.com/watch?v=1"}
    api.put("/api/player/state", json={"tracks": [t, t2, t3], "index": 0, "evil": {"x": 1}})
    st = api.get("/api/player/state").json()["state"]
    a, b, c = st["tracks"]
    assert a["stream_url"] == f"/api/stream/yt/{YT[0]}" and a["cover"] is None and a["url"] is None
    assert "extra" not in a and "downloaded" not in a and "evil" not in st
    assert b["stream_url"] == "/api/stream/sc/1000000001" and b["cover"] == "/api/local/sc/1000000001/cover"
    assert b["url"] == "https://soundcloud.com/u/t"
    assert c["cover"] is None and c["url"] is None
    stored = _row(api)
    assert "evil.example" not in json.dumps(stored) and "javascript" not in json.dumps(stored)


def test_cover_must_be_from_allowlist(api):
    """S13: обложка в очереди — только с хостов обложек источников (раньше проходил любой https://)."""
    ok = "https://i1.sndcdn.com/artworks-x-t500x500.jpg"
    t = [{**track_payload(id_=YT[0]), "cover": "https://evil.example/c.jpg"},
         {**track_payload(id_=YT[1]), "cover": "https://evilgoogleusercontent.com/c"},
         {**track_payload(id_=YT[2]), "cover": ok}]
    assert api.put("/api/player/state", json={"tracks": t, "index": 0}).status_code == 200
    assert [x["cover"] for x in api.get("/api/player/state").json()["state"]["tracks"]] == [None, None, ok]


def test_control_characters_in_text_are_neutralised(api):
    t = {**track_payload(id_=YT[0]), "title": "Песня\x00\nвторая строка", "artists": ["A\x1b", 7, "B"]}
    api.put("/api/player/state", json={"tracks": [t], "index": 0, "label": "Метка\r\n"})
    st = api.get("/api/player/state").json()["state"]
    assert st["tracks"][0]["title"] == "Песня  вторая строка"
    assert st["tracks"][0]["artists"] == ["A", "B"]
    assert st["label"] == "Метка"


# ---------------------------------------------------------------- испорченное и старое
@pytest.mark.parametrize("raw", [
    "{broken", "[]", "null", json.dumps({"v": 99, "tracks": [], "index": 0}),
    json.dumps({"v": 1, "tracks": [{"id": "x"}], "index": 0}),
    json.dumps({"v": 1, "tracks": [track_payload(id_=YT[0])], "index": 5}),
])
def test_corrupt_stored_row_is_ignored(api, raw):
    con = sqlite3.connect(api.profile.db_path)
    try:
        con.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('playback_state', ?)", (raw,))
        con.commit()
    finally:
        con.close()
    r = api.get("/api/player/state")
    assert r.status_code == 200 and r.json() == {"state": None}
    assert api.put("/api/player/state", json=_state()).status_code == 200        # следующее сохранение перезаписывает
    assert api.get("/api/player/state").json()["state"]["index"] == 1


def test_state_is_not_a_setting(api):
    api.put("/api/player/state", json=_state())
    values = api.get("/api/settings").json()["values"]
    assert "playback_state" not in values
    assert api.put("/api/settings", json={"playback_state": "{}"}).status_code == 400     # не меняется через настройки
    assert api.get("/api/player/state").json()["state"]["index"] == 1


# ---------------------------------------------------------------- перезапуск и сохранность данных
def test_state_survives_backend_restart(harness):
    with harness.client() as h:
        h.put("/api/player/state", json=_state())
        h.patch("/api/player/state", json={"key": f"yt:{YT[1]}", "index": 1, "position": 99.5})
    with harness.client() as h:                                              # новый lifespan, база открыта заново
        st = h.get("/api/player/state").json()["state"]
    assert st["position"] == 99.5 and st["repeat"] == "all" and st["shuffle"] is True
    assert [t["id"] for t in st["tracks"]] == YT


def test_profile_without_s10_state_and_other_data_untouched(api):
    """Старый профиль (до S10) работает как прежде; запись и удаление состояния не трогают другие данные."""
    api.post("/api/favorites", json=track_payload(id_=YT[0]))
    api.post("/api/history", json=track_payload(id_=YT[1]))
    api.put("/api/settings", json={"theme": "light", "volume": 0.4})
    pl = api.post("/api/playlists", json={"name": "Мой"}).json()
    before = {t: api.profile.sql(f"SELECT * FROM {t}") for t in ("favorites", "history", "playlists", "downloads")}
    settings_before = api.profile.sql("SELECT key, value FROM settings ORDER BY key")
    assert api.get("/api/player/state").json() == {"state": None}
    api.put("/api/player/state", json=_state())
    api.patch("/api/player/state", json={"key": f"yt:{YT[1]}", "index": 1, "position": 3})
    api.delete("/api/player/state")
    after = {t: api.profile.sql(f"SELECT * FROM {t}") for t in ("favorites", "history", "playlists", "downloads")}
    assert after == before
    assert api.profile.sql("SELECT key, value FROM settings ORDER BY key") == settings_before
    assert api.get("/api/settings").json()["values"]["theme"] == "light"
    assert pl["id"]
    assert api.profile.sql("PRAGMA user_version")[0][0] == 1                  # схема та же: v1


def test_concurrent_patches_and_puts_keep_valid_json(api):
    """Чтение-изменение-запись позиции идёт в транзакции: параллельные запросы не портят строку."""
    import threading
    api.put("/api/player/state", json=_state())
    errors: list[int] = []

    def work(k: int) -> None:
        for i in range(10):
            if k % 2:
                r = api.patch("/api/player/state", json={"key": f"yt:{YT[1]}", "index": 1, "position": float(i)})
            else:
                r = api.put("/api/player/state", json=_state(position=float(i)))
            if r.status_code != 200:
                errors.append(r.status_code)

    threads = [threading.Thread(target=work, args=(k,)) for k in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    st = api.get("/api/player/state").json()["state"]
    assert st is not None and 0 <= st["position"] <= 9


def test_beacon_endpoints_behave_like_put_and_patch(api):
    """navigator.sendBeacon умеет только POST: при закрытии окна те же проверки идут через POST."""
    assert api.post("/api/player/state", json=_state()).json() == {"ok": True}
    assert api.get("/api/player/state").json()["state"]["index"] == 1
    assert api.post("/api/player/state/position", json={"key": f"yt:{YT[1]}", "index": 1, "position": 8}).json() == {"ok": True}
    assert api.get("/api/player/state").json()["state"]["position"] == 8
    assert api.post("/api/player/state/position", json={"key": f"yt:{YT[0]}", "index": 1, "position": 9}).status_code == 409
    assert api.post("/api/player/state", json=_state(index=9)).status_code == 400
    assert api.get("/api/player/state").json()["state"]["position"] == 8



def test_album_id_is_restored_only_if_it_is_an_album(api):
    t1 = {**track_payload(id_=YT[0]), "album_id": "MPREb_alb00000001"}
    t2 = {**track_payload(id_=YT[1]), "album_id": "OLAK5uy_polar"}
    api.put("/api/player/state", json={"tracks": [t1, t2], "index": 0})
    a, b = api.get("/api/player/state").json()["state"]["tracks"]
    assert a["album_id"] == "MPREb_alb00000001" and b["album_id"] is None
