"""Тексты песен через LRCLIB (backend/lyrics.py): разбор LRC и обработка ответов сервиса."""
from __future__ import annotations

import asyncio

import httpx
import pytest

import lyrics
from wavetest.fakes import FakeNet, load_fixture


def fetch(net: FakeNet, artist="Aurora Wave", title="Northern Lights", duration=None):
    async def go():
        async with net.client() as c:
            return await lyrics.fetch_lyrics(c, artist, title, duration)
    return asyncio.run(go())


def test_parse_lrc():
    rows = lyrics._parse_lrc(load_fixture("lrclib/get_synced.json")["syncedLyrics"])
    assert rows == [{"t": 1.5, "text": "Первая строка"}, {"t": 62.25, "text": "Вторая строка"}]


@pytest.mark.parametrize("text, expected", [
    ("", []),
    ("[00:00] без дробной части", [{"t": 0.0, "text": "без дробной части"}]),
    ("[10:05.5]  пробелы  ", [{"t": 605.5, "text": "пробелы"}]),
    ("[ti: заголовок]\n[00:01.00]\n", []),                        # метаданные и пустые строки пропускаются
    ("просто текст без таймкодов", []),
])
def test_parse_lrc_edge_cases(text, expected):
    assert lyrics._parse_lrc(text) == expected


def test_fetch_success():
    net = FakeNet()
    net.lyrics = (200, load_fixture("lrclib/get_synced.json"))
    out = fetch(net, duration=225)
    assert out == {"synced": [{"t": 1.5, "text": "Первая строка"}, {"t": 62.25, "text": "Вторая строка"}],
                   "plain": "Первая строка\nВторая строка", "provider": "lrclib"}
    req = net.requests[0]
    assert str(req.url).startswith("https://lrclib.net/api/get?")
    assert dict(req.url.params) == {"artist_name": "Aurora Wave", "track_name": "Northern Lights", "duration": "225"}
    assert req.headers["user-agent"] == "Wave/0.1"


def test_fetch_truncates_long_params_and_omits_missing_duration():
    net = FakeNet()
    fetch(net, artist="A" * 500, title="T" * 500)
    params = dict(net.requests[0].url.params)
    assert len(params["artist_name"]) == 200 and len(params["track_name"]) == 200
    assert "duration" not in params


def test_not_found_is_empty_result():
    net = FakeNet()
    net.lyrics = (404, {"message": "Failed to find specified track"})
    assert fetch(net) == {"synced": [], "plain": None, "provider": "lrclib"}


@pytest.mark.parametrize("lyr", [(500, {"error": "x"}), (429, {"error": "rate"}), (200, httpx.ConnectError("offline"))])
def test_temporary_failure_is_none(lyr):
    net = FakeNet()
    net.lyrics = lyr
    assert fetch(net) is None


def test_plain_only_lyrics():
    net = FakeNet()
    net.lyrics = (200, {"plainLyrics": "строка", "syncedLyrics": None})
    assert fetch(net) == {"synced": [], "plain": "строка", "provider": "lrclib"}


@pytest.mark.xfail(strict=True, reason="M8: r.json() без защиты — не-JSON ответ LRCLIB бросает исключение вместо «временного сбоя»")
def test_non_json_response_is_temporary_failure():
    net = FakeNet()
    net.lyrics = (200, "<html>Cloudflare</html>")
    assert fetch(net) is None
