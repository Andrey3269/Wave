"""Настройки /api/settings (backend/userdata.py): ключи, валидация, папка загрузок, сохранение между запусками."""
from __future__ import annotations

import pytest

import catalog
import lyrics
import main
import paths
import stream


def test_defaults_and_paths(api):
    r = api.get("/api/settings")
    assert r.status_code == 200
    assert r.json() == {
        "values": {"default_source": "yt", "autoplay": True, "theme": "auto"},
        "paths": {"downloads": str(api.profile.downloads), "data": str(api.profile.data), "downloads_locked": True},
    }


@pytest.mark.parametrize("body, values, stored", [
    ({"theme": "dark"}, {"theme": "dark"}, ("theme", "dark")),
    ({"theme": "light"}, {"theme": "light"}, ("theme", "light")),
    ({"default_source": "sc"}, {"default_source": "sc"}, ("default_source", "sc")),
    ({"autoplay": False}, {"autoplay": False}, ("autoplay", "0")),
    ({"autoplay": "1"}, {"autoplay": True}, ("autoplay", "1")),
])
def test_valid_values_are_stored(api, body, values, stored):
    r = api.put("/api/settings", json=body)
    assert r.status_code == 200
    for k, v in values.items():
        assert r.json()["values"][k] == v
    assert api.profile.sql("SELECT key, value FROM settings WHERE key = ?", (stored[0],)) == [stored]
    assert api.get("/api/settings").json()["values"] == r.json()["values"]


@pytest.mark.parametrize("body", [{"theme": "neon"}, {"default_source": "spotify"}, {"autoplay": "yes"}, {"theme": True}])
def test_invalid_values_rejected(api, body):
    r = api.put("/api/settings", json=body)
    assert r.status_code == 400
    assert r.json()["detail"] == "Недопустимое значение настройки."
    assert api.profile.sql("SELECT COUNT(*) FROM settings")[0][0] == 0


def test_unknown_key_rejected(api):
    r = api.put("/api/settings", json={"volume_boost": "11"})
    assert r.status_code == 400 and r.json()["detail"] == "Неизвестная настройка."


def test_partial_write_before_error(api):
    """Ключи применяются по очереди: всё, что до ошибочного, уже сохранено (фиксируем как есть)."""
    r = api.put("/api/settings", json={"theme": "dark", "autoplay": "maybe"})
    assert r.status_code == 400
    assert api.get("/api/settings").json()["values"]["theme"] == "dark"


@pytest.mark.xfail(strict=True, reason="H3: настройки интерфейса (имя профиля, громкость, тексты) живут в localStorage "
                                       "и не сохраняются через /api/settings (план S2a)")
def test_ui_settings_are_persisted_by_backend(api):
    r = api.put("/api/settings", json={"profile_name": "Андрей"})
    assert r.status_code == 200
    assert api.get("/api/settings").json()["values"]["profile_name"] == "Андрей"


# ---------------------------------------------------------------------------
# Папка загрузок
# ---------------------------------------------------------------------------
def test_downloads_dir_locked_by_env(api, tmp_path):
    r = api.put("/api/settings", json={"downloads_dir": str(tmp_path / "elsewhere")})
    assert r.status_code == 400
    assert "WAVE_DOWNLOADS_DIR" in r.json()["detail"]
    assert not (tmp_path / "elsewhere").exists()


@pytest.fixture
def unlocked(api, monkeypatch):
    """Папка загрузок не задана переменной окружения (как у обычного пользователя)."""
    paths.set_downloads_dir(api.profile.downloads)          # до снятия переменной: запасной путь не понадобится
    monkeypatch.delenv("WAVE_DOWNLOADS_DIR")
    return api


def test_downloads_dir_change(unlocked, tmp_path):
    target = tmp_path / "Музыка" / "Wave"
    r = unlocked.put("/api/settings", json={"downloads_dir": str(target)})
    assert r.status_code == 200
    assert r.json()["paths"]["downloads"] == str(target.resolve())
    assert r.json()["paths"]["downloads_locked"] is False
    assert target.is_dir() and not (target / ".wave_write_test").exists()
    assert paths.downloads_dir() == target.resolve()
    assert unlocked.profile.sql("SELECT value FROM settings WHERE key = 'downloads_dir'") == [(str(target.resolve()),)]


def test_downloads_dir_must_be_absolute(unlocked):
    r = unlocked.put("/api/settings", json={"downloads_dir": "relative/folder"})
    assert r.status_code == 400
    assert paths.downloads_dir() == unlocked.profile.downloads


def test_downloads_dir_must_be_writable(unlocked, tmp_path):
    blocker = tmp_path / "file.txt"
    blocker.write_text("not a folder")
    r = unlocked.put("/api/settings", json={"downloads_dir": str(blocker / "sub")})
    assert r.status_code == 400
    assert r.json()["detail"] == "В эту папку нельзя записывать. Выберите другую."
    assert paths.downloads_dir() == unlocked.profile.downloads


def test_settings_survive_backend_restart(harness, monkeypatch, tmp_path):
    """Настройки в SQLite переживают перезапуск backend на том же профиле (порт значения не имеет)."""
    paths.set_downloads_dir(harness.profile.downloads)
    monkeypatch.delenv("WAVE_DOWNLOADS_DIR")
    target = tmp_path / "chosen"
    with harness.client() as h:
        assert h.put("/api/settings", json={"theme": "light", "autoplay": False}).status_code == 200
        assert h.put("/api/settings", json={"downloads_dir": str(target)}).status_code == 200
    paths.set_downloads_dir(None)                 # новый процесс: выбор папки в памяти потерян
    with harness.client() as h:
        body = h.get("/api/settings").json()
    assert body["values"] == {"default_source": "yt", "autoplay": False, "theme": "light"}
    assert body["paths"]["downloads"] == str(target.resolve())


# ---------------------------------------------------------------------------
# Очистка
# ---------------------------------------------------------------------------
def test_clear_cache_resets_memory_caches_only(api):
    main._cache[("q", 20, 0, "yt")] = (0.0, [])
    lyrics._cache[("a", "t")] = {"synced": [], "plain": "x", "provider": "lrclib"}
    catalog._artist_cache[("yt", "UCx")] = (0.0, {})
    stream._cache["yt:AAAAAAAAAA1"] = stream.Resolved("u", {}, 0)
    stream._heads["yt:AAAAAAAAAA1"] = stream.Head(b"", "*", "audio/mp4", 0)
    api.post("/api/history", json={"id": "AAAAAAAAAA1", "source": "yt", "title": "t", "artist": "a",
                                   "stream_url": "/api/stream/yt/AAAAAAAAAA1"})
    r = api.post("/api/settings/clear-cache")
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert not main._cache and not lyrics._cache and not catalog._artist_cache
    assert not stream._cache and not stream._heads
    assert api.profile.sql("SELECT COUNT(*) FROM history")[0][0] == 1          # история не тронута
