"""
S15: экспорт и импорт библиотеки в JSON (backend/collection.py).

Главное: на чистом профиле экспорт → импорт восстанавливает избранное (треки, альбомы, исполнители), плейлисты
с порядком треков и настройки 1:1; импорт проверяет весь файл до записи и пишет всё одной транзакцией — при любой
ошибке база не меняется.
"""
from __future__ import annotations

import json

import pytest

import collection
import userdata
from persistence import db as store
from wavetest import legacy_db
from wavetest.fakes import track_payload

A, B, C, D = "AAAAAAAAAA1", "BBBBBBBBBB1", "CCCCCCCCCC1", "DDDDDDDDDD1"
COVER = "https://lh3.googleusercontent.com/alb=w544-h544"
ALBUM = {"id": "MPREb_album1", "source": "yt", "title": "Альбом", "artist": "GSPD", "year": "2024", "cover": COVER,
         "track_count": 9, "playlist_id": "OLAK5uy_pl1"}
ARTIST = {"id": "UCartist0001", "source": "yt", "name": "GSPD", "cover": COVER, "subtitle": "1 млн подписчиков"}


def _fill(api) -> None:
    """Профиль со всем, что входит в экспорт: избранное трех видов, два плейлиста с перестановкой, настройки, история."""
    for i in (A, B):
        api.post("/api/favorites", json=track_payload(id_=i))
    api.post("/api/favorites", json=track_payload("sc", "1000000001", plays=42, released="2024-05-01", explicit=True))
    api.post("/api/library/albums", json=ALBUM)
    api.post("/api/library/albums", json={**ALBUM, "id": "777", "source": "sc", "playlist_id": None, "year": None})
    api.post("/api/library/artists", json=ARTIST)
    p1 = api.post("/api/playlists", json={"name": "Дорога"}).json()["id"]
    for i in (A, B, C):
        api.post(f"/api/playlists/{p1}/tracks", json=track_payload(id_=i))
    api.patch(f"/api/playlists/{p1}/tracks/order", json={"keys": [f"yt:{C}", f"yt:{A}", f"yt:{B}"]})
    p2 = api.post("/api/playlists", json={"name": "Пустой"}).json()["id"]
    assert p2
    api.put("/api/settings", json={"theme": "light", "default_source": "sc", "autoplay": False, "volume": 0.35,
                                   "profile_name": "Андрей", "history_limit": 2000,
                                   "mix_covers": {"theme_a": COVER}})
    api.post("/api/history", json=track_payload(id_=D))                     # история в экспорт не входит


def _state(api) -> dict:
    """Всё, что пользователь видит в библиотеке (без внутренних id плейлистов)."""
    pls = []
    for p in api.get("/api/playlists").json()["items"]:
        body = api.get(f"/api/playlists/{p['id']}").json()
        pls.append({**{k: v for k, v in p.items() if k != "id"}, "tracks": body["tracks"]})
    return {"favorites": api.get("/api/favorites").json()["items"],
            "albums": api.get("/api/library/albums").json()["items"],
            "artists": api.get("/api/library/artists").json()["items"],
            "playlists": pls, "settings": api.get("/api/settings").json()["values"]}


def _rows(profile) -> dict:
    """Строки базы, которые должны совпасть 1:1 (без автоинкрементных id)."""
    return {
        "favorites": profile.sql("SELECT key, data, added_at FROM favorites ORDER BY key"),
        "favorite_items": profile.sql("SELECT kind, source, item_id, data, added_at FROM favorite_items ORDER BY 1, 2, 3"),
        "playlists": profile.sql("SELECT name, created_at, updated_at FROM playlists ORDER BY name"),
        "playlist_tracks": profile.sql("SELECT p.name, t.position, t.key, t.data, t.added_at FROM playlist_tracks t"
                                       " JOIN playlists p ON p.id = t.playlist_id ORDER BY p.name, t.position"),
    }


def _fresh_profile(profile) -> None:
    """Тот же временный каталог, но базы нет: как новая установка Wave."""
    store.close()
    for suffix in ("", "-wal", "-shm"):
        (profile.data / f"wave.db{suffix}").unlink(missing_ok=True)


def _no_time(doc: dict) -> dict:
    return {k: v for k, v in doc.items() if k != "exported_at"}


def _post(api, doc) -> object:
    raw = doc if isinstance(doc, (bytes, str)) else json.dumps(doc, ensure_ascii=False)
    return api.post("/api/library/import", content=raw.encode() if isinstance(raw, str) else raw,
                    headers={"Content-Type": "application/json"})


# ---------------------------------------------------------------------------
def test_export_shape(api):
    _fill(api)
    r = api.get("/api/library/export")
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    doc = r.json()
    assert (doc["format"], doc["version"]) == ("wave-library", 1)
    assert [e["track"]["id"] for e in doc["favorites"]["tracks"]] == ["1000000001", B, A]
    assert all(isinstance(e["added_at"], float) for e in doc["favorites"]["tracks"])
    assert [e["item"]["id"] for e in doc["favorites"]["albums"]] == ["777", "MPREb_album1"]
    assert [e["item"]["id"] for e in doc["favorites"]["artists"]] == ["UCartist0001"]
    assert [p["name"] for p in doc["playlists"]] == ["Пустой", "Дорога"]
    assert [e["track"]["id"] for e in doc["playlists"][1]["tracks"]] == [C, A, B]
    assert doc["settings"]["theme"] == "light" and doc["settings"]["history_limit"] == 2000
    assert "downloads_dir" not in doc["settings"] and "playback_state" not in json.dumps(doc)
    assert "history" not in doc and str(api.profile.root) not in json.dumps(doc, ensure_ascii=False)   # путей нет


def test_export_import_on_clean_profile_is_one_to_one(harness, wave_profile):
    with harness.client() as api:
        _fill(api)
        doc = api.get("/api/library/export").json()
        state, rows = _state(api), _rows(api.profile)
    _fresh_profile(wave_profile)
    with harness.client() as api:
        assert _state(api)["favorites"] == []                               # профиль действительно пустой
        r = _post(api, doc)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["imported"] == {"tracks": 3, "albums": 2, "artists": 1, "playlists": 2, "playlist_tracks": 3,
                                    "settings": len(collection.EXPORT_SETTINGS)}
        assert body["skipped"] == {"existing": 0, "duplicate_playlists": 0, "missing_local": 0}
        assert body["settings"]["values"]["theme"] == "light"
        assert _state(api) == state
        assert _rows(api.profile) == rows
        assert _no_time(api.get("/api/library/export").json()) == _no_time(doc)       # второй экспорт — тот же файл
        assert api.get("/api/history").json()["tracks"] == []


def test_repeated_import_merges_without_duplicates(api):
    _fill(api)
    doc = api.get("/api/library/export").json()
    rows = _rows(api.profile)
    body = _post(api, doc).json()
    assert body["imported"]["tracks"] == body["imported"]["playlists"] == body["imported"]["albums"] == 0
    assert body["skipped"] == {"existing": 6, "duplicate_playlists": 2, "missing_local": 0}
    assert _rows(api.profile) == rows


def test_import_keeps_existing_favorites_and_adds_new(api):
    api.post("/api/favorites", json=track_payload(id_=D, title="Моя версия"))
    doc = {"format": "wave-library", "version": 1,
           "favorites": {"tracks": [{"added_at": 100.0, "track": track_payload(id_=D, title="Из файла")},
                                    {"added_at": 50.0, "track": track_payload(id_=A)}]}}
    body = _post(api, doc).json()
    assert body["imported"]["tracks"] == 1 and body["skipped"]["existing"] == 1
    favs = {t["id"]: t["title"] for t in api.get("/api/favorites").json()["items"]}
    assert favs == {D: "Моя версия", A: f"Песня {A}"}


def test_export_to_file_in_downloads(api):
    _fill(api)
    r = api.post("/api/library/export/file")
    assert r.status_code == 200
    from pathlib import Path
    path = Path(r.json()["path"])
    assert path.parent == api.profile.downloads and path.name.startswith("wave-library-") and path.suffix == ".json"
    assert _no_time(json.loads(path.read_text("utf-8"))) == _no_time(api.get("/api/library/export").json())
    second = Path(api.post("/api/library/export/file").json()["path"])
    assert second != path and path.is_file()                                # прежний файл не перезаписан


# ---------------------------------------------------------------------------
# Проверка файла: при любой ошибке ничего не меняется
# ---------------------------------------------------------------------------
def _valid_doc():
    return {"format": "wave-library", "version": 1,
            "favorites": {"tracks": [{"added_at": 1.0, "track": track_payload(id_=A)}],
                          "albums": [{"added_at": 1.0, "item": ALBUM}], "artists": [{"added_at": 1.0, "item": ARTIST}]},
            "playlists": [{"name": "Новый", "created_at": 1.0, "updated_at": 2.0,
                           "tracks": [{"added_at": 1.0, "track": track_payload(id_=B)}]}],
            "settings": {"theme": "dark"}}


def _broken(mutate):
    doc = _valid_doc()
    mutate(doc)
    return doc


BAD = {
    "not json": b"{not json",
    "not utf8": b"\xff\xfe\x00",
    "array": b"[]",
    "nan": b'{"format": "wave-library", "version": 1, "playlists": [{"name": "x", "created_at": NaN, "updated_at": 1, "tracks": []}]}',
    "other format": _broken(lambda d: d.update(format="other")),
    "future version": _broken(lambda d: d.update(version=2)),
    "unknown top key": _broken(lambda d: d.update(extra=1)),
    "bad track id": _broken(lambda d: d["playlists"][0]["tracks"].append({"added_at": 1, "track": track_payload(id_="../x")})),
    "bad source": _broken(lambda d: d["favorites"]["tracks"].append({"added_at": 1, "track": track_payload("spotify", A)})),
    "duplicate favorite": _broken(lambda d: d["favorites"]["tracks"].append({"added_at": 2, "track": track_payload(id_=A)})),
    "duplicate in playlist": _broken(lambda d: d["playlists"][0]["tracks"].append({"added_at": 2, "track": track_payload(id_=B)})),
    "duplicate album": _broken(lambda d: d["favorites"]["albums"].append({"added_at": 2, "item": ALBUM})),
    "bad date": _broken(lambda d: d["favorites"]["tracks"][0].update(added_at=-5)),
    "bool date": _broken(lambda d: d["favorites"]["tracks"][0].update(added_at=True)),
    "empty playlist name": _broken(lambda d: d["playlists"][0].update(name="  ")),
    "control in name": _broken(lambda d: d["playlists"][0].update(name="a\u0007b")),
    "unknown setting": _broken(lambda d: d["settings"].update(downloads_dir="C:\\Windows")),
    "bad setting": _broken(lambda d: d["settings"].update(volume=5)),
    "album bad id": _broken(lambda d: d["favorites"]["albums"].append({"added_at": 2, "item": {**ALBUM, "id": "a/b"}})),
    "missing track field": _broken(lambda d: d["favorites"]["tracks"][0]["track"].pop("title")),
}


@pytest.mark.parametrize("name", list(BAD))
def test_invalid_file_is_rejected_atomically(api, name):
    _fill(api)
    before = legacy_db.dump(api.profile.db_path)
    r = _post(api, BAD[name])
    assert r.status_code == 400, (name, r.text)
    assert "detail" in r.json()
    assert legacy_db.dump(api.profile.db_path) == before


def test_valid_part_is_not_written_when_later_part_is_bad(api):
    """Ошибка в последнем плейлисте — первые разделы файла тоже не записаны."""
    doc = _valid_doc()
    doc["playlists"].append({"name": "Битый", "created_at": 1, "updated_at": 1, "tracks": [{"added_at": 1, "track": {"id": 1}}]})
    assert _post(api, doc).status_code == 400
    assert api.get("/api/favorites").json()["items"] == [] and api.get("/api/playlists").json()["items"] == []
    assert api.get("/api/settings").json()["values"]["theme"] == "auto"


def test_failure_during_write_rolls_back(api, monkeypatch):
    _fill(api)
    before = legacy_db.dump(api.profile.db_path)
    doc = _valid_doc()
    doc["settings"]["history_limit"] = 1000

    def boom(db, limit):
        raise RuntimeError("сбой посреди записи")

    monkeypatch.setattr(userdata, "_trim", boom)                             # последний шаг транзакции импорта
    r = _post(api, doc)
    assert r.status_code == 500
    assert legacy_db.dump(api.profile.db_path) == before


def test_too_large_file_is_refused(api, monkeypatch):
    monkeypatch.setattr(collection, "IMPORT_MAX_BYTES", 1000)
    r = _post(api, json.dumps(_valid_doc()) + " " * 2000)
    assert r.status_code == 413
    assert api.get("/api/favorites").json()["items"] == []


def test_untrusted_urls_in_file_are_sanitised(api):
    doc = _valid_doc()
    doc["favorites"]["tracks"][0]["track"].update(cover="https://evil.example/x.jpg", url="javascript:alert(1)",
                                                  stream_url="http://evil.example/a.mp3")
    doc["favorites"]["artists"][0]["item"]["cover"] = "http://googleusercontent.com/x"
    assert _post(api, doc).status_code == 200
    t = api.get("/api/favorites").json()["items"][0]
    assert (t["cover"], t["url"], t["stream_url"]) == (None, None, f"/api/stream/yt/{A}")
    assert api.get("/api/library/artists").json()["items"][0]["cover"] is None


def test_local_tracks_are_imported_only_if_file_exists(api):
    doc = _valid_doc()
    local = track_payload("local", "0123456789abcdef", cover=None, artist_id=None, artist_ids=[])
    doc["favorites"]["tracks"].append({"added_at": 3.0, "track": local})
    doc["playlists"][0]["tracks"].append({"added_at": 3.0, "track": local})
    body = _post(api, doc).json()
    assert body["skipped"]["missing_local"] == 2 and body["imported"]["tracks"] == 1
    assert [t["id"] for t in api.get("/api/favorites").json()["items"]] == [A]
