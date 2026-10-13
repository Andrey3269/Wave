"""
GET /api/diagnostics и backend/diagnostics.py (S3): отчёт о компонентах воспроизведения YouTube.

Работает без сети (защита тестов роняет тест при любом внешнем соединении), быстро и без секретов.
"""
from __future__ import annotations

import json
import sys

import httpx
import pytest

import diagnostics
import jsruntime
import stream
from version import __version__
from wavetest.fakedeno import fake_name, make_fake_deno

SECRET_VALUES = ("/very/secret/cookies.txt", "user:pa55word", "10.9.8.7")


@pytest.fixture
def fake_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("WAVE_FAKE_PY", sys.executable)
    exe = make_fake_deno(tmp_path / "rt", name=fake_name())
    monkeypatch.setenv(jsruntime.ENV_PATH, str(exe))
    monkeypatch.setattr(jsruntime, "_which", lambda name: None)
    return exe


def test_endpoint_reports_configured_runtime(api, fake_runtime, monkeypatch):
    monkeypatch.setenv("YTDLP_COOKIES", SECRET_VALUES[0])
    monkeypatch.setenv("YTDLP_PROXY", f"socks5://{SECRET_VALUES[1]}@{SECRET_VALUES[2]}:1080")
    r = api.get("/api/diagnostics")
    assert r.status_code == 200
    d = r.json()
    assert d["schema"] == 1 and d["youtube_playback"] == "configured" and d["playback_verified"] is False
    assert d["app"]["version"] == __version__                        # S4: версия приложения из version.py
    assert d["js_runtime"]["ok"] and d["js_runtime"]["version"] == "2.9.7" and d["js_runtime"]["source"] == "env"
    assert d["js_runtime"]["min_version"] == "2.3.0" and d["js_runtime"]["configured_for_yt_dlp"] is True
    assert d["yt_dlp"]["version"] and d["solver"]["ejs_installed"] and d["solver"]["ejs_scripts_readable"]
    assert d["solver"]["ejs_version"] == d["solver"]["builtin_version"]
    assert d["deno_dir"] == {"path": f"<data>{jsruntime.os.sep}deno", "writable": True}
    assert d["settings"] == {"cookies_configured": True, "proxy_configured": True}
    assert d["issues"] == []
    text = r.text
    assert not [s for s in SECRET_VALUES if s in text]
    assert str(api.profile.data) not in text                         # каталог данных — как <data>


def test_endpoint_reports_missing_runtime(api, tmp_path, monkeypatch):
    monkeypatch.setenv(jsruntime.ENV_PATH, str(tmp_path / "нет" / "deno"))
    d = api.get("/api/diagnostics").json()
    assert d["youtube_playback"] == "degraded" and not d["js_runtime"]["ok"]
    assert [i["code"] for i in d["issues"]] == ["js_runtime_unavailable"]
    assert d["issues"][0]["severity"] == "critical" and jsruntime.ENV_PATH in d["issues"][0]["message"]
    assert diagnostics.exit_code(d) == 1


def test_report_does_not_dump_environment(api, fake_runtime, monkeypatch):
    monkeypatch.setenv("WAVE_SECRET_PROBE", "do-not-leak-me")
    assert "do-not-leak-me" not in api.get("/api/diagnostics").text


def test_frozen_bundled_version_mismatch_is_a_warning(tmp_path, monkeypatch):
    internal = tmp_path / "Wave Тест" / "_internal"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(internal), raising=False)
    monkeypatch.setenv("WAVE_FAKE_PY", sys.executable)
    monkeypatch.delenv(jsruntime.ENV_PATH, raising=False)
    monkeypatch.setattr(jsruntime, "BUNDLED_NAME", fake_name())
    make_fake_deno(internal / "runtime" / "deno", name=fake_name(), version_output="deno 2.8.0 (stable)")
    (internal / "runtime" / "deno.lock").write_text(json.dumps({"version": "2.9.7"}), encoding="utf-8")
    d = diagnostics.collect()
    assert d["app"]["frozen"] and d["js_runtime"]["source"] == "bundled"
    assert d["js_runtime"]["path"].startswith("<app>") and d["js_runtime"]["expected_version"] == "2.9.7"
    assert [(i["code"], i["severity"]) for i in d["issues"]] == [("js_runtime_version_mismatch", "warning")]
    assert diagnostics.exit_code(d) == 0


def _without_ejs(monkeypatch):
    real_import = __import__

    def no_ejs(name, *args, **kwargs):
        if name.startswith("yt_dlp_ejs"):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", no_ejs)


def test_missing_ejs_is_critical(monkeypatch, fake_runtime):
    """yt-dlp 2026.8.19 не содержит встроенного lib-скрипта: без yt-dlp-ejs Deno не решит задачи без сети."""
    from yt_dlp.extractor.youtube.jsc._builtin import vendor

    assert vendor.load_script("yt.solver.lib.js") is None          # факт о закреплённой версии yt-dlp
    _without_ejs(monkeypatch)
    d = diagnostics.collect()
    assert d["solver"]["ejs_installed"] is False and d["solver"]["usable"] is False
    assert [(i["code"], i["severity"]) for i in d["issues"]] == [("ejs_missing", "critical")]
    assert d["youtube_playback"] == "degraded" and diagnostics.exit_code(d) == 1


def test_ejs_version_mismatch_is_critical(monkeypatch, fake_runtime):
    import yt_dlp_ejs

    monkeypatch.setattr(yt_dlp_ejs, "version", "0.7.0")
    d = diagnostics.collect()
    assert [(i["code"], i["severity"]) for i in d["issues"]] == [("ejs_version_mismatch", "critical")]


def test_unreadable_ejs_scripts_are_critical(monkeypatch, fake_runtime):
    import yt_dlp_ejs.yt.solver as solver

    def missing():
        raise FileNotFoundError("lib.min.js")

    monkeypatch.setattr(solver, "lib", missing)
    d = diagnostics.collect()
    assert [(i["code"], i["severity"]) for i in d["issues"]] == [("ejs_scripts_unreadable", "critical")]


def test_text_report_states_that_playback_was_not_verified(fake_runtime):
    text = diagnostics.format_text(diagnostics.collect())
    assert f"\nВерсия приложения: {__version__}\n" in text
    assert "deno 2.9.7 supported=true" in text and "yt-dlp-ejs: 0.8.0" in text
    assert "не проверялось" in text and "Итог: конфигурация в порядке" in text


# ---------------------------------------------------------------------------
# --live: настоящий трек (здесь — подменённый резолвер и CDN)
# ---------------------------------------------------------------------------
def _cdn_client(status=206):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["range"] == "bytes=0-1023"
        return httpx.Response(status, content=b"\0" * 1024, headers={"Content-Range": "bytes 0-1023/5000"})
    return lambda: httpx.Client(transport=httpx.MockTransport(handler))


def test_live_check_success_does_not_leak_stream_url(monkeypatch, fake_runtime):
    url = "https://rr1.googlevideo.com/videoplayback?expire=2000000000&sig=SECRET"
    monkeypatch.setattr(stream, "_extract_sync", lambda s, i: stream.Resolved(url, {"User-Agent": "UA"}, 2e9))
    monkeypatch.setattr(diagnostics, "_http_client", _cdn_client())
    report = diagnostics.collect()
    report["live"] = diagnostics.live_check("dQw4w9WgXcQ")
    assert report["live"]["ok"] and report["live"]["range_request"] == {"status": 206, "content_range": True, "bytes": 1024}
    assert diagnostics.exit_code(report) == 0
    dumped = json.dumps(report) + diagnostics.format_text(report)
    assert "googlevideo" not in dumped and "SECRET" not in dumped


def test_live_check_failure_is_explained(monkeypatch):
    from yt_dlp.utils import DownloadError

    monkeypatch.setattr(jsruntime, "_status", jsruntime.RuntimeStatus(False, source="bundled", problem="not_found",
                                                                       checked_at=float("inf")))

    def fail(s, i):
        raise DownloadError("ERROR: [youtube] dQw4w9WgXcQ: Requested format is not available https://x.test/?sig=1")

    monkeypatch.setattr(stream, "_extract_sync", fail)
    live = diagnostics.live_check("dQw4w9WgXcQ")
    assert not live["ok"] and live["error_kind"] == "js_runtime"
    assert "https://" not in live["error"] and "sig=1" not in live["error"]
    assert diagnostics.exit_code({"issues": [], "live": live}) == 1


def test_live_check_rejects_bad_id():
    live = diagnostics.live_check("../../etc")
    assert live["error_kind"] == "bad_id" and not live["ok"]
