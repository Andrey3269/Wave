"""
Подделки внешнего мира для тестов Wave. Сети нет нигде:

  FakeNet        — httpx.MockTransport вместо общего httpx-клиента приложения: CDN с поддержкой
                   Range (https://cdn.test/...), LRCLIB (lrclib.net), хосты обложек. Запрос к любому
                   другому хосту записывается в `unexpected` и роняет тест.
  FakeResolver   — замена stream._extract_sync (yt-dlp): выдаёт ссылки на cdn.test, считает вызовы.
  FakeYTMusic    — замена ytmusicapi.YTMusic: ответы из фикстур, запись вызовов, сбои по запросу.
  FakeSoundCloud — замена soundcloud.search_tracks / soundcloud.api_get.
  ApiHarness     — собирает всё вместе и запускает приложение в TestClient или в asyncio.run.
"""
from __future__ import annotations

import asyncio
import json
import re
import threading
import time
from collections import defaultdict, deque
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable

import httpx

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
CDN_HOST = "cdn.test"
COVER_HOSTS = ("googleusercontent.com", "ytimg.com", "ggpht.com", "sndcdn.com")


def load_fixture(rel: str) -> Any:
    """JSON из tests/fixtures (например "ytmusic/search_songs.json"). Каждый вызов — свежая копия."""
    return json.loads((FIXTURES / rel).read_text(encoding="utf-8"))


def audio_bytes(n: int, seed: int = 0) -> bytes:
    """Детерминированные «аудио»-байты: каждая позиция различима, поэтому срезы легко сверять."""
    return bytes((i * 7 + seed) % 251 for i in range(n))


# ---------------------------------------------------------------------------
# Сеть
# ---------------------------------------------------------------------------
class TrackedStream(httpx.AsyncByteStream):
    """Тело ответа, которое помнит, закрыли ли его. hang=True: после чанков зависает (имитация длинного потока)."""

    def __init__(self, chunks: list[bytes], hang: bool = False, fail_after: int | None = None):
        self.chunks, self.hang, self.fail_after = chunks, hang, fail_after
        self.closed = False
        self.sent = 0

    async def __aiter__(self):
        for i, c in enumerate(self.chunks):
            if self.fail_after is not None and i >= self.fail_after:
                raise httpx.ReadError("обрыв соединения (тест)")
            self.sent += 1
            yield c
        if self.hang:
            await asyncio.Event().wait()

    async def aclose(self) -> None:
        self.closed = True


_RANGE = re.compile(r"^bytes=(\d*)-(\d*)$")


class FakeNet:
    def __init__(self):
        self.files: dict[str, tuple[bytes, str | None]] = {}   # путь на cdn.test -> (данные, Content-Type или None)
        self.queues: dict[str, deque] = defaultdict(deque)      # путь -> заготовленные ответы (по очереди)
        self.requests: list[httpx.Request] = []
        self.unexpected: list[str] = []
        self.streams: list[TrackedStream] = []
        self.support_ranges = True
        self.unknown_total = False                               # Content-Range: bytes a-b/*
        self.lyrics: tuple[int, Any] = (404, {"message": "not found"})
        self.lyrics_calls = 0
        self.images: dict[str, tuple[bytes, str]] = {}
        self.chunk = 4096                                        # размер чанков тела ответа CDN
        self.hang_streams = False                                # тело CDN зависает после данных
        self.fail_after: int | None = None                       # обрыв тела CDN после N чанков

    # ---- настройка ----
    def add_file(self, path: str, data: bytes, content_type: str | None = "audio/mp4") -> None:
        self.files[path] = (data, content_type)

    def queue(self, path: str, *responses) -> None:
        """Следующие ответы на путь: int (статус), Exception (ошибка соединения) или callable(request) -> Response."""
        self.queues[path].extend(responses)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handle), follow_redirects=True)

    def cdn_requests(self, path: str | None = None) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.host == CDN_HOST and (path is None or r.url.path == path)]

    def assert_no_unexpected(self) -> None:
        assert not self.unexpected, f"Запросы к неизвестным хостам: {self.unexpected}"

    # ---- обработка ----
    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        host = request.url.host
        if host == CDN_HOST:
            return self._cdn(request)
        if host == "lrclib.net":
            self.lyrics_calls += 1
            status, body = self.lyrics
            if isinstance(body, Exception):
                raise body
            if isinstance(body, (dict, list)):
                return httpx.Response(status, json=body)
            return httpx.Response(status, text=str(body), headers={"Content-Type": "text/html"})
        if host.endswith(COVER_HOSTS):
            url = str(request.url)
            if url in self.images:
                data, ctype = self.images[url]
                return httpx.Response(200, content=data, headers={"Content-Type": ctype})
            return httpx.Response(404)
        self.unexpected.append(str(request.url))
        raise httpx.ConnectError(f"тест: неизвестный хост {host}", request=request)

    def _stream_response(self, status: int, data: bytes, headers: dict, **kw) -> httpx.Response:
        chunks = [data[i:i + self.chunk] for i in range(0, len(data), self.chunk)] or [b""]
        kw.setdefault("hang", self.hang_streams)
        kw.setdefault("fail_after", self.fail_after)
        s = TrackedStream(chunks, **kw)
        self.streams.append(s)
        return httpx.Response(status, headers=headers, stream=s)

    def _cdn(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        q = self.queues.get(path)
        if q:
            item = q.popleft()
            if isinstance(item, Exception):
                raise item
            if callable(item):
                return item(request)
            return httpx.Response(int(item))
        return self.serve_file(request)

    def serve_file(self, request: httpx.Request) -> httpx.Response:
        """Обычный ответ CDN по файлу из `files` (с Range). Можно ставить в очередь: net.queue(path, net.serve_file, 500)."""
        path = request.url.path
        if path not in self.files:
            return httpx.Response(404)
        data, ctype = self.files[path]
        size = len(data)
        raw = request.headers.get("range")
        base = {"Accept-Ranges": "bytes"}
        if ctype is not None:                                    # None — CDN не прислал Content-Type
            base["Content-Type"] = ctype
        m = _RANGE.fullmatch(raw or "") if self.support_ranges else None
        if not m or (not m.group(1) and not m.group(2)):
            return self._stream_response(200, data, {**base, "Content-Length": str(size)})
        if m.group(1):
            start = int(m.group(1))
            end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
        else:
            start, end = max(size - int(m.group(2)), 0), size - 1
        if start >= size or end < start:
            return httpx.Response(416, headers={"Content-Range": f"bytes */{size}"})
        total = "*" if self.unknown_total else str(size)
        body = data[start:end + 1]
        return self._stream_response(206, body, {**base, "Content-Length": str(len(body)),
                                                 "Content-Range": f"bytes {start}-{end}/{total}"})


# ---------------------------------------------------------------------------
# Резолвер yt-dlp
# ---------------------------------------------------------------------------
class FakeResolver:
    """
    Подменяет stream._extract_sync(source, track_id) -> stream.Resolved. Работает в потоке пула резолвера,
    как настоящий. Ссылка: https://cdn.test/{source}/{id}?v=<номер резолва>.
    """

    def __init__(self, stream_module):
        self._stream = stream_module
        self.calls: dict[str, int] = defaultdict(int)
        self.errors: dict[str, deque] = defaultdict(deque)
        self.delay = 0.0
        self.expires_in = 3600.0
        self.headers = {"User-Agent": "test-agent", "Referer": "https://www.youtube.com/"}
        self.gate: threading.Event | None = None            # если задан, резолв ждёт события
        self.previews: set[str] = set()                      # "source:id", для которых источник отдаёт лишь фрагмент
        self._lock = threading.Lock()

    def fail(self, source: str, track_id: str, *errors: Exception) -> None:
        self.errors[f"{source}:{track_id}"].extend(errors)

    def total(self) -> int:
        return sum(self.calls.values())

    def __call__(self, source: str, track_id: str):
        key = f"{source}:{track_id}"
        with self._lock:
            self.calls[key] += 1
            n = self.calls[key]
            err = self.errors[key].popleft() if self.errors[key] else None
        if self.gate is not None:
            self.gate.wait(10)
        if self.delay:
            time.sleep(self.delay)
        if err is not None:
            raise err
        return self._stream.Resolved(url=f"https://{CDN_HOST}/{source}/{track_id}?v={n}", headers=dict(self.headers),
                                     expires_at=time.time() + self.expires_in, preview=key in self.previews)


# ---------------------------------------------------------------------------
# ytmusicapi и SoundCloud
# ---------------------------------------------------------------------------
class FakeYTMusic:
    """Минимальный двойник ytmusicapi.YTMusic. Ответы задаются атрибутами, сбои — через `fail`."""

    def __init__(self):
        self.songs: dict[str, list] = {}           # запрос -> результаты search(filter="songs"); "*" — по умолчанию
        self.search_results: dict[tuple[str, str], list] = {}   # (filter, запрос | "*") -> результаты
        self.artists: dict[str, dict] = {}
        self.artist_albums: dict[str, list] = {}
        self.albums: dict[str, dict] = {}
        self.playlists: dict[str, dict] = {}
        self.watch: dict[str, dict] = {}
        self.calls: list[tuple] = []
        self.failures: dict[str, Exception] = {}
        self.sleep: dict[str, float] = {}
        self._lock = threading.Lock()

    def fail(self, method: str, exc: Exception | None = None) -> None:
        self.failures[method] = exc or RuntimeError(f"{method}: сбой YouTube Music (тест)")

    def _enter(self, method: str, *args) -> None:
        with self._lock:
            self.calls.append((method, *args))
        if method in self.sleep:
            time.sleep(self.sleep[method])
        if method in self.failures:
            raise self.failures[method]

    def count(self, method: str) -> int:
        return sum(1 for c in self.calls if c[0] == method)

    def search(self, query, filter=None, limit=20, **kw):
        self._enter("search", query, filter, limit)
        if filter == "songs":
            items = self.songs.get(query, self.songs.get("*", []))
        else:
            items = self.search_results.get((filter, query), self.search_results.get((filter, "*"), []))
        return [dict(x) for x in items][:limit]

    def get_artist(self, channel_id):
        self._enter("get_artist", channel_id)
        return json.loads(json.dumps(self.artists[channel_id]))

    def get_artist_albums(self, browse_id, params, limit=100, **kw):
        self._enter("get_artist_albums", browse_id, params, limit)
        return [dict(x) for x in self.artist_albums.get(browse_id, [])]

    def get_album(self, browse_id):
        self._enter("get_album", browse_id)
        return json.loads(json.dumps(self.albums[browse_id]))

    def get_playlist(self, playlist_id, limit=100, **kw):
        self._enter("get_playlist", playlist_id, limit)
        pl = json.loads(json.dumps(self.playlists[playlist_id]))
        pl["tracks"] = pl.get("tracks", [])[:limit]
        return pl

    def get_watch_playlist(self, videoId=None, limit=25, **kw):
        self._enter("get_watch_playlist", videoId, limit)
        return json.loads(json.dumps(self.watch.get(videoId, {"tracks": []})))


class FakeSoundCloud:
    """Двойник soundcloud.search_tracks и soundcloud.api_get (неофициальный API v2)."""

    def __init__(self):
        self.tracks: dict[str, list] = {}          # запрос -> «сырые» треки; "*" — по умолчанию
        self.responses: dict[str, Any] = {}        # путь API -> ответ (dict) или исключение
        self.calls: list[tuple] = []
        self.search_error: Exception | None = None
        self.search_sleep = 0.0
        self._lock = threading.Lock()

    def search_tracks(self, query: str, limit: int) -> list[dict]:
        with self._lock:
            self.calls.append(("search_tracks", query, limit))
        if self.search_sleep:
            time.sleep(self.search_sleep)
        if self.search_error is not None:
            raise self.search_error
        return [dict(x) for x in self.tracks.get(query, self.tracks.get("*", []))][:limit]

    def api_get(self, path: str, query: dict | None = None):
        with self._lock:
            self.calls.append(("api_get", path, dict(query or {})))
        resp = self.responses.get(path)
        if resp is None:
            raise RuntimeError(f"SoundCloud: нет ответа для {path} (тест)")
        if isinstance(resp, Exception):
            raise resp
        if callable(resp):
            return resp(query or {})
        return json.loads(json.dumps(resp))

    def count(self, method: str) -> int:
        return sum(1 for c in self.calls if c[0] == method)


# ---------------------------------------------------------------------------
# Приложение целиком
# ---------------------------------------------------------------------------
class ApiHarness:
    """Приложение с подменёнными внешними зависимостями. Используется фикстурами `harness` и `api`."""

    BASE_URL = "http://127.0.0.1"         # TrustedHostMiddleware пропускает только 127.0.0.1 и localhost

    def __init__(self, modules: dict, profile, monkeypatch):
        self.m = modules
        self.profile = profile
        self.app = modules["main"].app
        self.ytm = FakeYTMusic()
        self.net = FakeNet()
        self.resolver = FakeResolver(modules["stream"])
        self.sc = FakeSoundCloud()
        self.lifespans = 0
        monkeypatch.setattr(modules["main"], "YTMusic", self._make_ytm)
        monkeypatch.setattr(modules["main"], "make_http_client", self.net.client)
        monkeypatch.setattr(modules["stream"], "_extract_sync", self.resolver)
        monkeypatch.setattr(modules["main"], "sc_search_raw", self.sc.search_tracks)
        monkeypatch.setattr(modules["soundcloud"], "search_tracks", self.sc.search_tracks)
        monkeypatch.setattr(modules["soundcloud"], "api_get", self.sc.api_get)
        self.http = None

    def _make_ytm(self):
        self.lifespans += 1
        return self.ytm

    @contextmanager
    def client(self):
        """TestClient с полным lifespan. Ошибки сервера возвращаются как HTTP 500 (их видно в проверках)."""
        from fastapi.testclient import TestClient
        with TestClient(self.app, base_url=self.BASE_URL, raise_server_exceptions=False) as c:
            self.http = c
            try:
                yield self
            finally:
                self.http = None
        self.net.assert_no_unexpected()

    # короткие методы
    def get(self, url, **kw):
        return self.http.get(url, **kw)

    def post(self, url, **kw):
        return self.http.post(url, **kw)

    def put(self, url, **kw):
        return self.http.put(url, **kw)

    def patch(self, url, **kw):
        return self.http.patch(url, **kw)

    def delete(self, url, **kw):
        return self.http.delete(url, **kw)

    @contextmanager
    def serve(self, timeout: float = 15.0):
        """
        Настоящий uvicorn на свободном порту 127.0.0.1 в отдельном потоке — как backend.start() в лаунчере
        (порт 0 → каждый запуск на своём порту). Даёт синхронный httpx.Client с base_url этого порта.
        Внешние зависимости подменены так же, как в client(); сеть вне loopback по-прежнему запрещена.
        """
        import socket

        import uvicorn

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(self.app, log_level="warning", lifespan="on"))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, name=f"uvicorn-{port}", daemon=True)
        thread.start()
        deadline = time.monotonic() + timeout
        while not server.started:
            if not thread.is_alive() or time.monotonic() > deadline:
                server.should_exit = True
                sock.close()
                raise RuntimeError(f"uvicorn на порту {port} не запустился")
            time.sleep(0.02)
        try:
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=10) as c:
                c.port = port
                yield c
        finally:
            server.should_exit = True
            thread.join(timeout)
            sock.close()
            self.net.assert_no_unexpected()
        if thread.is_alive():
            raise RuntimeError(f"uvicorn на порту {port} не остановился за {timeout} с")

    def run(self, fn: Callable[[httpx.AsyncClient], Any]):
        """
        Отдельный запуск приложения в asyncio.run: lifespan + httpx.ASGITransport. Позволяет слать
        параллельные запросы в одном event loop. Каждый вызов — новый event loop, как при перезапуске
        uvicorn сторожем лаунчера.
        """
        async def main():
            async with self.app.router.lifespan_context(self.app):
                transport = httpx.ASGITransport(app=self.app, raise_app_exceptions=False)
                async with httpx.AsyncClient(transport=transport, base_url=self.BASE_URL) as c:
                    return await fn(c)

        try:
            return asyncio.run(main())
        finally:
            self.net.assert_no_unexpected()


def track_payload(source: str = "yt", id_: str = "dQw4w9WgXcQ", **over) -> dict:
    """Тело Track для POST /api/favorites, /api/history, /api/playlists/{id}/tracks (как шлёт фронтенд)."""
    body = {"id": id_, "source": source, "title": f"Песня {id_}", "artist": "Артист", "artists": ["Артист"],
            "album": "Альбом", "duration": 215, "cover": "https://lh3.googleusercontent.com/x=w544-h544",
            "explicit": False, "stream_url": f"/api/stream/{source}/{id_}", "artist_id": "UCartist",
            "artist_ids": ["UCartist"], "url": None}
    body.update(over)
    return body
