"""
Защита HLS-прокси от SSRF (backend/streaming/netguard.py, S8): какие адреса можно запрашивать у источника,
проверка каждого редиректа. DNS подменён, сети нет.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

from streaming import config, netguard


@pytest.mark.parametrize("ip", ["93.184.216.34", "8.8.8.8", "2606:4700::6810:85e5", "151.101.1.1"])
def test_public_ips(ip):
    assert netguard.is_public_ip(ip)


@pytest.mark.parametrize("ip", [
    "127.0.0.1", "127.8.9.10", "10.0.0.1", "172.16.5.4", "192.168.1.1", "169.254.169.254",   # metadata облака
    "100.64.0.1", "0.0.0.0", "255.255.255.255", "224.0.0.1", "240.0.0.1", "192.0.2.1", "198.18.0.1",
    "::1", "::", "fe80::1", "fc00::1", "fd12::1", "ff02::1",
    "::ffff:127.0.0.1", "::ffff:169.254.169.254",          # IPv4-mapped
    "2002:7f00:1::1",                                       # 6to4 с 127.0.0.1 внутри
    "64:ff9b::a9fe:a9fe",                                    # NAT64 c 169.254.169.254
    "2001:0:4136:e378:8000:63bf:3fff:fdd2",                 # Teredo (клиент 192.0.2.45)
    "fe80::1%eth0", "not-an-ip",
])
def test_non_public_ips(ip):
    assert not netguard.is_public_ip(ip)


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def dns(monkeypatch):
    table: dict[str, list[str]] = {"cdn.example": ["93.184.216.34"], "two.example": ["93.184.216.34", "10.0.0.5"],
                                   "meta.example": ["169.254.169.254"], "v6.example": ["::1"]}

    async def resolve(host, port):
        if host in table:
            return table[host]
        raise OSError("nxdomain")
    monkeypatch.setattr(netguard, "resolve_host", resolve)
    return table


@pytest.mark.parametrize("url", ["https://cdn.example/a.m3u8?sig=1", "https://93.184.216.34/x", "https://cdn.example:8443/x"])
def test_allowed_urls(dns, url):
    _run(netguard.check_url(url))


@pytest.mark.parametrize("url", [
    "http://cdn.example/a.m3u8",            # только https
    "ftp://cdn.example/a", "file:///etc/passwd", "data:audio/mpeg;base64,AA",
    "https://user:pw@cdn.example/a",         # логин в адресе
    "https://127.0.0.1/a", "https://[::1]/a", "https://169.254.169.254/latest/meta-data/",
    "https://10.1.2.3:8765/api/settings",    # локальный backend / домашняя сеть
    "https://localhost/a", "https://router.local/a", "https://svc.internal/a", "https://nas.lan/a", "https://intranet/a",
    "https://two.example/a",                 # хоть один непубличный адрес — нельзя
    "https://meta.example/a", "https://v6.example/a",
    "https://nxdomain.example/a",            # не разрешается и прокси нет
    "https:///nohost", "https://[bad/a",
])
def test_blocked_urls(dns, url):
    with pytest.raises(netguard.BlockedUrl):
        _run(netguard.check_url(url))


def test_unresolvable_name_is_refused_even_with_user_proxy(dns, monkeypatch):
    """
    Замечание аудита S8: прежде при настроенном YTDLP_PROXY имя, которое локально не разрешается, пропускалось
    («разрешит прокси») — а DNS прокси мог вернуть частный адрес. Теперь такой адрес непроверяем — отказ.
    """
    monkeypatch.setattr(config, "YTDLP_PROXY", "http://127.0.0.1:3128")
    with pytest.raises(netguard.Unverifiable):
        _run(netguard.check_url("https://nxdomain.example/a"))
    with pytest.raises(netguard.BlockedUrl):
        _run(netguard.check_url("https://localhost/a"))               # локальные имена — нет и через прокси
    with pytest.raises(netguard.BlockedUrl):
        _run(netguard.check_url("https://meta.example/a"))            # разрешилось в metadata — нет
    assert _run(netguard.check_url("https://cdn.example/a")) == ["93.184.216.34"]


def test_redact_keeps_only_host():
    assert netguard.redact("https://cdn.example/p/x.m3u8?Policy=P&Signature=S") == "cdn.example"
    assert netguard.redact("not a url") == "?"


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)


def test_send_checks_every_redirect_hop(dns):
    seen = []

    def handler(request):
        seen.append(str(request.url))
        if request.url.path == "/start":
            return httpx.Response(302, headers={"Location": "/next"})
        if request.url.path == "/next":
            return httpx.Response(307, headers={"Location": "https://127.0.0.1:8765/api/settings"})
        return httpx.Response(200, text="should not be reached")

    async def go():
        async with _client(handler) as c:
            await netguard.send(c, "https://cdn.example/start", {})
    with pytest.raises(netguard.BlockedUrl):
        _run(go())
    assert seen == ["https://cdn.example/start", "https://cdn.example/next"]     # к 127.0.0.1 запроса не было


def test_send_follows_safe_redirects_and_reports_final_url(dns):
    def handler(request):
        if request.url.host == "cdn.example":
            return httpx.Response(301, headers={"Location": "https://93.184.216.34/final/p.m3u8?x=1"})
        return httpx.Response(200, text="#EXTM3U")

    async def go():
        async with _client(handler) as c:
            r = await netguard.send(c, "https://cdn.example/p.m3u8", {"User-Agent": "UA"})
            await r.aread()
            return r
    r = _run(go())
    assert r.status_code == 200 and str(r.request.url) == "https://93.184.216.34/final/p.m3u8?x=1"
    assert r.request.headers["user-agent"] == "UA"


def test_send_limits_redirect_count(dns):
    def handler(request):
        return httpx.Response(302, headers={"Location": f"/r{len(request.url.path)}"})

    async def go():
        async with _client(handler) as c:
            await netguard.send(c, "https://cdn.example/r", {})
    with pytest.raises(netguard.BlockedUrl):
        _run(go())
