"""Слой SQLite backend/persistence/db.py (S6): PRAGMA, транзакции, вложенность, многопоточность."""
from __future__ import annotations

import sqlite3
import threading

import pytest

import library
from persistence import db as store
from persistence import migrations


def test_pragmas(db):
    con = sqlite3.connect(db.db_path)            # journal_mode=WAL хранится в самом файле базы
    try:
        assert con.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        con.close()
    rows = {name: store.query(f"PRAGMA {name}")[0][0] for name in ("synchronous", "foreign_keys", "busy_timeout")}
    assert rows == {"synchronous": 1, "foreign_keys": 1, "busy_timeout": 5000}       # 1 = NORMAL


def test_single_queries_are_committed_immediately(db):
    library._q("INSERT INTO settings (key, value) VALUES ('k', 'v')")
    assert db.sql("SELECT value FROM settings WHERE key = 'k'") == [("v",)]          # видно другому соединению


def test_transaction_commits_all(db):
    with store.transaction() as con:
        con.execute("INSERT INTO settings (key, value) VALUES ('a', '1')")
        con.execute("INSERT INTO settings (key, value) VALUES ('b', '2')")
    assert db.sql("SELECT key FROM settings ORDER BY key") == [("a",), ("b",)]


def test_transaction_rolls_back_on_error(db):
    class Boom(Exception):
        pass

    with pytest.raises(Boom):
        with store.transaction() as con:
            con.execute("INSERT INTO settings (key, value) VALUES ('a', '1')")
            raise Boom
    with pytest.raises(sqlite3.IntegrityError):
        with store.transaction() as con:
            con.execute("INSERT INTO settings (key, value) VALUES ('b', '1')")
            con.execute("INSERT INTO settings (key, value) VALUES ('b', '2')")     # PRIMARY KEY
    assert db.sql("SELECT COUNT(*) FROM settings") == [(0,)]
    library._q("INSERT INTO settings (key, value) VALUES ('c', '1')")                # соединение исправно
    assert db.sql("SELECT key FROM settings") == [("c",)]


@pytest.mark.parametrize("inner", ["query", "transaction"])
def test_nested_use_fails_fast_instead_of_deadlock(db, inner):
    with pytest.raises(store.NestedTransactionError):
        with store.transaction() as con:
            con.execute("INSERT INTO settings (key, value) VALUES ('a', '1')")
            if inner == "query":
                library._q("SELECT 1")
            else:
                with store.transaction():
                    pass
    assert db.sql("SELECT COUNT(*) FROM settings") == [(0,)]                        # внешний блок откатился
    with store.transaction():                                                       # блокировка освобождена
        pass


def test_transaction_serializes_threads(db):
    """Чтение-изменение-запись внутри transaction() не теряет обновления при параллельных потоках."""
    library._q("INSERT INTO settings (key, value) VALUES ('n', '0')")

    def bump():
        for _ in range(50):
            with store.transaction() as con:
                n = int(con.execute("SELECT value FROM settings WHERE key = 'n'").fetchone()[0])
                con.execute("UPDATE settings SET value = ? WHERE key = 'n'", (str(n + 1),))

    threads = [threading.Thread(target=bump) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert db.sql("SELECT value FROM settings WHERE key = 'n'") == [("400",)]


def test_foreign_keys_enforced(db):
    with pytest.raises(sqlite3.IntegrityError):
        library._q("INSERT INTO playlist_tracks (playlist_id, position, key, data, added_at) VALUES (999, 0, 'yt:x', '{}', 0)")


def test_queries_before_open_fail_clearly(wave_profile):
    with pytest.raises(store.DatabaseUnavailable):
        library._q("SELECT 1")
    with pytest.raises(store.DatabaseUnavailable):
        with store.transaction():
            pass


def test_reopen_closes_previous_connection(db):
    """Повторный init (сторож лаунчера перезапускает backend) не держит два соединения и не теряет данные."""
    library._q("INSERT INTO settings (key, value) VALUES ('a', '1')")
    first = store._conn
    library.init()
    assert store._conn is not first
    with pytest.raises(sqlite3.ProgrammingError):
        first.execute("SELECT 1")                                                   # прежнее закрыто
    assert library._q("SELECT value FROM settings WHERE key = 'a'")[0][0] == "1"


def test_corrupt_file_gives_clear_startup_error(wave_profile):
    wave_profile.db_path.write_bytes(b"this is not a database at all" * 100)
    with pytest.raises(migrations.MigrationError) as e:
        library.init()
    assert "повреждён" in str(e.value) and store.startup_error() == str(e.value)
    assert not store.is_open()
    assert wave_profile.db_path.read_bytes() == b"this is not a database at all" * 100       # файл не тронут


def test_lifespan_failure_when_database_is_newer(harness, wave_profile):
    """База от более новой Wave: backend не стартует, текст ошибки доступен лаунчеру, база не изменена."""
    con = sqlite3.connect(wave_profile.db_path)
    con.execute("CREATE TABLE favorites (key TEXT PRIMARY KEY, data TEXT NOT NULL, added_at REAL NOT NULL)")
    con.execute("PRAGMA user_version = 99")
    con.commit()
    con.close()
    before = wave_profile.db_path.read_bytes()
    with pytest.raises(migrations.MigrationError):
        with harness.client():
            pass
    assert "более новой версией Wave" in store.startup_error()
    assert wave_profile.db_path.read_bytes() == before
    assert not (wave_profile.data / "backups").exists()
