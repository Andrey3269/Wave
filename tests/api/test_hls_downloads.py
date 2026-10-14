"""
Загрузка трека, который источник отдаёт только по HLS (S8): тот же поток, что играет плеер, проверенный
по контейнеру и собранный в .mp3 (mp3-HLS) или .m4a (fMP4-HLS); затем воспроизведение с диска без сети.
Политика S5 (скачивается то, что штатно играет) не меняется. CDN подменён, DNS подменён.
"""
from __future__ import annotations

import json
import time

import httpx
import pytest

import downloads
from wavetest.hlsfix import MP3_FRAME_SEC, fmp4_init, fmp4_segment, id3_tag, master_playlist, media_playlist, mp3_frames

SC = "1000000001"
KEY = f"sc:{SC}"
ROOT = f"/sc/{SC}/main.m3u8"
REQ = {"source": "sc", "id": SC, "title": "Only HLS", "artist": "Uploader", "album": None, "duration": 3, "cover": None}
FOLDER = f"Uploader - Only HLS [sc-{SC}]"
PL_TYPE = "application/vnd.apple.mpegurl"


def wait_job(api, key=KEY, timeout=8.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = api.get("/api/downloads/jobs").json().get(key)
        if job and job["state"] not in downloads.ACTIVE:
            return job
        time.sleep(0.02)
    raise AssertionError(f"загрузка {key} не завершилась: {api.get('/api/downloads/jobs').json()}")


def nothing_left(api) -> bool:
    return not list(api.profile.downloads.iterdir()) and api.profile.sql("SELECT COUNT(*) FROM downloads") == [(0,)]


def mp3_track(api, segs, durations=None, codec="mp3", names=None):
    names = names or [f"seg{i}.mp3" for i in range(len(segs))]
    for name, data in zip(names, segs):
        api.net.add_file(f"/sc/{SC}/{name}", data, "audio/mpeg")
    durs = durations or [round(len(d) / 417 * MP3_FRAME_SEC, 6) for d in segs]
    api.net.add_file(ROOT, media_playlist([f"{n}?Signature=S" for n in names], durs).encode(), PL_TYPE)
    api.resolver.hls[KEY] = codec


def test_mp3_hls_download_and_offline_playback(api):
    segs = [mp3_frames(40), mp3_frames(35), mp3_frames(20)]
    mp3_track(api, segs)
    r = api.post("/api/downloads", json=REQ)
    assert r.status_code == 202
    job = wait_job(api)
    assert job["state"] == "done" and job["progress"] == 100 and job["error"] is None

    folder = api.profile.downloads / FOLDER
    audio = (folder / "audio.mp3").read_bytes()
    assert audio == b"".join(segs) and not audio.startswith(b"#EXTM3U")
    assert sorted(p.name for p in folder.iterdir()) == ["audio.mp3", "metadata.json"]
    meta = json.loads((folder / "metadata.json").read_text(encoding="utf-8"))
    assert meta["audio"] == "audio.mp3" and meta["source_url"] == f"https://api.soundcloud.com/tracks/{SC}"
    assert api.profile.sql("SELECT key, audio, size FROM downloads") == [(KEY, "audio.mp3", len(audio))]
    assert api.resolver.calls[KEY] == 1                       # один резолв на проверку политики и на загрузку

    # Офлайн: тот же адрес играет файл с диска, без резолва и сети; /playback говорит local
    api.net.requests.clear()
    calls = api.resolver.total()
    r = api.get(f"/api/stream/sc/{SC}", headers={"Range": "bytes=0-"})
    assert r.status_code == 206 and r.content == audio and r.headers["content-type"] == "audio/mpeg"
    assert api.get(f"/api/stream/sc/{SC}/playback").json()["kind"] == "local"
    assert api.resolver.total() == calls and not api.net.requests


def test_id3_timestamp_tags_are_removed(api):
    """Packed audio (RFC 8216 §3.4): сегмент начинается с тега ID3 PRIV с меткой времени — в файл он не попадает."""
    segs = [id3_tag() + mp3_frames(20) for _ in range(3)]
    mp3_track(api, segs, durations=[20 * MP3_FRAME_SEC] * 3)
    api.post("/api/downloads", json=REQ)
    assert wait_job(api)["state"] == "done"
    assert (api.profile.downloads / FOLDER / "audio.mp3").read_bytes() == mp3_frames(60)


def test_frames_split_by_segment_boundaries(api):
    """Сегменты — байтовые срезы mp3 (так у SoundCloud): кадр, разрезанный границей, собирается без потерь."""
    stream = mp3_frames(60)
    cuts = [0, 7000, 15001, len(stream)]
    mp3_track(api, [stream[a:b] for a, b in zip(cuts, cuts[1:])], durations=[20 * MP3_FRAME_SEC] * 3)
    api.post("/api/downloads", json=REQ)
    assert wait_job(api)["state"] == "done"
    assert (api.profile.downloads / FOLDER / "audio.mp3").read_bytes() == stream


def test_fmp4_hls_download_is_m4a(api):
    init, segs = fmp4_init(), [fmp4_segment(i) for i in range(3)]
    api.net.add_file(f"/sc/{SC}/init.mp4", init, "audio/mp4")
    for i, d in enumerate(segs):
        api.net.add_file(f"/sc/{SC}/data{i:03d}.m4s", d, "audio/mp4")
    api.net.add_file(ROOT, media_playlist([f"data{i:03d}.m4s" for i in range(3)], map_uri="init.mp4").encode(), PL_TYPE)
    api.resolver.hls[KEY] = "aac"
    api.post("/api/downloads", json=REQ)
    job = wait_job(api)
    assert job["state"] == "done", job
    audio = (api.profile.downloads / FOLDER / "audio.m4a").read_bytes()
    assert audio == init + b"".join(segs)
    r = api.get(f"/api/stream/sc/{SC}", headers={"Range": "bytes=0-"})
    assert r.headers["content-type"] == "audio/mp4" and r.content == audio


def test_master_playlist_downloads_best_variant(api):
    low, high = mp3_frames(10), mp3_frames(12)
    api.net.add_file(ROOT, master_playlist([("low.m3u8", "BANDWIDTH=64000"), ("high.m3u8", "BANDWIDTH=160000")]).encode(),
                     PL_TYPE)
    for name, data in (("low", low), ("high", high)):
        api.net.add_file(f"/sc/{SC}/{name}.m3u8", media_playlist([f"{name}.mp3"], [len(data) / 417 * MP3_FRAME_SEC]).encode(),
                         PL_TYPE)
        api.net.add_file(f"/sc/{SC}/{name}.mp3", data, "audio/mpeg")
    api.resolver.hls[KEY] = "mp3"
    api.post("/api/downloads", json=REQ)
    assert wait_job(api)["state"] == "done"
    assert (api.profile.downloads / FOLDER / "audio.mp3").read_bytes() == high
    assert not api.net.cdn_requests(f"/sc/{SC}/low.mp3")


@pytest.mark.parametrize("segment, code", [
    ((b"\x47" + bytes(187)) * 20, "hls_container_unsupported"),           # MPEG-TS: без перепаковки не сохранить
    (b"OggS\x00\x02" + bytes(3000), "hls_container_unsupported"),         # неизвестный контейнер
    (mp3_frames(3) + b"<html>oops</html>" + mp3_frames(3), "hls_bad_segment"),
])
def test_unsupported_or_broken_segments_are_not_saved(api, segment, code):
    mp3_track(api, [segment])
    api.post("/api/downloads", json=REQ)
    job = wait_job(api)
    assert job["state"] == "error" and job["code"] == code and "yt-dlp" not in job["error"]
    assert nothing_left(api)


def test_duration_mismatch_is_rejected(api):
    """Плейлист обещает 100 с, а в сегментах 1 с аудио — неполный трек под видом полного не сохраняется."""
    mp3_track(api, [mp3_frames(40)], durations=[100.0])
    api.post("/api/downloads", json=REQ)
    job = wait_job(api)
    assert job["state"] == "error" and job["code"] == "hls_bad_segment" and nothing_left(api)


def test_live_playlist_is_refused(api):
    api.net.add_file(f"/sc/{SC}/seg0.mp3", mp3_frames(5), "audio/mpeg")
    api.net.add_file(ROOT, media_playlist(["seg0.mp3"], endlist=False).encode(), PL_TYPE)
    api.resolver.hls[KEY] = "mp3"
    api.post("/api/downloads", json=REQ)
    job = wait_job(api)
    assert job["state"] == "error" and job["code"] == "hls_live" and nothing_left(api)


def test_broken_segment_is_retried_without_duplicates(api):
    segs = [mp3_frames(30), mp3_frames(30)]
    mp3_track(api, segs)
    real = api.net.serve_file

    def broken_once(request):
        api.net.fail_after = 1
        api.net.chunk = 2000
        try:
            return real(request)
        finally:
            api.net.fail_after = None
    api.net.queue(f"/sc/{SC}/seg1.mp3", broken_once)
    api.post("/api/downloads", json=REQ)
    assert wait_job(api)["state"] == "done"
    assert (api.profile.downloads / FOLDER / "audio.mp3").read_bytes() == b"".join(segs)
    assert len(api.net.cdn_requests(f"/sc/{SC}/seg1.mp3")) == 2


def test_segment_that_keeps_breaking_fails_cleanly(api):
    mp3_track(api, [mp3_frames(30)])

    def dead(request):
        return httpx.Response(200, headers={"Content-Length": "12510", "Content-Type": "audio/mpeg"},
                              stream=httpx.ByteStream(b"\xff\xfb"))                # короче объявленного
    from streaming import hls_download
    api.net.queue(f"/sc/{SC}/seg0.mp3", *[dead] * (hls_download.SEGMENT_RETRIES + 1))
    api.post("/api/downloads", json=REQ)
    job = wait_job(api)
    assert job["state"] == "error" and job["code"] == "network" and nothing_left(api)


def test_stale_segment_link_is_refreshed(api):
    segs = [mp3_frames(20), mp3_frames(20)]
    mp3_track(api, segs)
    api.net.queue(f"/sc/{SC}/seg1.mp3", 403)
    api.post("/api/downloads", json=REQ)
    assert wait_job(api)["state"] == "done"
    assert api.resolver.calls[KEY] == 2
    assert (api.profile.downloads / FOLDER / "audio.mp3").read_bytes() == b"".join(segs)


def test_segment_on_internal_address_is_blocked(api):
    api.dns["evil.test"] = ["192.168.1.1"]
    api.net.add_file(ROOT, media_playlist(["https://evil.test/a.mp3"]).encode(), PL_TYPE)
    api.resolver.hls[KEY] = "mp3"
    api.post("/api/downloads", json=REQ)
    job = wait_job(api)
    assert job["state"] == "error" and job["code"] == "upstream_blocked" and nothing_left(api)
    assert not [q for q in api.net.requests if q.url.host == "evil.test"]


def test_hls_preview_is_still_refused(api):
    """Правило S5: фрагмент (превью Go+) не сохраняется — и для HLS тоже."""
    mp3_track(api, [mp3_frames(10)])
    api.resolver.previews.add(KEY)
    api.post("/api/downloads", json=REQ)
    job = wait_job(api)
    assert job["state"] == "error" and job["code"] == "preview_only" and nothing_left(api)
    assert not api.net.cdn_requests(ROOT)


def test_progress_is_reported_per_segment(api, monkeypatch):
    segs = [mp3_frames(10) for _ in range(4)]
    mp3_track(api, segs)
    seen = []
    real = downloads.hls_download._fetch

    async def spy(*a, **kw):
        data = await real(*a, **kw)
        seen.append(dict(downloads.jobs[KEY]))
        return data
    monkeypatch.setattr(downloads.hls_download, "_fetch", spy)
    api.post("/api/downloads", json=REQ)
    assert wait_job(api)["state"] == "done"
    assert [j["state"] for j in seen] == ["downloading"] * 4
    assert [j["progress"] for j in seen] == [0, 25, 50, 75]                # прогресс до записи текущего сегмента
