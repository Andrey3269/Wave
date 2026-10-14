"""
LIVE-5 (TEST_STRATEGY §7, этап S7): SoundCloud без подмен — поиск, поток, отказ API.
S8: поток может быть прогрессивным или HLS (N4) — проверка потока принимает оба вида (HLS подробно — LIVE-S8,
tests/live/test_live_hls.py).

По умолчанию НЕ запускается: нужен маркер `-m live` и доступ в сеть.

    python -m pytest -m live -rP tests/live/test_live_soundcloud.py

Вывод `-rP` — итог каждой проверки строкой JSON (без ссылок на поток и без client_id).
Данные пользователя не используются: профиль временный, как и в остальных тестах.
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import providers
import stream
from providers import soundcloud as sc_provider, youtube_music as yt_provider
from providers.base import ProviderError

pytestmark = pytest.mark.live
TARGETS = json.loads((Path(__file__).resolve().parent.parent / "live_targets.json").read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def fresh_providers():
    providers.reset()
    yield
    providers.reset()


def report(name: str, **data) -> None:
    print(json.dumps({"check": name, **data}, ensure_ascii=False))


def test_live5_soundcloud_search():
    """Поиск треков и артистов через адаптер (API v2, client_id от yt-dlp) — непустые нормализованные данные."""
    t0 = time.monotonic()
    tracks = asyncio.run(providers.sc.search_tracks(None, TARGETS["soundcloud_query"], 10, timeout=30))
    took = time.monotonic() - t0
    assert tracks, "SoundCloud не вернул треков"
    for t in tracks:
        assert t.source == "sc" and t.id.isdigit() and t.title and t.artist and t.stream_url == f"/api/stream/sc/{t.id}"
    cards, _more = asyncio.run(providers.sc.search_cards(None, "artists", TARGETS["soundcloud_query"], 5, 0, timeout=30))
    assert cards and all(c["source"] == "sc" and c["kind"] == "artist" and c["id"] for c in cards)
    assert providers.sc.health() == "ok"
    report("LIVE-5 search", tracks=len(tracks), artists=len(cards), seconds=round(took, 1))


def test_live5_soundcloud_progressive_stream():
    """
    Штатный резолвер (yt-dlp) отдаёт воспроизводимый поток. Прогрессивный — CDN отвечает 206 на диапазон
    (как до S8); HLS (S8, N4) — плейлист m3u8 с сегментами (подробная проверка — LIVE-S8).
    """
    tid = TARGETS["soundcloud_track_id"]
    t0 = time.monotonic()
    res = stream._extract_sync("sc", tid)                      # то же извлечение, что у плеера
    resolve_s = time.monotonic() - t0
    assert res.url.startswith("http") and not res.preview
    if res.kind == "hls":
        async def playlist_head():
            client = stream.make_http_client()
            try:
                r = await client.get(res.url, headers=res.headers)
                return r.status_code, r.text[:7]
            finally:
                await client.aclose()
        status, head = asyncio.run(playlist_head())
        assert status == 200 and head == "#EXTM3U"
        report("LIVE-5 stream", track=tid, kind="hls", codec=res.codec, status=status, resolve_seconds=round(resolve_s, 1))
        return

    async def first_bytes():
        client = stream.make_http_client()                     # тот же клиент, что у прокси приложения
        try:
            r = await client.get(res.url, headers={**res.headers, "Range": "bytes=0-65535"})
            return r.status_code, len(r.content), r.headers.get("content-type"), r.headers.get("content-range")
        finally:
            await client.aclose()

    status, size, ctype, crange = asyncio.run(first_bytes())
    assert status == 206 and size == 65536 and crange and crange.startswith("bytes 0-65535/")
    report("LIVE-5 stream", track=tid, kind="progressive", status=status, bytes=size, content_type=ctype,
           total=crange.split("/")[-1], resolve_seconds=round(resolve_s, 1))


def test_live5_soundcloud_api_refusal(monkeypatch):
    """
    Отказ API: (1) настоящий ответ «нет такого пользователя» — not_found, источник не уходит в паузу;
    (2) API недоступен (соединение отклонено) — unavailable и пауза 300 с только для SoundCloud,
    а YouTube Music продолжает отвечать.
    """
    with pytest.raises(ProviderError) as e:
        asyncio.run(providers.sc.artist_info(None, "999999999999999", timeout=30))
    assert e.value.kind == "not_found" and e.value.source == "sc" and not providers.sc.breaker.is_open()
    not_found = e.value.kind

    # API «упал»: закрытый локальный порт, соединение отклоняется. Локальный адрес — напрямую, мимо прокси
    # (в тестах YTDLP_PROXY и так пуст; это важно, если live-проверку запускают через прокси). Новый пул —
    # новые экземпляры yt-dlp с этими настройками.
    providers.sc.reset()
    monkeypatch.setattr(stream, "YTDLP_PROXY", None)
    monkeypatch.setattr(sc_provider, "API_BASE", "http://127.0.0.1:9/")
    t0 = time.monotonic()
    with pytest.raises(ProviderError) as e:
        asyncio.run(providers.sc.search_cards(None, "artists", "x", 5, 0, timeout=30))
    took = time.monotonic() - t0
    assert e.value.kind == "unavailable" and providers.sc.breaker.is_open() and took < 15
    with pytest.raises(ProviderError) as again:
        asyncio.run(providers.sc.search_cards(None, "albums", "x", 5, 0, timeout=30))
    assert again.value.cooldown                                # в паузе запрос не отправляется

    # YouTube Music при этом отвечает: запросы без ошибки, источник не на паузе. Пустая выдача — тоже ответ
    # (в некоторых сетях YouTube Music не отдаёт раздел песен), поэтому данные проверяются по артистам.
    state = SimpleNamespace(ytm=yt_provider.create_client())
    yt_tracks = asyncio.run(providers.yt.search_tracks(state, TARGETS["ytmusic_query"], 3, timeout=30))
    yt_artists, _ = asyncio.run(providers.yt.search_cards(state, "artists", TARGETS["ytmusic_query"], 3, 0, timeout=30))
    assert yt_artists and providers.yt.health() == "ok" and not providers.yt.breaker.is_open()
    report("LIVE-5 refusal", missing_user=not_found, api_down=e.value.kind, api_down_seconds=round(took, 1),
           sc_status=providers.sc.health(), yt_status=providers.yt.health(), yt_tracks=len(yt_tracks),
           yt_artists=len(yt_artists))
