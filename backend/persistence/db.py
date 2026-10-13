"""
Единый доступ к wave.db (S6).

* Одно соединение на процесс и одна `threading.Lock`: запросы приходят из пула потоков FastAPI, из фоновых
  загрузок и из потока сторожа лаунчера. Для настольного приложения с одним пользователем этого достаточно,
  а порядок записи становится простым и предсказуемым.
* Соединение работает в режиме autocommit (`isolation_level=None`): одиночный запрос (`query`) атомарен сам
  по себе, а несколько запросов объединяются явным `transaction()` — BEGIN IMMEDIATE … COMMIT, при любой
  ошибке ROLLBACK.
* PRAGMA: `journal_mode=WAL` (запись не блокирует чтение, сбой посреди записи не портит базу),
  `synchronous=NORMAL` (с WAL целостность сохраняется, теряются максимум последние транзакции при
  отключении питания), `foreign_keys=ON`, `busy_timeout=5000`.
* Вложенность запрещена явно: `query()` или второй `transaction()` из потока, который уже внутри
  `transaction()`, сразу дают NestedTransactionError, а не взаимную блокировку на `_lock`.

Перед открытием база приводится к текущей версии схемы (migrations.py). Если это не удалось, соединение
не открывается, а текст ошибки доступен лаунчеру через `startup_error()`.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from persistence import migrations

log = logging.getLogger("musicmeta.db")

BUSY_TIMEOUT_MS = 5000

_conn: sqlite3.Connection | None = None
_path: Path | None = None
_lock = threading.Lock()
_local = threading.local()           # .active = True, пока этот поток внутри transaction()
_startup_error: str | None = None


class NestedTransactionError(RuntimeError):
    """Запрос к базе изнутри transaction() того же потока: внутри блока работают с переданным соединением."""


class DatabaseUnavailable(RuntimeError):
    """База ещё не открыта (или не открылась при запуске)."""


def _connect(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(path, timeout=BUSY_TIMEOUT_MS / 1000, check_same_thread=False, isolation_level=None)
    try:
        con.row_factory = sqlite3.Row
        con.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        con.execute("PRAGMA foreign_keys = ON")
    except BaseException:
        con.close()
        raise
    return con


def _enable_wal(con: sqlite3.Connection) -> None:
    """WAL включается после обновления схемы: резервная копия снимается с базы в её исходном режиме."""
    try:
        mode = con.execute("PRAGMA journal_mode = WAL").fetchone()[0]
    except sqlite3.OperationalError as exc:            # например, база занята другим процессом
        log.warning("Режим WAL не включён (%r), база работает в прежнем режиме журнала", exc)
        return
    if str(mode).lower() != "wal":                     # файловая система без поддержки WAL
        log.warning("Режим WAL недоступен для %s, используется журнал %s", _path, mode)
    con.execute("PRAGMA synchronous = NORMAL")


def open(path: Path, backups_dir: Path) -> migrations.Result:   # noqa: A001 — имя по смыслу модуля
    """
    Открывает базу: закрывает прежнее соединение (перезапуск backend сторожем), обновляет схему,
    включает WAL. При ошибке бросает migrations.MigrationError, соединение не остаётся открытым.
    """
    global _conn, _path, _startup_error
    close()
    _startup_error = None
    _path = Path(path)
    try:
        con = _connect(_path)
    except sqlite3.Error as exc:
        _startup_error = f"Не удалось открыть базу данных Wave {_path} ({exc})."
        log.error("%s", _startup_error)
        raise migrations.MigrationError(_startup_error) from exc
    try:
        result = migrations.migrate(con, Path(backups_dir))
        _enable_wal(con)
    except migrations.MigrationError as exc:
        con.close()
        _startup_error = str(exc)
        log.error("База данных не открыта: %s", exc)
        raise
    except sqlite3.DatabaseError as exc:                # «file is not a database» и подобное
        con.close()
        _startup_error = (f"Файл базы данных Wave {_path} повреждён или не является базой SQLite ({exc}). "
                          "Файл не изменён.")
        log.error("%s", _startup_error)
        raise migrations.MigrationError(_startup_error) from exc
    except BaseException:
        con.close()
        raise
    with _lock:
        _conn = con
    return result


def close() -> None:
    """Закрывает соединение (при закрытии последнего соединения SQLite переносит WAL в основной файл)."""
    global _conn
    with _lock:
        if _conn is not None:
            try:
                _conn.close()
            except sqlite3.Error as exc:
                log.warning("Соединение с базой закрыто с ошибкой: %r", exc)
            _conn = None


def startup_error() -> str | None:
    """Почему база не открылась при последней попытке (для страницы ошибки лаунчера) или None."""
    return _startup_error


def is_open() -> bool:
    return _conn is not None


def path() -> Path | None:
    return _path


def _check_not_nested() -> None:
    if getattr(_local, "active", False):
        raise NestedTransactionError("Запрос к базе внутри transaction(): используйте переданное соединение")


def _require() -> sqlite3.Connection:
    if _conn is None:
        raise DatabaseUnavailable(_startup_error or "База данных не открыта")
    return _conn


def query(sql: str, args: tuple = ()) -> list[sqlite3.Row]:
    """Один запрос, атомарно. Возвращает строки (для INSERT/UPDATE/DELETE — пустой список)."""
    _check_not_nested()
    with _lock:
        return _require().execute(sql, args).fetchall()


def execute(sql: str, args: tuple = ()) -> sqlite3.Cursor:
    """Один запрос, когда нужен курсор (`rowcount`, `lastrowid`). Строки читать через query()."""
    _check_not_nested()
    with _lock:
        return _require().execute(sql, args)


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    """
    Несколько запросов как одна операция: всё или ничего. Блокировка держится до конца блока, поэтому
    чтение и запись внутри не перемежаются с другими запросами приложения. BEGIN IMMEDIATE сразу берёт
    блокировку записи SQLite: проверка и вставка не разойдутся и с другим соединением (busy_timeout).
    Внутри блока используйте только переданное соединение.
    """
    _check_not_nested()
    with _lock:
        con = _require()
        _local.active = True
        try:
            con.execute("BEGIN IMMEDIATE")
            try:
                yield con
                con.execute("COMMIT")
            except BaseException:
                if con.in_transaction:
                    con.execute("ROLLBACK")
                raise
        finally:
            _local.active = False
