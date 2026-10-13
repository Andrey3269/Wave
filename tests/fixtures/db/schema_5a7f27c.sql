-- Схема wave.db, которую создавал код исходного коммита 5a7f27c (library.init, userdata.init, playlists.init).
-- Снята 2026-10-13 запуском init() этого коммита на пустом профиле: тексты CREATE совпадают с sqlite_master
-- дословно, порядок тот же. user_version = 0, journal_mode = delete.
CREATE TABLE downloads (
    key TEXT PRIMARY KEY,          -- "source:track_id"
    source TEXT NOT NULL, track_id TEXT NOT NULL,
    title TEXT, artist TEXT, album TEXT, duration INTEGER,
    folder TEXT NOT NULL,          -- имя папки внутри каталога загрузок
    audio TEXT NOT NULL, cover TEXT, lyrics TEXT,
    size INTEGER, created_at REAL,
    root TEXT                      -- каталог загрузок на момент скачивания
);
CREATE TABLE favorites (key TEXT PRIMARY KEY, data TEXT NOT NULL, added_at REAL NOT NULL);
CREATE TABLE history (id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT NOT NULL, data TEXT NOT NULL, played_at REAL NOT NULL, skipped INTEGER NOT NULL DEFAULT 0);
CREATE INDEX history_key ON history(key);
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE playlists (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE playlist_tracks (id INTEGER PRIMARY KEY AUTOINCREMENT, playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE, position INTEGER NOT NULL, key TEXT NOT NULL, data TEXT NOT NULL, added_at REAL NOT NULL, UNIQUE (playlist_id, key));
CREATE INDEX playlist_tracks_pl ON playlist_tracks(playlist_id, position);
