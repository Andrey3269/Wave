"""
Живые источники (TEST_STRATEGY §7). По умолчанию НЕ запускаются: нужен маркер `-m live` и доступ в сеть.

    python -m pytest -m live

LIVE-1 — ytmusicapi: поиск треков, артистов и альбомов возвращает непустые нормализованные данные.
LIVE-6 — LRCLIB: синхронный текст для известного трека.
Данные пользователя не используются: профиль временный, как и в остальных тестах.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

import catalog
import lyrics
import main

pytestmark = pytest.mark.live
TARGETS = json.loads((Path(__file__).resolve().parent.parent / "live_targets.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def ytm():
    from ytmusicapi import YTMusic
    return YTMusic()


def test_live1_ytmusic_tracks(ytm):
    tracks = main._search_yt_sync(ytm, TARGETS["ytmusic_query"], 5)
    assert tracks, "YouTube Music не вернул треков"
    t = tracks[0]
    assert t.source == "yt" and len(t.id) == 11 and t.title and t.artists and t.stream_url == f"/api/stream/yt/{t.id}"


@pytest.mark.parametrize("kind", ["artists", "albums"])
def test_live1_ytmusic_cards(ytm, kind):
    cards = catalog._yt_search(ytm, TARGETS["ytmusic_query"], kind, 5)
    assert cards and all(c["id"] and c["source"] == "yt" for c in cards)


def test_live6_lrclib_synced_lyrics():
    async def go():
        async with httpx.AsyncClient() as client:
            target = TARGETS["lyrics"]
            return await lyrics.fetch_lyrics(client, target["artist"], target["title"], target["duration"])

    out = asyncio.run(go())
    assert out is not None, "LRCLIB недоступен"
    assert out["synced"] and all(isinstance(r["t"], float) and r["text"] for r in out["synced"])
