"""Маршруты /api/stream/... (S8: сборка частей движка стриминга)."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

import library
from streaming import local, proxy, resolver

router = APIRouter(prefix="/api/stream", tags=["stream"])


def _check(source: str, track_id: str) -> resolver.Source:
    cfg = resolver.SOURCES.get(source)
    if cfg is None:
        raise HTTPException(404, "Неизвестный источник")
    if not cfg.id_re.fullmatch(track_id):
        raise HTTPException(400, "Некорректный идентификатор трека")
    return cfg


@router.get("/{source}/{track_id}")
async def stream_track(source: str, track_id: str, request: Request, warm: int = 0):
    _check(source, track_id)

    # Скачанный трек играем с диска: работает без интернета и мгновенно (Range поддерживает FileResponse)
    path = library.audio_path(source, track_id)
    if path:
        if warm:
            return Response(status_code=204)
        return local.serve(path)

    # Прогрев (?warm=1): заранее получить ссылку и первую порцию. Ответ уходит сразу.
    if warm:
        return proxy._start_warm(request.app.state.http, request.app.state.resolver, source, track_id)

    return await proxy.serve(request, source, track_id)
