"""
Общая обвязка pytest для Wave: изоляция данных, запрет сети, сброс состояния backend.

Порядок важен: tests/wavetest/guard.py импортируется первым, то есть ДО того, как любой тест
импортирует модули backend.

Три уровня защиты пользовательских данных:
  1. переменные окружения: WAVE_DATA_DIR / WAVE_DOWNLOADS_DIR указывают во временный каталог,
     а HOME / USERPROFILE / LOCALAPPDATA / XDG_* — в отдельный временный «дом». Даже если тест
     сотрёт WAVE_DATA_DIR, запасной путь paths.data_dir() окажется во временном каталоге;
  2. перед каждым тестом проверяется, что paths.data_dir() и paths.downloads_dir() лежат
     внутри каталога этого теста, иначе сессия прерывается (pytest.exit, код 2);
  3. аудит-хук Python (sys.addaudithook) блокирует запись файлов, создание каталогов,
     переименование, удаление и sqlite3.connect вне системного временного каталога, а также
     любые сетевые соединения, кроме loopback. Нарушение записывается и роняет тест даже тогда,
     когда код приложения перехватывает исключение (например, `except Exception`).

Тесты с маркером `live` (живые источники) не запускаются по умолчанию; для них разрешена сеть.
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

# Временный профиль и аудит-хук включаются при этом импорте — до любого импорта backend.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from wavetest.guard import (  # noqa: E402
    PROXY_ENV, REAL_LOCATIONS, REAL_SNAPSHOT, SESSION_ROOT, _GUARD, _norm, _snapshot, take_guard_violations,
)

# ---------------------------------------------------------------------------
# Хуки и фикстуры pytest
# ---------------------------------------------------------------------------
def pytest_report_header(config):
    return [f"wave: временный профиль сессии {SESSION_ROOT}",
            "wave: запись вне системного temp и внешняя сеть заблокированы (кроме -m live)"]


def pytest_sessionfinish(session, exitstatus):
    _GUARD["active"] = False
    after = _snapshot(REAL_LOCATIONS)
    if after != REAL_SNAPSHOT:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
        sys.stderr.write(f"\nЗАЩИТА: изменились реальные каталоги пользователя {REAL_LOCATIONS}:\n"
                         f"  до:    {REAL_SNAPSHOT}\n  после: {after}\n")


@pytest.fixture(scope="session", autouse=True)
def _guard_session():
    _GUARD["active"] = True
    yield
    _GUARD["active"] = False
    left = take_guard_violations()
    assert not left, f"Нарушения защиты тестов: {left}"


@pytest.fixture(autouse=True)
def _guard_test(request):
    """Каждый тест должен закончиться без нарушений защиты (сеть, запись вне temp)."""
    before = len(_GUARD["violations"])
    live = request.node.get_closest_marker("live") is not None
    _GUARD["network_allowed"] = live
    if live:
        os.environ.update(PROXY_ENV)              # живой сети может понадобиться прокси пользователя
    yield
    _GUARD["network_allowed"] = False
    for k in PROXY_ENV:
        os.environ.pop(k, None)
    new = _GUARD["violations"][before:]
    del _GUARD["violations"][before:]
    if new:
        pytest.fail("Нарушение защиты тестов: " + "; ".join(new), pytrace=False)


def _backend():
    """Ленивый импорт backend (main тянет все модули). Возвращает словарь модулей."""
    import catalog
    import downloads
    import library
    import lyrics
    import main
    import paths
    import playlists
    import recs
    import soundcloud
    import stream
    import userdata
    return {"catalog": catalog, "downloads": downloads, "library": library, "lyrics": lyrics, "main": main,
            "paths": paths, "playlists": playlists, "recs": recs, "soundcloud": soundcloud, "stream": stream,
            "userdata": userdata}


def reset_backend_state() -> None:
    """Сбрасывает модульное состояние backend, чтобы тесты не влияли друг на друга."""
    m = _backend()
    m["main"]._cache.clear()
    m["main"]._down_until.clear()
    st = m["stream"]
    st._cache.clear()
    st._inflight.clear()
    st._heads.clear()
    st._prefetching.clear()
    st._background.clear()
    # Семафор резолвера создаётся при импорте и привязывается к первому event loop (находка M1).
    # Каждый тест получает новый, иначе один тест сломал бы другие. Сам дефект воспроизводит
    # tests/api/test_stream_lifespan.py, где сброса между перезапусками нет.
    st._resolve_sem = asyncio.Semaphore(st.MAX_PARALLEL_RESOLVES)
    m["catalog"]._artist_cache.clear()
    m["lyrics"]._cache.clear()
    m["downloads"].jobs.clear()
    m["downloads"]._background.clear()
    m["paths"].set_downloads_dir(None)
    m["soundcloud"]._ie = None
    db = m["library"]._db
    if db is not None:
        db.close()
        m["library"]._db = None


@dataclass
class Profile:
    root: Path
    data: Path
    downloads: Path

    @property
    def db_path(self) -> Path:
        return self.data / "wave.db"

    def sql(self, query: str, args: tuple = ()) -> list[tuple]:
        """Прямой запрос к тестовой базе (отдельное соединение) — для проверки того, что реально записано."""
        con = sqlite3.connect(self.db_path)
        try:
            return con.execute(query, args).fetchall()
        finally:
            con.close()


@pytest.fixture(autouse=True)
def wave_profile(tmp_path, monkeypatch) -> Profile:
    """Одноразовый профиль на каждый тест: свои каталоги данных и загрузок, чистое состояние backend."""
    data, downloads = tmp_path / "data", tmp_path / "downloads"
    data.mkdir()
    downloads.mkdir()
    monkeypatch.setenv("WAVE_DATA_DIR", str(data))
    monkeypatch.setenv("WAVE_DOWNLOADS_DIR", str(downloads))
    reset_backend_state()

    paths = _backend()["paths"]
    for name, actual, expected in (("data_dir", paths.data_dir(), data), ("downloads_dir", paths.downloads_dir(), downloads)):
        if _norm(actual) != _norm(expected):
            pytest.exit(f"ЗАЩИТА: paths.{name}() = {actual}, ожидался временный {expected}", returncode=2)
    yield Profile(root=tmp_path, data=data, downloads=downloads)
    reset_backend_state()


@pytest.fixture
def db(wave_profile):
    """Инициализированная тестовая база (как в lifespan: library, userdata, playlists)."""
    m = _backend()
    m["library"].init()
    m["userdata"].init()
    m["playlists"].init()
    return wave_profile


@pytest.fixture
def backend():
    """Модули backend (уже с изолированным окружением)."""
    return _backend()


@pytest.fixture
def harness(wave_profile, monkeypatch):
    """
    Приложение с подменёнными внешними зависимостями, ещё не запущенное:
      * ytmusicapi.YTMusic  -> wavetest.fakes.FakeYTMusic;
      * общий httpx-клиент  -> MockTransport (wavetest.fakes.FakeNet: CDN, LRCLIB, обложки);
      * резолвер yt-dlp     -> wavetest.fakes.FakeResolver;
      * SoundCloud API      -> wavetest.fakes.FakeSoundCloud.
    Любой запрос к неизвестному хосту в FakeNet роняет тест. Запуск: harness.client() или harness.run().
    """
    from wavetest.fakes import ApiHarness
    return ApiHarness(_backend(), wave_profile, monkeypatch)


@pytest.fixture
def api(harness):
    """То же, что `harness`, но уже запущенное в TestClient (lifespan выполнен)."""
    with harness.client() as h:
        yield h
