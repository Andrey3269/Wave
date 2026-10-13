"""
Тестовые базы прошлых версий Wave для tests/unit/test_db_migrations.py (S6).

Схемы — tests/fixtures/db/*.sql (дословно как создавал старый код). Данные заполняются здесь так, как
их писали прошлые версии: снимки треков в JSON без полей, появившихся позже (explicit, plays, released),
кириллица, пропуски в history.skipped, «дырки» в позициях плейлиста после удаления трека, строки
загрузок с root и без него.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "db"
SCHEMAS = {
    "5a7f27c": FIXTURES / "schema_5a7f27c.sql",
    "without_root": FIXTURES / "schema_without_root.sql",
}


def _track(i: int, source: str = "yt") -> dict:
    tid = f"LEGACY{i:05d}" if source == "yt" else str(1_000_000_000 + i)
    return {"id": tid, "source": source, "title": f"Песня №{i} «ВОЛНА»", "artist": "Исполнитель",
            "artists": ["Исполнитель", "Гость"], "album": None if i % 3 else "Альбом", "duration": 180 + i,
            "cover": f"https://lh3.googleusercontent.com/c{i}=w544-h544", "stream_url": f"/api/stream/{source}/{tid}",
            "artist_id": "UClegacy", "artist_ids": ["UClegacy", None], "url": None}


def _columns(con: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in con.execute(f"PRAGMA table_info({table})")]


def build(path: Path, schema: str, history_rows: int = 40) -> None:
    """Создаёт базу со схемой `schema` и данными, как их оставила бы прошлая версия."""
    con = sqlite3.connect(path)
    try:
        con.executescript(SCHEMAS[schema].read_text(encoding="utf-8"))
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}

        has_root = "root" in _columns(con, "downloads")
        for i, (source, root) in enumerate((("yt", "C:\\Users\\Пользователь\\Music\\Wave"), ("sc", None))):
            t = _track(i, source)
            row = [f"{source}:{t['id']}", source, t["id"], t["title"], t["artist"], t["album"], t["duration"],
                   f"{t['artist']} - {t['title']} [{source}-{t['id']}]", "audio.m4a",
                   "cover.jpg" if i == 0 else None, "lyrics.lrc" if i == 0 else None, 4_000_000 + i, 1_700_000_000.5 + i]
            if has_root:
                row.append(root)
            con.execute(f"INSERT INTO downloads VALUES ({','.join('?' * len(row))})", row)

        for i in range(3):
            t = _track(10 + i, "sc" if i == 2 else "yt")
            con.execute("INSERT INTO favorites VALUES (?, ?, ?)",
                        (f"{t['source']}:{t['id']}", json.dumps(t, ensure_ascii=False), 1_700_000_100.0 + i))

        for i in range(history_rows):
            t = _track(20 + i % 7)
            con.execute("INSERT INTO history (key, data, played_at, skipped) VALUES (?, ?, ?, ?)",
                        (f"yt:{t['id']}", json.dumps(t, ensure_ascii=False), 1_700_001_000.0 + i, int(i % 5 == 0)))

        con.executemany("INSERT INTO settings VALUES (?, ?)", [
            ("theme", "dark"), ("default_source", "sc"), ("autoplay", "0"), ("volume", "0.35"),
            ("downloads_dir", "C:\\Users\\Пользователь\\Music\\Wave"), ("profile_name", "Слушатель"),
            ("mix_covers", '{"indie":"https://lh3.googleusercontent.com/m=w544-h544"}'), ("ui_local_migrated", "1"),
        ])

        if "playlists" in tables:
            con.executemany("INSERT INTO playlists (name, created_at, updated_at) VALUES (?, ?, ?)",
                            [("Дорога домой", 1_700_002_000.0, 1_700_002_500.0), ("Пустой", 1_700_002_100.0, 1_700_002_100.0)])
            for pos in (0, 1, 3, 4):                     # позиция 2 — трек удалили, «дырка» осталась
                t = _track(40 + pos)
                con.execute("INSERT INTO playlist_tracks (playlist_id, position, key, data, added_at) VALUES (1, ?, ?, ?, ?)",
                            (pos, f"yt:{t['id']}", json.dumps(t, ensure_ascii=False), 1_700_002_200.0 + pos))
        con.commit()
    finally:
        con.close()


def dump(path: Path) -> dict[str, list[tuple]]:
    """Всё содержимое пользовательских таблиц, строки по первичному ключу — для сравнения до и после."""
    con = sqlite3.connect(path)
    try:
        tables = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        return {t: con.execute(f"SELECT * FROM {t} ORDER BY 1").fetchall() for t in tables}
    finally:
        con.close()
