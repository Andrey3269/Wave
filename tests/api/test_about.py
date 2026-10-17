"""
S16: экран «О программе / Диагностика» — GET /api/about и защита вывода GET /api/diagnostics.

Отчёт строится без сети (FakeNet роняет тест при любом внешнем соединении) и без дочерних процессов, не содержит
секретов (cookies, прокси, токенов) и полных путей: каталоги — относительно домашнего (~) или только последним
элементом, пути компонентов — относительно <app>/<data>/~ или «…/имя».
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

import diagnostics
import jsruntime
from version import __version__
from wavetest.fakedeno import fake_name, make_fake_deno

SECRETS = ("/very/secret/cookies.txt", "user:pa55word", "10.9.8.7", "tok3n-SECRET")


@pytest.fixture
def secrets_env(monkeypatch):
    monkeypatch.setenv("YTDLP_COOKIES", SECRETS[0])
    monkeypatch.setenv("YTDLP_PROXY", f"socks5://{SECRETS[1]}@{SECRETS[2]}:1080")
    monkeypatch.setenv("WAVE_SOME_TOKEN", SECRETS[3])


def test_about_reports_version_system_storage_and_caches(api, secrets_env):
    api.post("/api/favorites", json={"id": "AAAAAAAAAA1", "source": "yt", "title": "Т", "artist": "И",
                                     "stream_url": "/api/stream/yt/AAAAAAAAAA1"})
    r = api.get("/api/about")
    assert r.status_code == 200
    d = r.json()
    assert d["schema"] == 1 and d["app"]["version"] == __version__ and d["app"]["name"] == "Wave"
    assert d["app"]["python"] and d["app"]["os"] and d["app"]["cpus"] >= 1
    assert d["storage"]["schema_version"] == 3 and d["storage"]["counts"]["favorites"] == 1
    assert d["storage"]["db_bytes"] > 0
    assert set(d["caches"]) == {"search", "lyrics", "artists", "stream_links", "stream_heads", "hls_sessions"}
    for name, c in d["caches"].items():
        assert 0 <= c["items"] <= c["maxsize"] and c["ttl"] > 0, name
    assert d["settings"] == {"cookies_configured": True, "proxy_configured": True}
    assert d["startup"]["restarts"] >= 0
    if sys.platform.startswith("linux") or sys.platform == "win32":
        assert d["process"]["rss_mb"] > 0 and d["process"]["uptime_s"] >= 0
        assert d["startup"]["backend_ready_ms"] is not None and d["startup"]["backend_ready_ms"] >= 0


def test_about_has_no_secrets_or_full_paths(api, secrets_env):
    text = api.get("/api/about").text
    assert not [s for s in SECRETS if s in text]
    for p in (api.profile.data, api.profile.downloads, api.profile.root):
        assert str(p) not in text and str(p).replace("\\", "\\\\") not in text
    d = json.loads(text)
    for key in ("data_dir", "downloads_dir"):                     # временный профиль вне ~: только имя каталога
        v = d["storage"][key]
        assert v.startswith("~") or v.startswith("…"), v


def test_home_relative_paths(monkeypatch, tmp_path):
    home = tmp_path / "Users" / "Иван Петров"
    (home / "AppData" / "Local" / "Wave").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    assert diagnostics._home_relative(home / "AppData" / "Local" / "Wave") == os.sep.join(["~", "AppData", "Local", "Wave"])
    assert diagnostics._home_relative(tmp_path / "D" / "Music") == f"…{os.sep}Music"
    assert "Иван" not in diagnostics._home_relative(tmp_path / "elsewhere")


def test_safe_path_hides_paths_outside_known_roots(monkeypatch, tmp_path):
    outside = tmp_path / "Users" / "Иван Петров" / "tools" / "deno.exe"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "other-home"))
    shown = diagnostics.safe_path(outside)
    assert shown == f"…{os.sep}deno.exe" and "Иван" not in shown
    assert diagnostics.safe_path(None) is None
    data_file = Path(os.environ["WAVE_DATA_DIR"]) / "deno"
    assert diagnostics.safe_path(data_file) == f"<data>{os.sep}deno"


def test_scrub_replaces_known_dirs_urls_and_secrets(monkeypatch):
    data = os.environ["WAVE_DATA_DIR"]
    text = (f"PermissionError: [Errno 13] Permission denied: '{data}{os.sep}deno{os.sep}x' "
            "https://rr1.googlevideo.com/videoplayback?sig=SECRET token=abc")
    out = diagnostics.scrub(text)
    assert data not in out and "<data>" in out and "googlevideo" not in out and "SECRET" not in out
    assert len(diagnostics.scrub("x" * 1000)) == 300


def test_diagnostics_runtime_outside_home_shows_only_file_name(api, tmp_path, monkeypatch):
    monkeypatch.setenv("WAVE_FAKE_PY", sys.executable)
    exe = make_fake_deno(tmp_path / "Иван Петров" / "rt", name=fake_name())
    monkeypatch.setenv(jsruntime.ENV_PATH, str(exe))
    monkeypatch.setattr(jsruntime, "_which", lambda name: None)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    r = api.get("/api/diagnostics")
    d = r.json()
    assert d["js_runtime"]["ok"] and d["js_runtime"]["path"] == f"…{os.sep}{exe.name}"
    assert all(c["path"] == f"…{os.sep}{exe.name}" for c in d["js_runtime"]["candidates"])
    assert "Иван" not in r.text and str(tmp_path) not in r.text


def test_missing_runtime_detail_is_scrubbed(api, monkeypatch):
    data = os.environ["WAVE_DATA_DIR"]
    monkeypatch.setenv(jsruntime.ENV_PATH, str(Path(data) / "нет" / "deno"))
    r = api.get("/api/diagnostics")
    assert r.json()["youtube_playback"] == "degraded" and data not in r.text


def test_parallel_diagnostics_run_one_at_a_time(monkeypatch):
    running, peak = [0], [0]
    lock = threading.Lock()

    def slow(refresh=True):
        with lock:
            running[0] += 1
            peak[0] = max(peak[0], running[0])
        time.sleep(0.05)
        with lock:
            running[0] -= 1
        return {"ok": True}

    monkeypatch.setattr(diagnostics, "collect", slow)
    threads = [threading.Thread(target=diagnostics.collect_serialized) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert peak[0] == 1


def test_mark_ready_counts_restarts(monkeypatch):
    monkeypatch.setattr(diagnostics, "_ready", {})
    first = diagnostics.mark_ready()
    second = diagnostics.mark_ready()
    assert first["restarts"] == 0 and second["restarts"] == 1
    assert second["first_ready_ms"] == first["first_ready_ms"]
