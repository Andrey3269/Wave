"""
LIVE-S8: SoundCloud HLS без подмен (S8, N4). По умолчанию НЕ запускается: нужен маркер `-m live` и сеть.

    python -m pytest -m live -rP tests/live/test_live_hls.py

Настоящее приложение (TestClient с lifespan: httpx-клиент, резолвер yt-dlp, HLS-прокси, netguard с настоящим
DNS, загрузчик) против настоящего SoundCloud. Профиль и загрузки временные. Ссылки и подписи в вывод не попадают:
итог каждой проверки — строка JSON (только хосты CDN).

LIVE-S8-1  /playback → HLS-прокси → сегменты: для каждого вида HLS SoundCloud (mp3 и AAC/fMP4) поток
           принудительно выбирается этим видом (из настоящего списка форматов yt-dlp), сегменты проходят
           проверку контейнера.
LIVE-S8-2  загрузка HLS-only трека (тот же принудительный выбор) → .mp3 / .m4a, проверка файла, офлайн 206.
LIVE-S8-3  без принуждения: несколько резолвов подряд (N4 — у SoundCloud меняется набор форматов), каждый
           через /playback играет (прогрессивный — 206, HLS — плейлист и первый сегмент). Печатает
           распределение видов потока.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

import downloads
import paths
import providers
from streaming import containers, netguard, resolver

pytestmark = pytest.mark.live
TARGETS = json.loads((Path(__file__).resolve().parent.parent / "live_targets.json").read_text(encoding="utf-8"))
TRACK = TARGETS["soundcloud_track_id"]


@pytest.fixture(autouse=True)
def fresh_providers():
    providers.reset()
    yield
    providers.reset()


def report(name: str, **data) -> None:
    print(json.dumps({"check": name, **data}, ensure_ascii=False))


def _force_hls(monkeypatch, codec: str) -> dict:
    """Выбор формата ограничен HLS с нужным кодеком из НАСТОЯЩЕГО ответа yt-dlp (остальное — как у плеера)."""
    seen: dict = {}
    real = resolver.choose_format

    def only(info):
        formats = info.get("formats") or []
        seen["offered"] = sorted(str(f.get("format_id")) for f in formats)
        picked = [f for f in formats if resolver.classify(f) == ("hls", codec)
                  and "preview" not in str(f.get("format_id")).lower()]
        if not picked:
            raise resolver.NoPlayableFormat(f"SoundCloud сейчас не предложил HLS {codec}: {seen['offered']}")
        return real({"formats": picked})
    monkeypatch.setattr(resolver, "choose_format", only)
    return seen


def _client():
    from fastapi.testclient import TestClient

    import main
    return TestClient(main.app, base_url="http://127.0.0.1")


@pytest.mark.parametrize("codec", ["mp3", "aac"])
def test_live_s8_1_hls_playback_through_proxy(monkeypatch, codec):
    seen = _force_hls(monkeypatch, codec)
    hosts: set[str] = set()
    real_send = netguard.send

    async def spy(client, url, headers):
        hosts.add(netguard.redact(url))
        return await real_send(client, url, headers)
    monkeypatch.setattr(netguard, "send", spy)
    with _client() as c:
        t0 = time.monotonic()
        r = c.get(f"/api/stream/sc/{TRACK}/playback")
        if r.status_code == 404 and r.headers.get("X-Wave-Error") == "no_playable_format":
            pytest.skip(f"BLOCKED: {r.json()['detail']} (форматы: {seen.get('offered')})")
        assert r.status_code == 200, r.text
        info = r.json()
        assert info["kind"] == "hls" and info["codec"] == codec
        text = c.get(info["url"]).text
        assert "sndcdn" not in text and "soundcloud.cloud" not in text and "Signature" not in text
        uris = [ln for ln in text.splitlines() if ln and not ln.startswith("#")]
        maps = [ln.split('URI="', 1)[1].rstrip('"') for ln in text.splitlines() if ln.startswith("#EXT-X-MAP:")]
        got = []
        if maps:
            init = c.get(maps[0])
            assert init.status_code == 200
            containers.check_init(init.content)
            got.append(len(init.content))
        joiner = containers.Mp3Joiner()
        for u in uris[:2]:
            seg = c.get(u)
            assert seg.status_code == 200, seg.text[:200]
            if maps:
                containers.check_fragment(seg.content)
            else:
                joiner.feed(seg.content)
            got.append(len(seg.content))
        took = round(time.monotonic() - t0, 1)
    report("LIVE-S8-1", codec=codec, segments=len(uris), init=bool(maps), bytes=got, seconds=took,
           cdn_hosts=sorted(hosts), formats_offered=seen.get("offered"))


@pytest.mark.parametrize("codec", ["mp3", "aac"])
def test_live_s8_2_hls_download(monkeypatch, codec):
    seen = _force_hls(monkeypatch, codec)
    req = {"source": "sc", "id": TRACK, "title": f"live-hls-{codec}", "artist": "Wave live test", "album": None,
           "duration": None, "cover": None}
    with _client() as c:
        t0 = time.monotonic()
        assert c.post("/api/downloads", json=req).status_code == 202
        key = f"sc:{TRACK}"
        while time.monotonic() - t0 < 240:
            job = c.get("/api/downloads/jobs").json().get(key)
            if job and job["state"] not in downloads.ACTIVE:
                break
            time.sleep(0.5)
        if job and job.get("code") == "no_playable_format":
            pytest.skip(f"BLOCKED: {job['error']} (форматы: {seen.get('offered')})")
        assert job and job["state"] == "done", f"загрузка не завершилась: {job}"
        folder = next(p for p in paths.downloads_dir().iterdir() if p.name.endswith(f"[sc-{TRACK}]"))
        meta = json.loads((folder / "metadata.json").read_text(encoding="utf-8"))
        audio = folder / meta["audio"]
        assert audio.suffix == (".mp3" if codec == "mp3" else ".m4a")
        assert audio.stat().st_size == job["total"] > 0
        head = audio.read_bytes()[:16]
        assert not head.startswith(b"#EXTM3U")
        probe = None
        if shutil.which("ffprobe"):                         # необязательная внешняя проверка, если ffprobe есть
            out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_name",
                                  "-of", "json", str(audio)], capture_output=True, text=True, timeout=60)
            probe = json.loads(out.stdout or "{}")
            assert out.returncode == 0 and probe.get("streams"), out.stderr
            dec = subprocess.run(["ffmpeg", "-v", "error", "-i", str(audio), "-f", "null", "-"],
                                 capture_output=True, text=True, timeout=120)
            assert dec.returncode == 0 and not dec.stderr.strip(), dec.stderr[:500]
        play = c.get(f"/api/stream/sc/{TRACK}", headers={"Range": "bytes=0-1023"})
        assert play.status_code == 206 and len(play.content) == 1024
        took = round(time.monotonic() - t0, 1)
    report("LIVE-S8-2", codec=codec, audio=meta["audio"], bytes=job["total"], seconds=took,
           ffprobe=(probe or {}).get("format"), codecs=[s.get("codec_name") for s in (probe or {}).get("streams", [])])


def test_live_s8_3_every_resolve_is_playable():
    kinds = []
    with _client() as c:
        for _ in range(int(TARGETS.get("soundcloud_resolve_runs", 6))):
            resolver._invalidate("sc", TRACK)
            r = c.get(f"/api/stream/sc/{TRACK}/playback")
            assert r.status_code == 200, r.text
            info = r.json()
            if info["kind"] == "hls":
                text = c.get(info["url"]).text
                first = next(ln for ln in text.splitlines() if ln and not ln.startswith("#"))
                assert c.get(first).status_code == 200
                kinds.append(f"hls/{info['codec']}")
            else:
                s = c.get(info["url"], headers={"Range": "bytes=0-1023"})
                assert s.status_code == 206 and len(s.content) == 1024
                kinds.append(info["kind"])
    report("LIVE-S8-3", runs=len(kinds), kinds={k: kinds.count(k) for k in sorted(set(kinds))}, playable=len(kinds))
