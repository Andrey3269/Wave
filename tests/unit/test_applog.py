"""Маскирование журнала (backend/applog.py, S13, находка L1): сообщения, трейсбеки, ссылки и IP из googlevideo."""
from __future__ import annotations

import io
import logging
import sys
import threading

import pytest

import applog
import diagnostics

GV = ("https://rr3---sn-4g5e6nzz.googlevideo.com/videoplayback?expire=1760000000&ei=AbCd&ip=93.184.216.34"
      "&id=o-ABCDEF&itag=140&source=youtube&sig=AJfQdSswRgIhAKs&lsig=AG3C_xAwRQIgSecret&n=nToken&pot=PoTokenValue")
GV_PATH = "https://manifest.googlevideo.com/api/manifest/hls_playlist/expire/1760000000/ei/x/ip/93.184.216.34/sig/ABC/file/index.m3u8"
SECRET_BITS = ("93.184.216.34", "sn-4g5e6nzz", "AJfQdSswRgIhAKs", "AG3C_xAwRQIgSecret", "nToken", "PoTokenValue", "o-ABCDEF")


def _clean(text: str) -> None:
    for bit in SECRET_BITS:
        assert bit not in text, bit


@pytest.mark.parametrize("raw", [GV, GV_PATH, f"Ошибка запроса {GV!r}", f"GET {GV} -> 403",
                                 "ConnectError: [Errno 110] rr3---sn-4g5e6nzz.googlevideo.com:443 timed out"])
def test_googlevideo_urls_and_hosts_are_masked(raw):
    out = applog.redact(raw)
    _clean(out)
    assert "googlevideo.com" in out                      # видно, что речь о потоке YouTube


@pytest.mark.parametrize("raw, hidden, kept", [
    ("клиент 93.184.216.34 и 10.0.0.5", ["93.184.216.34", "10.0.0.5"], []),
    ("сервер на 127.0.0.1:8765 и 0.0.0.0", [], ["127.0.0.1:8765", "0.0.0.0"]),
    ("WebView2 120.0.2210.91, Python 3.12.3, yt-dlp 2025.10.22", [], ["120.0.2210.91", "3.12.3", "2025.10.22"]),
    ("адрес [2001:db8:85a3::8a2e:370:7334]:443", ["2001:db8:85a3::8a2e:370:7334"], []),
    ("loopback ::1 и время 14:27:33, деление 10:20", [], ["::1", "14:27:33", "10:20"]),
])
def test_ip_addresses(raw, hidden, kept):
    out = applog.redact(raw)
    for h in hidden:
        assert h not in out
    for k in kept:
        assert k in out


@pytest.mark.parametrize("raw, secret", [
    ("YTDLP_PROXY=http://user:pa55w0rd@proxy.example:8080", "pa55w0rd"),
    ("socks5h://alice:hunter2@127.0.0.1:1080", "hunter2"),
    ("Authorization: Bearer eyJhbGciOi.abc.def", "eyJhbGciOi"),
    ("headers={'Authorization': 'Basic dXNlcjpwYXNz'}", "dXNlcjpwYXNz"),
    ("Cookie: SID=abc123; HSID=def456; SAPISID=ghi789", "def456"),
    ("set-cookie: VISITOR_INFO1_LIVE=zzz; Path=/", "zzz"),
    ('{"access_token": "tok-123", "x": 1}', "tok-123"),
    ("client_id=Abc123XYZ", "Abc123XYZ"),
    ("https://api-v2.soundcloud.com/tracks?client_id=Abc123XYZ&limit=10", "Abc123XYZ"),
    ("https://cf-media.sndcdn.com/a.mp3?Policy=eyJTdGF0ZW1lbnQi&Signature=sigvalue&Key-Pair-Id=APKA", "sigvalue"),
])
def test_secrets_are_masked(raw, secret):
    assert secret not in applog.redact(raw)


def test_query_parameter_names_stay_for_debugging():
    out = applog.redact("https://lrclib.net/api/get?artist_name=X&track_name=Y&duration=215")
    assert out == "https://lrclib.net/api/get?artist_name=***&track_name=***&duration=***"


def test_plain_messages_are_unchanged():
    msg = "Поиск yt: 20 треков за 0.4 с; папка C:\\Users\\Имя\\Music\\Wave; ключ yt:dQw4w9WgXcQ"
    assert applog.redact(msg) == msg


def _logger(name: str):
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    handler.addFilter(applog.RedactFilter())
    log = logging.getLogger(f"wave.test.{name}")
    log.handlers[:] = [handler]
    log.propagate = False
    log.setLevel(logging.DEBUG)
    return log, stream


def test_traceback_is_masked():
    """L1: текст исключения в трейсбеке (exc_info) маскируется так же, как сообщение."""
    log, stream = _logger("tb")
    try:
        raise RuntimeError(f"HTTP 403 для {GV}")
    except RuntimeError:
        log.exception("Поток не получен %s", GV)
    out = stream.getvalue()
    assert "Traceback (most recent call last)" in out and "RuntimeError: HTTP 403" in out
    _clean(out)


def test_exc_info_tuple_and_stack_info_are_masked():
    log, stream = _logger("stack")
    err = ValueError("ip=93.184.216.34 sig=AJfQdSswRgIhAKs")
    log.error("сбой", exc_info=(type(err), err, None), stack_info=True)
    log.info("стек", stack_info=True, extra={})
    _clean(stream.getvalue())


def test_args_with_exceptions_are_masked():
    log, stream = _logger("args")
    log.warning("Провайдер не ответил: %r", OSError(f"connect to {GV}"))
    _clean(stream.getvalue())


def test_setup_puts_filter_on_file_handler(wave_profile, monkeypatch):
    """Файл журнала приложения (applog.setup, вызывает лаунчер) пишет уже замаскированный текст, включая трейсбеки."""
    root = logging.getLogger()
    saved = (root.handlers[:], root.level)
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    monkeypatch.setattr(threading, "excepthook", threading.excepthook)
    try:
        path = applog.setup()
        try:
            raise ConnectionError(f"обрыв {GV}")
        except ConnectionError:
            logging.getLogger("wave.test").exception("сбой потока %s", GV)
        for h in root.handlers:
            h.flush()
        text = path.read_text(encoding="utf-8")
    finally:
        for h in root.handlers:
            h.close()
        root.handlers[:], root.level = saved
    assert "сбой потока" in text and "ConnectionError" in text
    _clean(text)


def test_diagnostics_error_text_has_no_urls_or_ips():
    out = diagnostics._sanitize(f"ERROR: unable to download {GV} from 93.184.216.34 token=abc")
    _clean(out)
    assert "token=***" in out
