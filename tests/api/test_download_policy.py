"""
Разрешение источника на сохранение трека (backend/download_policy.py, S5).

YouTube: только видео под лицензией Creative Commons (поле license из того же резолва yt-dlp).
SoundCloud: только если автор включил скачивание (downloadable, has_downloads_left); сохраняется исходный
файл автора по ссылке tracks/{id}/download. Отказ — задача в состоянии error с понятной причиной и кодом,
без файлов на диске и без обращения к CDN. Уже скачанные треки политика не касается.
Сети нет: резолвер, SoundCloud и CDN подменены (tests/wavetest/fakes.py).
"""
from __future__ import annotations

import json
import time

import pytest

import downloads
import library
import soundcloud
import stream
from wavetest.fakes import audio_bytes

YT = "AAAAAAAAAA1"
SC = "123456789"
YT_REQ = {"source": "yt", "id": YT, "title": "Песня", "artist": "Артист", "album": None, "duration": 100, "cover": None}
SC_REQ = {"source": "sc", "id": SC, "title": "Demo", "artist": "Indie Author", "album": None, "duration": 100,
          "cover": None}
DATA = audio_bytes(9_000, seed=11)
CC = "Creative Commons Attribution license (reuse allowed)"
ORIGINAL = f"https://cdn.test/sc-original/{SC}/demo.wav"


@pytest.fixture
def api_dl(api, monkeypatch):
    monkeypatch.setattr(downloads, "DL_CHUNK", 4096)
    api.net.add_file(f"/yt/{YT}", DATA, "audio/mp4")
    return api


def job_of(api, key, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = api.get("/api/downloads/jobs").json().get(key)
        if job and job["state"] not in downloads.ACTIVE:
            return job
        time.sleep(0.02)
    raise AssertionError(f"задача {key} не завершилась")


def nothing_saved(api):
    assert list(api.profile.downloads.iterdir()) == []
    assert api.profile.sql("SELECT COUNT(*) FROM downloads") == [(0,)]


def sc_track(**over):
    info = {"id": int(SC), "kind": "track", "title": "Demo", "downloadable": True, "has_downloads_left": True,
            "license": "cc-by", "policy": "ALLOW", "streamable": True}
    info.update(over)
    return info


# ---------------------------------------------------------------------------
# YouTube
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("lic, code, phrase", [
    (None, "license_unknown", "не сообщил лицензию"),
    ("Standard YouTube License", "download_not_permitted", "Creative Commons"),
    ("", "license_unknown", "не сообщил лицензию"),
])
def test_youtube_without_creative_commons_is_refused(api_dl, lic, code, phrase):
    if lic is not None:
        api_dl.resolver.licenses[f"yt:{YT}"] = lic
    r = api_dl.post("/api/downloads", json=YT_REQ)
    assert r.status_code == 202 and r.json()["state"] == "preparing"
    job = job_of(api_dl, f"yt:{YT}")
    assert job["state"] == "error" and job["code"] == code and phrase in job["error"]
    assert "Слушать его можно" in job["error"]                      # причина не звучит как поломка
    assert not api_dl.net.cdn_requests()                           # ни одного байта аудио не скачано
    nothing_saved(api_dl)


def test_youtube_creative_commons_is_saved_with_basis(api_dl):
    api_dl.resolver.licenses[f"yt:{YT}"] = CC
    api_dl.post("/api/downloads", json=YT_REQ)
    job = job_of(api_dl, f"yt:{YT}")
    assert job["state"] == "done" and job["code"] is None
    assert job["received"] == job["total"] == len(DATA)
    [folder] = api_dl.profile.downloads.iterdir()
    meta = json.loads((folder / "metadata.json").read_text(encoding="utf-8"))
    assert meta["permission"] == "creative_commons" and meta["license"] == CC
    assert api_dl.resolver.calls[f"yt:{YT}"] == 1                  # проверка и загрузка — один резолв


def test_request_body_cannot_grant_permission(api_dl):
    """Поля «разрешения» в теле запроса игнорируются: решает только источник."""
    body = {**YT_REQ, "license": CC, "permission": "creative_commons", "allowed": True}
    api_dl.post("/api/downloads", json=body)
    assert job_of(api_dl, f"yt:{YT}")["code"] == "license_unknown"
    nothing_saved(api_dl)


def test_youtube_resolve_error_is_reported_as_is(api_dl):
    api_dl.resolver.fail("yt", YT, stream.DownloadError("Sign in to confirm you're not a bot"))
    api_dl.post("/api/downloads", json=YT_REQ)
    job = job_of(api_dl, f"yt:{YT}")
    assert job["state"] == "error" and job["code"] == "bot_check" and "не от бота" in job["error"]
    nothing_saved(api_dl)


def test_refused_track_can_be_requested_again(api_dl):
    """После отказа задача завершена: повторный запрос снова проверяет разрешение (лицензия могла смениться)."""
    api_dl.post("/api/downloads", json=YT_REQ)
    assert job_of(api_dl, f"yt:{YT}")["state"] == "error"
    stream._cache.clear()
    api_dl.resolver.licenses[f"yt:{YT}"] = CC
    api_dl.post("/api/downloads", json=YT_REQ)
    assert job_of(api_dl, f"yt:{YT}")["state"] == "done"


def test_existing_download_is_kept_and_plays_whatever_the_policy(api_dl):
    """Скачанное раньше (до S5 или под прежними правилами) не удаляется и играет с диска."""
    folder = api_dl.profile.downloads / library.safe_folder("Артист", "Песня", "yt", YT)
    folder.mkdir()
    (folder / "audio.m4a").write_bytes(DATA)
    library.add(source="yt", track_id=YT, title="Песня", artist="Артист", album=None, duration=100,
                folder=folder.name, audio="audio.m4a", cover=None, lyrics=None, size=len(DATA), created_at=1.0,
                root=str(api_dl.profile.downloads))
    r = api_dl.post("/api/downloads", json=YT_REQ)
    assert r.json() == {"state": "done", "progress": 100}
    assert api_dl.resolver.total() == 0
    play = api_dl.get(f"/api/stream/yt/{YT}", headers={"Range": "bytes=10-19"})
    assert play.status_code == 206 and play.content == DATA[10:20]
    assert [t["id"] for t in api_dl.get("/api/downloads").json()["items"]] == [YT]


# ---------------------------------------------------------------------------
# SoundCloud
# ---------------------------------------------------------------------------
@pytest.fixture
def sc_ok(api, monkeypatch):
    monkeypatch.setattr(downloads, "DL_CHUNK", 4096)
    api.sc.responses[f"tracks/{SC}"] = sc_track()
    api.sc.download_links[SC].append(ORIGINAL)
    api.net.add_file(f"/sc-original/{SC}/demo.wav", DATA, "audio/wav")
    return api


def test_soundcloud_original_file_is_saved(sc_ok):
    r = sc_ok.post("/api/downloads", json=SC_REQ)
    assert r.status_code == 202
    job = job_of(sc_ok, f"sc:{SC}")
    assert job["state"] == "done", job
    [folder] = sc_ok.profile.downloads.iterdir()
    assert folder.name == f"Indie Author - Demo [sc-{SC}]"
    assert (folder / "audio.wav").read_bytes() == DATA                   # исходный формат, без перекодирования
    meta = json.loads((folder / "metadata.json").read_text(encoding="utf-8"))
    assert meta["permission"] == "creator_download" and meta["license"] == "cc-by" and meta["audio"] == "audio.wav"
    assert sc_ok.resolver.total() == 0                                  # поток для воспроизведения не сохраняется
    assert [r.headers["range"] for r in sc_ok.net.cdn_requests()] == ["bytes=0-4095", "bytes=4096-8191",
                                                                      "bytes=8192-12287"]
    play = sc_ok.get(f"/api/stream/sc/{SC}", headers={"Range": "bytes=0-9"})
    assert play.status_code == 206 and play.content == DATA[:10] and play.headers["content-type"] == "audio/wav"


@pytest.mark.parametrize("over, code, phrase", [
    ({"downloadable": False}, "download_not_permitted", "Автор не разрешил"),
    ({"downloadable": None}, "download_not_permitted", "Автор не разрешил"),
    ({"has_downloads_left": False}, "download_limit_reached", "Лимит скачиваний"),
])
def test_soundcloud_refusals_from_track_flags(sc_ok, over, code, phrase):
    sc_ok.sc.responses[f"tracks/{SC}"] = sc_track(**over)
    sc_ok.post("/api/downloads", json=SC_REQ)
    job = job_of(sc_ok, f"sc:{SC}")
    assert job["state"] == "error" and job["code"] == code and phrase in job["error"]
    assert sc_ok.sc.count("download_link") == 0 and not sc_ok.net.cdn_requests()
    nothing_saved(sc_ok)


def test_soundcloud_missing_flag_is_not_assumed(sc_ok):
    info = sc_track()
    del info["downloadable"]
    sc_ok.sc.responses[f"tracks/{SC}"] = info
    sc_ok.post("/api/downloads", json=SC_REQ)
    job = job_of(sc_ok, f"sc:{SC}")
    assert job["code"] == "permission_unknown" and "не сообщил" in job["error"]
    nothing_saved(sc_ok)


@pytest.mark.parametrize("kind, code, phrase", [
    ("auth_required", "auth_required", "только вошедшим пользователям"),
    ("not_available", "download_not_permitted", "не выдал файл"),
    ("rate_limited", "rate_limited", "ограничил число запросов"),
    ("failed", "source_failed", "Попробуйте позже"),
])
def test_soundcloud_download_link_errors(sc_ok, kind, code, phrase):
    sc_ok.sc.download_links[SC].clear()
    sc_ok.sc.download_links[SC].append(soundcloud.DownloadLinkError(kind, 401))
    sc_ok.post("/api/downloads", json=SC_REQ)
    job = job_of(sc_ok, f"sc:{SC}")
    assert job["state"] == "error" and job["code"] == code and phrase in job["error"]
    nothing_saved(sc_ok)


def test_soundcloud_unreachable(sc_ok):
    sc_ok.sc.responses[f"tracks/{SC}"] = RuntimeError("timeout")
    sc_ok.post("/api/downloads", json=SC_REQ)
    job = job_of(sc_ok, f"sc:{SC}")
    assert job["code"] == "source_unavailable" and "не ответил" in job["error"]
    nothing_saved(sc_ok)


def test_soundcloud_link_must_be_https(sc_ok):
    sc_ok.sc.download_links[SC].clear()
    sc_ok.sc.download_links[SC].append("http://cdn.test/sc-original/x.wav")
    sc_ok.post("/api/downloads", json=SC_REQ)
    assert job_of(sc_ok, f"sc:{SC}")["code"] == "source_failed"
    assert not sc_ok.net.cdn_requests()
    nothing_saved(sc_ok)


def test_soundcloud_unplayable_original_format_is_not_saved(sc_ok):
    url = f"https://cdn.test/sc-original/{SC}/demo.aiff"
    sc_ok.sc.download_links[SC].clear()
    sc_ok.sc.download_links[SC].append(url)
    sc_ok.net.add_file(f"/sc-original/{SC}/demo.aiff", DATA, "audio/aiff")
    sc_ok.post("/api/downloads", json=SC_REQ)
    job = job_of(sc_ok, f"sc:{SC}")
    assert job["state"] == "error" and job["code"] == "unsupported_format"
    nothing_saved(sc_ok)


def test_soundcloud_format_from_content_disposition(sc_ok):
    url = f"https://cdn.test/sc-original/{SC}/blob"
    sc_ok.sc.download_links[SC].clear()
    sc_ok.sc.download_links[SC].append(url)

    def serve(request):
        resp = sc_ok.net.serve_file(request)
        resp.headers["Content-Type"] = "application/octet-stream"
        resp.headers["Content-Disposition"] = 'attachment; filename="Demo (master).flac"'
        return resp

    sc_ok.net.add_file(f"/sc-original/{SC}/blob", DATA, None)
    for _ in range(3):
        sc_ok.net.queue(f"/sc-original/{SC}/blob", serve)
    sc_ok.post("/api/downloads", json=SC_REQ)
    assert job_of(sc_ok, f"sc:{SC}")["state"] == "done"
    [folder] = sc_ok.profile.downloads.iterdir()
    assert (folder / "audio.flac").read_bytes() == DATA


def test_soundcloud_expired_link_is_renewed_once(sc_ok):
    renewed = f"https://cdn.test/sc-original/{SC}/renewed.wav"
    sc_ok.sc.download_links[SC].append(renewed)
    sc_ok.net.add_file(f"/sc-original/{SC}/renewed.wav", DATA, "audio/wav")
    sc_ok.net.queue(f"/sc-original/{SC}/demo.wav", sc_ok.net.serve_file, 403)   # протухла после первой порции
    sc_ok.post("/api/downloads", json=SC_REQ)
    job = job_of(sc_ok, f"sc:{SC}")
    assert job["state"] == "done", job
    assert sc_ok.sc.count("download_link") == 2
    [folder] = sc_ok.profile.downloads.iterdir()
    assert (folder / "audio.wav").read_bytes() == DATA


def test_soundcloud_link_failing_twice_fails_cleanly(sc_ok):
    sc_ok.net.queue(f"/sc-original/{SC}/demo.wav", 403, 403)
    sc_ok.post("/api/downloads", json=SC_REQ)
    job = job_of(sc_ok, f"sc:{SC}")
    assert job["state"] == "error"
    nothing_saved(sc_ok)


def test_soundcloud_download_plays_offline_without_soundcloud(sc_ok):
    sc_ok.post("/api/downloads", json=SC_REQ)
    assert job_of(sc_ok, f"sc:{SC}")["state"] == "done"
    sc_ok.sc.responses.clear()
    sc_ok.sc.download_links.clear()
    calls = len(sc_ok.sc.calls)
    play = sc_ok.get(f"/api/stream/sc/{SC}", headers={"Range": "bytes=0-"})
    assert play.status_code == 206
    assert len(sc_ok.sc.calls) == calls and sc_ok.resolver.total() == 0
