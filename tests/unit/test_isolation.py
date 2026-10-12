"""
Защита тестовой среды (TEST_STRATEGY §3): тесты не пишут в реальный профиль и не выходят в сеть
даже при неправильной конфигурации. Эти тесты проверяют саму защиту из tests/conftest.py.
"""
from __future__ import annotations

import os
import socket
import sqlite3
from pathlib import Path

import pytest

from wavetest import guard as harness_conf


def _outside_path() -> Path:
    """Путь заведомо вне временного каталога: корень репозитория."""
    return harness_conf.ROOT / "wave-guard-probe.tmp"


def test_paths_point_to_per_test_temp_dirs(backend, wave_profile):
    paths = backend["paths"]
    assert paths.data_dir() == wave_profile.data
    assert paths.downloads_dir() == wave_profile.downloads
    real = harness_conf.REAL_LOCATIONS
    assert os.path.normcase(str(paths.data_dir())) != os.path.normcase(str(real["data"]))
    assert os.path.normcase(str(paths.downloads_dir())) != os.path.normcase(str(real["downloads"]))


def test_fallback_without_env_still_lands_in_temp(backend, monkeypatch, wave_profile):
    """Неправильная конфигурация: WAVE_DATA_DIR и WAVE_DOWNLOADS_DIR стёрты. Запасные пути — во временном «доме»."""
    monkeypatch.delenv("WAVE_DATA_DIR")
    monkeypatch.delenv("WAVE_DOWNLOADS_DIR")
    paths = backend["paths"]
    session_root = os.path.normcase(str(harness_conf.SESSION_ROOT))
    for p in (paths.data_dir(), paths.downloads_dir()):
        assert os.path.normcase(str(p.resolve())).startswith(session_root), p


def test_library_init_uses_temp_db(backend, wave_profile):
    backend["library"].init()
    assert wave_profile.db_path.is_file()
    assert wave_profile.sql("SELECT name FROM sqlite_master WHERE name = 'downloads'")


def test_write_outside_temp_is_blocked(wave_profile):
    target = _outside_path()
    with pytest.raises(harness_conf.GuardViolation):
        target.write_text("x")
    assert not target.exists()
    assert harness_conf.take_guard_violations(), "нарушение должно быть записано"


def test_sqlite_outside_temp_is_blocked(wave_profile):
    with pytest.raises(harness_conf.GuardViolation):
        sqlite3.connect(harness_conf.REAL_LOCATIONS["data"] / "wave.db")
    assert harness_conf.take_guard_violations()


def test_mkdir_outside_temp_is_blocked(wave_profile):
    with pytest.raises(harness_conf.GuardViolation):
        os.mkdir(harness_conf.ROOT / "wave-guard-probe-dir")
    assert harness_conf.take_guard_violations()


def test_swallowed_violation_still_recorded(wave_profile):
    """Код приложения часто ловит `except Exception`: нарушение всё равно фиксируется."""
    try:
        _outside_path().write_text("x")
    except Exception:
        pass
    assert harness_conf.take_guard_violations()


def test_external_network_is_blocked(wave_profile):
    with pytest.raises(harness_conf.GuardViolation):
        socket.getaddrinfo("music.youtube.com", 443)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        with pytest.raises(harness_conf.GuardViolation):
            s.connect(("93.184.216.34", 443))
    assert len(harness_conf.take_guard_violations()) == 2


def test_loopback_is_allowed(wave_profile):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        with socket.create_connection(srv.getsockname(), timeout=2):
            pass
    assert not harness_conf.guard_violations()


def test_ytmusic_without_fake_cannot_reach_network(wave_profile):
    """Настоящий ytmusicapi без подмены упирается в защиту, а не в YouTube."""
    from ytmusicapi import YTMusic
    with pytest.raises(Exception):
        YTMusic().search("test", filter="songs", limit=1)
    assert any("сетевое соединение" in v for v in harness_conf.take_guard_violations())


def test_fake_net_rejects_unknown_hosts(api):
    """Подменённый httpx-клиент приложения отвечает только за cdn.test, lrclib.net и хосты обложек."""
    import asyncio

    async def go():
        async with api.net.client() as c:
            return await c.get("https://example.com/")

    with pytest.raises(Exception):
        asyncio.run(go())
    assert api.net.unexpected == ["https://example.com/"]
    api.net.unexpected.clear()


def test_secrets_scrubbed_from_environment():
    for k in ("YTDLP_COOKIES", "YTDLP_PROXY"):
        assert k not in os.environ
