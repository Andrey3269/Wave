"""
Живые источники (TEST_STRATEGY §7). По умолчанию НЕ запускаются: нужен маркер `-m live` и доступ в сеть.

    python -m pytest -m live

LIVE-1 — ytmusicapi: поиск треков, артистов и альбомов возвращает непустые нормализованные данные.
         С S7 — через адаптер (providers/youtube_music.py): настоящий клиент с таймаутами сессии, пул
         источника, нормализация и классификация ошибок — так же, как в приложении.
LIVE-6 — LRCLIB: синхронный текст для известного трека.
Данные пользователя не используются: профиль временный, как и в остальных тестах.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

import lyrics
import providers
from providers import youtube_music as yt_provider

pytestmark = pytest.mark.live
TARGETS = json.loads((Path(__file__).resolve().parent.parent / "live_targets.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def state():
    return SimpleNamespace(ytm=yt_provider.create_client())    # как в lifespan приложения


@pytest.fixture(autouse=True)
def fresh_provider():
    providers.yt.reset()
    yield
    providers.yt.reset()


def test_live1_ytmusic_tracks(state):
    tracks = asyncio.run(providers.yt.search_tracks(state, TARGETS["ytmusic_query"], 5, timeout=30))
    assert tracks, "YouTube Music не вернул треков"
    t = tracks[0]
    assert t.source == "yt" and len(t.id) == 11 and t.title and t.artists and t.stream_url == f"/api/stream/yt/{t.id}"
    assert providers.yt.health() == "ok"


@pytest.mark.parametrize("kind", ["artists", "albums"])
def test_live1_ytmusic_cards(state, kind):
    cards, _more = asyncio.run(providers.yt.search_cards(state, kind, TARGETS["ytmusic_query"], 5, 0, timeout=30))
    assert cards and all(c["id"] and c["source"] == "yt" and c["kind"] == kind[:-1] for c in cards)


def test_live6_lrclib_synced_lyrics():
    async def go():
        async with httpx.AsyncClient() as client:
            target = TARGETS["lyrics"]
            return await lyrics.fetch_lyrics(client, target["artist"], target["title"], target["duration"])

    out = asyncio.run(go())
    assert out is not None, "LRCLIB недоступен"
    assert out["synced"] and all(isinstance(r["t"], float) and r["text"] for r in out["synced"])
