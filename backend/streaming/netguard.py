"""
Защита HLS-прокси от SSRF (S8).

Адреса плейлистов и сегментов приходят из ответа источника, а не из кода Wave, поэтому backend не должен
слепо ходить по ним: иначе подменённый или испорченный плейлист превратил бы локальный сервер Wave в
прокси к localhost, домашней сети или метаданным облачной ВМ. Перед КАЖДЫМ запросом к источнику и после
каждого редиректа проверяется:
  * схема только https, есть имя хоста, нет логина/пароля в адресе;
  * все IP-адреса, в которые разрешается имя, публичные: не loopback, не частные, не link-local
    (в т.ч. 169.254.169.254 — метаданные облака), не CGNAT 100.64/10, не multicast/reserved/unspecified;
    для IPv6 проверяются и встроенные IPv4 (::ffff:a.b.c.d, 6to4, Teredo, NAT64 64:ff9b::/96).
Редиректы клиент сам не выполняет (follow_redirects=False): каждый Location проверяется заново.

Остаточный риск: между проверкой DNS и соединением имя теоретически может разрешиться иначе (DNS
rebinding). При прокси YTDLP_PROXY соединение идёт через прокси пользователя; если имя локально не
разрешается, проверяется только то, что это обычное публичное имя (не localhost/.local/.internal/...).
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from urllib.parse import urljoin, urlsplit

import httpx

from streaming import config

log = logging.getLogger("musicmeta.stream.hls")

MAX_REDIRECTS = 5
DNS_TIMEOUT = 5.0
REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_LOCAL_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home.arpa", ".intranet", ".corp", ".home")


class BlockedUrl(Exception):
    """Адрес источника недопустим (схема, хост или IP)."""


def redact(url: str) -> str:
    """Для журнала: только хост. Путь и query у CDN часто содержат подписи и токены."""
    try:
        return urlsplit(url).hostname or "?"
    except ValueError:
        return "?"


def is_public_ip(value: str) -> bool:
    try:
        ip = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return False
    candidates = [ip]
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped:
            candidates.append(ip.ipv4_mapped)
        if ip.sixtofour:
            candidates.append(ip.sixtofour)
        if ip.teredo:
            candidates.extend(ip.teredo)
        if ip in _NAT64:
            candidates.append(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
    for c in candidates:
        if (not c.is_global or c.is_multicast or c.is_reserved or c.is_loopback or c.is_link_local
                or c.is_private or c.is_unspecified):
            return False
    return True


async def resolve_host(host: str, port: int) -> list[str]:
    """IP-адреса хоста (подменяется в тестах). Ошибка разрешения — OSError."""
    loop = asyncio.get_running_loop()
    infos = await asyncio.wait_for(loop.getaddrinfo(host, port, type=socket.SOCK_STREAM), DNS_TIMEOUT)
    return sorted({info[4][0] for info in infos})


def _plausible_public_name(host: str) -> bool:
    h = host.lower().rstrip(".")
    return "." in h and h != "localhost" and not h.endswith(_LOCAL_SUFFIXES)


async def check_url(url: str) -> None:
    """BlockedUrl, если по этому адресу ходить нельзя."""
    try:
        parts = urlsplit(url)
        port = parts.port or 443
    except ValueError as exc:
        raise BlockedUrl("некорректный адрес") from exc
    if parts.scheme != "https":
        raise BlockedUrl(f"схема {parts.scheme!r} не разрешена")
    if parts.username is not None or parts.password is not None:
        raise BlockedUrl("логин и пароль в адресе не разрешены")
    host = parts.hostname
    if not host:
        raise BlockedUrl("нет имени хоста")
    try:
        ipaddress.ip_address(host.split("%", 1)[0])
        literal = True
    except ValueError:
        literal = False
    if literal:
        if not is_public_ip(host):
            raise BlockedUrl("непубличный IP-адрес")
        return
    if not _plausible_public_name(host):
        raise BlockedUrl("локальное имя хоста")
    try:
        addrs = await resolve_host(host, port)
    except (OSError, asyncio.TimeoutError) as exc:
        if config.YTDLP_PROXY:
            return                       # имя разрешит прокси пользователя; локальные имена отсеяны выше
        raise BlockedUrl("имя хоста не разрешается") from exc
    if not addrs or not all(is_public_ip(a) for a in addrs):
        raise BlockedUrl("имя хоста указывает на непубличный адрес")


async def send(client: httpx.AsyncClient, url: str, headers: dict[str, str]) -> httpx.Response:
    """
    GET с потоковым телом и проверкой адреса на каждом шаге редиректа. Возвращает ответ последнего шага
    (его request.url — итоговый адрес, от него разрешаются относительные URI плейлиста).
    httpx.HTTPError — сбой сети; BlockedUrl — недопустимый адрес или слишком много редиректов.
    """
    for _hop in range(MAX_REDIRECTS + 1):
        await check_url(url)
        resp = await client.send(client.build_request("GET", url, headers=headers), stream=True,
                                 follow_redirects=False)
        location = resp.headers.get("location")
        if resp.status_code in REDIRECT_STATUSES and location:
            await resp.aclose()
            url = urljoin(url, location)
            continue
        return resp
    raise BlockedUrl("слишком много перенаправлений")
