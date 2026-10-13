-- Более старая база: у downloads ещё нет колонки root (её добавлял ALTER TABLE в library.init),
-- и нет таблиц плейлистов. Точная схема версий до 5a7f27c в репозитории не сохранилась: фикстура
-- моделирует оба известных отличия (нет колонки и нет части таблиц), тексты CREATE — как в 5a7f27c.
CREATE TABLE downloads (
    key TEXT PRIMARY KEY,          -- "source:track_id"
    source TEXT NOT NULL, track_id TEXT NOT NULL,
    title TEXT, artist TEXT, album TEXT, duration INTEGER,
    folder TEXT NOT NULL,          -- имя папки внутри каталога загрузок
    audio TEXT NOT NULL, cover TEXT, lyrics TEXT,
    size INTEGER, created_at REAL
);
CREATE TABLE favorites (key TEXT PRIMARY KEY, data TEXT NOT NULL, added_at REAL NOT NULL);
CREATE TABLE history (id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT NOT NULL, data TEXT NOT NULL, played_at REAL NOT NULL, skipped INTEGER NOT NULL DEFAULT 0);
CREATE INDEX history_key ON history(key);
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
