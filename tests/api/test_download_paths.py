"""
Индекс загрузок не доверяется (находка N3): строка базы с путём за пределы каталога загрузок не даёт
прочитать, отдать или удалить посторонний файл ни одним эндпоинтом и не приводит к HTTP 500.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import library

YT = "AAAAAAAAAA1"
OWN = library.safe_folder("Артист", "Песня", "yt", YT)


@pytest.fixture
def outside(api):
    """Посторонний каталог рядом с каталогом загрузок, с «секретными» файлами."""
    d = api.profile.root / "outside"
    d.mkdir()
    for name in ("audio.mp3", "cover.jpg", "lyrics.json"):
        (d / name).write_bytes(b"SECRET")
    (d / "lyrics.json").write_text(json.dumps({"synced": [], "plain": "SECRET", "provider": "x"}), encoding="utf-8")
    return d


def add_row(profile, **over):
    row = dict(source="yt", track_id=YT, title="Песня", artist="Артист", album=None, duration=60, folder=OWN,
               audio="audio.mp3", cover="cover.jpg", lyrics="lyrics.json", size=6, created_at=1.0,
               root=str(profile.downloads))
    row.update(over)
    library.add(**row)


def snapshot(d):
    return sorted((p.name, p.read_bytes()) for p in d.iterdir())


BAD_ROWS = [
    pytest.param({"folder": "../outside"}, id="folder-dotdot"),
    pytest.param({"folder": "x/../../outside"}, id="folder-nested-dotdot"),
    pytest.param({"folder": "outside", "root": "relative/root"}, id="root-relative"),
    pytest.param({"audio": "../../outside/audio.mp3", "cover": "../../outside/cover.jpg",
                  "lyrics": "../../outside/lyrics.json"}, id="files-dotdot"),
]


def _absolute_outside(profile):
    return {"folder": str(profile.root / "outside")}


@pytest.mark.parametrize("over", BAD_ROWS)
def test_unsafe_row_is_never_read(api, outside, over):
    (api.profile.downloads / OWN).mkdir()                       # папка с правильным именем есть, но пуста
    add_row(api.profile, **over)
    before = snapshot(outside)

    listed = api.get("/api/downloads")
    assert listed.status_code == 200 and listed.json()["items"] == []

    play = api.get(f"/api/stream/yt/{YT}", headers={"Range": "bytes=0-"})
    assert play.status_code < 500 or play.headers.get("x-wave-error")    # уходит к источнику, а не к чужому файлу
    assert b"SECRET" not in play.content

    cover = api.get(f"/api/local/yt/{YT}/cover")
    assert cover.status_code == 404 and b"SECRET" not in cover.content

    lyr = api.get("/api/lyrics", params={"artist": "Артист", "title": "Песня", "source": "yt", "id": YT})
    assert lyr.status_code == 200 and "SECRET" not in lyr.text

    assert snapshot(outside) == before
    # Строка — данные пользователя: при чтении она пропускается, но не удаляется
    assert api.profile.sql("SELECT COUNT(*) FROM downloads") == [(1,)]


def test_absolute_folder_is_never_read(api, outside):
    add_row(api.profile, **_absolute_outside(api.profile))
    assert api.get("/api/downloads").json()["items"] == []
    assert api.get(f"/api/local/yt/{YT}/cover").status_code == 404
    assert library.audio_path("yt", YT) is None


@pytest.mark.parametrize("over", [*BAD_ROWS, pytest.param({"folder": "outside", "root": None}, id="foreign-folder")])
def test_delete_never_touches_foreign_files(api, outside, over):
    if over.get("root", "") is None:                             # чужая папка внутри «своего» корня
        over = {**over, "root": str(api.profile.root)}
    add_row(api.profile, **over)
    before = snapshot(outside)
    r = api.delete(f"/api/downloads/yt/{YT}")
    # Папка не своя -> files_kept; своя папка с «плохими» именами файлов удаляется целиком, наружу не выходя
    expected = {"ok": True} if over.get("folder", OWN) == OWN else {"ok": True, "files_kept": True}
    assert r.status_code == 200 and r.json() == expected
    assert snapshot(outside) == before
    assert api.profile.sql("SELECT COUNT(*) FROM downloads") == [(0,)]


def test_delete_absolute_folder_keeps_files(api, outside):
    add_row(api.profile, **_absolute_outside(api.profile))
    before = snapshot(outside)
    assert api.delete(f"/api/downloads/yt/{YT}").json() == {"ok": True, "files_kept": True}
    assert snapshot(outside) == before and outside.is_dir()


def test_symlink_folder_pointing_outside_is_ignored(api, outside):
    link = api.profile.downloads / OWN
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("символические ссылки недоступны в этой среде")
    add_row(api.profile)
    assert api.get("/api/downloads").json()["items"] == []
    assert api.get(f"/api/local/yt/{YT}/cover").status_code == 404
    before = snapshot(outside)
    assert api.delete(f"/api/downloads/yt/{YT}").json() == {"ok": True, "files_kept": True}
    assert snapshot(outside) == before


def test_own_folder_is_deleted_completely(api):
    d = api.profile.downloads / OWN
    d.mkdir()
    for name in ("audio.mp3", "cover.jpg", "lyrics.json", "metadata.json"):
        (d / name).write_bytes(b"x")
    add_row(api.profile)
    assert [t["id"] for t in api.get("/api/downloads").json()["items"]] == [YT]
    assert api.delete(f"/api/downloads/yt/{YT}").json() == {"ok": True}
    assert not d.exists()
    assert sorted(p.name for p in api.profile.downloads.iterdir()) == []


@pytest.mark.parametrize("name, ok", [("audio.m4a", True), ("Артист - Песня [yt-x]", True), ("..", False), (".", False),
                                      ("", False), ("a/b", False), ("a\\b", False), ("C:x", False), (None, False)])
def test_single_component_rule(name, ok):
    assert library._component(name) is ok


# ---------------------------------------------------------------------------
# S13: остаток N3 — каталог загрузок из строки (root), ссылки внутри своей папки, испорченные файлы,
# папка загрузок из настроек. Ответ — отказ (404 / пропуск строки), а не 500 и не чужой файл.
# ---------------------------------------------------------------------------
def _assert_not_served(api):
    listed = api.get("/api/downloads")
    assert listed.status_code == 200 and listed.json()["items"] == []
    play = api.get(f"/api/stream/yt/{YT}", headers={"Range": "bytes=0-"})
    assert b"SECRET" not in play.content and (play.status_code < 500 or play.headers.get("x-wave-error"))
    pb = api.get(f"/api/stream/yt/{YT}/playback")
    assert pb.status_code != 200 or pb.json()["kind"] != "local"
    cover = api.get(f"/api/local/yt/{YT}/cover")
    assert cover.status_code == 404 and b"SECRET" not in cover.content
    lyr = api.get("/api/lyrics", params={"artist": "Артист", "title": "Песня", "source": "yt", "id": YT})
    assert lyr.status_code == 200 and "SECRET" not in lyr.text


def test_foreign_folder_under_root_from_database_is_never_read(api, outside):
    """root из строки базы указывает на чужой каталог, folder — обычная папка в нём: файлы не читаются и не удаляются."""
    add_row(api.profile, folder="outside", root=str(api.profile.root))
    before = snapshot(outside)
    _assert_not_served(api)
    assert api.delete(f"/api/downloads/yt/{YT}").json() == {"ok": True, "files_kept": True}
    assert snapshot(outside) == before


def test_wave_folder_of_another_track_is_not_read_for_this_key(api):
    """Ключ строки не совпадает с её источником и id: папка другого трека не отдаётся под чужим ключом."""
    other = "BBBBBBBBBB1"
    d = api.profile.downloads / library.safe_folder("Другой", "Трек", "yt", other)
    d.mkdir()
    for name in ("audio.mp3", "cover.jpg"):
        (d / name).write_bytes(b"SECRET")
    import sqlite3
    con = sqlite3.connect(api.profile.db_path)
    with con:
        con.execute("INSERT INTO downloads (key, source, track_id, title, artist, album, duration, folder, audio, cover,"
                    " lyrics, size, created_at, root) VALUES (?, 'yt', ?, 't', 'a', NULL, 1, ?, 'audio.mp3', 'cover.jpg',"
                    " NULL, 6, 1, ?)", (f"yt:{YT}", other, d.name, str(api.profile.downloads)))
    con.close()
    _assert_not_served(api)
    assert sorted(p.name for p in d.iterdir()) == ["audio.mp3", "cover.jpg"]


@pytest.mark.parametrize("bad_id", ["../x", "a/b", "short"])
def test_row_with_invalid_track_id_is_never_read(api, outside, bad_id):
    import sqlite3
    con = sqlite3.connect(api.profile.db_path)
    with con:
        con.execute("INSERT INTO downloads (key, source, track_id, title, artist, album, duration, folder, audio, cover,"
                    " lyrics, size, created_at, root) VALUES (?, 'yt', ?, 't', 'a', NULL, 1, ?, 'audio.mp3', 'cover.jpg',"
                    " NULL, 6, 1, ?)", (f"yt:{bad_id}", bad_id, f"x [yt-{bad_id}]", str(api.profile.root)))
    con.close()
    assert api.get("/api/downloads").json()["items"] == []
    assert api.profile.sql("SELECT COUNT(*) FROM downloads") == [(1,)]


@pytest.mark.parametrize("column", ["audio", "cover", "lyrics"])
def test_symlinked_file_in_own_folder_is_not_served(api, outside, column):
    d = api.profile.downloads / OWN
    d.mkdir()
    for name in ("audio.mp3", "cover.jpg", "lyrics.json", "metadata.json"):
        (d / name).write_bytes(b"own")
    (d / "lyrics.json").write_text(json.dumps({"synced": [], "plain": "своё", "provider": "lrclib"}), encoding="utf-8")
    name = {"audio": "audio.mp3", "cover": "cover.jpg", "lyrics": "lyrics.json"}[column]
    (d / name).unlink()
    try:
        (d / name).symlink_to(outside / name)
    except (OSError, NotImplementedError):
        pytest.skip("символические ссылки недоступны в этой среде")
    add_row(api.profile)
    assert b"SECRET" not in api.get(f"/api/stream/yt/{YT}").content
    assert b"SECRET" not in api.get(f"/api/local/yt/{YT}/cover").content
    assert "SECRET" not in api.get("/api/lyrics", params={"artist": "Артист", "title": "Песня", "source": "yt", "id": YT}).text
    before = snapshot(outside)
    assert api.delete(f"/api/downloads/yt/{YT}").json() == {"ok": True}      # своя папка удаляется, ссылка — без цели
    assert not d.exists() and snapshot(outside) == before


# короткие id: длинный id параметра попадает в PYTEST_CURRENT_TEST, а на Windows переменная окружения ≤ 32 767 символов
@pytest.mark.parametrize("content", [b"{broken", b"\xff\xfe\x00garbage", b"[1, 2]", b'{"synced": "x"}', b"", b"[" * 50_000],
                         ids=["broken-json", "not-utf8", "array", "synced-not-list", "empty", "deep-nesting"])
def test_corrupt_saved_lyrics_fall_back_to_service_not_500(api, content):
    d = api.profile.downloads / OWN
    d.mkdir()
    for name in ("audio.mp3", "metadata.json"):
        (d / name).write_bytes(b"own")
    (d / "lyrics.json").write_bytes(content)
    add_row(api.profile, cover=None)
    r = api.get("/api/lyrics", params={"artist": "Артист", "title": "Песня", "source": "yt", "id": YT})
    assert r.status_code == 200 and set(r.json()) >= {"synced", "plain"}


# --- папка загрузок из настроек (строка settings.downloads_dir) ---
@pytest.fixture
def user_mode(harness, monkeypatch):
    """Обычный пользователь: папка загрузок не задана переменной окружения, выбор хранится в базе."""
    monkeypatch.delenv("WAVE_DOWNLOADS_DIR")
    with harness.client():                                   # создаём базу текущей схемы
        pass
    return harness


def _save_dir(harness, value) -> None:
    import paths
    import sqlite3
    con = sqlite3.connect(harness.profile.db_path)
    with con:
        con.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('downloads_dir', ?)", (value,))
    con.close()
    paths.set_downloads_dir(None)                            # новый запуск: выбор папки в памяти потерян


@pytest.mark.parametrize("kind", ["relative", "file", "nul", "empty"])
def test_invalid_downloads_dir_in_database_is_ignored(user_mode, tmp_path, kind):
    import paths
    blocker = tmp_path / "file.txt"
    blocker.write_text("x")
    value = {"relative": "relative/dir", "file": str(blocker), "nul": str(tmp_path / "a\x00b"), "empty": "  "}[kind]
    _save_dir(user_mode, value)
    with user_mode.client() as h:
        s = h.get("/api/settings")
        assert s.status_code == 200 and s.json()["paths"]["downloads"] == str(paths.default_downloads_dir())
        assert h.get("/api/downloads").status_code == 200
    assert not (Path.cwd() / "relative").exists()


def test_unavailable_downloads_dir_does_not_break_reads(user_mode, tmp_path, outside):
    """Папка из настроек недоступна (путь через файл, как отключённый диск): чтение — 200, загрузка — ошибка задачи."""
    blocker = tmp_path / "file.txt"
    blocker.write_text("x")
    unavailable = blocker / "Wave"
    _save_dir(user_mode, str(unavailable))
    add = dict(root=None)
    with user_mode.client() as h:
        library.add(**{**dict(source="yt", track_id=YT, title="Песня", artist="Артист", album=None, duration=60,
                              folder=OWN, audio="audio.mp3", cover="cover.jpg", lyrics=None, size=6, created_at=1.0), **add})
        s = h.get("/api/settings")
        assert s.status_code == 200 and s.json()["paths"]["downloads"] == str(unavailable)
        listed = h.get("/api/downloads")
        assert listed.status_code == 200 and listed.json()["items"] == []
        assert h.get(f"/api/local/yt/{YT}/cover").status_code == 404
        assert h.delete(f"/api/downloads/yt/{YT}").status_code == 200
        r = h.post("/api/downloads", json={"source": "yt", "id": YT, "title": "Песня", "artist": "Артист"})
        assert r.status_code == 202
        import time
        for _ in range(200):
            job = h.get("/api/downloads/jobs").json().get(f"yt:{YT}", {})
            if job.get("state") not in ("queued", "preparing", "downloading", "processing"):
                break
            time.sleep(0.02)
        assert job.get("state") == "error"
    assert blocker.read_text() == "x"
