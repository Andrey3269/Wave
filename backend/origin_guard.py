"""
Защита локального API от кросс-доменных изменяющих запросов (S13, находка M7 — CSRF).

Backend слушает 127.0.0.1 на случайном порту, интерфейс открыт с того же адреса (same-origin). Любая страница
в браузере пользователя может отправить на этот адрес «простой» POST (form, text/plain, sendBeacon): CORS такой
запрос не останавливает, браузер лишь не даёт прочитать ответ. Поэтому изменяющие запросы проверяются на сервере:

* GET, HEAD, OPTIONS — без изменений (данные они не меняют; чтение чужим сайтом и так закрыто CORS, preflight
  обрабатывает CORSMiddleware).
* POST, PUT, PATCH, DELETE и другие методы:
  - заголовок Origin есть — запрос выполняется, только если Origin совпадает с адресом самого сервера
    (http://<Host>, Host уже проверен TrustedHostMiddleware: 127.0.0.1 или localhost). Иначе — 403. Значение
    `null` (песочница, file://, data:) — тоже 403;
  - Origin нет — смотрим Sec-Fetch-Site: `cross-site` и `same-site` (другой порт того же хоста) — 403.
    Без обоих заголовков запрос пришёл не из браузерной страницы (тесты, curl, лаунчер) — выполняется.

Chromium и WebView2 присылают Origin во всех запросах, кроме GET/HEAD, в том числе same-origin fetch и
navigator.sendBeacon, поэтому штатный интерфейс проверку проходит без изменений. Разрешённых «чужих» origin нет:
режим разработки с Live Server (ALLOWED_ORIGINS в main.py) по-прежнему может только читать (CORS: GET, OPTIONS).
"""
from __future__ import annotations

import json
import logging

log = logging.getLogger("musicmeta.security")

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
CROSS_FETCH_SITES = frozenset({b"cross-site", b"same-site"})
DETAIL = "Запрос с другого сайта отклонён."
CONFLICT = b"\x00conflict"       # один заголовок несколько раз с разными значениями: запрос отклоняется
_LOGGED_MAX = 64                 # о каждом чужом origin пишем в журнал один раз (страница может слать запросы в цикле)


class OriginGuard:
    """Чистый ASGI-middleware (без BaseHTTPMiddleware: не буферизует потоковые ответы и не меняет их)."""

    def __init__(self, app):
        self.app = app
        self._logged: set[str] = set()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method", "GET").upper() in SAFE_METHODS:
            await self.app(scope, receive, send)
            return
        headers = scope.get("headers") or []
        origin = _header(headers, b"origin")
        if origin is not None:
            host = _header(headers, b"host")
            allowed = (origin is not CONFLICT and host is not None and host is not CONFLICT
                       and origin.strip().lower() == b"http://" + host.strip().lower())
        else:
            site = _header(headers, b"sec-fetch-site")
            allowed = site is None or (site is not CONFLICT and site.strip().lower() not in CROSS_FETCH_SITES)
        if allowed:
            await self.app(scope, receive, send)
            return
        if origin is not None:
            why = "несколько разных Origin" if origin is CONFLICT else origin.decode("latin-1")[:200]
        else:
            why = "несколько разных Sec-Fetch-Site" if site is CONFLICT else f"Sec-Fetch-Site: {site.decode('latin-1')[:40]}"
        if why not in self._logged and len(self._logged) < _LOGGED_MAX:
            self._logged.add(why)
            log.warning("Отклонён кросс-доменный %s %s (%s)", scope.get("method"), scope.get("path"), why)
        await _forbidden(send)


def _header(headers, name: bytes) -> bytes | None:
    """Значение заголовка, None (заголовка нет) или CONFLICT (несколько разных значений — так браузер не шлёт)."""
    values = {v for k, v in headers if k.lower() == name}
    if not values:
        return None
    return values.pop() if len(values) == 1 else CONFLICT


async def _forbidden(send) -> None:
    body = json.dumps({"detail": DETAIL, "code": "forbidden_origin"}, ensure_ascii=False).encode("utf-8")
    await send({"type": "http.response.start", "status": 403,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})
