"""
LIVE-7 (TEST_STRATEGY §7, этап S14): «радио» трека — источник кандидатов рекомендаций — у живых YouTube Music и
SoundCloud непустое и нормализуется адаптером так же, как в приложении (backend/recs.py, задание radio).

По умолчанию НЕ запускается: нужен маркер `-m live` и доступ в сеть.

    python -m pytest -m live -rP tests/live/test_live_recs.py

Треки — публичные цели из tests/live_targets.json. Данные пользователя не используются: профиль временный.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import providers
import recs
from providers import youtube_music as yt_provider

pytestmark = pytest.mark.live
TARGETS = json.loads((Path(__file__).resolve().parent.parent / "live_targets.json").read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def fresh_providers():
    providers.reset()
    yield
    providers.reset()


def _check(tracks, source: str, own_id: str) -> None:
    assert tracks, f"«радио» {source} пустое"
    for t in tracks:
        assert t.source == source and t.id and t.title and t.artist and t.stream_url == f"/api/stream/{source}/{t.id}"
    print(json.dumps({"check": f"LIVE-7 {source}", "tracks": len(tracks), "self": any(t.id == own_id for t in tracks),
                      "artists": len({recs._name(t.artist) for t in tracks})}, ensure_ascii=False))


def test_live7_youtube_music_radio():
    state = SimpleNamespace(ytm=yt_provider.create_client())          # как в lifespan приложения
    vid = TARGETS["youtube_video_id"]
    tracks = asyncio.run(providers.yt.radio(state, vid, 25, timeout=recs.JOB_TIMEOUT * 2))
    _check(tracks, "yt", vid)


def test_live7_soundcloud_radio():
    tid = TARGETS["soundcloud_track_id"]
    tracks = asyncio.run(providers.sc.radio(None, tid, 25, timeout=recs.JOB_TIMEOUT * 2))
    _check(tracks, "sc", tid)
