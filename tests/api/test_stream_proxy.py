"""
Прокси аудиопотока /api/stream/{source}/{id} (backend/stream.py) против подменённого CDN.

CDN — wavetest.fakes.FakeNet (поддерживает Range как настоящий googlevideo), резолвер yt-dlp —
wavetest.fakes.FakeResolver. Внешней сети нет.
"""
from __future__ import annotations

import asyncio
import gc
import threading
import time

import httpx
import pytest

import stream
from wavetest.fakes import audio_bytes

YT = "AAAAAAAAAA1"
PATH = f"/api/stream/yt/{YT}"
CDN_PATH = f"/yt/{YT}"
SIZE = 5000
DATA = audio_bytes(SIZE)


@pytest.fixture
def cdn(api, monkeypatch):
    """Трек на CDN (5000 байт) и порция прокси 1000 байт, чтобы проверить обрезку диапазонов."""
    monkeypatch.setattr(stream, "STREAM_CHUNK_BYTES", 1000)
    api.net.add_file(CDN_PATH, DATA, "audio/mp4")
    return api


# ---------------------------------------------------------------------------
# 200 / 206 / 416
# ---------------------------------------------------------------------------
def test_open_ended_range_returns_first_chunk(cdn):
    r = cdn.get(PATH, headers={"Range": "bytes=0-"})
    assert r.status_code == 206
    assert r.headers["content-range"] == "bytes 0-999/5000"
    assert r.headers["content-length"] == "1000"
    assert r.headers["accept-ranges"] == "bytes"
    assert r.headers["content-type"] == "audio/mp4"
    assert r.content == DATA[:1000]
    up = cdn.net.cdn_requests(CDN_PATH)[-1]
    assert up.headers["range"] == "bytes=0-999"               # клиенту — порция, а не весь файл
    assert up.headers["accept-encoding"] == "identity"         # сжатие сломало бы подсчёт байтов
    assert up.headers["user-agent"] == "test-agent"            # безопасные заголовки yt-dlp пробрасываются
    assert up.headers["referer"] == "https://www.youtube.com/"


def test_explicit_range(cdn):
    r = cdn.get(PATH, headers={"Range": "bytes=100-199"})
    assert (r.status_code, r.headers["content-range"], r.content) == (206, "bytes 100-199/5000", DATA[100:200])


def test_seek_near_end_open_range(cdn):
    r = cdn.get(PATH, headers={"Range": "bytes=4500-"})
    assert cdn.net.cdn_requests()[-1].headers["range"] == "bytes=4500-5499"
    assert (r.status_code, r.headers["content-range"], r.content) == (206, "bytes 4500-4999/5000", DATA[4500:])


def test_suffix_range_is_forwarded_unchanged(cdn):
    r = cdn.get(PATH, headers={"Range": "bytes=-500"})
    assert cdn.net.cdn_requests()[-1].headers["range"] == "bytes=-500"
    assert (r.status_code, r.headers["content-range"], r.content) == (206, "bytes 4500-4999/5000", DATA[-500:])


def test_no_range_returns_whole_file_with_200(cdn):
    r = cdn.get(PATH)
    assert cdn.net.cdn_requests()[-1].headers["range"] == "bytes=0-"
    assert r.status_code == 200
    assert "content-range" not in r.headers
    assert r.headers["content-length"] == str(SIZE)
    assert r.content == DATA


@pytest.mark.parametrize("raw", ["bytes=abc", "bytes=0-1,5-6", "items=0-10"])
def test_unsupported_range_is_ignored(cdn, raw):
    """RFC 9110: непонятный Range игнорируется — весь файл, 200."""
    r = cdn.get(PATH, headers={"Range": raw})
    assert r.status_code == 200 and r.content == DATA
    assert cdn.net.cdn_requests()[-1].headers["range"] == "bytes=0-"


@pytest.mark.xfail(strict=True, reason="L2: пустой диапазон 'bytes=-' пересылается источнику как есть")
def test_empty_range_is_not_forwarded(cdn):
    r = cdn.get(PATH, headers={"Range": "bytes=-"})
    assert cdn.net.cdn_requests()[-1].headers["range"] == "bytes=0-"
    assert r.status_code == 200


def test_upstream_ignores_range(cdn):
    cdn.net.support_ranges = False
    r = cdn.get(PATH, headers={"Range": "bytes=100-"})
    assert r.status_code == 200 and "content-range" not in r.headers
    assert r.content == DATA and r.headers["content-length"] == str(SIZE)


def test_unsatisfiable_range_returns_416(cdn):
    r = cdn.get(PATH, headers={"Range": "bytes=6000-"})
    assert r.status_code == 416
    assert r.headers["content-range"] == "bytes */5000"
    assert r.headers["accept-ranges"] == "bytes"
    assert all(s.closed for s in cdn.net.streams)


def test_unknown_total_size_is_proxied(cdn):
    cdn.net.unknown_total = True
    r = cdn.get(PATH, headers={"Range": "bytes=0-"})
    assert (r.status_code, r.headers["content-range"]) == (206, "bytes 0-999/*")


def test_soundcloud_default_content_type(api):
    api.net.add_file("/sc/1000000001", DATA[:100], "")
    r = api.get("/api/stream/sc/1000000001", headers={"Range": "bytes=0-"})
    assert r.status_code == 206
    assert r.headers["content-type"] in ("audio/mpeg", "")     # пустой Content-Type CDN проксируется как есть


# ---------------------------------------------------------------------------
# Валидация
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("path, status", [
    ("/api/stream/spotify/abc", 404),
    ("/api/stream/yt/short", 400),
    ("/api/stream/yt/AAAAAAAAAA1x", 400),
    ("/api/stream/sc/12ab", 400),
    ("/api/stream/sc/1234567890123456", 400),
])
def test_invalid_source_or_id(api, path, status):
    r = api.get(path)
    assert r.status_code == status
    assert api.resolver.total() == 0 and not api.net.requests


# ---------------------------------------------------------------------------
# Протухшие ссылки и повторный резолв
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("stale", [401, 403, 404, 410])
def test_stale_link_is_resolved_again(cdn, stale):
    cdn.net.queue(CDN_PATH, stale)
    r = cdn.get(PATH, headers={"Range": "bytes=0-"})
    assert r.status_code == 206 and r.content == DATA[:1000]
    assert cdn.resolver.calls[f"yt:{YT}"] == 2
    assert [q.url.params["v"] for q in cdn.net.cdn_requests()] == ["1", "2"]   # вторая попытка — новая ссылка


def test_stale_twice_gives_502(cdn):
    cdn.net.queue(CDN_PATH, 403, 403)
    r = cdn.get(PATH, headers={"Range": "bytes=0-"})
    assert r.status_code == 502
    assert cdn.resolver.calls[f"yt:{YT}"] == 2
    assert f"yt:{YT}" not in stream._cache


def test_connection_error_triggers_re_resolve(cdn):
    cdn.net.queue(CDN_PATH, httpx.ConnectError("reset"))
    r = cdn.get(PATH, headers={"Range": "bytes=0-"})
    assert r.status_code == 206
    assert cdn.resolver.calls[f"yt:{YT}"] == 2


def test_upstream_server_error_is_not_retried(cdn):
    cdn.net.queue(CDN_PATH, 500)
    r = cdn.get(PATH, headers={"Range": "bytes=0-"})
    assert r.status_code == 502
    assert "HTTP 500" in r.json()["detail"]
    assert cdn.resolver.calls[f"yt:{YT}"] == 1


def test_expired_cached_link_is_refreshed(cdn):
    stream._cache[f"yt:{YT}"] = stream.Resolved(f"https://cdn.test{CDN_PATH}?v=old", {}, time.time() - 1)
    r = cdn.get(PATH, headers={"Range": "bytes=0-"})
    assert r.status_code == 206
    assert cdn.resolver.calls[f"yt:{YT}"] == 1
    assert cdn.net.cdn_requests()[-1].url.params["v"] == "1"


def test_alive_cached_link_is_reused(cdn):
    for _ in range(3):
        assert cdn.get(PATH, headers={"Range": "bytes=0-"}).status_code == 206
    assert cdn.resolver.calls[f"yt:{YT}"] == 1
    assert len(cdn.net.cdn_requests()) == 3


@pytest.mark.parametrize("exc, status", [
    (stream.StreamUnavailable("hls"), 404),
    (RuntimeError("boom"), 502),
])
def test_resolver_failure_status(api, exc, status):
    api.resolver.fail("yt", YT, exc)
    r = api.get(PATH, headers={"Range": "bytes=0-"})
    assert r.status_code == status
    assert not api.net.requests


# ---------------------------------------------------------------------------
# Параллельные запросы
# ---------------------------------------------------------------------------
def test_parallel_requests_same_track_resolve_once(harness, monkeypatch):
    monkeypatch.setattr(stream, "STREAM_CHUNK_BYTES", 1000)
    harness.net.add_file(CDN_PATH, DATA)
    harness.resolver.delay = 0.2

    async def go(c):
        return await asyncio.gather(*(c.get(PATH, headers={"Range": f"bytes={i * 1000}-"}) for i in range(5)))

    rs = harness.run(go)
    assert [r.status_code for r in rs] == [206] * 5
    assert [r.content for r in rs] == [DATA[i * 1000:(i + 1) * 1000] for i in range(5)]
    assert harness.resolver.calls[f"yt:{YT}"] == 1
    assert not stream._inflight


def test_parallel_resolves_are_bounded(harness, monkeypatch):
    """Одновременно работает не больше MAX_PARALLEL_RESOLVES резолвов yt-dlp."""
    ids = [f"BBBBBBBBBB{i}" for i in range(8)]
    for i in ids:
        harness.net.add_file(f"/yt/{i}", DATA[:100])
    lock, state = threading.Lock(), {"active": 0, "peak": 0}
    real = harness.resolver

    def counting(source, track_id):
        with lock:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
        try:
            time.sleep(0.1)
            return real(source, track_id)
        finally:
            with lock:
                state["active"] -= 1

    monkeypatch.setattr(stream, "_extract_sync", counting)

    async def go(c):
        return await asyncio.gather(*(c.get(f"/api/stream/yt/{i}", headers={"Range": "bytes=0-"}) for i in ids))

    rs = harness.run(go)
    assert [r.status_code for r in rs] == [206] * len(ids)
    assert 1 <= state["peak"] <= stream.MAX_PARALLEL_RESOLVES
    assert real.total() == len(ids)


# ---------------------------------------------------------------------------
# Прогрев (?warm=1) и ответ из памяти
# ---------------------------------------------------------------------------
def test_warm_then_play_from_memory(harness, monkeypatch):
    monkeypatch.setattr(stream, "STREAM_CHUNK_BYTES", 1000)
    harness.net.add_file(CDN_PATH, DATA)

    async def go(c):
        first = await c.get(PATH, params={"warm": 1})
        await asyncio.gather(*list(stream._background))
        again = await c.get(PATH, params={"warm": 1})
        before = len(harness.net.cdn_requests())
        play = await c.get(PATH, headers={"Range": "bytes=200-"})
        return first, again, play, before

    first, again, play, before = harness.run(go)
    assert first.status_code == 202 and again.status_code == 204
    assert play.status_code == 206
    assert play.headers["content-range"] == "bytes 200-999/5000"   # из головы: до конца прогретой порции
    assert play.content == DATA[200:1000]
    assert len(harness.net.cdn_requests()) == before                # к источнику не ходили
    assert harness.resolver.calls[f"yt:{YT}"] == 1


def test_warm_failure_leaves_no_head(harness):
    harness.net.queue(CDN_PATH, 500)

    async def go(c):
        r = await c.get(PATH, params={"warm": 1})
        await asyncio.gather(*list(stream._background))
        return r

    assert harness.run(go).status_code == 202
    assert not stream._heads and not stream._prefetching


def test_warm_states(api):
    key = f"yt:{YT}"
    stream._prefetching.add(key)
    assert api.get(PATH, params={"warm": 1}).status_code == 202          # уже готовится
    stream._prefetching.clear()
    stream._inflight.update({"yt:x1": None, "yt:x2": None})              # сервер занят резолвами
    try:
        assert api.get(PATH, params={"warm": 1}).status_code == 503
    finally:
        stream._inflight.clear()
    assert api.resolver.total() == 0


# ---------------------------------------------------------------------------
# Обрыв со стороны клиента
# ---------------------------------------------------------------------------
def _asgi_scope(path: str, range_: str) -> dict:
    return {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"}, "http_version": "1.1",
            "method": "GET", "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": b"",
            "root_path": "", "headers": [(b"host", b"127.0.0.1"), (b"range", range_.encode())],
            "client": ("127.0.0.1", 50000), "server": ("127.0.0.1", 80)}


@pytest.mark.parametrize("blocked_in", ["upstream", "send"])
def test_client_disconnect_closes_upstream(harness, blocked_in):
    """
    Клиент (тег <audio>) оборвал запрос при перемотке. Соединение с CDN должно закрыться и в случае,
    когда прокси ждёт данные от CDN, и когда ждёт отправки клиенту. Схема как у uvicorn (ASGI spec 2.3).
    """
    # Прокси читает CDN кусками по CHUNK_SIZE (64 КБ), поэтому трек больше нескольких кусков
    harness.net.add_file(CDN_PATH, audio_bytes(4 * stream.CHUNK_SIZE + 100))
    harness.net.chunk = stream.CHUNK_SIZE
    harness.net.hang_streams = blocked_in == "upstream"

    async def go(_client):
        disconnect = asyncio.Event()
        sent: list[dict] = []
        first_sent = 0
        request_given = False

        async def receive():
            nonlocal request_given
            if not request_given:
                request_given = True
                return {"type": "http.request", "body": b"", "more_body": False}
            await disconnect.wait()
            return {"type": "http.disconnect"}

        async def send(msg):
            nonlocal first_sent
            sent.append(msg)
            if msg["type"] == "http.response.body" and msg.get("body"):
                first_sent += 1
                disconnect.set()
                if blocked_in == "send":
                    await asyncio.Event().wait()                # клиент не читает — отправка висит

        await asyncio.wait_for(harness.app(_asgi_scope(PATH, "bytes=0-"), receive, send), 5)
        for _ in range(5):                                       # дать финализатору генератора отработать
            gc.collect()
            await asyncio.sleep(0)
        return sent, first_sent

    sent, first_sent = harness.run(go)
    assert sent[0]["type"] == "http.response.start" and sent[0]["status"] == 206
    assert first_sent >= 1
    assert harness.net.streams and all(s.closed for s in harness.net.streams), \
        [(s.sent, s.closed) for s in harness.net.streams]


def test_upstream_break_mid_stream_ends_response(cdn):
    """
    CDN оборвал тело на середине. Ошибки сервера нет, соединение с CDN закрыто, а тело короче
    объявленного Content-Length — плеер видит обрыв и перезапрашивает диапазон. Байты, которые прокси
    успел получить, но ещё не отдал (читает кусками по 64 КБ), при обрыве теряются: фиксируем как есть.
    """
    cdn.net.chunk = 200
    cdn.net.fail_after = 2
    r = cdn.get(PATH, headers={"Range": "bytes=0-"})
    assert r.status_code == 206
    assert r.headers["content-length"] == "1000"
    assert len(r.content) < 1000 and DATA.startswith(r.content)
    assert all(s.closed for s in cdn.net.streams)
