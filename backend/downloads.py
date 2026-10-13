"""
Загрузки: трек, обложка, текст и metadata.json сохраняются в отдельную папку.

  Downloads/Wave/Artist - Title [yt-ID]/
      audio.m4a | audio.mp3   (формат источника как есть, ffmpeg не нужен)
      cover.jpg
      lyrics.json             (только если текст найден)
      metadata.json

Папка собирается во временном каталоге *.part и переименовывается целиком, поэтому
после сбоя не остаётся полузагруженных треков. Состояние загрузок живёт в памяти
(UI опрашивает /api/downloads/jobs), итог записывается в SQLite (library.py).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
from typing import Literal
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

import library
import paths
from lyrics import fetch_lyrics
from stream import CHUNK_SIZE, CONTENT_RANGE_RE, SOURCES, _connect

log = logging.getLogger("musicmeta.downloads")
router = APIRouter(prefix="/api", tags=["downloads"])

DL_CHUNK = 4 * 1024 * 1024                    # скачиваем порциями по 4 МБ (источники режут длинные запросы)
ACTIVE = {"preparing", "downloading", "processing"}
COVER_HOSTS = ("googleusercontent.com", "ytimg.com", "ggpht.com", "sndcdn.com")   # откуда разрешено брать обложки
EXT = {"mp4": ".m4a", "mpeg": ".mp3", "webm": ".webm", "ogg": ".ogg"}

jobs: dict[str, dict] = {}
_background: set[asyncio.Task] = set()


class DownloadRequest(BaseModel):
    source: Literal["yt", "sc"]
    id: str
    title: str
    artist: str
    album: str | None = None
    duration: int | None = None
    cover: str | None = None


def _ext(content_type: str) -> str:
    return next((e for k, e in EXT.items() if k in content_type), ".m4a")


async def _download_audio(client, rs, req: DownloadRequest, dest, job: dict) -> str:
    """Качает аудио порциями с Range; возвращает расширение файла."""
    pos, total, ext = 0, None, ".m4a"
    with open(dest, "wb") as f:
        while total is None or pos < total:
            up = await _connect(client, rs, req.source, req.id, f"bytes={pos}-{pos + DL_CHUNK - 1}")
            got, whole = 0, False
            try:
                if up.status_code == 206:
                    m = CONTENT_RANGE_RE.fullmatch(up.headers.get("content-range", ""))
                    total = int(m.group(3)) if m and m.group(3) != "*" else None
                elif up.status_code == 200:                 # источник проигнорировал Range: файл целиком
                    total, whole = int(up.headers.get("content-length") or 0) or None, True
                else:
                    raise RuntimeError(f"Источник вернул HTTP {up.status_code}")
                if pos == 0:
                    ext = _ext(up.headers.get("content-type", ""))
                async for chunk in up.aiter_raw(CHUNK_SIZE):
                    f.write(chunk)
                    got += len(chunk)
                    if total:
                        job["progress"] = min(99, int((pos + got) * 100 / total))
            finally:
                await up.aclose()
            pos += got
            if whole or got == 0 or (total is None and got < DL_CHUNK):
                break
    return ext


async def _download_cover(client, url: str | None, folder) -> str | None:
    if not url:
        return None
    u = urlparse(url)
    if u.scheme != "https" or not (u.hostname or "").endswith(COVER_HOSTS):
        return None
    try:
        r = await client.get(url, timeout=10)
        ctype = r.headers.get("content-type", "")
        if r.status_code != 200 or not ctype.startswith("image/"):
            return None
        name = "cover.png" if "png" in ctype else "cover.webp" if "webp" in ctype else "cover.jpg"
        (folder / name).write_bytes(r.content)
        return name
    except Exception as exc:
        log.info("Обложка не скачана: %r", exc)
        return None


async def _run(client, rs, req: DownloadRequest) -> None:
    key = f"{req.source}:{req.id}"
    job = jobs[key]
    name = library.safe_folder(req.artist, req.title, req.source, req.id)
    target = library.folder_path(name)
    tmp = target.with_name(target.name + ".part")
    try:
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)

        job["state"] = "downloading"
        ext = await _download_audio(client, rs, req, tmp / "audio.part", job)
        audio = f"audio{ext}"
        os.replace(tmp / "audio.part", tmp / audio)

        job.update(state="processing", progress=99)
        cover = await _download_cover(client, req.cover, tmp)
        lyr = await fetch_lyrics(client, req.artist, req.title, req.duration)
        lyrics_file = None
        if lyr and (lyr["synced"] or lyr["plain"]):           # пустой файл «для вида» не создаём
            (tmp / "lyrics.json").write_text(json.dumps(lyr, ensure_ascii=False), encoding="utf-8")
            lyrics_file = "lyrics.json"

        meta = {"id": req.id, "source": req.source, "title": req.title, "artist": req.artist,
                "album": req.album, "duration": req.duration, "source_url": SOURCES[req.source].page_url(req.id),
                "audio": audio, "cover": cover, "lyrics": lyrics_file}
        (tmp / "metadata.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

        shutil.rmtree(target, ignore_errors=True)
        os.replace(tmp, target)
        library.add(source=req.source, track_id=req.id, title=req.title, artist=req.artist, album=req.album,
                    duration=req.duration, folder=name, audio=audio, cover=cover, lyrics=lyrics_file,
                    size=(target / audio).stat().st_size, created_at=time.time(), root=str(target.parent))
        job.update(state="done", progress=100)
    except HTTPException as exc:
        job.update(state="error", error=str(exc.detail))
    except Exception:
        log.exception("Загрузка %s не удалась", key)
        job.update(state="error", error="Не удалось скачать трек. Попробуйте ещё раз.")
    finally:
        job["finished_at"] = time.time()
        shutil.rmtree(tmp, ignore_errors=True)


def cleanup_partial() -> None:
    """Убирает недокачанные папки *.part (остаются, если приложение закрыли во время загрузки)."""
    for p in paths.downloads_dir().glob("*.part"):
        shutil.rmtree(p, ignore_errors=True)


def _check(source: str, track_id: str) -> None:
    cfg = SOURCES.get(source)
    if cfg is None or not cfg.id_re.fullmatch(track_id):
        raise HTTPException(400, "Некорректный трек")


@router.post("/downloads", status_code=202)
async def start_download(req: DownloadRequest, request: Request):
    _check(req.source, req.id)
    key = f"{req.source}:{req.id}"
    if library.get(req.source, req.id):
        return {"state": "done", "progress": 100}
    if key in jobs and jobs[key]["state"] in ACTIVE:
        return jobs[key]
    jobs[key] = {"state": "preparing", "progress": 0, "error": None}
    task = asyncio.create_task(_run(request.app.state.http, request.app.state.resolver, req))
    _background.add(task)
    task.add_done_callback(_background.discard)
    return jobs[key]


@router.get("/downloads/jobs")
def list_jobs():
    now = time.time()
    for k in [k for k, j in jobs.items() if j.get("finished_at") and now - j["finished_at"] > 60]:
        del jobs[k]                      # завершённые задачи UI успевает прочитать за минуту
    return jobs


@router.get("/downloads")
def list_downloads():
    return {"items": [library.to_track(r) for r in library.list_items()], "dir": str(paths.downloads_dir())}


@router.delete("/downloads/{source}/{track_id}")
def remove_download(source: str, track_id: str):
    _check(source, track_id)
    try:
        library.delete(source, track_id)
    except library.FolderKept:
        # Запись указывала не на папку, созданную Wave для этого трека: её файлы не трогаем (N3).
        # Путь в ответ не отдаём, он остаётся в журнале.
        return {"ok": True, "files_kept": True}
    except OSError as exc:
        log.warning("Удаление %s:%s не удалось: %r", source, track_id, exc)
        raise HTTPException(500, "Не удалось удалить локальную копию. Попробуйте ещё раз.") from exc
    return {"ok": True}


@router.get("/local/{source}/{track_id}/cover")
def local_cover(source: str, track_id: str):
    _check(source, track_id)
    p = library.cover_path(source, track_id)
    if not p:
        raise HTTPException(404, "Обложки нет")
    return FileResponse(p)