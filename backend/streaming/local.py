"""
Воспроизведение скачанного трека с диска тем же адресом /api/stream/{source}/{id}: работает без
интернета и мгновенно. Range (в том числе несколько диапазонов) обрабатывает Starlette FileResponse.
"""
from __future__ import annotations

from pathlib import Path

from fastapi.responses import FileResponse

MIME = {".m4a": "audio/mp4", ".mp3": "audio/mpeg", ".webm": "audio/webm", ".ogg": "audio/ogg"}


def mime(path: Path) -> str:
    return MIME.get(path.suffix, "audio/mp4")


def serve(path: Path) -> FileResponse:
    return FileResponse(path, media_type=mime(path))
