"""Настройки /api/settings (backend/userdata.py): ключи, валидация, папка загрузок, сохранение между запусками."""
from __future__ import annotations

import pytest

import catalog
import library
import lyrics
import main
import paths
import stream


DEFAULT_VALUES = {"default_source": "yt", "autoplay": True, "theme": "auto",
                  "volume": 1.0, "last_volume": 1.0, "lyrics_visible": True, "profile_name": "", "mix_covers": {},
                  "history_limit": 10_000}                                  # S6: ретеншн истории


def test_defaults_and_paths(api):
    r = api.get("/api/settings")
    assert r.status_code == 200
    assert r.json() == {
        "values": DEFAULT_VALUES,
        "paths": {"downloads": str(api.profile.downloads), "data": str(api.profile.data), "downloads_locked": True},
        "meta": {"local_ui_migrated": False},
    }


def test_original_keys_keep_their_shape(api):
    """Контракт до S2a: прежние ключи values и paths на месте и того же типа (новые поля только добавлены)."""
    body = api.get("/api/settings").json()
    assert {k: body["values"][k] for k in ("default_source", "autoplay", "theme")} == \
        {"default_source": "yt", "autoplay": True, "theme": "auto"}
    assert set(body["paths"]) == {"downloads", "data", "downloads_locked"}


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


@pytest.mark.parametrize("body", [
    {"theme": "dark", "autoplay": "maybe"},                                   # ошибочный ключ последним
    {"autoplay": "maybe", "theme": "dark"},                                   # и первым
    {"theme": "dark", "volume": 0.4, "profile_name": "Ок", "lyrics_visible": 2},
    {"theme": "dark", "volume": 0.4, "no_such_key": "1"},
])
def test_write_is_atomic(api, body):
    """N1: если хоть один ключ ошибочен, 400 и ни одна настройка не изменилась (раньше ключи до ошибки сохранялись)."""
    before = api.get("/api/settings").json()
    r = api.put("/api/settings", json=body)
    assert r.status_code == 400
    assert api.get("/api/settings").json() == before
    assert api.profile.sql("SELECT COUNT(*) FROM settings")[0][0] == 0


def test_valid_keys_are_written_together(api):
    r = api.put("/api/settings", json={"theme": "light", "volume": 0.25, "profile_name": "Аня"})
    assert r.status_code == 200
    assert dict(api.profile.sql("SELECT key, value FROM settings")) == \
        {"theme": "light", "volume": "0.25", "profile_name": "Аня"}


def test_ui_settings_are_persisted_by_backend(api):
    """H3: имя профиля сохраняется через /api/settings (раньше — только в localStorage окна)."""
    r = api.put("/api/settings", json={"profile_name": "Андрей"})
    assert r.status_code == 200
    assert api.get("/api/settings").json()["values"]["profile_name"] == "Андрей"


@pytest.mark.parametrize("body, key, public, stored", [
    ({"volume": 0.42}, "volume", 0.42, "0.42"),
    ({"volume": 0}, "volume", 0.0, "0.0"),
    ({"volume": 1}, "volume", 1.0, "1.0"),
    ({"volume": 0.123456}, "volume", 0.123, "0.123"),
    ({"last_volume": 0.6}, "last_volume", 0.6, "0.6"),
    ({"lyrics_visible": False}, "lyrics_visible", False, "0"),
    ({"lyrics_visible": "1"}, "lyrics_visible", True, "1"),
    ({"profile_name": "  Mercury  "}, "profile_name", "Mercury", "Mercury"),
    ({"profile_name": ""}, "profile_name", "", ""),
    ({"profile_name": "Я" * 24}, "profile_name", "Я" * 24, "Я" * 24),
    ({"mix_covers": {"mood": "https://lh3.googleusercontent.com/a=w544-h544"}}, "mix_covers",
     {"mood": "https://lh3.googleusercontent.com/a=w544-h544"}, '{"mood":"https://lh3.googleusercontent.com/a=w544-h544"}'),
    ({"mix_covers": {"indie": "/api/local/yt/AAAAAAAAAA1/cover", "shake": None}}, "mix_covers",
     {"indie": "/api/local/yt/AAAAAAAAAA1/cover"}, '{"indie":"/api/local/yt/AAAAAAAAAA1/cover"}'),
    ({"mix_covers": {}}, "mix_covers", {}, "{}"),
])
def test_ui_values_are_stored(api, body, key, public, stored):
    r = api.put("/api/settings", json=body)
    assert r.status_code == 200, r.text
    assert r.json()["values"][key] == public
    assert api.profile.sql("SELECT value FROM settings WHERE key = ?", (key,)) == [(stored,)]
    assert api.get("/api/settings").json()["values"] == r.json()["values"]


@pytest.mark.parametrize("body", [
    {"volume": 1.5}, {"volume": -0.1}, {"volume": "0.5"}, {"volume": True}, {"volume": None},
    {"last_volume": [0.5]},
    {"lyrics_visible": "yes"}, {"lyrics_visible": 1.0},
    {"profile_name": "Я" * 25}, {"profile_name": 42}, {"profile_name": "a\nb"},
    {"mix_covers": "https://x"}, {"mix_covers": {"mood": "javascript:alert(1)"}},
    {"mix_covers": {"../x": "https://lh3.googleusercontent.com/a"}},
    {"mix_covers": {"mood": "//evil.example/x.png"}},
    {"mix_covers": {f"m{i}": "https://a.b/c" for i in range(17)}},
    {"theme": None}, {"default_source": ["yt"]},
])
def test_invalid_ui_values_rejected(api, body):
    r = api.put("/api/settings", json=body)
    assert r.status_code == 400
    assert r.json()["detail"] == "Недопустимое значение настройки."
    assert api.profile.sql("SELECT COUNT(*) FROM settings")[0][0] == 0


def test_corrupt_stored_values_fall_back_to_defaults(api):
    """Повреждённая строка в базе (ручная правка, старая версия) не роняет чтение настроек."""
    for k, v in (("volume", "громко"), ("last_volume", "7"), ("mix_covers", "{не json"), ("theme", "neon")):
        library._q("INSERT INTO settings (key, value) VALUES (?, ?)", (k, v))
    r = api.get("/api/settings")
    assert r.status_code == 200
    values = r.json()["values"]
    assert (values["volume"], values["last_volume"], values["mix_covers"], values["theme"]) == (1.0, 1.0, {}, "auto")


@pytest.mark.parametrize("cover", ["http://lh3.googleusercontent.com/a", "https://evil.example/x.png",
                                   "https://evilgoogleusercontent.com/a", "/api/local/whatever"])
def test_mix_cover_must_be_from_cover_allowlist(api, cover):
    """S13: обложка микса — из того же разрешённого списка, что обложки треков (domain/snapshot.py)."""
    assert api.put("/api/settings", json={"mix_covers": {"mood": cover}}).status_code == 400
    assert api.put("/api/settings", json={"mix_covers": {"mood": f"/api/local/yt/{'A' * 11}/cover"}}).status_code == 200


def test_stored_mix_covers_outside_allowlist_are_dropped_on_read(api):
    library._q("INSERT INTO settings (key, value) VALUES (?, ?)",
               ("mix_covers", '{"mood":"https://lh3.googleusercontent.com/ok","bad":"http://evil.example/x"}'))
    assert api.get("/api/settings").json()["values"]["mix_covers"] == {"mood": "https://lh3.googleusercontent.com/ok"}


# ---------------------------------------------------------------------------
# Однократный перенос из localStorage (S2a)
# ---------------------------------------------------------------------------
LEGACY = {"volume": 0.35, "last_volume": 0.5, "lyrics_visible": False, "profile_name": "Старое имя",
          "mix_covers": {"mood": "https://lh3.googleusercontent.com/m=w544-h544"}}


def test_migration_copies_legacy_values_once(api):
    r = api.post("/api/settings/migrate-local", json=LEGACY)
    assert r.status_code == 200
    body = r.json()
    assert sorted(body["migrated"]) == sorted(LEGACY) and body["skipped"] == []
    assert body["meta"] == {"local_ui_migrated": True}
    assert {k: body["values"][k] for k in LEGACY} == LEGACY
    # второй вызов (другой порт = другой localStorage со старыми данными) ничего не меняет
    r2 = api.post("/api/settings/migrate-local", json={**LEGACY, "volume": 0.9, "profile_name": "Чужое"})
    assert r2.status_code == 200 and r2.json()["migrated"] == []
    assert r2.json()["values"]["volume"] == 0.35 and r2.json()["values"]["profile_name"] == "Старое имя"


def test_migration_with_nothing_to_move_still_closes_it(api):
    """Первый запуск после обновления без старых данных: перенос отмечается, позже чужие данные не применяются."""
    assert api.post("/api/settings/migrate-local", json={}).json()["meta"]["local_ui_migrated"] is True
    late = api.post("/api/settings/migrate-local", json={"volume": 0.1}).json()
    assert late["migrated"] == [] and late["values"]["volume"] == 1.0


def test_migration_does_not_overwrite_backend_values(api):
    api.put("/api/settings", json={"volume": 0.8, "profile_name": "Новое"})
    body = api.post("/api/settings/migrate-local", json=LEGACY).json()
    assert body["values"]["volume"] == 0.8 and body["values"]["profile_name"] == "Новое"
    assert body["values"]["lyrics_visible"] is False                      # этого ключа в базе не было
    assert "volume" in body["skipped"] and "profile_name" in body["skipped"]


def test_migration_skips_bad_values_one_by_one(api):
    """Мусор в localStorage не мешает перенести остальное; не-интерфейсные ключи перенос не трогает."""
    body = api.post("/api/settings/migrate-local", json={
        "volume": 7, "profile_name": "Имя", "theme": "light", "downloads_dir": "/etc",
        "mix_covers": {"mood": "https://lh3.googleusercontent.com/ok", "bad": "javascript:x"},
    }).json()
    assert sorted(body["migrated"]) == ["mix_covers", "profile_name"]
    assert sorted(body["skipped"]) == ["downloads_dir", "theme", "volume"]
    assert body["values"]["mix_covers"] == {"mood": "https://lh3.googleusercontent.com/ok"}
    assert body["values"]["theme"] == "auto" and body["values"]["volume"] == 1.0
    assert api.profile.sql("SELECT value FROM settings WHERE key = 'downloads_dir'") == []


def test_boot_script_sets_theme_from_database(api):
    r = api.get("/api/boot.js")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/javascript")
    assert r.headers["cache-control"] == "no-store"
    assert r.text.rstrip().endswith('("auto");')
    api.put("/api/settings", json={"theme": "light"})
    assert api.get("/api/boot.js").text.rstrip().endswith('("light");')


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
    assert body["values"] == {**DEFAULT_VALUES, "autoplay": False, "theme": "light"}
    assert body["paths"]["downloads"] == str(target.resolve())


UI_STATE = {"theme": "light", "volume": 0.4, "last_volume": 0.55, "lyrics_visible": False,
            "profile_name": "Порт-тест", "mix_covers": {"indie": "https://lh3.googleusercontent.com/i=w544-h544"}}


def test_ui_settings_survive_restart_on_another_port(harness):
    """
    H3: два настоящих запуска backend (uvicorn) на разных портах с одним профилем. В WebView это два разных
    origin, то есть два разных localStorage; настройки интерфейса должны прийти из базы.
    """
    with harness.serve() as a:
        port_a = a.port
        assert a.put("/api/settings", json=UI_STATE).status_code == 200
    with harness.serve() as b:
        assert b.port != port_a
        values = b.get("/api/settings").json()["values"]
    assert {k: values[k] for k in UI_STATE} == UI_STATE


def test_failed_downloads_dir_leaves_other_keys_untouched(unlocked, tmp_path):
    """N1 с папкой загрузок: непригодная папка — 400, тема из того же запроса не сохранена."""
    blocker = tmp_path / "file.txt"
    blocker.write_text("not a folder")
    r = unlocked.put("/api/settings", json={"theme": "dark", "downloads_dir": str(blocker / "sub")})
    assert r.status_code == 400
    assert unlocked.get("/api/settings").json()["values"]["theme"] == "auto"
    assert paths.downloads_dir() == unlocked.profile.downloads


def test_invalid_key_prevents_downloads_dir_change(unlocked, tmp_path):
    """Папка не создаётся и не выбирается, если в том же запросе есть ошибочный ключ."""
    target = tmp_path / "never"
    r = unlocked.put("/api/settings", json={"downloads_dir": str(target), "theme": "neon"})
    assert r.status_code == 400
    assert not target.exists()
    assert paths.downloads_dir() == unlocked.profile.downloads


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
