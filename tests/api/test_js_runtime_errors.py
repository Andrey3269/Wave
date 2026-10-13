"""
Ошибки воспроизведения YouTube, связанные с JS-runtime (S3, требование 13), и жизненный цикл
дочерних процессов при остановке backend.

Отсутствие или поломка Deno не выдаётся за «трек недоступен»: у ответа свой тип (X-Wave-Error) и текст.
Настоящие причины (трек удалён, нет сети, возраст) по-прежнему называются как есть.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from yt_dlp.utils import DownloadError

import jsruntime
import library
import stream
from wavetest.fakes import audio_bytes

YT = "AAAAAAAAAA1"
SC = "1000000001"
FORMAT_ERROR = "ERROR: [youtube] AAAAAAAAAA1: Requested format is not available. Use --list-formats"


def _missing(monkeypatch, source="bundled", problem="not_found"):
    st = jsruntime.RuntimeStatus(False, source=source, path=Path("/x/runtime/deno/deno"), problem=problem,
                                 detail="файл не найден", checked_at=float("inf"))
    monkeypatch.setattr(jsruntime, "_status", st)
    return st


def _get(api, source=YT, src="yt"):
    return api.get(f"/api/stream/{src}/{source}", headers={"Range": "bytes=0-"})


def test_missing_runtime_is_reported_separately(api, monkeypatch):
    _missing(monkeypatch)
    api.resolver.fail("yt", YT, DownloadError(FORMAT_ERROR))
    r = _get(api)
    assert r.status_code == 503 and r.headers[stream.ERROR_HEADER] == "js_runtime"
    detail = r.json()["detail"]
    assert detail == jsruntime.user_message() and "Трек недоступен" != detail
    assert "/x/runtime" not in detail and "deno/deno" not in detail          # без путей


def test_missing_runtime_in_frozen_build_asks_to_reinstall(api, monkeypatch):
    _missing(monkeypatch)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    api.resolver.fail("yt", YT, RuntimeError("yt-dlp не вернул прямой URL"))
    r = _get(api)
    assert r.headers[stream.ERROR_HEADER] == "js_runtime" and "Переустановите приложение" in r.json()["detail"]


@pytest.mark.parametrize("message, status, kind", [
    ("ERROR: [youtube] AAAAAAAAAA1: Video unavailable. This video has been removed by the uploader", 404, "unavailable"),
    ("ERROR: [youtube] AAAAAAAAAA1: Private video. Sign in if you've been granted access", 404, "unavailable"),
    ("ERROR: Unable to download webpage: <urlopen error [Errno -3] Temporary failure in name resolution>", 503, "network"),
    ("ERROR: [youtube] AAAAAAAAAA1: Sign in to confirm your age. This video may be inappropriate for some users.",
     403, "age_restricted"),
])
def test_real_causes_are_not_blamed_on_runtime(api, monkeypatch, message, status, kind):
    """Трек действительно недоступен (или нет сети): это не ошибка компонента, даже если его нет."""
    _missing(monkeypatch)
    api.resolver.fail("yt", YT, DownloadError(message))
    r = _get(api)
    assert (r.status_code, r.headers[stream.ERROR_HEADER]) == (status, kind)


def test_runtime_failure_reported_by_yt_dlp(api, monkeypatch):
    """Runtime есть, но yt-dlp сообщил о сбое решения JS-задачи: отдельный текст, не «трек недоступен»."""
    exc = DownloadError(FORMAT_ERROR)
    exc.wave_js_issue = "[youtube] [jsc:deno] Error running deno process (returncode: 1)"
    api.resolver.fail("yt", YT, exc)
    r = _get(api)
    assert r.status_code == 502 and r.headers[stream.ERROR_HEADER] == "js_runtime_failed"
    assert "deno process" not in r.json()["detail"]


def test_format_error_with_healthy_runtime_is_unchanged(api):
    api.resolver.fail("yt", YT, DownloadError(FORMAT_ERROR))
    r = _get(api)
    assert r.status_code == 502 and r.headers[stream.ERROR_HEADER] == "extractor"


def test_soundcloud_errors_are_not_attributed_to_runtime(api, monkeypatch):
    _missing(monkeypatch)
    api.resolver.fail("sc", SC, DownloadError("ERROR: [soundcloud] 1000000001: Unable to extract track"))
    r = _get(api, SC, "sc")
    assert r.headers[stream.ERROR_HEADER] == "extractor"


def test_youtube_still_plays_without_runtime_when_yt_dlp_succeeds(api, monkeypatch):
    """Урезанный режим yt-dlp без JS: если поток всё же получен, воспроизведение не блокируется."""
    _missing(monkeypatch)
    api.net.add_file(f"/yt/{YT}", audio_bytes(3000))
    r = _get(api)
    assert r.status_code == 206 and r.content == audio_bytes(3000)


def test_downloaded_track_plays_without_runtime(api, monkeypatch):
    _missing(monkeypatch)
    folder = library.safe_folder("Артист", "Песня", "yt", YT)
    d = api.profile.downloads / folder
    d.mkdir(parents=True)
    (d / "audio.m4a").write_bytes(audio_bytes(2000))
    library.add(source="yt", track_id=YT, title="Песня", artist="Артист", album=None, duration=60, folder=folder,
                audio="audio.m4a", cover=None, lyrics=None, size=2000, created_at=1.0, root=str(api.profile.downloads))
    r = api.get(f"/api/stream/yt/{YT}")
    assert r.status_code == 200 and r.content == audio_bytes(2000)
    assert api.resolver.total() == 0


def test_js_issue_is_attached_by_extract(monkeypatch):
    """_extract_sync прикрепляет к DownloadError предупреждение yt-dlp о JS-задачах из этого же извлечения."""
    class YDL:
        def extract_info(self, url, download):
            jsruntime.YDL_LOGGER.warning("[youtube] [jsc:deno] Error running deno process (returncode: 1)")
            raise DownloadError(FORMAT_ERROR)

    monkeypatch.setattr(stream, "_get_ydl", lambda source: YDL())
    jsruntime.YDL_LOGGER.warning("[youtube] [jsc:deno] старое предупреждение")     # из прошлого извлечения
    with pytest.raises(DownloadError) as e:
        stream._extract_sync("yt", YT)
    assert "returncode: 1" in e.value.wave_js_issue

    class Clean:
        def extract_info(self, url, download):
            raise DownloadError(FORMAT_ERROR)

    monkeypatch.setattr(stream, "_get_ydl", lambda source: Clean())
    with pytest.raises(DownloadError) as e:
        stream._extract_sync("yt", YT)
    assert e.value.wave_js_issue is None                                          # begin() забыл старое


def test_backend_shutdown_terminates_runtime_processes(harness):
    """Остановка backend (закрытие окна, перезапуск сторожем) завершает дочерние процессы yt-dlp."""
    from yt_dlp.utils import Popen

    with harness.client():
        proc = Popen([sys.executable, "-c", "import time; time.sleep(60)"], stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL)
        assert proc.poll() is None
    assert proc.wait(timeout=5) is not None
    assert jsruntime.GUARD.live() == []
