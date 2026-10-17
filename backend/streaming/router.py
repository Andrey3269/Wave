"""Маршруты /api/stream/... (S8: сборка частей движка стриминга)."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

import library
import localmusic
from domain.models import SOURCE_LOCAL
from streaming import hls_proxy, local, proxy, resolver

router = APIRouter(prefix="/api/stream", tags=["stream"])


# ---------------------------------------------------------------------------
# HLS (S8). Маршруты объявлены раньше общего /{source}/{track_id}: «hls» — не источник.
# ---------------------------------------------------------------------------
@router.get("/hls/{token}/{pl}.m3u8")
async def hls_playlist(token: str, pl: str, request: Request):
    """Плейлист сеанса (корневой main, вариант vN, дорожка aN) с адресами прокси вместо адресов источника."""
    return await hls_proxy.playlist(request, token, pl)


@router.get("/hls/{token}/{pl}/{res}")
async def hls_resource(token: str, pl: str, res: str, request: Request):
    """Сегмент (sSEQ) или init-секция (mN) — потоком от источника."""
    return await hls_proxy.resource(request, token, pl, res)


@router.delete("/hls/{token}", status_code=204)
async def hls_release(token: str):
    """Плеер больше не играет этот трек: сеанс и разобранные плейлисты освобождаются сразу, не дожидаясь TTL."""
    hls_proxy.release(token)
    return Response(status_code=204)


def _local_file(track_id: str):
    """Файл локального трека (S15, источник local) или понятная ошибка. Путь строит localmusic из id."""
    if not localmusic.ID_RE.fullmatch(track_id):
        raise HTTPException(400, "Некорректный идентификатор трека")
    path = localmusic.audio_path(track_id)
    if path is None:
        raise HTTPException(404, "Файл локального трека не найден: возможно, его удалили из Wave.")
    return path


def _check(source: str, track_id: str) -> resolver.Source:
    cfg = resolver.SOURCES.get(source)
    if cfg is None:
        raise HTTPException(404, "Неизвестный источник")
    if not cfg.id_re.fullmatch(track_id):
        raise HTTPException(400, "Некорректный идентификатор трека")
    return cfg


@router.get("/{source}/{track_id}/playback")
async def playback(source: str, track_id: str, request: Request):
    """
    Как играть трек (S8): {"kind": "local" | "progressive" | "hls", "url": ..., "preview": ...}.
      local       — скачанный файл, url = /api/stream/{source}/{id} (с диска, без сети);
      progressive — обычный поток, url = /api/stream/{source}/{id} (Range-прокси);
      hls         — url = /api/stream/hls/{token}/main.m3u8 для hls.js; корневой плейлист уже проверен.
    Ошибки — как у потока: понятный текст в detail и тип в X-Wave-Error.
    """
    direct = f"/api/stream/{source}/{track_id}"
    if source == SOURCE_LOCAL:                                   # S15: импортированный файл — всегда с диска
        _local_file(track_id)
        return {"kind": "local", "url": direct, "preview": False}
    _check(source, track_id)
    if library.audio_path(source, track_id):
        return {"kind": "local", "url": direct, "preview": False}
    res = await resolver._resolve(request.app.state.resolver, source, track_id)
    if res.kind != resolver.HLS:
        return {"kind": "progressive", "url": direct, "preview": res.preview}
    session = hls_proxy.open_session(source, track_id)
    await hls_proxy.prepare(request, session)
    return {"kind": "hls", "url": f"/api/stream/hls/{session.token}/main.m3u8", "preview": res.preview,
            "codec": res.codec}


@router.get("/{source}/{track_id}")
async def stream_track(source: str, track_id: str, request: Request, warm: int = 0):
    """Аудиопоток трека: 200 / 206 / 416 по Range; ?warm=1 — прогрев."""
    return await _stream(source, track_id, request, warm, is_head=False)


@router.head("/{source}/{track_id}")
async def stream_track_head(source: str, track_id: str, request: Request):
    """
    HEAD (S8): 200 и заголовки всего ресурса без тела. Раньше HEAD уходил мимо маршрута в раздачу фронтенда
    и получал 404 (находка L2).
    """
    return await _stream(source, track_id, request, 0, is_head=True)


async def _stream(source: str, track_id: str, request: Request, warm: int, is_head: bool):
    if source == SOURCE_LOCAL:                                   # S15: импортированный файл, Range — FileResponse
        path = _local_file(track_id)
        return Response(status_code=204) if warm and not is_head else local.serve(path)
    _check(source, track_id)

    # Скачанный трек играем с диска: работает без интернета и мгновенно (Range поддерживает FileResponse)
    path = library.audio_path(source, track_id)
    if path:
        if warm and not is_head:
            return Response(status_code=204)
        return local.serve(path)

    if is_head:
        return await proxy.head(request, source, track_id)

    # Прогрев (?warm=1): заранее получить ссылку и первую порцию. Ответ уходит сразу.
    if warm:
        return proxy._start_warm(request.app.state.http, request.app.state.resolver, source, track_id)

    return await proxy.serve(request, source, track_id)
