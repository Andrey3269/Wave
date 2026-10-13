"""
Схема SQLite версии v1 (backend/persistence/migrations.py, S6) — та же, что создавал код до S6.
Если схема меняется, этот тест обновляется вместе с новой миграцией и тестом в test_db_migrations.py.
"""
from __future__ import annotations

import sqlite3

import library
import paths
import playlists
import userdata
from wavetest.fakes import audio_bytes

EXPECTED_COLUMNS = {
    "downloads": ["key", "source", "track_id", "title", "artist", "album", "duration", "folder", "audio", "cover",
                  "lyrics", "size", "created_at", "root"],
    "favorites": ["key", "data", "added_at"],
    "history": ["id", "key", "data", "played_at", "skipped"],
    "settings": ["key", "value"],
    "playlists": ["id", "name", "created_at", "updated_at"],
    "playlist_tracks": ["id", "playlist_id", "position", "key", "data", "added_at"],
}


def _init_all():
    library.init()
    userdata.init()
    playlists.init()


def _columns(db_path, table):
    con = sqlite3.connect(db_path)
    try:
        return [r[1] for r in con.execute(f"PRAGMA table_info({table})")]
    finally:
        con.close()


def test_database_location(db):
    assert paths.data_dir() / "wave.db" == db.db_path
    assert db.db_path.is_file()


def test_schema_v0(db):
    tables = {r[0] for r in db.sql("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")}
    assert tables == set(EXPECTED_COLUMNS)
    for table, cols in EXPECTED_COLUMNS.items():
        assert _columns(db.db_path, table) == cols, table
    indexes = {r[0] for r in db.sql("SELECT name FROM sqlite_master WHERE type = 'index' AND name NOT LIKE 'sqlite_%'")}
    assert indexes == {"history_key", "playlist_tracks_pl"}


def test_schema_version_is_current(db):
    """S6: версия схемы записана в user_version (до S6 было 0 — версионирования не было)."""
    from persistence import migrations
    assert db.sql("PRAGMA user_version") == [(migrations.CURRENT_VERSION,)] == [(1,)]


def test_init_is_idempotent_and_keeps_data(db):
    library._q("INSERT INTO favorites (key, data, added_at) VALUES ('yt:x', '{}', 1)")
    library._q("INSERT INTO settings (key, value) VALUES ('theme', 'dark')")
    _init_all()
    _init_all()
    assert db.sql("SELECT key FROM favorites") == [("yt:x",)]
    assert userdata.get_setting("theme") == "dark"


def test_old_database_without_root_column_is_upgraded(wave_profile):
    """База прошлой версии (downloads без колонки root): init добавляет колонку и не теряет строки."""
    con = sqlite3.connect(wave_profile.db_path)
    con.execute("CREATE TABLE downloads (key TEXT PRIMARY KEY, source TEXT NOT NULL, track_id TEXT NOT NULL, title TEXT,"
                " artist TEXT, album TEXT, duration INTEGER, folder TEXT NOT NULL, audio TEXT NOT NULL, cover TEXT,"
                " lyrics TEXT, size INTEGER, created_at REAL)")
    con.execute("INSERT INTO downloads VALUES ('yt:AAAAAAAAAA1','yt','AAAAAAAAAA1','T','A',NULL,10,'f','audio.m4a',NULL,NULL,3,1.0)")
    con.execute("CREATE TABLE favorites (key TEXT PRIMARY KEY, data TEXT NOT NULL, added_at REAL NOT NULL)")
    con.execute("INSERT INTO favorites VALUES ('yt:AAAAAAAAAA1', '{\"id\": \"AAAAAAAAAA1\"}', 1.0)")
    con.commit()
    con.close()

    _init_all()
    assert _columns(wave_profile.db_path, "downloads") == EXPECTED_COLUMNS["downloads"]
    assert wave_profile.sql("SELECT key, root FROM downloads") == [("yt:AAAAAAAAAA1", None)]
    assert wave_profile.sql("SELECT key FROM favorites") == [("yt:AAAAAAAAAA1",)]


def test_old_row_without_root_resolves_against_current_downloads_dir(wave_profile):
    """Строка без root (старая версия) ищет файл в текущем каталоге загрузок."""
    _init_all()
    folder = wave_profile.downloads / "A - T [yt-AAAAAAAAAA1]"
    folder.mkdir()
    (folder / "audio.m4a").write_bytes(audio_bytes(10))
    library.add(source="yt", track_id="AAAAAAAAAA1", title="T", artist="A", album=None, duration=1,
                folder=folder.name, audio="audio.m4a", cover=None, lyrics=None, size=10, created_at=1.0)
    assert library.audio_path("yt", "AAAAAAAAAA1") == folder / "audio.m4a"


def test_index_row_disappears_when_file_is_gone(db):
    library.add(source="yt", track_id="AAAAAAAAAA1", title="T", artist="A", album=None, duration=1, folder="gone",
                audio="audio.m4a", cover=None, lyrics=None, size=1, created_at=1.0, root=str(db.downloads))
    assert library.get("yt", "AAAAAAAAAA1") is None
    assert library.list_items() == []
    assert db.sql("SELECT COUNT(*) FROM downloads") == [(0,)]


def test_delete_removes_folder_and_row(db):
    folder = db.downloads / "A - T [yt-AAAAAAAAAA1]"
    folder.mkdir()
    for name in ("audio.m4a", "cover.jpg", "metadata.json"):
        (folder / name).write_bytes(b"x")
    library.add(source="yt", track_id="AAAAAAAAAA1", title="T", artist="A", album=None, duration=1,
                folder=folder.name, audio="audio.m4a", cover="cover.jpg", lyrics=None, size=1, created_at=1.0,
                root=str(db.downloads))
    assert library.delete("yt", "AAAAAAAAAA1") is True
    assert not folder.exists()
    assert db.sql("SELECT COUNT(*) FROM downloads") == [(0,)]
    assert library.delete("yt", "AAAAAAAAAA1") is False


def test_download_row_keeps_its_own_root(db, tmp_path):
    """Смена папки загрузок не теряет старые загрузки: у каждой записи свой root."""
    old_root = tmp_path / "old-root"
    folder = old_root / "A - T [yt-AAAAAAAAAA1]"
    folder.mkdir(parents=True)
    (folder / "audio.m4a").write_bytes(b"x")
    library.add(source="yt", track_id="AAAAAAAAAA1", title="T", artist="A", album=None, duration=1,
                folder=folder.name, audio="audio.m4a", cover=None, lyrics=None, size=1, created_at=1.0,
                root=str(old_root))
    paths.set_downloads_dir(tmp_path / "new-root")
    assert library.audio_path("yt", "AAAAAAAAAA1") == folder.resolve() / "audio.m4a"


def test_get_setting_defaults_and_stored_values(db):
    assert userdata.get_setting("theme") == "auto"                       # значение по умолчанию из DEFAULTS
    assert userdata.get_setting("missing", "fallback") == "fallback"
    library._q("INSERT OR REPLACE INTO settings (key, value) VALUES ('downloads_dir', ?)", ("C:\\Музыка",))
    assert userdata.get_setting("downloads_dir") == "C:\\Музыка"
