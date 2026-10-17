"""
Версии схемы wave.db (S6). Номер версии хранится в `PRAGMA user_version`.

* v0 — база без номера: всё, что создавали версии Wave до S6 (исходный `5a7f27c` и более ранние, где у
  `downloads` ещё не было колонки `root`), а также совсем пустой файл.
* v1 — фиксация рабочей схемы на момент S6: `downloads` (с `root`), `favorites`, `history`, `settings`,
  `playlists`, `playlist_tracks` и их индексы. Таблицы и колонки те же, что создавал прежний код, поэтому
  база v1 открывается и предыдущими версиями Wave (они создают таблицы через IF NOT EXISTS).
* v2 (S14) — таблица `rec_exclusions`: исключения из рекомендаций (трек или исполнитель). Только новая таблица,
  существующие таблицы и строки не меняются. Версии Wave S6–S13 (знают до v1) базу v2 не откроют (п. 1 ниже);
  для отката на них — резервная копия `backups/wave-<дата>-v1.db`, сделанная при обновлении.
* v3 (S15) — таблицы `favorite_items` (избранные альбомы и исполнители) и `local_tracks` (локальная музыка,
  импортированная в Wave). Только новые таблицы, прежние таблицы и строки не меняются. Версии Wave до S15 (знают
  до v2) базу v3 не откроют; для отката — копия `backups/wave-<дата>-v2.db`, сделанная при обновлении.

Порядок обновления базы (`migrate`):
1. Номер версии больше известного → ошибка запуска: база от более новой Wave, трогать её нельзя.
2. Номер равен текущему → только проверка, что нужные таблицы и колонки на месте.
3. Номер меньше → резервная копия через `sqlite3.backup()` в `<data>/backups/wave-<дата>-v<N>.db`, где
   N — версия схемы **в копии** (то есть до обновления); хранятся три последние копии. Затем все шаги одной
   транзакцией вместе с новым `user_version` и проверкой результата. Любая ошибка → откат, исходная база
   не изменена, приложение не стартует (MigrationError с понятным текстом).
Пустой файл (новая установка) обновляется без копии: сохранять в нём нечего.

Новую версию добавляют только при реальной необходимости: функция `_vN`, запись в MIGRATIONS,
CURRENT_VERSION, REQUIRED_COLUMNS и тест в tests/unit/test_db_migrations.py на базе прошлой версии.
"""
from __future__ import annotations

import logging
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

log = logging.getLogger("musicmeta.db")

CURRENT_VERSION = 3
BACKUP_KEEP = 3                     # сколько последних резервных копий хранить

# ---------------------------------------------------------------------------
# v1: рабочая схема на момент S6. Текст совпадает с тем, что создавали library.py, userdata.py,
# playlists.py до S6 (порядок колонок важен: на него опирается tests/unit/test_sqlite_schema.py).
# ---------------------------------------------------------------------------
V1_TABLES = (
    "CREATE TABLE IF NOT EXISTS downloads ("
    " key TEXT PRIMARY KEY,"                    # "source:track_id"
    " source TEXT NOT NULL, track_id TEXT NOT NULL,"
    " title TEXT, artist TEXT, album TEXT, duration INTEGER,"
    " folder TEXT NOT NULL,"                    # имя папки внутри каталога загрузок
    " audio TEXT NOT NULL, cover TEXT, lyrics TEXT,"
    " size INTEGER, created_at REAL,"
    " root TEXT)",                              # каталог загрузок на момент скачивания
    "CREATE TABLE IF NOT EXISTS favorites (key TEXT PRIMARY KEY, data TEXT NOT NULL, added_at REAL NOT NULL)",
    "CREATE TABLE IF NOT EXISTS history (id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT NOT NULL,"
    " data TEXT NOT NULL, played_at REAL NOT NULL, skipped INTEGER NOT NULL DEFAULT 0)",
    "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS playlists (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,"
    " created_at REAL NOT NULL, updated_at REAL NOT NULL)",
    "CREATE TABLE IF NOT EXISTS playlist_tracks (id INTEGER PRIMARY KEY AUTOINCREMENT,"
    " playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE, position INTEGER NOT NULL,"
    " key TEXT NOT NULL, data TEXT NOT NULL, added_at REAL NOT NULL, UNIQUE (playlist_id, key))",
)
V1_INDEXES = (
    "CREATE INDEX IF NOT EXISTS history_key ON history(key)",
    "CREATE INDEX IF NOT EXISTS playlist_tracks_pl ON playlist_tracks(playlist_id, position)",
)

# Колонки, без которых код текущей версии не работает. Проверяются после обновления и при каждом запуске.
REQUIRED_COLUMNS: dict[str, tuple[str, ...]] = {
    "downloads": ("key", "source", "track_id", "title", "artist", "album", "duration", "folder", "audio",
                  "cover", "lyrics", "size", "created_at", "root"),
    "favorites": ("key", "data", "added_at"),
    "history": ("id", "key", "data", "played_at", "skipped"),
    "settings": ("key", "value"),
    "playlists": ("id", "name", "created_at", "updated_at"),
    "playlist_tracks": ("id", "playlist_id", "position", "key", "data", "added_at"),
    "rec_exclusions": ("kind", "value", "label", "created_at"),                   # v2
    "favorite_items": ("kind", "source", "item_id", "data", "added_at"),          # v3
    "local_tracks": ("id", "title", "artist", "album", "duration", "ext", "size", "sha256", "created_at"),  # v3
}


def columns(con: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in con.execute(f"PRAGMA table_info({table})")]


def _v1(con: sqlite3.Connection) -> None:
    """База без номера → v1. Создаёт недостающие таблицы, добавляет `downloads.root` старым базам."""
    for ddl in V1_TABLES:
        con.execute(ddl)
    if "root" not in columns(con, "downloads"):                 # база до появления колонки root
        con.execute("ALTER TABLE downloads ADD COLUMN root TEXT")
    for ddl in V1_INDEXES:
        con.execute(ddl)


# ---------------------------------------------------------------------------
# v2 (S14): исключения из рекомендаций. kind = "track" (value = "источник:id") или "artist" (value — имя,
# нормализованное как в recs.py). Первичный ключ (kind, value) — повторное исключение не создаёт дубль.
# ---------------------------------------------------------------------------
V2_TABLES = (
    "CREATE TABLE IF NOT EXISTS rec_exclusions ("
    " kind TEXT NOT NULL CHECK (kind IN ('track', 'artist')),"
    " value TEXT NOT NULL, label TEXT NOT NULL, created_at REAL NOT NULL,"
    " PRIMARY KEY (kind, value))",
)


def _v2(con: sqlite3.Connection) -> None:
    """v1 → v2: новая таблица rec_exclusions. Данные других таблиц не затрагиваются."""
    for ddl in V2_TABLES:
        con.execute(ddl)


# ---------------------------------------------------------------------------
# v3 (S15): библиотека.
# * favorite_items — избранные альбомы и исполнители (карточка каталога в data, JSON). Ключ (kind, source, item_id):
#   повторное добавление не создаёт дубль. Избранные треки по-прежнему в favorites (их формат не меняется).
# * local_tracks — аудиофайлы, импортированные пользователем. Файл лежит в <data>/local/<id><ext>; путь из базы не
#   читается: имя файла строится из id (16 hex) и расширения из белого списка (backend/localmusic.py). sha256 —
#   повторный импорт того же файла не создаёт копию.
# ---------------------------------------------------------------------------
V3_TABLES = (
    "CREATE TABLE IF NOT EXISTS favorite_items ("
    " kind TEXT NOT NULL CHECK (kind IN ('album', 'artist')),"
    " source TEXT NOT NULL, item_id TEXT NOT NULL, data TEXT NOT NULL, added_at REAL NOT NULL,"
    " PRIMARY KEY (kind, source, item_id))",
    "CREATE TABLE IF NOT EXISTS local_tracks ("
    " id TEXT PRIMARY KEY, title TEXT NOT NULL, artist TEXT NOT NULL, album TEXT, duration INTEGER,"
    " ext TEXT NOT NULL, size INTEGER NOT NULL, sha256 TEXT NOT NULL UNIQUE, created_at REAL NOT NULL)",
)


def _v3(con: sqlite3.Connection) -> None:
    """v2 → v3: новые таблицы favorite_items и local_tracks. Данные других таблиц не затрагиваются."""
    for ddl in V3_TABLES:
        con.execute(ddl)


MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {1: _v1, 2: _v2, 3: _v3}


# ---------------------------------------------------------------------------
class MigrationError(RuntimeError):
    """База не открыта и не изменена. Текст показывается пользователю при запуске."""


@dataclass
class Result:
    before: int                     # версия схемы до запуска
    after: int                      # версия после
    backup: Path | None             # копия, сделанная перед обновлением (None — обновлять было нечего)


def user_version(con: sqlite3.Connection) -> int:
    return con.execute("PRAGMA user_version").fetchone()[0]


def _user_tables(con: sqlite3.Connection) -> list[str]:
    return [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]


def check_schema(con: sqlite3.Connection) -> None:
    """Все таблицы и колонки текущей версии на месте; иначе MigrationError."""
    problems = []
    for table, needed in REQUIRED_COLUMNS.items():
        have = set(columns(con, table))
        if not have:
            problems.append(f"нет таблицы {table}")
        elif missing := [c for c in needed if c not in have]:
            problems.append(f"в таблице {table} нет колонок {', '.join(missing)}")
    if problems:
        raise MigrationError("Структура базы данных Wave не совпадает с ожидаемой: " + "; ".join(problems) + ".")


# ---------------------------------------------------------------------------
# Резервные копии
# ---------------------------------------------------------------------------
_BACKUP_RE = re.compile(r"^wave-(\d{8}-\d{6})(?:-(\d+))?-v(\d+)\.db$")


def _backup_key(p: Path) -> tuple[str, int]:
    m = _BACKUP_RE.match(p.name)
    return (m.group(1), int(m.group(2) or 1)) if m else ("", 0)


def list_backups(directory: Path) -> list[Path]:
    """Резервные копии Wave в каталоге, от старых к новым (по времени в имени)."""
    if not directory.is_dir():
        return []
    return sorted((p for p in directory.iterdir() if p.is_file() and _BACKUP_RE.match(p.name)), key=_backup_key)


def _free_name(directory: Path, stamp: str, version: int) -> Path:
    """Имя, которого ещё нет: совпадение по секунде получает номер, существующая копия не перезаписывается."""
    target = directory / f"wave-{stamp}-v{version}.db"
    n = 1
    while target.exists() or target.with_name(target.name + ".part").exists():
        n += 1
        target = directory / f"wave-{stamp}-{n}-v{version}.db"
    return target


def make_backup(con: sqlite3.Connection, directory: Path, version: int, now: float | None = None) -> Path:
    """
    Полная копия базы через `sqlite3.backup()` (корректна и для WAL, и при открытом соединении).
    Пишется во временный файл `.part`, проверяется `PRAGMA quick_check` и только потом получает своё имя:
    оборванная копия никогда не вытеснит целую при ротации.
    """
    directory.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(time.time() if now is None else now))
    target = _free_name(directory, stamp, version)
    part = target.with_name(target.name + ".part")
    try:
        dst = sqlite3.connect(part)
        try:
            con.backup(dst)
            check = dst.execute("PRAGMA quick_check").fetchone()[0]
        finally:
            dst.close()
        if check != "ok":
            raise sqlite3.DatabaseError(f"копия не прошла проверку: {check}")
        os.replace(part, target)
    except BaseException:
        try:
            part.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    prune_backups(directory, keep=BACKUP_KEEP, protect=target)
    log.info("Резервная копия базы перед обновлением: %s", target)
    return target


def prune_backups(directory: Path, keep: int = BACKUP_KEEP, protect: Path | None = None) -> None:
    """Оставляет `keep` последних копий Wave. Чужие файлы в каталоге не трогает."""
    backups = list_backups(directory)
    for old in backups[:max(0, len(backups) - keep)]:
        if protect is not None and old == protect:
            continue
        try:
            old.unlink()
        except OSError as exc:
            log.warning("Старую резервную копию %s удалить не удалось: %r", old, exc)


# ---------------------------------------------------------------------------
def migrate(con: sqlite3.Connection, backups_dir: Path) -> Result:
    """
    Приводит базу к CURRENT_VERSION. Соединение должно быть в режиме autocommit (isolation_level=None):
    транзакцией здесь управляет сам код. При любой ошибке база остаётся такой, какой была.
    """
    before = user_version(con)
    if before > CURRENT_VERSION:
        raise MigrationError(
            f"База данных создана более новой версией Wave (схема v{before}, эта версия знает до "
            f"v{CURRENT_VERSION}). Чтобы не повредить данные, установите последнюю версию Wave.")
    if before == CURRENT_VERSION:
        check_schema(con)
        return Result(before, before, None)

    backup = None
    if _user_tables(con):                       # в пустом файле сохранять нечего
        try:
            backup = make_backup(con, backups_dir, before)
        except (OSError, sqlite3.Error) as exc:
            raise MigrationError(
                f"Не удалось создать резервную копию базы данных в {backups_dir} перед её обновлением "
                f"({exc}). База не изменена. Освободите место на диске или проверьте права на папку.") from exc

    try:
        con.execute("BEGIN IMMEDIATE")
        for version in range(before + 1, CURRENT_VERSION + 1):
            MIGRATIONS[version](con)
            con.execute(f"PRAGMA user_version = {version}")
        check_schema(con)
        con.execute("COMMIT")
    except BaseException as exc:
        if con.in_transaction:
            con.execute("ROLLBACK")
        if isinstance(exc, MigrationError):
            raise MigrationError(f"{exc} Обновление базы отменено, данные не изменены."
                                 + (f" Резервная копия: {backup}" if backup else "")) from exc
        if isinstance(exc, Exception):
            raise MigrationError(
                f"Не удалось обновить базу данных Wave до версии {CURRENT_VERSION} ({exc}). "
                "Обновление отменено, данные не изменены." + (f" Резервная копия: {backup}" if backup else "")) from exc
        raise
    log.info("База данных обновлена: v%d → v%d", before, CURRENT_VERSION)
    return Result(before, CURRENT_VERSION, backup)
