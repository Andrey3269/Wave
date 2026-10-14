"""
Закрепление проверенного IP в момент соединения (S8, замечание аудита к streaming/netguard.py).

Клиент HLS (netguard.make_client / GuardedTransport) сам разрешает имя ЛОКАЛЬНО, проверяет все адреса и
соединяется только с проверенным IP: напрямую или через прокси пользователя командой CONNECT к IP-литералу.
Здесь — настоящие сокеты на 127.0.0.1: поддельный HTTP-прокси, у которого «свой DNS» отличается от локального
(для имени он ответил бы частным адресом), и простой сервер для прямого режима. Записывается, что реально
дошло до прокси/сервера: цель CONNECT, заголовки, SNI из TLS ClientHello. Внешней сети нет: локальный DNS
подменён (netguard.resolve_host), «публичный» адрес из него за прокси никуда не соединяется.
"""
from __future__ import annotations

import asyncio
import base64
import struct

import httpcore
import httpx
import pytest

from streaming import netguard

PUBLIC = "93.184.216.34"


class FakeDNS:
    """Локальный DNS: имя -> адреса; ответы можно задать очередью (для rebinding)."""

    def __init__(self, table):
        self.table = {k: list(v) if isinstance(v[0], str) else v for k, v in table.items()}
        self.calls: list[str] = []

    async def __call__(self, host, port):
        self.calls.append(host)
        answer = self.table.get(host)
        if answer is None:
            raise OSError("nxdomain")
        if isinstance(answer[0], list):                       # очередь разных ответов
            return answer.pop(0) if len(answer) > 1 else answer[0]
        return answer


def _sni(client_hello: bytes) -> str | None:
    """Имя из расширения server_name в TLS ClientHello (RFC 8446 §4.1.2, RFC 6066 §3)."""
    try:
        if client_hello[0] != 0x16 or client_hello[5] != 0x01:
            return None
        p = 5 + 4 + 2 + 32                                    # record, handshake header, version, random
        p += 1 + client_hello[p]                              # session id
        p += 2 + struct.unpack(">H", client_hello[p:p + 2])[0]   # cipher suites
        p += 1 + client_hello[p]                              # compression
        end = p + 2 + struct.unpack(">H", client_hello[p:p + 2])[0]
        p += 2
        while p < end:
            ext, size = struct.unpack(">HH", client_hello[p:p + 4])
            if ext == 0:
                name_len = struct.unpack(">H", client_hello[p + 7:p + 9])[0]
                return client_hello[p + 9:p + 9 + name_len].decode("ascii")
            p += 4 + size
    except (IndexError, struct.error):
        return None
    return None


class FakeProxy:
    """
    HTTP-прокси на 127.0.0.1. «Свой DNS» прокси (proxy_dns) отличается от локального: если бы клиент прислал
    CONNECT по имени, прокси соединился бы с частным адресом — это записывается в resolved_by_proxy.
    reply=200: после CONNECT читает TLS ClientHello и записывает SNI, затем закрывает соединение.
    """

    def __init__(self, reply: int = 403, proxy_dns: dict | None = None):
        self.reply = reply
        self.proxy_dns = proxy_dns or {"cdn.example": "10.0.0.5"}
        self.connections = 0
        self.requests: list[str] = []          # строки запроса CONNECT
        self.headers: list[dict] = []
        self.resolved_by_proxy: list[str] = []
        self.sni: list[str | None] = []

    async def handle(self, reader, writer):
        self.connections += 1
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            lines = head.decode("latin-1").split("\r\n")
            self.requests.append(lines[0])
            self.headers.append({k.lower(): v.strip() for k, _, v in (ln.partition(":") for ln in lines[1:] if ln)})
            target = lines[0].split(" ")[1].rsplit(":", 1)[0].strip("[]")
            if target in self.proxy_dns:                       # имя пришло в прокси: он разрешил бы его сам
                self.resolved_by_proxy.append(self.proxy_dns[target])
            if self.reply != 200:
                writer.write(f"HTTP/1.1 {self.reply} Denied\r\nContent-Length: 0\r\n\r\n".encode())
                await writer.drain()
                return
            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await writer.drain()
            hello = await asyncio.wait_for(reader.read(4096), 5)
            self.sni.append(_sni(hello))
        except (asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionError):
            pass
        finally:
            writer.close()


class SniServer:
    """Прямой режим: «сервер» на 127.0.0.1 записывает соединение и SNI из ClientHello, затем закрывает его."""

    def __init__(self):
        self.connections = 0
        self.sni: list[str | None] = []

    async def handle(self, reader, writer):
        self.connections += 1
        try:
            self.sni.append(_sni(await asyncio.wait_for(reader.read(4096), 5)))
        except (asyncio.TimeoutError, ConnectionError):
            pass
        finally:
            writer.close()


async def _serve(handler):
    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


def _client(proxy_url: str | None) -> httpx.AsyncClient:
    # bind_ipv4=False: привязка к 0.0.0.0 для защиты тестов выглядит как внешний сетевой вызов; фильтр
    # «только IPv4» в PinnedBackend от неё не зависит и проверяется отдельно
    return httpx.AsyncClient(transport=netguard.GuardedTransport(proxy_url, bind_ipv4=False),
                             timeout=httpx.Timeout(5.0), follow_redirects=False, trust_env=False)


@pytest.fixture
def dns(monkeypatch):
    fake = FakeDNS({"cdn.example": [PUBLIC], "private.example": ["10.0.0.7"], "meta.example": ["169.254.169.254"],
                    "v6only.example": ["2606:4700::6810:85e5"], "loop.example": ["127.0.0.1"]})
    monkeypatch.setattr(netguard, "resolve_host", fake)
    return fake


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Через прокси пользователя
# ---------------------------------------------------------------------------
def test_proxy_receives_only_the_locally_verified_ip_never_the_hostname(dns):
    """
    Прокси с ОТДЕЛЬНЫМ DNS-результатом (для cdn.example его DNS вернул бы 10.0.0.5): клиент присылает CONNECT
    к IP, проверенному локальным DNS, — прокси имя не получает и разрешать ему нечего.
    """
    proxy = FakeProxy(reply=403)

    async def go():
        server, port = await _serve(proxy.handle)
        async with server, _client(f"http://127.0.0.1:{port}") as c:
            with pytest.raises(httpx.ProxyError):
                await netguard.send(c, "https://cdn.example/seg0.mp3?Signature=S", {})
    _run(go())
    assert proxy.requests == [f"CONNECT {PUBLIC}:443 HTTP/1.1"]
    assert proxy.headers[0]["host"] == f"{PUBLIC}:443"
    assert proxy.resolved_by_proxy == []                      # частный адрес из DNS прокси не использовался
    assert "cdn.example" not in repr(proxy.requests + proxy.headers)


def test_tls_through_proxy_is_verified_for_the_hostname(dns):
    """После CONNECT к IP рукопожатие TLS идёт с SNI = имя хоста: сертификат проверяется по имени, не по IP."""
    proxy = FakeProxy(reply=200)

    async def go():
        server, port = await _serve(proxy.handle)
        async with server, _client(f"http://127.0.0.1:{port}") as c:
            with pytest.raises(httpx.HTTPError):               # поддельный прокси обрывает рукопожатие
                await netguard.send(c, "https://cdn.example/seg0.mp3", {})
    _run(go())
    assert proxy.requests == [f"CONNECT {PUBLIC}:443 HTTP/1.1"] and proxy.sni == ["cdn.example"]


def test_proxy_credentials_are_sent_to_the_proxy(dns):
    proxy = FakeProxy(reply=407)

    async def go():
        server, port = await _serve(proxy.handle)
        async with server, _client(f"http://user%40x:p%3Ass@127.0.0.1:{port}") as c:
            with pytest.raises(httpx.ProxyError):
                await netguard.send(c, "https://cdn.example/a", {})
    _run(go())
    assert proxy.headers[0]["proxy-authorization"] == "Basic " + base64.b64encode(b"user@x:p:ss").decode()


@pytest.mark.parametrize("host, error", [("private.example", netguard.BlockedUrl), ("meta.example", netguard.BlockedUrl),
                                         ("loop.example", netguard.BlockedUrl), ("nxdomain.example", netguard.Unverifiable)])
def test_private_or_unresolvable_name_never_reaches_the_proxy(dns, host, error):
    """Домен разрешается локально в частный адрес (или не разрешается) — отказ ДО соединения с прокси."""
    proxy = FakeProxy(reply=200)

    async def go():
        server, port = await _serve(proxy.handle)
        async with server, _client(f"http://127.0.0.1:{port}") as c:
            with pytest.raises(error):
                await c.get(f"https://{host}/a")               # и без предварительной проверки send()
            with pytest.raises(error):
                await netguard.send(c, f"https://{host}/a", {})
    _run(go())
    assert proxy.connections == 0


@pytest.mark.parametrize("proxy_url", ["https://127.0.0.1:8443", "socks5://127.0.0.1:1080", "socks5h://127.0.0.1:1",
                                       "http://", "ftp://127.0.0.1:21"])
def test_proxy_types_without_ip_tunnel_are_refused(dns, proxy_url):
    """Через прокси, к которому туннель на IP-литерал не построить, адрес проверить нельзя — отказ, а не «можно»."""
    with pytest.raises(netguard.Unverifiable):
        netguard.proxy_route(proxy_url)

    async def go():
        async with _client(proxy_url) as c:
            with pytest.raises(netguard.Unverifiable):
                await c.get("https://cdn.example/a")
    _run(go())


def test_proxy_route_parsing():
    assert netguard.proxy_route(None) is None
    r = netguard.proxy_route("http://proxy.lan:3128")
    assert (r.host, r.port, r.auth) == ("proxy.lan", 3128, None)
    assert netguard.proxy_route("http://10.0.0.1").port == 80


@pytest.mark.parametrize("reply", [b"HTTP/1.1 502 Bad Gateway\r\n\r\n", b"SSH-2.0-OpenSSH\r\n\r\n",
                                   b"HTTP/1.1 200 OK\r\n\r\nEXTRA", b"HTTP/1.1 200 OK\r\n" + b"X: y\r\n" * 2000])
def test_bad_proxy_replies_fail_the_connection(dns, reply):
    async def handle(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(reply)
        await writer.drain()
        writer.close()

    async def go():
        server, port = await _serve(handle)
        async with server, _client(f"http://127.0.0.1:{port}") as c:
            with pytest.raises(httpx.ProxyError):
                await c.get("https://cdn.example/a")
    _run(go())


# ---------------------------------------------------------------------------
# Прямой режим (без прокси). Чтобы было куда соединяться, 127.0.0.1 разрешён ТОЛЬКО в этих тестах.
# ---------------------------------------------------------------------------
@pytest.fixture
def allow_loopback(monkeypatch):
    real = netguard.is_public_ip
    monkeypatch.setattr(netguard, "is_public_ip", lambda ip: ip == "127.0.0.1" or real(ip))


def _direct(port: int):
    """Клиент без прокси, но к порту тестового сервера (порт берётся из адреса запроса)."""
    return _client(None), f"https://loop.example:{port}/seg0.mp3"


def test_direct_connection_goes_to_the_verified_ip_with_hostname_sni(dns, allow_loopback):
    srv = SniServer()

    async def go():
        server, port = await _serve(srv.handle)
        client, url = _direct(port)
        async with server, client:
            with pytest.raises(httpx.HTTPError):
                await netguard.send(client, url, {})
    _run(go())
    assert srv.connections == 1 and srv.sni == ["loop.example"]
    assert dns.calls.count("loop.example") == 2               # проверка до отправки и в момент соединения


def test_dns_rebinding_between_check_and_connect_is_blocked(dns, allow_loopback):
    """Первый ответ DNS — разрешённый адрес (проверка проходит), второй — частный: соединения нет."""
    srv = SniServer()
    dns.table["loop.example"] = [["127.0.0.1"], ["10.0.0.9"]]

    async def go():
        server, port = await _serve(srv.handle)
        client, url = _direct(port)
        async with server, client:
            with pytest.raises(netguard.BlockedUrl):
                await netguard.send(client, url, {})
    _run(go())
    assert srv.connections == 0


def test_loopback_target_is_blocked_without_test_override(dns):
    srv = SniServer()

    async def go():
        server, port = await _serve(srv.handle)
        client, url = _direct(port)
        async with server, client:
            with pytest.raises(netguard.BlockedUrl):
                await client.get(url)
    _run(go())
    assert srv.connections == 0


def test_direct_mode_uses_ipv4_only(dns):
    """Без прокси Wave ходит только по IPv4 (как основной клиент): у имени только IPv6 — ошибка соединения."""
    async def go():
        async with _client(None) as c:
            with pytest.raises(httpx.ConnectError):
                await c.get("https://v6only.example/a")
    _run(go())


def test_guarded_client_wiring():
    c = netguard.make_client()
    try:
        pool = c._transport._pool
        assert isinstance(pool, httpcore.AsyncConnectionPool) and isinstance(pool._network_backend, netguard.PinnedBackend)
        assert pool._local_address == "0.0.0.0" and pool._retries == 0
        assert c.follow_redirects is False and c.trust_env is False
    finally:
        _run(c.aclose())
