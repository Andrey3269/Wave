"""
Чистые функции стриминга (backend/stream.py): разбор Range, обрезка до порции, ответ из «головы»,
срок жизни ссылки, извлечение потока из ответа yt-dlp и перевод ошибок резолвера в HTTP.
"""
from __future__ import annotations

import asyncio
import time

import pytest
from fastapi import HTTPException
from yt_dlp.utils import DownloadError

import stream

N = stream.STREAM_CHUNK_BYTES


# ---------------------------------------------------------------------------
# Range
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("raw, groups", [
    ("bytes=0-", ("0", "")),
    ("bytes=10-20", ("10", "20")),
    ("bytes=-500", ("", "500")),
    ("bytes=-", ("", "")),          # формально совпадает с шаблоном (см. L2 в test_stream_proxy)
])
def test_range_regex_accepts(raw, groups):
    assert stream.RANGE_RE.fullmatch(raw).groups() == groups


@pytest.mark.parametrize("raw", ["", "bytes=abc", "bytes=0-1,5-6", "items=0-1", "bytes 0-1", "bytes=1.5-2", " bytes=0-"])
def test_range_regex_rejects(raw):
    assert stream.RANGE_RE.fullmatch(raw) is None


@pytest.mark.parametrize("raw, expected", [
    ("bytes=0-", f"bytes=0-{N - 1}"),                       # открытый конец -> одна порция
    (f"bytes={N}-", f"bytes={N}-{2 * N - 1}"),
    ("bytes=10-20", "bytes=10-20"),                          # явный конец внутри порции — как есть
    (f"bytes=0-{10 * N}", f"bytes=0-{N - 1}"),               # явный конец дальше порции — обрезается
    ("bytes=-500", "bytes=-500"),                            # суффикс не трогаем
    ("bytes=-", "bytes=-"),                                  # пустой диапазон уходит как есть (L2)
    ("garbage", "garbage"),
])
def test_cap_range(raw, expected):
    assert stream._cap_range(raw) == expected


def test_cap_range_respects_configured_chunk(monkeypatch):
    monkeypatch.setattr(stream, "STREAM_CHUNK_BYTES", 1000)
    assert stream._cap_range("bytes=4500-") == "bytes=4500-5499"


# ---------------------------------------------------------------------------
# Ответ из прогретой «головы»
# ---------------------------------------------------------------------------
def _head(n=1000, total="5000"):
    return stream.Head(data=bytes(range(256)) * (n // 256) + bytes(range(n % 256)), total=total,
                       content_type="audio/mp4", stored_at=time.time())


def test_serve_from_head_open_end():
    head = _head()
    r = stream._serve_from_head(head, "bytes=0-")
    assert r.status_code == 206
    assert r.headers["content-range"] == "bytes 0-999/5000"
    assert r.body == head.data
    assert r.headers["accept-ranges"] == "bytes"


def test_serve_from_head_inner_range_and_clamp():
    head = _head()
    r = stream._serve_from_head(head, "bytes=100-199")
    assert (r.headers["content-range"], r.body) == ("bytes 100-199/5000", head.data[100:200])
    r = stream._serve_from_head(head, "bytes=900-4000")       # конец за пределами головы -> до конца головы
    assert (r.headers["content-range"], r.body) == ("bytes 900-999/5000", head.data[900:])


@pytest.mark.parametrize("raw", ["bytes=1000-", "bytes=5000-", "bytes=-100", "bytes=-", "bytes=500-100", "junk"])
def test_serve_from_head_declines(raw):
    """Начало вне головы, суффикс, перевёрнутый и битый диапазон — идём к источнику."""
    assert stream._serve_from_head(_head(), raw) is None


def test_serve_from_head_unknown_total():
    r = stream._serve_from_head(_head(total="*"), "bytes=0-9")
    assert r.headers["content-range"] == "bytes 0-9/*"


def test_head_cache_lru_and_ttl(monkeypatch):
    for i in range(stream.HEAD_CACHE_ITEMS + 2):
        stream._head_put(f"yt:{i}", _head())
    assert len(stream._heads) == stream.HEAD_CACHE_ITEMS
    assert stream._head_get("yt:0") is None and stream._head_get("yt:1") is None   # вытеснены старейшие
    old = _head()
    old.stored_at = time.time() - stream.HEAD_TTL - 1
    stream._head_put("yt:old", old)
    assert stream._head_get("yt:old") is None and "yt:old" not in stream._heads


# ---------------------------------------------------------------------------
# Срок жизни ссылки
# ---------------------------------------------------------------------------
def test_url_expiry_from_expire_param():
    assert stream._url_expiry("https://rr1.googlevideo.com/videoplayback?expire=2000000000&x=1") == 2000000000 - stream.EXPIRY_MARGIN


@pytest.mark.parametrize("url", ["https://cdn.example/a.mp3", "https://x/videoplayback?expire=soon"])
def test_url_expiry_default_ttl(url):
    t0 = time.time()
    assert t0 + stream.DEFAULT_TTL - 1 <= stream._url_expiry(url) <= time.time() + stream.DEFAULT_TTL + 1


def test_resolved_alive():
    assert stream.Resolved("u", {}, time.time() + 10).alive()
    assert not stream.Resolved("u", {}, time.time() - 1).alive()


# ---------------------------------------------------------------------------
# Извлечение потока из ответа yt-dlp
# ---------------------------------------------------------------------------
class _FakeYDL:
    def __init__(self, info):
        self.info, self.urls = info, []

    def extract_info(self, url, download):
        assert download is False
        self.urls.append(url)
        return self.info


def _use_ydl(monkeypatch, info) -> _FakeYDL:
    ydl = _FakeYDL(info)
    monkeypatch.setattr(stream, "_get_ydl", lambda source: ydl)
    return ydl


def test_extract_keeps_only_safe_headers(monkeypatch):
    ydl = _use_ydl(monkeypatch, {
        "url": "https://rr1.googlevideo.com/videoplayback?expire=2000000000",
        "http_headers": {"User-Agent": "UA", "Referer": "https://www.youtube.com/", "Cookie": "SID=secret",
                         "Authorization": "Bearer x", "X-Custom": "1", "Accept-Language": "ru"},
    })
    res = stream._extract_sync("yt", "AAAAAAAAAA1")
    assert ydl.urls == ["https://www.youtube.com/watch?v=AAAAAAAAAA1"]
    assert res.url.startswith("https://rr1.googlevideo.com/")
    assert res.headers == {"User-Agent": "UA", "Referer": "https://www.youtube.com/", "Accept-Language": "ru"}
    assert res.expires_at == 2000000000 - stream.EXPIRY_MARGIN


def test_extract_prefers_requested_formats(monkeypatch):
    _use_ydl(monkeypatch, {"url": "https://top/", "requested_formats": [{"url": "https://first/", "http_headers": {"Accept": "*/*"}},
                                                                        {"url": "https://second/"}]})
    res = stream._extract_sync("yt", "AAAAAAAAAA1")
    assert res.url == "https://first/" and res.headers == {"Accept": "*/*"}


def test_extract_without_url_fails(monkeypatch):
    _use_ydl(monkeypatch, {"title": "no url"})
    with pytest.raises(RuntimeError):
        stream._extract_sync("yt", "AAAAAAAAAA1")


def test_soundcloud_requires_progressive_http(monkeypatch):
    ydl = _use_ydl(monkeypatch, {"url": "https://cf-hls-media.sndcdn.com/playlist.m3u8", "protocol": "m3u8_native"})
    with pytest.raises(stream.StreamUnavailable):
        stream._extract_sync("sc", "1000000001")
    assert ydl.urls == ["https://api.soundcloud.com/tracks/1000000001"]
    ydl.info = {"url": "https://cf-media.sndcdn.com/a.mp3", "protocol": "https"}
    assert stream._extract_sync("sc", "1000000001").url == "https://cf-media.sndcdn.com/a.mp3"


def test_ydl_options(monkeypatch):
    opts = stream.ydl_opts("bestaudio")
    assert opts["format"] == "bestaudio" and opts["skip_download"] is True and opts["noplaylist"] is True
    assert opts["socket_timeout"] == 7 and opts["retries"] == 3
    assert opts["source_address"] == "0.0.0.0" and "proxy" not in opts and "cookiefile" not in opts
    monkeypatch.setattr(stream, "YTDLP_PROXY", "socks5://127.0.0.1:1080")
    monkeypatch.setattr(stream, "YTDLP_COOKIES", "/tmp/cookies.txt")
    opts = stream.ydl_opts()
    assert opts["proxy"] == "socks5://127.0.0.1:1080" and "source_address" not in opts
    assert opts["cookiefile"] == "/tmp/cookies.txt"


@pytest.mark.parametrize("source, good, bad", [
    ("yt", ["AAAAAAAAAA1", "a-b_c-d_e-f"], ["AAAAAAAAAA", "AAAAAAAAAA12", "AAAAAAAAA/1", "../../etc/x"]),
    ("sc", ["1", "123456789012345"], ["", "1234567890123456", "12a", "-1"]),
])
def test_track_id_patterns(source, good, bad):
    rx = stream.SOURCES[source].id_re
    assert all(rx.fullmatch(x) for x in good)
    assert not any(rx.fullmatch(x) for x in bad)


# ---------------------------------------------------------------------------
# Ошибки резолвера -> HTTP
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("exc, status", [
    (stream.StreamUnavailable("hls only"), 404),
    (DownloadError("ERROR: [youtube] x: Sign in to confirm your age"), 403),
    (DownloadError("ERROR: [youtube] x: Video unavailable"), 404),
    (DownloadError("ERROR: [youtube] x: Private video"), 404),
    (DownloadError("ERROR: unable to download webpage: timed out"), 502),
    (RuntimeError("yt-dlp не вернул прямой URL"), 502),
])
def test_resolver_error_mapping(monkeypatch, exc, status):
    def boom(source, track_id):
        raise exc
    monkeypatch.setattr(stream, "_extract_sync", boom)
    with pytest.raises(HTTPException) as e:
        asyncio.run(stream._resolve_uncached("yt", "AAAAAAAAAA1"))
    assert e.value.status_code == status
    assert "yt:AAAAAAAAAA1" not in stream._cache          # неудачный резолв не кэшируется


def test_network_failure_message_is_misleading(monkeypatch):
    """L6: при отсутствии сети пользователь видит «проверьте yt-dlp/куки» (фиксируем текущий текст до S2d)."""
    def boom(source, track_id):
        raise DownloadError("ERROR: Unable to download webpage: <urlopen error [Errno -3] Temporary failure in name resolution>")
    monkeypatch.setattr(stream, "_extract_sync", boom)
    with pytest.raises(HTTPException) as e:
        asyncio.run(stream._resolve_uncached("yt", "AAAAAAAAAA1"))
    assert e.value.status_code == 502
    assert "yt-dlp/куки" in e.value.detail


def test_successful_resolve_is_cached(monkeypatch):
    monkeypatch.setattr(stream, "_extract_sync", lambda s, i: stream.Resolved("https://cdn.test/x", {}, time.time() + 60))
    res = asyncio.run(stream._resolve_uncached("yt", "AAAAAAAAAA1"))
    assert stream._cache["yt:AAAAAAAAAA1"] is res
