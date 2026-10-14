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

Закрепление адреса (S8, замечание аудита). Проверить имя и потом отдать его библиотеке, которая разрешит
его заново, недостаточно: ответ DNS может измениться между проверкой и соединением (DNS rebinding), а
прокси пользователя (YTDLP_PROXY) разрешает имя своим DNS — и может получить частный адрес в своей сети.
Поэтому HLS-запросы идут через отдельный клиент (make_client), у которого сетевой слой (PinnedBackend) в
момент соединения сам разрешает имя ЛОКАЛЬНО, проверяет все полученные IP и соединяется только с
проверенным IP: напрямую либо через прокси командой `CONNECT <IP-литерал>:<порт>` — прокси имя хоста не
получает и ничего не разрешает. TLS (SNI и проверка сертификата) и пул соединений по-прежнему привязаны к
имени хоста. Имя, которое локально не разрешается, отклоняется (Unverifiable) — даже при настроенном прокси:
проверить, куда оно ведёт, нельзя. Прокси, через которые туннель к IP-литералу построить нельзя (https://,
socks), для HLS не используются: такой запрос отклоняется, а не считается безопасным.
"""
from __future__ import annotations

import asyncio
import base64
import ipaddress
import logging
import socket
import ssl
from dataclasses import dataclass
from urllib.parse import unquote, urljoin, urlsplit

import certifi
import httpcore
import httpx

from streaming import config

log = logging.getLogger("musicmeta.stream.hls")

MAX_REDIRECTS = 5
DNS_TIMEOUT = 5.0
MAX_ADDRESSES = 3                 # сколько проверенных IP одного имени пробовать при сбое соединения
MAX_PROXY_REPLY = 16 * 1024       # ответ прокси на CONNECT
REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_LOCAL_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home.arpa", ".intranet", ".corp", ".home")


class BlockedUrl(Exception):
    """Адрес источника недопустим (схема, хост или IP)."""


class Unverifiable(BlockedUrl):
    """Куда ведёт адрес, проверить нельзя (имя не разрешается локально, неподходящий тип прокси) — отказ."""


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


async def resolve_checked(host: str, port: int) -> list[str]:
    """
    Проверенные IP имени хоста: IP-литерал — сам по себе, имя — разрешается ЛОКАЛЬНО (resolve_host), и все
    адреса должны быть публичными. BlockedUrl — адрес недопустим, Unverifiable — имя не разрешается.
    """
    bare = host.strip("[]").split("%", 1)[0]
    try:
        ipaddress.ip_address(bare)
    except ValueError:
        pass
    else:
        if not is_public_ip(bare):
            raise BlockedUrl("непубличный IP-адрес")
        return [bare]
    if not _plausible_public_name(host):
        raise BlockedUrl("локальное имя хоста")
    try:
        addrs = await resolve_host(host, port)
    except (OSError, asyncio.TimeoutError) as exc:
        # Прокси пользователя (если он есть) разрешил бы имя своим DNS — но проверить его ответ нельзя
        raise Unverifiable("имя хоста не разрешается локально") from exc
    if not addrs or not all(is_public_ip(a) for a in addrs):
        raise BlockedUrl("имя хоста указывает на непубличный адрес")
    return addrs


async def check_url(url: str) -> list[str]:
    """Проверенные IP адреса или BlockedUrl, если по этому адресу ходить нельзя."""
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
    return await resolve_checked(host, port)


# ---------------------------------------------------------------------------
# Соединение только с проверенным IP
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ProxyRoute:
    host: str
    port: int
    auth: str | None                  # значение Proxy-Authorization (Basic) или None


def proxy_route(proxy_url: str | None) -> ProxyRoute | None:
    """Прокси пользователя для HLS: только http:// (CONNECT к IP-литералу). Иначе — Unverifiable."""
    if not proxy_url:
        return None
    try:
        u = urlsplit(proxy_url)
        port = u.port
    except ValueError as exc:
        raise Unverifiable("некорректный адрес прокси") from exc
    if u.scheme != "http" or not u.hostname:
        raise Unverifiable(f"через прокси типа {u.scheme or '?'} адрес сервера проверить нельзя")
    auth = None
    if u.username is not None:
        raw = f"{unquote(u.username)}:{unquote(u.password or '')}".encode("utf-8")
        auth = "Basic " + base64.b64encode(raw).decode("ascii")
    return ProxyRoute(u.hostname, port or 80, auth)


async def _tunnel(stream, ip: str, port: int, auth: str | None, timeout: float | None) -> None:
    """CONNECT к проверенному IP через прокси пользователя; httpcore.ProxyError, если прокси отказал."""
    target = f"[{ip}]:{port}" if ":" in ip else f"{ip}:{port}"
    head = f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n"
    if auth:
        head += f"Proxy-Authorization: {auth}\r\n"
    await stream.write((head + "\r\n").encode("ascii"), timeout=timeout)
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = await stream.read(4096, timeout=timeout)
        if not chunk:
            raise httpcore.ProxyError("прокси закрыл соединение")
        buf += chunk
        if len(buf) > MAX_PROXY_REPLY:
            raise httpcore.ProxyError("слишком длинный ответ прокси")
    head_bytes, _, rest = buf.partition(b"\r\n\r\n")
    status = head_bytes.split(b"\r\n", 1)[0].decode("latin-1").split(" ", 2)
    if len(status) < 2 or not status[0].startswith("HTTP/1.") or not status[1].isdigit() \
            or not 200 <= int(status[1]) < 300:
        raise httpcore.ProxyError("прокси отказал в соединении: " + " ".join(status[1:])[:60])
    if rest:
        raise httpcore.ProxyError("лишние данные после ответа прокси")


class PinnedBackend(httpcore.AsyncNetworkBackend):
    """
    Сетевой слой клиента HLS: connect_tcp(имя) -> проверка всех IP имени (resolve_checked) -> соединение с
    проверенным IP напрямую (только IPv4, как и основной клиент Wave без прокси) или через прокси командой
    CONNECT к IP-литералу. Имя хоста дальше нужно только TLS (SNI, проверка сертификата) — его выставляет
    httpcore по адресу запроса.
    """

    def __init__(self, proxy_url: str | None, base: httpcore.AsyncNetworkBackend | None = None):
        self._proxy_url = proxy_url
        self._base = base or httpcore.AnyIOBackend()

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        route = proxy_route(self._proxy_url)
        addrs = await resolve_checked(host, port)
        if route is None:
            addrs = [a for a in addrs if ":" not in a]
            if not addrs:
                raise httpcore.ConnectError("у сервера нет IPv4-адреса")
        last: Exception | None = None
        for ip in addrs[:MAX_ADDRESSES]:
            try:
                if route is None:
                    return await self._base.connect_tcp(ip, port, timeout=timeout, local_address=local_address,
                                                        socket_options=socket_options)
                stream = await self._base.connect_tcp(route.host, route.port, timeout=timeout,
                                                      socket_options=socket_options)
                try:
                    await _tunnel(stream, ip, port, route.auth, timeout)
                except BaseException:
                    await stream.aclose()
                    raise
                return stream
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last = exc
        raise last if last is not None else httpcore.ConnectError("нет адресов")

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise Unverifiable("локальные сокеты не разрешены")

    async def sleep(self, seconds: float) -> None:
        await self._base.sleep(seconds)


class GuardedTransport(httpx.AsyncHTTPTransport):
    """Транспорт httpx с PinnedBackend: соединения только с проверенными IP (пул — по имени хоста)."""

    def __init__(self, proxy_url: str | None = None, base: httpcore.AsyncNetworkBackend | None = None,
                 bind_ipv4: bool = True):
        ssl_ctx = ssl.create_default_context(cafile=certifi.where())
        limits = httpx.Limits(max_connections=20, max_keepalive_connections=8, keepalive_expiry=10.0)
        super().__init__(verify=ssl_ctx, limits=limits)
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=ssl_ctx,
            max_connections=limits.max_connections,
            max_keepalive_connections=limits.max_keepalive_connections,
            keepalive_expiry=limits.keepalive_expiry,
            http1=True,
            http2=False,
            # без прокси — исходящий адрес IPv4, как у основного клиента (ссылки googlevideo привязаны к IP)
            local_address="0.0.0.0" if bind_ipv4 and not proxy_url else None,
            retries=0,
            network_backend=PinnedBackend(proxy_url, base),
        )


def make_client() -> httpx.AsyncClient:
    """HTTP-клиент HLS-прокси и HLS-загрузки (создаётся в lifespan, как основной клиент приложения)."""
    return httpx.AsyncClient(
        transport=GuardedTransport(config.YTDLP_PROXY),
        timeout=httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0),
        follow_redirects=False,
        trust_env=False,
    )


async def send(client: httpx.AsyncClient, url: str, headers: dict[str, str]) -> httpx.Response:
    """
    GET с потоковым телом и проверкой адреса на каждом шаге редиректа. Возвращает ответ последнего шага
    (его request.url — итоговый адрес, от него разрешаются относительные URI плейлиста).
    httpx.HTTPError — сбой сети; BlockedUrl — недопустимый адрес или слишком много редиректов.

    Здесь адрес проверяется заранее (понятная ошибка до отправки); клиент make_client дополнительно проверяет
    и закрепляет IP в момент соединения (PinnedBackend) — именно это закрывает rebinding и DNS прокси.
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
