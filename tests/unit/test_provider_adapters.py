"""
Адаптеры источников (backend/providers/youtube_music.py, soundcloud.py, S7):
  * перевод исключений библиотек в ProviderError (тип, источник, текст);
  * НАСТОЯЩИЕ таймауты HTTP-клиентов: локальный сервер принимает соединение и молчит — запрос ytmusicapi
    (сессия requests) и запрос yt-dlp к API SoundCloud (socket_timeout) завершаются сами, поток пула
    освобождается без помощи asyncio.wait_for (находка M2). Сеть вне loopback по-прежнему запрещена.
"""
from __future__ import annotations

import asyncio
import io
import json
import socket
import threading
import time
from types import SimpleNamespace

import pytest
import requests
from yt_dlp import YoutubeDL
from yt_dlp.networking import Response
from yt_dlp.networking.exceptions import HTTPError as YdlHTTPError, TransportError
from yt_dlp.utils import DownloadError, ExtractorError
from ytmusicapi.exceptions import YTMusicGatedError, YTMusicServerError, YTMusicUserError

from providers import soundcloud as scp, youtube_music as ytp
from providers.base import ProviderError, user_message

YT, SC = ytp.PROVIDER, scp.PROVIDER


# ---------------------------------------------------------------------------
# Классификация YouTube Music
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("exc, kind", [
    (requests.ReadTimeout("read timed out"), "timeout"),
    (requests.ConnectTimeout("connect timed out"), "timeout"),
    (socket.timeout("timed out"), "timeout"),
    (requests.ConnectionError("Name or service not known"), "unavailable"),
    (requests.exceptions.ProxyError("proxy"), "unavailable"),
    (requests.exceptions.SSLError("ssl"), "unavailable"),
    (YTMusicServerError("Server returned HTTP 429: Too Many Requests.\n"), "rate_limited"),
    (YTMusicServerError("Server returned HTTP 404: Not Found.\nRequested entity was not found."), "not_found"),
    (YTMusicServerError("Server returned HTTP 400: Bad Request.\nInvalid argument"), "not_found"),
    (YTMusicServerError("Server returned HTTP 403: Forbidden.\n"), "restricted"),
    (YTMusicServerError("Server returned HTTP 503: Service Unavailable.\n"), "unavailable"),
    (YTMusicServerError("что-то иное"), "internal"),
    (YTMusicGatedError("consent dialog"), "restricted"),
    (YTMusicUserError("Invalid album browseId provided, must start with MPRE."), "not_found"),
    (json.JSONDecodeError("Expecting value", "<html>", 0), "unavailable"),
    (KeyError("contents"), "internal"),
])
def test_youtube_errors_are_classified(exc, kind):
    err = YT.classify(exc)
    assert isinstance(err, ProviderError) and err.kind == kind and err.source == "yt"
    assert err.user_message == user_message(kind, "YouTube Music")


# ---------------------------------------------------------------------------
# Классификация SoundCloud
# ---------------------------------------------------------------------------
def http_error(status: int) -> ExtractorError:
    resp = Response(io.BytesIO(b""), "https://api-v2.soundcloud.com/search/tracks", {}, status=status)
    return ExtractorError("Unable to download JSON metadata", cause=YdlHTTPError(resp))


@pytest.mark.parametrize("exc, kind", [
    (http_error(429), "rate_limited"),
    (http_error(401), "auth_required"),
    (http_error(403), "auth_required"),
    (http_error(404), "not_found"),
    (http_error(410), "not_found"),
    (http_error(502), "unavailable"),
    (http_error(418), "internal"),
    (ExtractorError("x", cause=TransportError(msg="The read operation timed out")), "timeout"),
    (ExtractorError("x", cause=TransportError(msg="[Errno -2] Name or service not known")), "unavailable"),
    (DownloadError("ERROR: x", exc_info=None), "internal"),
    (socket.timeout("timed out"), "timeout"),
    # yt-dlp обновился и внутренний метод исчез (находка M10): ошибка адаптера, а не всего приложения
    (AttributeError("'SoundcloudIE' object has no attribute '_call_api'"), "internal"),
])
def test_soundcloud_errors_are_classified(exc, kind):
    err = SC.classify(exc)
    assert err.kind == kind and err.source == "sc" and err.user_message == user_message(kind, "SoundCloud")


def test_cause_chain_is_followed_without_looping():
    inner = http_error(429)
    outer = RuntimeError("обёртка")
    outer.__cause__ = inner
    inner.__context__ = outer                                  # петля в цепочке
    assert SC.classify(outer).kind == "rate_limited"


# ---------------------------------------------------------------------------
# Настоящие таймауты HTTP-клиентов
# ---------------------------------------------------------------------------
@pytest.fixture
def silent_server():
    """Принимает TCP-соединения на 127.0.0.1 и ничего не отвечает (имитация зависшего источника)."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(16)
    srv.settimeout(0.1)
    conns, stop = [], threading.Event()

    def accept():
        while not stop.is_set():
            try:
                c, _ = srv.accept()
                conns.append(c)
            except OSError:
                pass

    t = threading.Thread(target=accept, daemon=True)
    t.start()
    yield srv.getsockname()[1]
    stop.set()
    t.join(2)
    for c in conns:
        c.close()
    srv.close()


def test_youtube_client_uses_session_with_timeouts():
    ytm = ytp.create_client()                                  # без сети: защита тестов уронила бы тест
    assert ytm._session.request.keywords["timeout"] == (ytp.CONNECT_TIMEOUT, ytp.READ_TIMEOUT)
    assert ytp.READ_TIMEOUT < 30                               # короче значения ytmusicapi по умолчанию


def test_youtube_search_on_a_silent_server_ends_by_itself(silent_server, monkeypatch):
    """Настоящий ytmusicapi.search: запрос к молчащему серверу обрывается таймаутом сессии, а не ожиданием."""
    import ytmusicapi.ytmusic as ytm_mod

    monkeypatch.setattr(ytp, "READ_TIMEOUT", 0.3)
    monkeypatch.setattr(ytm_mod, "YTM_BASE_API", f"http://127.0.0.1:{silent_server}/youtubei/v1/")
    monkeypatch.setattr(ytm_mod, "get_visitor_id", lambda request_func: {"X-Goog-Visitor-Id": "test"})
    ytm = ytp.create_client()
    ytm._session.trust_env = False
    state = SimpleNamespace(ytm=ytm)
    YT.reset()
    try:
        t0 = time.monotonic()
        with pytest.raises(ProviderError) as e:
            asyncio.run(YT.search_tracks(state, "тест", 5, timeout=20))     # ожидание намного дольше таймаута HTTP
        took = time.monotonic() - t0
        assert e.value.kind == "timeout" and isinstance(e.value.__cause__, requests.ReadTimeout)
        assert took < 3, f"таймаут сессии не сработал: {took:.1f} с"
        assert YT.executor.stats()["outstanding"] == 0 and YT.executor.stats()["abandoned"] == 0
    finally:
        YT.reset()


def test_soundcloud_api_on_a_silent_server_ends_by_itself(silent_server, monkeypatch):
    """Настоящий yt-dlp (_call_api): запрос к молчащему API обрывается socket_timeout, поток свободен."""
    monkeypatch.setattr(scp, "SOCKET_TIMEOUT", 0.3)
    monkeypatch.setattr(scp, "API_BASE", f"http://127.0.0.1:{silent_server}/")
    opts = scp.ydl_options()
    assert opts["socket_timeout"] == 0.3
    ydl = YoutubeDL(opts)
    ie = ydl.get_info_extractor("Soundcloud")
    ie._CLIENT_ID = "test-client-id"                           # без initialize(): ни одного запроса к SoundCloud
    monkeypatch.setattr(scp, "_get_ie", lambda: ie)
    SC.reset()
    try:
        t0 = time.monotonic()
        with pytest.raises(ProviderError) as e:
            asyncio.run(SC.search_cards(None, "artists", "тест", 5, 0, timeout=20))
        took = time.monotonic() - t0
        assert e.value.kind == "timeout" and e.value.source == "sc"
        assert took < 3, f"socket_timeout не сработал: {took:.1f} с"
        assert SC.executor.stats()["outstanding"] == 0
        assert SC.breaker.is_open()                            # таймаут переводит SoundCloud в cooldown
    finally:
        ydl.close()
        SC.reset()


def test_soundcloud_threads_have_their_own_client(monkeypatch):
    """Вместо одного экземпляра под глобальной блокировкой — свой экземпляр в каждом потоке."""
    made = []

    class FakeIE:
        def initialize(self):
            pass

    class FakeYDL:
        def __init__(self, opts):
            made.append(threading.current_thread().name)

        def get_info_extractor(self, name):
            assert name == "Soundcloud"
            return FakeIE()

        def close(self):
            pass

    monkeypatch.setattr(scp, "YoutubeDL", FakeYDL)
    seen = {}
    barrier = threading.Barrier(2)

    def work(n):
        barrier.wait(2)
        seen[n] = scp._get_ie()
        assert scp._get_ie() is seen[n]                       # в своём потоке — тот же экземпляр
        scp._drop_ie()

    threads = [threading.Thread(target=work, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(made) == 2 and seen[0] is not seen[1]
