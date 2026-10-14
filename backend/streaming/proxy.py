"""
Прокси progressive-потока с поддержкой Range (HTTP 206).

    <audio> --Range--> /api/stream/{source}/{id} --Range--> CDN (прямая ссылка от yt-dlp)
                       <--206 + чанки------                 <--206 + чанки--

Браузер сам шлёт `Range: bytes=N-` при старте и перемотке; диапазон режется до одной порции и
прозрачно пробрасывается источнику, ответ — 206 + Content-Range.
"""
from __future__ import annotations

import logging
import os
import ssl
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import AsyncIterator

import certifi
import httpx
from fastapi import Request
from fastapi.responses import Response, StreamingResponse

from streaming import config, errors, ranges, resolver

log = logging.getLogger("musicmeta.stream")

CHUNK_SIZE = 64 * 1024            # размер куска при перекачке внутри одного ответа
# Максимальный размер одной порции трека. Браузер просит "bytes=0-" (весь файл), а мы
# отвечаем только первой порцией (206 + Content-Range), и плеер сам докачивает
# следующие по мере надобности. Так же работает YouTube Music: быстрый старт и
# перемотка, и CDN не режет скорость на длинных запросах.
STREAM_CHUNK_BYTES = int(os.getenv("STREAM_CHUNK_KB", "1024")) * 1024

# Кэш «голов»: первая порция прогретых треков лежит в памяти (24 x 1 МБ ~ 24 МБ).
HEAD_CACHE_ITEMS = 24
HEAD_TTL = 30 * 60
STALE_STATUSES = {401, 403, 404, 410}  # ответы CDN при протухшей ссылке


def make_http_client() -> httpx.AsyncClient:
    """Общий HTTP-клиент для всего приложения (keep-alive, пул соединений)."""
    # Явный контекст на сертификатах certifi (он ставится вместе с httpx). Так мы не
    # зависим от переменных окружения SSL_CERT_FILE / SSL_CERT_DIR: если они на
    # компьютере указывают на несуществующий файл, httpx иначе падает при старте.
    ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    # Ссылки googlevideo привязаны к IP, с которого их получил yt-dlp. Если httpx уйдёт
    # по IPv6, Google ответит 403. local_address="0.0.0.0" = только IPv4 (при прокси не нужно).
    transport = httpx.AsyncHTTPTransport(
        verify=ssl_ctx,
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
        local_address=None if config.YTDLP_PROXY else "0.0.0.0",
        proxy=config.YTDLP_PROXY or None,
        retries=1,  # повтор при ошибке установки соединения
    )
    return httpx.AsyncClient(
        transport=transport,
        timeout=httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0),
        follow_redirects=True,
    )


# ---------------------------------------------------------------------------
# Соединение с источником
# ---------------------------------------------------------------------------
async def _connect(
    client: httpx.AsyncClient, rs: resolver.ResolverState, source: str, track_id: str, upstream_range: str
) -> httpx.Response:
    """
    Открывает потоковое соединение с CDN. Если ссылка протухла
    (401/403/404/410) или соединение не удалось — один раз перерезолвим и повторим.
    """
    last: int | None = None                       # HTTP-статус последней неудачи или None (нет соединения)
    for _attempt in range(2):
        res = await resolver._resolve(rs, source, track_id)
        if res.kind != resolver.PROGRESSIVE:
            raise hls_required()
        headers = {**res.headers, "Accept-Encoding": "identity", "Range": upstream_range}
        try:
            upstream = await client.send(
                client.build_request("GET", res.url, headers=headers), stream=True
            )
        except httpx.HTTPError as exc:
            log.warning("Сбой соединения с источником (%s:%s): %r", source, track_id, exc)
            resolver._invalidate(source, track_id)
            last = None
            continue

        if upstream.status_code in STALE_STATUSES:
            log.info("Ссылка протухла (%s:%s): HTTP %s", source, track_id, upstream.status_code)
            await upstream.aclose()
            resolver._invalidate(source, track_id)
            last = upstream.status_code
            continue
        return upstream

    label = resolver.SOURCES[source].label
    if last is None:
        raise errors._err(503, "network", f"Нет связи с сервером аудио {label}. Проверьте подключение к интернету.")
    raise errors._err(502, "link_unavailable", f"{label} не отдаёт аудио по ссылке даже после её обновления "
                                               f"(HTTP {last}). Попробуйте позже.")


class UpstreamBroken(Exception):
    """Источник оборвал тело ответа раньше, чем передал всё объявленное (N2)."""


def hls_required() -> Exception:
    """
    Трек доступен только как HLS (S8). Адрес progressive-потока никогда не отдаёт плейлист m3u8 под видом
    аудиофайла: плеер получает HLS через /api/stream/{source}/{id}/playback, загрузчик — свой путь (downloads).
    """
    return errors._err(409, "hls_required", "Этот трек передаётся по HLS: его воспроизводит плеер Wave, прямой "
                                            "аудиофайл недоступен.")


async def _body(upstream: httpx.Response, expected: int | None = None) -> AsyncIterator[bytes]:
    """
    Перекачивает байты клиенту по мере получения; при разрыве/отмене гарантированно закрывает источник.

    N2 (наблюдение S1): раньше тело читалось кусками по 64 КБ (aiter_raw(CHUNK_SIZE) копит байты до полного
    куска), и при обрыве CDN уже полученный «хвост» терялся, а ответ завершался как успешный. Теперь каждый
    фрагмент сети уходит клиенту сразу, а обрыв — UpstreamBroken: AbortableStreamingResponse прерывает ответ
    без завершающего сообщения, сервер закрывает соединение, и плеер видит неполный ответ и запрашивает
    продолжение с того байта, который реально получил.
    """
    sent = 0
    try:
        async for chunk in upstream.aiter_raw():
            sent += len(chunk)
            yield chunk
    except httpx.HTTPError as exc:
        log.info("Поток оборван источником после %d байт: %s", sent, type(exc).__name__)
        raise UpstreamBroken from exc
    finally:
        await upstream.aclose()
    if expected is not None and sent < expected:
        log.info("Источник закрыл поток раньше времени: %d из %d байт", sent, expected)
        raise UpstreamBroken


class AbortableStreamingResponse(StreamingResponse):
    """
    StreamingResponse, который при UpstreamBroken не отправляет завершающее пустое сообщение тела.
    ASGI-сервер (uvicorn) в таком случае закрывает соединение: при объявленном Content-Length клиент
    получает меньше байтов, при chunked — без завершающего чанка. Ответ не выглядит успешно законченным.
    """

    async def stream_response(self, send) -> None:
        await send({"type": "http.response.start", "status": self.status_code, "headers": self.raw_headers})
        try:
            async for chunk in self.body_iterator:
                await send({"type": "http.response.body", "body": chunk, "more_body": True})
        except UpstreamBroken:
            return
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    async def __call__(self, scope, receive, send) -> None:
        """
        Тело закрывается всегда — и после обычного конца, и при отключении клиента (Starlette тогда отменяет
        передачу, а генератор, остановленный на yield, иначе закрылся бы только сборщиком мусора). Закрытие
        генератора закрывает ответ источника и освобождает слот соединения HLS (hls_proxy._LeasedStream).
        """
        try:
            await super().__call__(scope, receive, send)
        finally:
            closer = getattr(self.body_iterator, "aclose", None)
            if closer is not None:
                try:
                    await closer()
                except Exception as exc:                     # закрытие не должно подменять исходную ошибку
                    log.info("Тело ответа закрыто с ошибкой: %s", type(exc).__name__)


# ---------------------------------------------------------------------------
# Прогрев: ссылка + первая порция трека заранее лежат на сервере
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class Head:
    data: bytes            # первая порция трека
    total: str             # полный размер файла из Content-Range ("*" если неизвестен)
    content_type: str
    stored_at: float


_heads: OrderedDict[str, Head] = OrderedDict()   # LRU; данные уровня процесса, как и кэш ссылок


def _head_get(key: str) -> Head | None:
    head = _heads.get(key)
    if head and time.time() - head.stored_at < HEAD_TTL:
        _heads.move_to_end(key)
        return head
    _heads.pop(key, None)
    return None


def _head_put(key: str, head: Head) -> None:
    _heads[key] = head
    _heads.move_to_end(key)
    while len(_heads) > HEAD_CACHE_ITEMS:
        _heads.popitem(last=False)


def _serve_from_head(head: Head, raw_range: str) -> Response | None:
    """Отвечает на Range из памяти, если начало диапазона попадает в сохранённую порцию."""
    m = ranges.RANGE_RE.fullmatch(raw_range)
    if not m or not m.group(1):
        return None
    start, size = int(m.group(1)), len(head.data)
    if start >= size:
        return None
    end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
    if end < start:
        return None
    return Response(
        content=head.data[start : end + 1],
        status_code=206,
        headers={
            "Content-Type": head.content_type,
            "Accept-Ranges": "bytes",
            "Content-Range": f"bytes {start}-{end}/{head.total}",
            "Cache-Control": "private, max-age=3600",
        },
    )


async def _prefetch_head(client: httpx.AsyncClient, rs: resolver.ResolverState, source: str, track_id: str) -> None:
    key = f"{source}:{track_id}"
    t0 = time.monotonic()
    try:
        res = await resolver._resolve(rs, source, track_id)
        if res.kind != resolver.PROGRESSIVE:
            log.info("Прогрет %s: HLS-поток, ссылка получена за %.1f с", key, time.monotonic() - t0)
            return                                # для HLS прогрев = готовая ссылка; «голова» не нужна
        upstream = await _connect(client, rs, source, track_id, f"bytes=0-{STREAM_CHUNK_BYTES - 1}")
        try:
            m = ranges.CONTENT_RANGE_RE.fullmatch(upstream.headers.get("content-range", ""))
            if upstream.status_code != 206 or not m:
                return
            data = await upstream.aread()
            ctype = upstream.headers.get("content-type", "audio/mpeg" if source == "sc" else "audio/mp4")
        finally:
            await upstream.aclose()
        _head_put(key, Head(data=data, total=m.group(3), content_type=ctype, stored_at=time.time()))
        log.info("Прогрет %s: %d КБ за %.1f с", key, len(data) // 1024, time.monotonic() - t0)
    except Exception as exc:
        log.info("Прогрев %s не удался: %r", key, exc)
    finally:
        rs.prefetching.discard(key)


def _start_warm(client: httpx.AsyncClient, rs: resolver.ResolverState, source: str, track_id: str) -> Response:
    """
    Ответ на ?warm=1: 204 уже готово, 202 готовится, 503 сервер занят (фронтенд повторит позже).
    Сам ответ уходит сразу, прогрев идёт в фоне.
    """
    key = f"{source}:{track_id}"
    if _head_get(key) is not None:
        return Response(status_code=204)
    if key in rs.prefetching:
        return Response(status_code=202)
    if len(rs.inflight) >= 2 or len(rs.prefetching) >= 3:
        return Response(status_code=503)
    rs.check()
    rs.prefetching.add(key)
    rs.spawn(_prefetch_head(client, rs, source, track_id))
    return Response(status_code=202)


def _cap_range(raw: str) -> str:
    """Обрезает диапазон до одной порции STREAM_CHUNK_BYTES (ranges.cap)."""
    return ranges.cap(raw, STREAM_CHUNK_BYTES)


def _default_type(source: str) -> str:
    return "audio/mpeg" if source == "sc" else "audio/mp4"


# ---------------------------------------------------------------------------
# Ответ на GET /api/stream/{source}/{id} для потока из источника
# ---------------------------------------------------------------------------
async def serve(request: Request, source: str, track_id: str) -> Response:
    cfg = resolver.SOURCES[source]
    client, rs = request.app.state.http, request.app.state.resolver

    # --- Разбираем Range клиента. Поддерживаем один диапазон: bytes=a-b | bytes=a- | bytes=-n.
    # Битый, пустой (`bytes=-`) или множественный диапазон игнорируется (RFC 9110): ответ — весь файл, 200.
    raw_range = (request.headers.get("range") or "").strip()
    client_range = ranges.parse_single(raw_range)
    client_wants_range = client_range is not None
    # Диапазон клиента режем до одной порции: ответ будет 206 с честным Content-Range,
    # а плеер сам запросит продолжение. Если Range не прислан (curl, прямая ссылка),
    # отдаём файл целиком обычным 200.
    upstream_range = _cap_range(client_range.header()) if client_wants_range else "bytes=0-"

    # Трек прогрет: начало лежит в памяти, отвечаем мгновенно, без обращения к источнику
    if client_wants_range:
        head = _head_get(f"{source}:{track_id}")
        if head:
            from_memory = _serve_from_head(head, client_range.header())
            if from_memory is not None:
                return from_memory

    upstream = await _connect(client, rs, source, track_id, upstream_range)
    status = upstream.status_code

    # Диапазон за пределами файла: пробрасываем 416 как есть
    if status == 416:
        content_range = upstream.headers.get("content-range", "bytes */0")
        await upstream.aclose()
        return Response(status_code=416, headers={"Content-Range": content_range, "Accept-Ranges": "bytes"})

    if status not in (200, 206):
        await upstream.aclose()
        raise errors._err(502, "upstream_status",
                          f"Сервер аудио {cfg.label} ответил ошибкой HTTP {status}. Попробуйте позже.")

    headers = {
        "Content-Type": upstream.headers.get("content-type", _default_type(source)),
        "Accept-Ranges": "bytes",
        "Cache-Control": "private, max-age=3600",
    }
    expected = None
    if length := upstream.headers.get("content-length"):
        headers["Content-Length"] = length
        expected = int(length) if length.isdigit() else None

    # 206 отдаём, только если клиент просил диапазон И источник его выдал.
    # Если диапазон не просили — это обычный 200 (Content-Length = размер всего файла).
    content_range = upstream.headers.get("content-range")
    if client_wants_range and status == 206 and content_range:
        headers["Content-Range"] = content_range
        code = 206
    else:
        code = 200

    return AbortableStreamingResponse(_body(upstream, expected), status_code=code, headers=headers)


async def head(request: Request, source: str, track_id: str) -> Response:
    """
    HEAD /api/stream/{source}/{id} (S8, находка L2): 200 и заголовки всего ресурса без тела. Range для HEAD не
    применяется (RFC 9110 определяет его только для GET). Размер берётся из прогретой «головы» или из одного
    запроса `bytes=0-0` к источнику (тот же путь, что у плеера: резолв, перерезолв протухшей ссылки, ошибки).
    """
    headers = {"Accept-Ranges": "bytes", "Cache-Control": "private, max-age=3600"}
    cached = _head_get(f"{source}:{track_id}")
    if cached is not None:
        headers["Content-Type"] = cached.content_type
        if cached.total != "*":
            headers["Content-Length"] = cached.total
        return _head_response(headers)

    upstream = await _connect(request.app.state.http, request.app.state.resolver, source, track_id, "bytes=0-0")
    try:
        status = upstream.status_code
        headers["Content-Type"] = upstream.headers.get("content-type", _default_type(source))
        m = ranges.CONTENT_RANGE_RE.fullmatch(upstream.headers.get("content-range", ""))
        if status == 206 and m and m.group(3) != "*":
            headers["Content-Length"] = m.group(3)
        elif status == 200 and upstream.headers.get("content-length"):
            headers["Content-Length"] = upstream.headers["content-length"]
        elif status == 416:                                  # пустой файл: даже нулевой байт не удовлетворим
            headers["Content-Length"] = "0"
        elif status not in (200, 206):
            label = resolver.SOURCES[source].label
            raise errors._err(502, "upstream_status",
                              f"Сервер аудио {label} ответил ошибкой HTTP {status}. Попробуйте позже.")
    finally:
        await upstream.aclose()
    return _head_response(headers)


def _head_response(headers: dict) -> Response:
    """Ответ на HEAD. Если размер неизвестен, Content-Length нет вовсе (Starlette иначе подставил бы «0»)."""
    resp = Response(status_code=200, headers=headers)
    if "Content-Length" not in headers:
        del resp.headers["content-length"]
    return resp
