"""
LIVE-8 (S5): настоящая загрузка через приложение без подмен. По умолчанию НЕ запускается.

    python -m pytest -m live -rP tests/live/test_live_downloads.py

Нужен доступ к YouTube и SoundCloud; для YouTube — Deno, как в LIVE-2 (WAVE_JS_RUNTIME или deno в PATH).
Цели публичные (tests/live_targets.json), без входа в аккаунт. Профиль и каталог загрузок временные
(tests/conftest.py). Ссылки на поток в вывод не попадают: печатается итог одной строкой JSON.

LIVE-8a — YouTube Music: POST /api/downloads → done; аудио, metadata.json, запись в «Скачанных»;
           трек играет с диска (206) без нового резолва.
LIVE-8b — SoundCloud: то же для публичного трека через штатный поток (прогрессивный формат).
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

import downloads
import jsruntime
import paths
import stream

pytestmark = pytest.mark.live
TARGETS = json.loads((Path(__file__).resolve().parent.parent / "live_targets.json").read_text(encoding="utf-8"))


@pytest.fixture
def real_runtime():
    jsruntime.reset()
    st = jsruntime.status()
    if not st.ok:
        pytest.fail(f"Для LIVE-8a нужен Deno ≥ 2.3.0: {st.problem} ({st.detail})", pytrace=False)
    yield st
    jsruntime.GUARD.terminate_all()


def _download_and_play(source: str, track_id: str, monkeypatch) -> dict:
    from fastapi.testclient import TestClient

    import main

    resolves = []
    original = stream._extract_sync
    monkeypatch.setattr(stream, "_extract_sync", lambda s, i: resolves.append(i) or original(s, i))
    req = {"source": source, "id": track_id, "title": f"live-{source}", "artist": "Wave live test",
           "album": None, "duration": None, "cover": None}
    with TestClient(main.app, base_url="http://127.0.0.1") as c:
        t0 = time.monotonic()
        assert c.post("/api/downloads", json=req).status_code == 202
        key = f"{source}:{track_id}"
        while time.monotonic() - t0 < 180:
            job = c.get("/api/downloads/jobs").json().get(key)
            if job and job["state"] not in downloads.ACTIVE:
                break
            time.sleep(0.5)
        took = round(time.monotonic() - t0, 1)
        assert job and job["state"] == "done", f"загрузка не завершилась: {job}"

        [item] = [t for t in c.get("/api/downloads").json()["items"] if t["id"] == track_id]
        folder = next(p for p in paths.downloads_dir().iterdir() if p.name.endswith(f"[{source}-{track_id}]"))
        meta = json.loads((folder / "metadata.json").read_text(encoding="utf-8"))
        audio = folder / meta["audio"]
        assert audio.stat().st_size == job["total"] > 0

        before = len(resolves)
        play = c.get(f"/api/stream/{source}/{track_id}", headers={"Range": "bytes=0-1023"})
        assert play.status_code == 206 and len(play.content) == 1024
        assert len(resolves) == before, "скачанный трек должен играть с диска без резолва"
    return {"seconds": took, "audio": meta["audio"], "bytes": job["total"], "lyrics": meta["lyrics"] is not None,
            "listed": bool(item)}


def test_live8a_youtube_download(real_runtime, monkeypatch):
    out = _download_and_play("yt", TARGETS["youtube_video_id"], monkeypatch)
    print(json.dumps({"live": "8a", **out}, ensure_ascii=False))


def test_live8b_soundcloud_download(monkeypatch):
    out = _download_and_play("sc", TARGETS["soundcloud_track_id"], monkeypatch)
    print(json.dumps({"live": "8b", **out}, ensure_ascii=False))
