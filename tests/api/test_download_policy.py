"""
Какие треки скачиваются (backend/download_policy.py, S5, решение владельца 2026-10-13).

Трек YouTube Music или SoundCloud скачивается, если его аудиопоток штатно получен тем же резолвером, что и для
воспроизведения; сохраняется этот поток в исходном формате. Лицензия Creative Commons и признак `downloadable`
SoundCloud не проверяются. Отказ — только если поток получить нельзя (ошибка резолвера как есть) или источник
отдаёт лишь фрагмент (`preview_only`): задача в состоянии error с понятной причиной и кодом, без файлов на диске
и без обращения к CDN. Уже скачанные треки не проверяются и играют с диска.
Сети нет: резолвер, SoundCloud и CDN подменены (tests/wavetest/fakes.py).
"""
from __future__ import annotations

import json
import time

import pytest

import downloads
import library
import stream
from wavetest.fakes import audio_bytes

YT = "AAAAAAAAAA1"
SC = "123456789"
YT_REQ = {"source": "yt", "id": YT, "title": "Песня", "artist": "Артист", "album": "Альбом", "duration": 100,
          "cover": None}
SC_REQ = {"source": "sc", "id": SC, "title": "Demo", "artist": "Indie Author", "album": None, "duration": 100,
          "cover": None}
REQS = {"yt": YT_REQ, "sc": SC_REQ}
DATA = audio_bytes(9_000, seed=11)


@pytest.fixture
def api_dl(api, monkeypatch):
    monkeypatch.setattr(downloads, "DL_CHUNK", 4096)
    api.net.add_file(f"/yt/{YT}", DATA, "audio/mp4")
    api.net.add_file(f"/sc/{SC}", DATA, "audio/mpeg")
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


# ---------------------------------------------------------------------------
# Воспроизводимый трек скачивается
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("source, track_id, audio, page", [
    ("yt", YT, "audio.m4a", f"https://www.youtube.com/watch?v={YT}"),
    ("sc", SC, "audio.mp3", f"https://api.soundcloud.com/tracks/{SC}"),
])
def test_playable_track_is_downloaded_from_its_stream(api_dl, source, track_id, audio, page):
    """Без лицензии Creative Commons и без признака downloadable: сохраняется штатный поток в исходном формате."""
    key = f"{source}:{track_id}"
    api_dl.post("/api/downloads", json=REQS[source])
    job = job_of(api_dl, key)
    assert job["state"] == "done" and job["code"] is None and job["error"] is None
    assert job["received"] == job["total"] == len(DATA)

    [folder] = api_dl.profile.downloads.iterdir()
    assert (folder / audio).read_bytes() == DATA
    meta = json.loads((folder / "metadata.json").read_text(encoding="utf-8"))
    assert meta["source"] == source and meta["audio"] == audio and meta["source_url"] == page
    assert set(meta) == {"id", "source", "title", "artist", "album", "duration", "source_url", "audio", "cover",
                         "lyrics"}                                      # формат metadata.json как до S5
    assert api_dl.resolver.calls[key] == 1                              # проверка и загрузка — один резолв
    assert api_dl.net.cdn_requests(f"/{source}/{track_id}")             # байты — из того же потока, что у плеера
    assert api_dl.sc.count("api_get") == 0                              # downloadable SoundCloud не запрашивается


def test_soundcloud_download_plays_offline_without_soundcloud(api_dl):
    api_dl.post("/api/downloads", json=SC_REQ)
    assert job_of(api_dl, f"sc:{SC}")["state"] == "done"
    before = api_dl.resolver.total()
    api_dl.resolver.fail("sc", SC, stream.DownloadError("Unable to download JSON metadata: <urlopen error "
                                                        "[Errno 11001] getaddrinfo failed>"))
    play = api_dl.get(f"/api/stream/sc/{SC}", headers={"Range": "bytes=0-99"})
    assert play.status_code == 206 and play.content == DATA[:100]
    assert play.headers["content-type"].startswith("audio/mpeg")
    assert api_dl.resolver.total() == before                            # без обращения к источнику


# ---------------------------------------------------------------------------
# Поток получить нельзя — понятная причина, ничего не сохранено
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("source, track_id, exc, code, phrase", [
    ("yt", YT, stream.DownloadError("ERROR: [youtube] x: Video unavailable"), "unavailable", "недоступен"),
    ("yt", YT, stream.DownloadError("Sign in to confirm your age"), "age_restricted", "по возрасту"),
    ("yt", YT, stream.DownloadError("Sign in to confirm you're not a bot"), "bot_check", "не от бота"),
    ("yt", YT, stream.DownloadError("HTTP Error 429: Too Many Requests"), "rate_limited", "ограничил"),
    ("sc", SC, stream.DownloadError("<urlopen error [Errno 11001] getaddrinfo failed>"), "network", "Нет связи"),
    ("sc", SC, stream.DownloadError("ERROR: [soundcloud] 404: Not Found"), "unavailable", "недоступен"),
    ("sc", SC, stream.StreamUnavailable("SoundCloud: только HLS-поток (m3u8_native)"), "no_direct_stream",
     "нет прямого аудиопотока"),
])
def test_unavailable_stream_is_reported(api_dl, source, track_id, exc, code, phrase):
    api_dl.resolver.fail(source, track_id, exc)
    r = api_dl.post("/api/downloads", json=REQS[source])
    assert r.status_code == 202 and r.json()["state"] == "preparing"
    job = job_of(api_dl, f"{source}:{track_id}")
    assert job["state"] == "error" and job["code"] == code and phrase in job["error"]
    assert not api_dl.net.cdn_requests()                                # ни одного байта аудио
    nothing_saved(api_dl)


def test_download_error_matches_playback_error(api_dl):
    """Текст причины тот же, что получает плеер для этого трека."""
    api_dl.resolver.fail("yt", YT, stream.DownloadError("Video unavailable"), stream.DownloadError("Video unavailable"))
    api_dl.post("/api/downloads", json=YT_REQ)
    job = job_of(api_dl, f"yt:{YT}")
    play = api_dl.get(f"/api/stream/yt/{YT}", headers={"Range": "bytes=0-1"})
    assert play.status_code == 404
    assert play.json()["detail"] == job["error"] and play.headers[stream.ERROR_HEADER] == job["code"]


def test_preview_only_is_not_saved_as_full_track(api_dl):
    """SoundCloud Go+: источник отдаёт 30-секундный фрагмент — его не сохраняем под видом полного трека."""
    api_dl.resolver.previews.add(f"sc:{SC}")
    api_dl.post("/api/downloads", json=SC_REQ)
    job = job_of(api_dl, f"sc:{SC}")
    assert job["state"] == "error" and job["code"] == "preview_only" and "фрагмент" in job["error"]
    assert not api_dl.net.cdn_requests()
    nothing_saved(api_dl)


def test_failed_track_can_be_requested_again(api_dl):
    """После временной ошибки задача завершена, повторный запрос снова получает поток и скачивает трек."""
    api_dl.resolver.fail("yt", YT, stream.DownloadError("HTTP Error 429: Too Many Requests"))
    api_dl.post("/api/downloads", json=YT_REQ)
    assert job_of(api_dl, f"yt:{YT}")["code"] == "rate_limited"
    api_dl.post("/api/downloads", json=YT_REQ)
    assert job_of(api_dl, f"yt:{YT}")["state"] == "done"


def test_existing_download_is_kept_and_plays_without_the_source(api_dl):
    """Скачанное раньше не проверяется повторно, не удаляется и играет с диска без резолва."""
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
