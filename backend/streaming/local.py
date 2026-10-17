"""
Воспроизведение скачанного трека с диска тем же адресом /api/stream/{source}/{id}: работает без
интернета и мгновенно. Range (в том числе несколько диапазонов) обрабатывает Starlette FileResponse.

S8 (находка L2): синтаксически неверный Range (`bytes=abc`, `bytes=-`, `bytes=500-100`) по RFC 9110
игнорируется — ответ 200 с файлом целиком, а не 400. Для HEAD диапазон не применяется (Range определён
только для GET): ответ 200 с размером всего файла.
"""
from __future__ import annotations

from pathlib import Path

from fastapi.responses import FileResponse

from streaming import ranges

MIME = {".m4a": "audio/mp4", ".mp3": "audio/mpeg", ".webm": "audio/webm", ".ogg": "audio/ogg",
        # S15: форматы локальной музыки (backend/localmusic.py)
        ".weba": "audio/webm", ".oga": "audio/ogg", ".opus": "audio/ogg", ".flac": "audio/flac", ".wav": "audio/wav"}


def mime(path: Path) -> str:
    return MIME.get(path.suffix, "audio/mp4")


class _LocalFileResponse(FileResponse):
    """FileResponse, который не видит Range, если его по RFC нужно игнорировать."""

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") == "http":
            raw = next((v.decode("latin-1") for k, v in scope.get("headers", []) if k == b"range"), None)
            if raw is not None and (scope.get("method", "GET").upper() == "HEAD" or not ranges.is_valid_for_file(raw)):
                scope = {**scope, "headers": [(k, v) for k, v in scope["headers"] if k != b"range"]}
        await super().__call__(scope, receive, send)


def serve(path: Path) -> FileResponse:
    return _LocalFileResponse(path, media_type=mime(path))
