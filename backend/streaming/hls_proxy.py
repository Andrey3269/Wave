"""
HLS-прокси Wave (S8): плейлисты и сегменты SoundCloud через локальный backend.

Зачем прокси, а не прямой запрос hls.js к источнику: у AAC-HLS SoundCloud нет CORS-заголовков (WebView2
отказал бы), прокси пользователя (YTDLP_PROXY) действует только в backend, а подписанные адреса CDN не
должны попадать во фронтенд.

Модель безопасности. Фронтенд не передаёт адресов. /api/stream/{source}/{id}/playback для разрешённого
приложением трека создаёт сеанс с непрозрачным токеном (secrets.token_urlsafe), и дальше плеер обращается
только к /api/stream/hls/{token}/...: варианты, дорожки, сегменты и init-секции адресуются номерами внутри
сеанса. Адреса источника backend берёт из своего резолвера и из разобранных им же плейлистов; каждый
запрос и каждый редирект проходит netguard (https, только публичные IP). Сеансы ограничены по числу,
времени простоя и общему возрасту; плейлисты — по размеру; соединения — по числу и таймаутам. Сегменты
передаются потоком, без загрузки в память.

Маршруты (router.py):
    GET    /api/stream/hls/{token}/{pl}.m3u8       pl: main (корневой), vN (вариант master), aN (дорожка)
    GET    /api/stream/hls/{token}/{pl}/sSEQ       сегмент с порядковым номером SEQ
    GET    /api/stream/hls/{token}/{pl}/mN         init-секция N (#EXT-X-MAP)
    DELETE /api/stream/hls/{token}                 освободить сеанс (плеер переключил трек)
"""
from __future__ import annotations

import asyncio
import logging
import re
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass, field

import httpx
from fastapi import Request
from fastapi.responses import Response

from streaming import errors, hls, netguard, resolver
from streaming.proxy import STALE_STATUSES, AbortableStreamingResponse, UpstreamBroken, _body

log = logging.getLogger("musicmeta.stream.hls")

MAX_SESSIONS = 16                 # одновременно живущих сеансов (каждый — один трек)
IDLE_TTL = 30 * 60                # сеанс без обращений дольше 30 мин удаляется (плеер пересоздаст его сам)
MAX_AGE = 6 * 3600                # и в любом случае не живёт дольше 6 ч
MAX_PLAYLIST_BYTES = 2 * 1024 * 1024
PLAYLIST_TIMEOUT = 20.0           # на получение плейлиста целиком (включая редиректы)
ROOT_REUSE_SEC = 60.0             # корневой плейлист, проверенный /playback, плеер получает без нового запроса
PLAYLIST_TYPE = "application/vnd.apple.mpegurl"

TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
PLAYLIST_ID_RE = re.compile(r"^(main|v\d{1,3}|a\d{1,3})$")
RESOURCE_RE = re.compile(r"^(s|m)(\d{1,9})$")
_SEGMENT_TYPES = {".mp3": "audio/mpeg", ".aac": "audio/aac", ".m4s": "audio/mp4", ".mp4": "audio/mp4",
                  ".m4a": "audio/mp4", ".ts": "video/mp2t"}


@dataclass
class Session:
    token: str
    source: str
    track_id: str
    created: float = field(default_factory=time.monotonic)
    last_used: float = field(default_factory=time.monotonic)
    root_url: str | None = None                 # адрес корневого плейлиста, от которого разобраны плейлисты
    root_text: str | None = None
    root_fetched: float = 0.0
    root_sig: tuple | None = None
    playlists: dict[str, tuple[str, hls.Playlist]] = field(default_factory=dict)   # pl -> (текст, разбор)
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.source}:{self.track_id}"


_sessions: OrderedDict[str, Session] = OrderedDict()    # токен -> сеанс (LRU)
_by_key: dict[str, str] = {}                            # "sc:123" -> токен


def _expired(s: Session, now: float) -> bool:
    return now - s.last_used > IDLE_TTL or now - s.created > MAX_AGE


def _drop(token: str) -> None:
    s = _sessions.pop(token, None)
    if s is not None and _by_key.get(s.key) == token:
        _by_key.pop(s.key, None)


def _sweep() -> None:
    now = time.monotonic()
    for token in [t for t, s in _sessions.items() if _expired(s, now)]:
        _drop(token)
    while len(_sessions) > MAX_SESSIONS:
        _drop(next(iter(_sessions)))


def open_session(source: str, track_id: str) -> Session:
    """Сеанс для трека, разрешённого приложением (источник и id уже проверены маршрутом). Повторный вызов
    для того же трека возвращает живой сеанс, а не плодит новые."""
    _sweep()
    token = _by_key.get(f"{source}:{track_id}")
    s = _sessions.get(token) if token else None
    if s is None:
        s = Session(token=secrets.token_urlsafe(18), source=source, track_id=track_id)
        _sessions[s.token] = s
        _by_key[s.key] = s.token
        _sweep()
    s.last_used = time.monotonic()
    _sessions.move_to_end(s.token)
    return s


def get_session(token: str) -> Session | None:
    if not TOKEN_RE.fullmatch(token or ""):
        return None
    s = _sessions.get(token)
    if s is None:
        return None
    now = time.monotonic()
    if _expired(s, now):
        _drop(token)
        return None
    s.last_used = now
    _sessions.move_to_end(token)
    return s


def release(token: str) -> bool:
    if token in _sessions:
        _drop(token)
        return True
    return False


def clear() -> None:
    _sessions.clear()
    _by_key.clear()


def stats() -> dict:
    return {"sessions": len(_sessions), "max_sessions": MAX_SESSIONS}


# ---------------------------------------------------------------------------
# Ошибки
# ---------------------------------------------------------------------------
def _label(s: Session) -> str:
    return resolver.SOURCES[s.source].label


def session_expired() -> Exception:
    return errors._err(404, "hls_session_expired", "Сеанс воспроизведения устарел. Запустите трек ещё раз.")


def _changed() -> Exception:
    return errors._err(409, "hls_changed", "Источник сменил формат потока. Запустите трек ещё раз.")


def _bad_playlist(s: Session, exc: Exception) -> Exception:
    log.warning("Плохой HLS-плейлист %s: %s", s.key, exc)
    if isinstance(exc, hls.PlaylistEncrypted):
        return errors._err(403, "hls_encrypted", f"{_label(s)} передаёт этот трек только в зашифрованном виде — "
                                                 "Wave такие потоки не воспроизводит.")
    return errors._err(502, "hls_bad_playlist", f"{_label(s)} прислал некорректный HLS-плейлист. Попробуйте позже.")


def _blocked(s: Session, exc: Exception) -> Exception:
    log.warning("HLS %s: адрес источника отклонён защитой: %s", s.key, exc)
    return errors._err(502, "upstream_blocked", f"{_label(s)} указал недопустимый адрес потока, воспроизведение "
                                                "остановлено ради безопасности.")


def _network(s: Session, exc: Exception) -> Exception:
    log.info("HLS %s: сбой сети: %s", s.key, type(exc).__name__)
    return errors._err(503, "network", f"Нет связи с сервером аудио {_label(s)}. Проверьте подключение к интернету.")


def _upstream_status(s: Session, status: int) -> Exception:
    return errors._err(502, "upstream_status",
                       f"Сервер аудио {_label(s)} ответил ошибкой HTTP {status}. Попробуйте позже.")


def _stale(s: Session, status: int) -> Exception:
    return errors._err(502, "link_unavailable", f"{_label(s)} не отдаёт аудио по ссылке даже после её обновления "
                                                f"(HTTP {status}). Попробуйте позже.")


# ---------------------------------------------------------------------------
# Запросы к источнику
# ---------------------------------------------------------------------------
class _Stale(Exception):
    def __init__(self, status: int):
        self.status = status


async def _open(client: httpx.AsyncClient, rs: resolver.ResolverState, s: Session, url: str,
                extra: dict[str, str] | None = None) -> httpx.Response:
    """Соединение с источником под семафором и netguard. _Stale — ссылка протухла (401/403/404/410)."""
    headers = {**s.headers, "Accept-Encoding": "identity", **(extra or {})}
    async with rs.hls_sem:
        try:
            resp = await netguard.send(client, url, headers)
        except netguard.BlockedUrl as exc:
            raise _blocked(s, exc) from exc
        except httpx.HTTPError as exc:
            raise _network(s, exc) from exc
    if resp.status_code in STALE_STATUSES:
        await resp.aclose()
        raise _Stale(resp.status_code)
    return resp


async def _read_playlist(client, rs, s: Session, url: str) -> tuple[str, str]:
    """Текст плейлиста (не больше MAX_PLAYLIST_BYTES) и его итоговый адрес после редиректов."""
    async def go() -> tuple[str, str]:
        resp = await _open(client, rs, s, url)
        try:
            if resp.status_code != 200:
                raise _upstream_status(s, resp.status_code)
            declared = resp.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > MAX_PLAYLIST_BYTES:
                raise _bad_playlist(s, hls.PlaylistError("плейлист слишком большой"))
            buf = bytearray()
            async for chunk in resp.aiter_raw():
                buf += chunk
                if len(buf) > MAX_PLAYLIST_BYTES:
                    raise _bad_playlist(s, hls.PlaylistError("плейлист слишком большой"))
            final = str(resp.request.url)
        except httpx.HTTPError as exc:
            raise _network(s, exc) from exc
        finally:
            await resp.aclose()
        try:
            return buf.decode("utf-8"), final
        except UnicodeDecodeError as exc:
            raise _bad_playlist(s, hls.PlaylistError("плейлист не в UTF-8")) from exc
    try:
        return await asyncio.wait_for(go(), PLAYLIST_TIMEOUT)
    except asyncio.TimeoutError as exc:
        raise _network(s, exc) from exc


async def _resolve_hls(rs, s: Session) -> resolver.Resolved:
    res = await resolver._resolve(rs, s.source, s.track_id)
    if res.kind != resolver.HLS:
        raise _changed()                              # теперь у трека прогрессивный поток: плеер начнёт заново
    s.headers = dict(res.headers)
    return res


async def _load_root(client, rs, s: Session, *, refresh: bool = False) -> None:
    """Корневой плейлист трека (по ссылке из резолвера). При протухшей ссылке — один перерезолв."""
    if not refresh and s.root_text is not None and time.monotonic() - s.root_fetched < ROOT_REUSE_SEC:
        return
    for attempt in range(2):
        if refresh or attempt:
            resolver._invalidate(s.source, s.track_id)
        res = await _resolve_hls(rs, s)
        try:
            text, final = await _read_playlist(client, rs, s, res.url)
        except _Stale as st:
            if attempt:
                raise _stale(s, st.status) from None
            log.info("HLS %s: ссылка на плейлист протухла (HTTP %s), перерезолв", s.key, st.status)
            continue
        try:
            parsed = hls.parse(text, final)
        except hls.PlaylistError as exc:
            raise _bad_playlist(s, exc) from exc
        sig = parsed.signature()
        if s.root_sig is not None and sig != s.root_sig:
            s.playlists.clear()
            s.root_sig = None
            raise _changed()
        s.root_url, s.root_text, s.root_sig, s.root_fetched = final, text, sig, time.monotonic()
        s.playlists = {"main": (text, parsed)}
        return


async def _load_playlist(client, rs, s: Session, pl: str, *, refresh: bool = False) -> tuple[str, hls.Playlist]:
    if pl == "main":
        await _load_root(client, rs, s, refresh=refresh)
        return s.playlists["main"]
    if not refresh and pl in s.playlists:
        return s.playlists[pl]
    old = s.playlists.get(pl)                         # до перезагрузки корня: её результат сравним с прежним
    await _load_root(client, rs, s, refresh=refresh)
    _text, root = s.playlists["main"]
    if not isinstance(root, hls.MasterPlaylist):
        raise session_expired()                       # у трека нет вариантов: такой адрес плеер не получал
    items = root.variants if pl[0] == "v" else root.renditions
    i = int(pl[1:])
    if i >= len(items):
        raise session_expired()
    try:
        text, final = await _read_playlist(client, rs, s, items[i].uri)
    except _Stale as st:
        if refresh:
            raise _stale(s, st.status) from None
        return await _load_playlist(client, rs, s, pl, refresh=True)
    try:
        parsed = hls.parse(text, final)
    except hls.PlaylistError as exc:
        raise _bad_playlist(s, exc) from exc
    if not isinstance(parsed, hls.MediaPlaylist):
        raise _bad_playlist(s, hls.PlaylistError("вложенный master-плейлист"))
    if old is not None and old[1].signature() != parsed.signature():
        raise _changed()
    s.playlists[pl] = (text, parsed)
    return text, parsed


def _base(s: Session) -> str:
    return f"/api/stream/hls/{s.token}"


def _render(s: Session, pl: str, text: str, parsed: hls.Playlist) -> str:
    base = _base(s)
    try:
        if isinstance(parsed, hls.MasterPlaylist):
            return hls.rewrite_master(text, parsed, lambda i: f"{base}/v{i}.m3u8", lambda i: f"{base}/a{i}.m3u8")
        return hls.rewrite_media(text, parsed, lambda seg: f"{base}/{pl}/s{seg.seq}", lambda i: f"{base}/{pl}/m{i}")
    except hls.PlaylistError as exc:
        raise _bad_playlist(s, exc) from exc


# ---------------------------------------------------------------------------
# Обработчики маршрутов
# ---------------------------------------------------------------------------
async def prepare(request: Request, s: Session) -> None:
    """Проверить корневой плейлист сразу при /playback: честная ошибка до старта плеера, и плеер получает
    уже проверенный плейлист без второго запроса к источнику."""
    await _load_root(request.app.state.http, request.app.state.resolver, s)


async def playlist(request: Request, token: str, pl: str) -> Response:
    s = get_session(token)
    if s is None or not PLAYLIST_ID_RE.fullmatch(pl):
        raise session_expired()
    text, parsed = await _load_playlist(request.app.state.http, request.app.state.resolver, s, pl)
    return Response(_render(s, pl, text, parsed), media_type=PLAYLIST_TYPE, headers={"Cache-Control": "no-store"})


def _content_type(upstream: httpx.Response, uri: str) -> str:
    ctype = upstream.headers.get("content-type", "")
    if ctype and not ctype.startswith(("text/", "application/octet-stream", "binary/")):
        return ctype
    path = uri.split("?", 1)[0].lower()
    return next((t for ext, t in _SEGMENT_TYPES.items() if path.endswith(ext)), "application/octet-stream")


async def resource(request: Request, token: str, pl: str, res: str) -> Response:
    """Сегмент или init-секция: потоком, с N2-поведением при обрыве (как у progressive-прокси)."""
    s = get_session(token)
    m = RESOURCE_RE.fullmatch(res or "")
    if s is None or not PLAYLIST_ID_RE.fullmatch(pl) or m is None:
        raise session_expired()
    client, rs = request.app.state.http, request.app.state.resolver
    kind, num = m.group(1), int(m.group(2))
    for attempt in range(2):
        _text, parsed = await _load_playlist(client, rs, s, pl, refresh=bool(attempt))
        if not isinstance(parsed, hls.MediaPlaylist):
            raise session_expired()
        item = parsed.segment(num) if kind == "s" else (parsed.maps[num] if num < len(parsed.maps) else None)
        if item is None:
            raise session_expired()
        extra = {}
        if item.byterange is not None:
            offset, length = item.byterange
            extra["Range"] = f"bytes={offset}-{offset + length - 1}"
        try:
            upstream = await _open(client, rs, s, item.uri, extra)
        except _Stale as st:
            if attempt:
                raise _stale(s, st.status) from None
            log.info("HLS %s: ссылка на сегмент протухла (HTTP %s), обновляю плейлист", s.key, st.status)
            continue
        return await _segment_response(s, upstream, item)
    raise session_expired()                           # недостижимо: цикл выходит return или raise


async def _segment_response(s: Session, upstream: httpx.Response, item: hls.Resource) -> Response:
    headers = {"Content-Type": _content_type(upstream, item.uri), "Cache-Control": "no-store"}
    status = upstream.status_code
    if item.byterange is None:
        if status != 200:
            await upstream.aclose()
            raise _upstream_status(s, status)
        length = upstream.headers.get("content-length")
        expected = int(length) if length and length.isdigit() else None
        body = _body(upstream, expected)
    else:
        offset, expected = item.byterange
        if status == 206:
            body = _body(upstream, expected)
        elif status == 200 and offset == 0:          # источник проигнорировал Range: берём только начало файла
            body = _take(_body(upstream, None), expected)
        else:
            await upstream.aclose()
            raise _upstream_status(s, status)
    if expected is not None:
        headers["Content-Length"] = str(expected)
    return AbortableStreamingResponse(body, status_code=200, headers=headers)


async def _take(body, limit: int):
    """Первые limit байт тела; меньше — обрыв (UpstreamBroken), как и у обычного сегмента."""
    left = limit
    try:
        async for chunk in body:
            if len(chunk) >= left:
                yield chunk[:left]
                return
            left -= len(chunk)
            yield chunk
        raise UpstreamBroken
    finally:
        await body.aclose()
