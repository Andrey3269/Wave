"""
Миграции wave.db (backend/persistence/migrations.py, S6) на изолированных базах прошлых версий.

Базы: «как в 5a7f27c» (исходный коммит, без user_version), «старее, без downloads.root и без плейлистов»
(tests/fixtures/db/*.sql + данные из wavetest/legacy_db.py) и уже актуальная (создана текущим кодом).
Все базы лежат во временном профиле теста; настоящая база пользователя не открывается.
"""
from __future__ import annotations

import os
import sqlite3

import pytest

import library
import playlists
import userdata
from persistence import db as store
from persistence import migrations
from wavetest import legacy_db
from wavetest.fakes import track_payload

LEGACY = ["5a7f27c", "without_root"]


def _startup():
    """То же, что lifespan при запуске backend."""
    library.init()
    userdata.init()
    playlists.init()


def _version(path) -> int:
    con = sqlite3.connect(path)
    try:
        return con.execute("PRAGMA user_version").fetchone()[0]
    finally:
        con.close()


def _journal(path) -> str:
    con = sqlite3.connect(path)
    try:
        return con.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        con.close()


def _schema(path) -> list[tuple]:
    con = sqlite3.connect(path)
    try:
        return con.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall()
    finally:
        con.close()


def _backups(profile):
    return migrations.list_backups(profile.data / "backups")


def _expected_after(before: dict, schema: str) -> dict:
    """Строки после миграции: те же; у базы без root к строкам загрузок добавлена пустая колонка root."""
    after = dict(before)
    if schema == "without_root":
        after["downloads"] = [row + (None,) for row in before["downloads"]]
        after["playlists"] = []
        after["playlist_tracks"] = []
    return after


# ---------------------------------------------------------------------------
# Миграция без потери данных
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("schema", LEGACY)
def test_legacy_database_is_migrated_without_data_loss(wave_profile, schema):
    legacy_db.build(wave_profile.db_path, schema)
    before = legacy_db.dump(wave_profile.db_path)
    assert _version(wave_profile.db_path) == 0

    _startup()
    store.close()                                           # перенести WAL в файл, чтобы читать его напрямую

    after = legacy_db.dump(wave_profile.db_path)
    assert after == _expected_after(before, schema)
    for table, rows in before.items():                     # количество строк — по каждой таблице отдельно
        assert len(after[table]) == len(rows), table
    assert _version(wave_profile.db_path) == migrations.CURRENT_VERSION == 1
    con = sqlite3.connect(wave_profile.db_path)
    try:
        for table, cols in migrations.REQUIRED_COLUMNS.items():
            assert [r[1] for r in con.execute(f"PRAGMA table_info({table})")] == list(cols), table
        indexes = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'index' AND name NOT LIKE 'sqlite_%'")}
    finally:
        con.close()
    assert indexes == {"history_key", "playlist_tracks_pl"}
    assert _journal(wave_profile.db_path) == "wal"


@pytest.mark.parametrize("schema", LEGACY)
def test_migrated_data_is_served_by_the_api(harness, wave_profile, schema):
    """Выборочное содержимое после миграции видно через API так же, как в прошлой версии."""
    legacy_db.build(wave_profile.db_path, schema)
    before = legacy_db.dump(wave_profile.db_path)
    with harness.client() as api:
        favs = api.get("/api/favorites").json()["items"]
        assert [f["id"] for f in favs] == ["1000000012", "LEGACY00011", "LEGACY00010"]      # новые сверху
        assert favs[1]["title"] == "Песня №11 «ВОЛНА»" and "explicit" not in favs[1]          # снимок как был
        hist = api.get("/api/history", params={"limit": 50}).json()["tracks"]
        assert len(hist) == 7                                                                 # 7 разных треков
        values = api.get("/api/settings").json()["values"]
        assert (values["theme"], values["default_source"], values["autoplay"], values["volume"],
                values["profile_name"]) == ("dark", "sc", False, 0.35, "Слушатель")
        lists = api.get("/api/playlists").json()["items"]
        if schema == "5a7f27c":
            assert [p["name"] for p in lists] == ["Дорога домой", "Пустой"]
            tracks = api.get("/api/playlists/1").json()["tracks"]
            assert [t["id"] for t in tracks] == ["LEGACY00040", "LEGACY00041", "LEGACY00043", "LEGACY00044"]
            # порядок сохранён, новый трек — в конец, после «дырки» в позициях
            assert api.post("/api/playlists/1/tracks", json=track_payload(id_="NEWTRACK001")).json() == {"added": True}
            assert api.get("/api/playlists/1").json()["tracks"][-1]["id"] == "NEWTRACK001"
        else:
            assert lists == []
            assert api.post("/api/playlists", json={"name": "Первый"}).status_code == 201
        assert userdata.recent_plays(300)[0]["key"] == "yt:" + legacy_db._track(20 + 39 % 7)["id"]
    assert len(before["history"]) == 40


# ---------------------------------------------------------------------------
# Резервная копия до миграции
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("schema", LEGACY)
def test_backup_is_made_before_migration(wave_profile, schema):
    legacy_db.build(wave_profile.db_path, schema)
    before = legacy_db.dump(wave_profile.db_path)
    before_schema = _schema(wave_profile.db_path)

    _startup()

    backups = _backups(wave_profile)
    assert len(backups) == 1
    name = backups[0].name
    assert name.startswith("wave-") and name.endswith("-v0.db")             # v0 — версия схемы в копии
    assert legacy_db.dump(backups[0]) == before                              # копия — база до обновления
    assert _schema(backups[0]) == before_schema
    assert _version(backups[0]) == 0
    assert not list((wave_profile.data / "backups").glob("*.part"))


def test_fresh_install_needs_no_backup(wave_profile):
    _startup()
    assert _version(wave_profile.db_path) == 1
    assert _backups(wave_profile) == []


def test_backup_failure_stops_startup_and_leaves_database_untouched(wave_profile):
    legacy_db.build(wave_profile.db_path, "5a7f27c")
    raw = wave_profile.db_path.read_bytes()
    (wave_profile.data / "backups").write_text("это файл, а не папка")       # копию создать нельзя
    with pytest.raises(migrations.MigrationError) as e:
        _startup()
    assert "резервную копию" in str(e.value) and "База не изменена" in str(e.value)
    assert store.startup_error() == str(e.value) and not store.is_open()
    assert wave_profile.db_path.read_bytes() == raw                         # ни одного изменённого байта


# ---------------------------------------------------------------------------
# Откат при сбое
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("schema", LEGACY)
def test_failed_migration_rolls_back(wave_profile, monkeypatch, schema):
    """Сбой после того, как шаг уже изменил схему (CREATE, ALTER): всё откатывается, user_version прежний."""
    legacy_db.build(wave_profile.db_path, schema)
    before = legacy_db.dump(wave_profile.db_path)
    before_schema = _schema(wave_profile.db_path)
    real = migrations.MIGRATIONS[1]

    def broken(con):
        real(con)                                         # таблицы созданы, колонка root добавлена…
        assert "root" in migrations.columns(con, "downloads")
        raise RuntimeError("искусственный сбой")          # …и тут сбой

    monkeypatch.setitem(migrations.MIGRATIONS, 1, broken)
    with pytest.raises(migrations.MigrationError) as e:
        _startup()
    assert "искусственный сбой" in str(e.value) and "данные не изменены" in str(e.value)
    assert "Резервная копия" in str(e.value)
    assert not store.is_open()
    with pytest.raises(store.DatabaseUnavailable):        # приложение не работает с полуобновлённой базой
        library._q("SELECT 1")

    assert legacy_db.dump(wave_profile.db_path) == before
    assert _schema(wave_profile.db_path) == before_schema   # ни новых таблиц, ни колонки root
    assert _version(wave_profile.db_path) == 0
    assert _journal(wave_profile.db_path) == "delete"       # WAL не включён: база в исходном режиме
    assert len(_backups(wave_profile)) == 1                 # копия, сделанная до попытки, осталась

    monkeypatch.setitem(migrations.MIGRATIONS, 1, real)     # исправленная версия: следующий запуск обновит базу
    _startup()
    store.close()
    assert legacy_db.dump(wave_profile.db_path) == _expected_after(before, schema)


def test_unexpected_old_structure_is_refused(wave_profile):
    """Неизвестная старая структура (в favorites нет added_at): понятная ошибка, откат, данные целы."""
    con = sqlite3.connect(wave_profile.db_path)
    con.execute("CREATE TABLE favorites (key TEXT PRIMARY KEY, data TEXT NOT NULL)")
    con.execute("INSERT INTO favorites VALUES ('yt:x', '{}')")
    con.commit()
    con.close()
    before_schema = _schema(wave_profile.db_path)
    with pytest.raises(migrations.MigrationError) as e:
        _startup()
    assert "в таблице favorites нет колонок added_at" in str(e.value)
    assert _schema(wave_profile.db_path) == before_schema and _version(wave_profile.db_path) == 0
    assert legacy_db.dump(wave_profile.db_path) == {"favorites": [("yt:x", "{}")]}


def test_user_version_is_transactional(wave_profile):
    """Основа отката: PRAGMA user_version внутри отменённой транзакции не сохраняется."""
    con = sqlite3.connect(wave_profile.db_path, isolation_level=None)
    try:
        con.execute("BEGIN IMMEDIATE")
        con.execute("PRAGMA user_version = 7")
        con.execute("ROLLBACK")
        assert con.execute("PRAGMA user_version").fetchone()[0] == 0
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Идемпотентность и уже актуальная база
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("schema", LEGACY)
def test_repeated_startup_is_idempotent(wave_profile, schema):
    legacy_db.build(wave_profile.db_path, schema)
    _startup()
    store.close()
    first, first_schema = legacy_db.dump(wave_profile.db_path), _schema(wave_profile.db_path)
    backups = _backups(wave_profile)

    for _ in range(3):
        _startup()
        store.close()
    assert legacy_db.dump(wave_profile.db_path) == first                      # данные не продублированы
    assert _schema(wave_profile.db_path) == first_schema
    assert _version(wave_profile.db_path) == 1
    assert _backups(wave_profile) == backups                                  # обновлять нечего — копий нет


def test_current_database_is_left_alone(harness, wave_profile):
    """База, созданная текущей версией, с данными: повторные запуски её не меняют и копий не делают."""
    with harness.client() as api:
        api.post("/api/favorites", json=track_payload(id_="CURRENT0001"))
        pid = api.post("/api/playlists", json={"name": "Текущий"}).json()["id"]
        api.post(f"/api/playlists/{pid}/tracks", json=track_payload(id_="CURRENT0002"))
        api.post("/api/history", json=track_payload(id_="CURRENT0003"))
    store.close()
    before, before_schema = legacy_db.dump(wave_profile.db_path), _schema(wave_profile.db_path)
    assert _version(wave_profile.db_path) == 1

    result = store.open(wave_profile.db_path, wave_profile.data / "backups")
    assert (result.before, result.after, result.backup) == (1, 1, None)
    store.close()
    with harness.client() as api:
        assert [f["id"] for f in api.get("/api/favorites").json()["items"]] == ["CURRENT0001"]
    store.close()
    assert legacy_db.dump(wave_profile.db_path) == before
    assert _schema(wave_profile.db_path) == before_schema
    assert _backups(wave_profile) == []


def test_previous_wave_can_still_open_v1_database(wave_profile):
    """Откат на версию до S6: её init (CREATE TABLE IF NOT EXISTS + ALTER root в try) не ломается на базе v1."""
    legacy_db.build(wave_profile.db_path, "5a7f27c")
    _startup()
    store.close()
    before = legacy_db.dump(wave_profile.db_path)
    old_init = legacy_db.SCHEMAS["5a7f27c"].read_text(encoding="utf-8")
    old_init = old_init.replace("CREATE TABLE ", "CREATE TABLE IF NOT EXISTS ").replace(
        "CREATE INDEX ", "CREATE INDEX IF NOT EXISTS ")
    con = sqlite3.connect(wave_profile.db_path)
    try:
        con.executescript(old_init)
        with pytest.raises(sqlite3.OperationalError, match="duplicate column"):
            con.execute("ALTER TABLE downloads ADD COLUMN root TEXT")       # прежний код это и ожидал
    finally:
        con.close()
    assert legacy_db.dump(wave_profile.db_path) == before


# ---------------------------------------------------------------------------
# Имена и ротация резервных копий
# ---------------------------------------------------------------------------
def _db_with_row(path, value: str) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE IF NOT EXISTS t (v TEXT)")
    con.execute("DELETE FROM t")
    con.execute("INSERT INTO t VALUES (?)", (value,))
    con.commit()
    return con


def test_backup_names_never_overwrite_existing_copy(tmp_path):
    con = _db_with_row(tmp_path / "wave.db", "первая")
    try:
        first = migrations.make_backup(con, tmp_path / "backups", 0, now=1_760_000_000)
        _db_with_row(tmp_path / "wave.db", "вторая").close()
        second = migrations.make_backup(con, tmp_path / "backups", 0, now=1_760_000_000)   # та же секунда
    finally:
        con.close()
    assert first != second and second.name.endswith("-2-v0.db")
    for path, value in ((first, "первая"), (second, "вторая")):
        c = sqlite3.connect(path)
        try:
            assert c.execute("SELECT v FROM t").fetchall() == [(value,)]
        finally:
            c.close()


def test_only_three_latest_backups_are_kept(tmp_path):
    backups = tmp_path / "backups"
    backups.mkdir()
    (backups / "заметки.txt").write_text("чужой файл")
    (backups / "wave-manual.db").write_bytes(b"x")                           # не наш формат имени
    con = _db_with_row(tmp_path / "wave.db", "x")
    try:
        made = [migrations.make_backup(con, backups, 0, now=1_760_000_000 + 60 * i) for i in range(5)]
    finally:
        con.close()
    assert migrations.list_backups(backups) == made[-3:]
    assert (backups / "заметки.txt").exists() and (backups / "wave-manual.db").exists()
    assert not list(backups.glob("*.part"))


def test_broken_copy_does_not_push_out_good_ones(tmp_path):
    """Копия, которая не записалась, не становится «последней» и не вытесняет целые копии."""
    backups = tmp_path / "backups"
    con = _db_with_row(tmp_path / "wave.db", "x")
    try:
        good = [migrations.make_backup(con, backups, 0, now=1_760_000_000 + 60 * i) for i in range(3)]
    finally:
        con.close()

    class DiskFull:
        def backup(self, dst):
            dst.execute("CREATE TABLE half (x)")           # что-то успело записаться в .part
            raise sqlite3.OperationalError("диск заполнен")

    with pytest.raises(sqlite3.OperationalError):
        migrations.make_backup(DiskFull(), backups, 0, now=1_760_009_999)
    assert migrations.list_backups(backups) == good
    assert not list(backups.glob("*.part"))


def test_rotation_during_real_migration(wave_profile):
    """Перед миграцией в папке уже три старые копии: после неё снова три, новая — среди них."""
    backups = wave_profile.data / "backups"
    backups.mkdir()
    for day in ("20250101", "20250201", "20250301"):
        (backups / f"wave-{day}-120000-v0.db").write_bytes(b"old")
    legacy_db.build(wave_profile.db_path, "5a7f27c")
    _startup()
    names = [p.name for p in _backups(wave_profile)]
    assert len(names) == 3 and names[:2] == ["wave-20250201-120000-v0.db", "wave-20250301-120000-v0.db"]
    assert names[2].endswith("-v0.db") and os.path.getsize(backups / names[2]) > 3
