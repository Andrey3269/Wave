"""
Индекс загрузок не доверяется (находка N3): строка базы с путём за пределы каталога загрузок не даёт
прочитать, отдать или удалить посторонний файл ни одним эндпоинтом и не приводит к HTTP 500.
"""
from __future__ import annotations

import json

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
