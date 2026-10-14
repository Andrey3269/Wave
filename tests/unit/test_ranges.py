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


def test_soundcloud_hls_only_resolves_as_hls(monkeypatch):
    """
    S8 (N4): раньше ответ SoundCloud только с HLS давал StreamUnavailable («нет прямого потока»). Теперь это
    поток типа hls (его играет hls.js через HLS-прокси); прогрессивный файл по-прежнему предпочтительнее.
    """
    ydl = _use_ydl(monkeypatch, {"url": "https://cf-hls-media.sndcdn.com/playlist.m3u8", "protocol": "m3u8_native"})
    res = stream._extract_sync("sc", "1000000001")
    assert ydl.urls == ["https://api.soundcloud.com/tracks/1000000001"]
    assert (res.kind, res.url) == ("hls", "https://cf-hls-media.sndcdn.com/playlist.m3u8")
    ydl.info = {"url": "https://cf-media.sndcdn.com/a.mp3", "protocol": "https"}
    res = stream._extract_sync("sc", "1000000001")
    assert (res.kind, res.url) == ("progressive", "https://cf-media.sndcdn.com/a.mp3")


def test_youtube_never_resolves_to_hls(monkeypatch):
    """YouTube играет только прогрессивный файл: HLS-ответ (если yt-dlp его выбрал) — «нет прямого потока»."""
    _use_ydl(monkeypatch, {"url": "https://manifest.googlevideo.com/x/index.m3u8", "protocol": "m3u8_native"})
    with pytest.raises(stream.StreamUnavailable):
        stream._extract_sync("yt", "AAAAAAAAAA1")


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
def _uncached(source: str, track_id: str):
    """Резолв в отдельном event loop со своим состоянием резолвера (как в одном запуске приложения)."""
    async def go():
        return await stream._resolve_uncached(stream.ResolverState(), source, track_id)
    return asyncio.run(go())


@pytest.mark.parametrize("exc, status", [
    (stream.StreamUnavailable("hls only"), 404),
    (DownloadError("ERROR: [youtube] x: Sign in to confirm your age"), 403),
    (DownloadError("ERROR: [youtube] x: Video unavailable"), 404),
    (DownloadError("ERROR: [youtube] x: Private video"), 404),
    (DownloadError("ERROR: unable to download webpage: timed out"), 503),
    (RuntimeError("yt-dlp не вернул прямой URL"), 502),
])
def test_resolver_error_mapping(monkeypatch, exc, status):
    def boom(source, track_id):
        raise exc
    monkeypatch.setattr(stream, "_extract_sync", boom)
    with pytest.raises(HTTPException) as e:
        _uncached("yt", "AAAAAAAAAA1")
    assert e.value.status_code == status
    assert "yt:AAAAAAAAAA1" not in stream._cache          # неудачный резолв не кэшируется


# ---------------------------------------------------------------------------
# Понятные тексты ошибок стрима (S2d, находка L6)
# ---------------------------------------------------------------------------
# Реальные формулировки yt-dlp (в том числе Windows: WinError/Errno 11001, 10060).
NETWORK_ERRORS = [
    "ERROR: Unable to download webpage: <urlopen error [Errno -3] Temporary failure in name resolution> (caused by URLError)",
    "ERROR: [youtube] AAAAAAAAAA1: Unable to download API page: <urlopen error [Errno 11001] getaddrinfo failed>",
    "ERROR: Unable to download webpage: <urlopen error [WinError 10060] A connection attempt failed> (caused by URLError)",
    "ERROR: Unable to download webpage: <urlopen error [Errno 101] Network is unreachable>",
    "ERROR: Unable to download webpage: <urlopen error [Errno 111] Connection refused>",
    "ERROR: Unable to download webpage: ('Connection aborted.', RemoteDisconnected('Remote end closed connection'))",
    "ERROR: Unable to download webpage: The read operation timed out",
    "ERROR: Unable to download webpage: [SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol",
]


def _detail(monkeypatch, message: str, source: str = "yt", tid: str = "AAAAAAAAAA1"):
    def boom(s, i):
        raise DownloadError(message)
    monkeypatch.setattr(stream, "_extract_sync", boom)
    with pytest.raises(HTTPException) as e:
        _uncached(source, tid)
    return e.value


@pytest.mark.parametrize("message", NETWORK_ERRORS)
def test_network_failure_is_not_blamed_on_cookies(monkeypatch, message):
    """Нет сети -> «нет связи», без упоминания cookies и yt-dlp (раньше: «проверьте yt-dlp/куки»)."""
    e = _detail(monkeypatch, message)
    assert e.status_code == 503 and e.headers[stream.ERROR_HEADER] == "network"
    assert e.detail == "Нет связи с YouTube Music. Проверьте подключение к интернету."


def test_network_message_names_soundcloud(monkeypatch):
    e = _detail(monkeypatch, NETWORK_ERRORS[0], "sc", "1000000001")
    assert e.detail == "Нет связи с SoundCloud. Проверьте подключение к интернету."


@pytest.mark.parametrize("message", [
    "ERROR: [youtube] AAAAAAAAAA1: Sign in to confirm you’re not a bot. Use --cookies-from-browser or --cookies for the authentication.",
    "ERROR: [youtube] AAAAAAAAAA1: Sign in to confirm you're not a bot. This helps protect our community.",
])
def test_cookies_are_mentioned_only_for_bot_check(monkeypatch, message):
    e = _detail(monkeypatch, message)
    assert e.status_code == 503 and e.headers[stream.ERROR_HEADER] == "bot_check"
    assert "не от бота" in e.detail and "YTDLP_COOKIES" in e.detail


def test_bot_check_with_cookies_configured_does_not_reveal_path(monkeypatch):
    monkeypatch.setattr(stream, "YTDLP_COOKIES", "/home/user/secret/cookies.txt")
    e = _detail(monkeypatch, "ERROR: [youtube] x: Sign in to confirm you're not a bot")
    assert "устарел" in e.detail and "/home/user" not in e.detail and "cookies.txt" not in e.detail


@pytest.mark.parametrize("message, status, kind", [
    ("ERROR: [youtube] x: Sign in to confirm your age. This video may be inappropriate for some users.", 403, "age_restricted"),
    ("ERROR: Unable to download webpage: HTTP Error 429: Too Many Requests", 503, "rate_limited"),
    ("ERROR: [youtube] x: Requested format is not available. Use --list-formats for a list of available formats", 502, "extractor"),
    ("ERROR: [youtube] x: nsig extraction failed: Some formats may be missing", 502, "extractor"),
    ("ERROR: [youtube] x: Video unavailable. This video has been removed by the uploader", 404, "unavailable"),
    ("ERROR: [youtube] x: Private video. Sign in if you've been granted access to this video", 404, "unavailable"),
    ("ERROR: Unable to download webpage: HTTP Error 403: Forbidden", 502, "resolve_failed"),
    ("ERROR: something new and unexpected", 502, "resolve_failed"),
])
def test_resolver_error_kinds(monkeypatch, message, status, kind):
    e = _detail(monkeypatch, message)
    assert (e.status_code, e.headers[stream.ERROR_HEADER]) == (status, kind)
    if kind not in ("bot_check",):
        assert "cookies" not in e.detail.lower() and "куки" not in e.detail.lower()


def test_proxy_failure_is_a_configuration_message_without_secrets(monkeypatch):
    monkeypatch.setattr(stream, "YTDLP_PROXY", "socks5://user:pa55@10.0.0.5:1080")
    e = _detail(monkeypatch, "ERROR: Unable to download webpage: <urlopen error Tunnel connection failed: 407 Proxy "
                             "Authentication Required> socks5://user:pa55@10.0.0.5:1080")
    assert e.status_code == 502 and e.headers[stream.ERROR_HEADER] == "proxy"
    assert "YTDLP_PROXY" in e.detail
    for secret in ("user", "pa55", "10.0.0.5", "socks5://"):
        assert secret not in e.detail


def test_proxy_words_without_configured_proxy_are_network(monkeypatch):
    e = _detail(monkeypatch, "ERROR: Unable to download webpage: <urlopen error [Errno 111] Connection refused> proxy")
    assert e.headers[stream.ERROR_HEADER] == "network"


SENSITIVE = ("https://", "googlevideo", "signature=", "sig=", "expire=", "Errno", "Traceback", "urlopen", "WinError")


@pytest.mark.parametrize("message", NETWORK_ERRORS + [
    "ERROR: Unable to download webpage: HTTP Error 403: Forbidden https://rr1---sn.googlevideo.com/videoplayback?expire=1&sig=SECRET",
    "ERROR: [youtube] x: Requested format is not available https://www.youtube.com/watch?v=x&signature=ABC",
    "Traceback (most recent call last): ... ERROR: unexpected",
])
def test_user_message_never_contains_technical_details(monkeypatch, message):
    detail = _detail(monkeypatch, message).detail
    assert not [s for s in SENSITIVE if s in detail], detail


def test_internal_error_hides_exception_text(monkeypatch):
    def boom(s, i):
        raise RuntimeError("secret token=abc at https://example.test/x?sig=1")
    monkeypatch.setattr(stream, "_extract_sync", boom)
    with pytest.raises(HTTPException) as e:
        _uncached("yt", "AAAAAAAAAA1")
    assert e.value.detail == "Внутренняя ошибка резолвера" and e.value.headers[stream.ERROR_HEADER] == "internal"


def test_successful_resolve_is_cached(monkeypatch):
    monkeypatch.setattr(stream, "_extract_sync", lambda s, i: stream.Resolved("https://cdn.test/x", {}, time.time() + 60))
    res = _uncached("yt", "AAAAAAAAAA1")
    assert stream._cache["yt:AAAAAAAAAA1"] is res


# ---------------------------------------------------------------------------
# S8: разбор Range без регулярных «дыр» (L2)
# ---------------------------------------------------------------------------
from streaming import ranges  # noqa: E402


@pytest.mark.parametrize("raw, expected", [
    ("bytes=0-", ranges.ByteRange(0, None)),
    ("bytes=10-20", ranges.ByteRange(10, 20)),
    ("bytes=7-7", ranges.ByteRange(7, 7)),
    ("bytes=-500", ranges.ByteRange(None, None, suffix=500)),
])
def test_parse_single_valid(raw, expected):
    assert ranges.parse_single(raw) == expected


@pytest.mark.parametrize("raw", ["", "bytes=-", "bytes=20-10", "bytes=abc", "bytes=0-1,5-6", "items=0-1",
                                 "bytes 0-1", " bytes=0-", "bytes=1.5-2", "bytes=--1"])
def test_parse_single_ignores_invalid(raw):
    assert ranges.parse_single(raw) is None


@pytest.mark.parametrize("raw, header", [("bytes=0-", "bytes=0-"), ("bytes=5-9", "bytes=5-9"), ("bytes=-3", "bytes=-3")])
def test_byte_range_header_roundtrip(raw, header):
    assert ranges.parse_single(raw).header() == header


@pytest.mark.parametrize("raw, ok", [
    ("bytes=0-9", True), ("bytes=0-9,20-29", True), ("bytes=-5", True), ("bytes=99999-", True), ("BYTES=0-1", True),
    ("bytes= 0-9 , 20-29", True),
    ("bytes=-", False), ("bytes=abc", False), ("bytes=9-0", False), ("bytes=0-9,abc", False), ("items=0-1", False),
    ("bytes=", False), ("", False), ("bytes=0-9,", False),
])
def test_is_valid_for_file(raw, ok):
    assert ranges.is_valid_for_file(raw) is ok


def test_cap_keeps_suffix_and_limits_start():
    assert ranges.cap("bytes=-10", 100) == "bytes=-10"
    assert ranges.cap("bytes=50-", 100) == "bytes=50-149"
    assert ranges.cap("bytes=50-60", 100) == "bytes=50-60"
