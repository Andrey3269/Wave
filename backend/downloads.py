"""
Загрузки: трек, обложка, текст и metadata.json сохраняются в отдельную папку.

  Downloads/Wave/Artist - Title [yt-ID]/
      audio.m4a | audio.mp3   (формат потока источника как есть, ffmpeg не нужен; HLS-поток — проверенные и
                               собранные сегменты: mp3-HLS -> .mp3, fMP4-HLS -> .m4a, S8)
      cover.jpg
      lyrics.json             (только если текст найден)
      metadata.json

Ход загрузки (состояние задачи в /api/downloads/jobs):
  queued       — S11: ждёт свободного места, одновременно выполняется не больше MAX_PARALLEL загрузок;
  preparing    — штатное получение потока тем же резолвером, что и для воспроизведения (download_policy);
  downloading  — аудио порциями с Range, progress = доля от размера (received/total в байтах);
  processing   — обложка и текст (их отсутствие или сбой загрузку не отменяет), metadata.json;
  done         — папка перенесена на место, запись в SQLite (library.py), трек в «Скачанных»;
  error        — error: текст для пользователя, code: машинный код причины (как X-Wave-Error);
  cancelled    — S11: отменена пользователем (code="cancelled") или остановкой приложения (code="shutdown");
                 это не ошибка источника: error=None, трек можно скачать снова.

Папка собирается во временном каталоге *.part и переименовывается целиком, поэтому
после сбоя или отмены не остаётся полузагруженных треков. Состояние загрузок живёт в памяти
(UI опрашивает /api/downloads/jobs), итог записывается в SQLite (library.py).

S11 (остаток M8): задачами управляет DownloadManager (app.state.downloads, создаётся и закрывается в lifespan):
лимит параллельности, очередь, отмена (DELETE /api/downloads/jobs/{source}/{id}), запись файлов вне event loop
(download_io), остановка всех задач при выходе до закрытия HTTP-клиентов.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

import download_policy
import library
import paths
from download_io import ThreadFile, clear_part_dir, remove_partial, run_in_thread
from lyrics import fetch_lyrics
from streaming import hls_download
from streaming.errors import ERROR_HEADER
from streaming.proxy import _connect
from streaming.ranges import CONTENT_RANGE_RE
from streaming.resolver import HLS, SOURCES

log = logging.getLogger("musicmeta.downloads")
router = APIRouter(prefix="/api", tags=["downloads"])

DL_CHUNK = 4 * 1024 * 1024                    # скачиваем порциями по 4 МБ (источники режут длинные запросы)
WRITE_BATCH = 1024 * 1024                     # S11: на диск — пачками до 1 МБ (одна операция в потоке на пачку)
MAX_BREAKS = 3                                # обрывов подряд без новых байтов, после которых загрузка — ошибка
MAX_TOTAL_BREAKS = 20                         # и всего обрывов за одну загрузку
# S11: одновременно выполняемых загрузок (от получения потока до записи в индекс). Значение из согласованного
# TARGET_ARCHITECTURE §9: каждая загрузка держит соединение с CDN и резолв yt-dlp, а источники ограничивают частоту
# запросов (rate_limited, bot_check); две параллельные загрузки уже заполняют обычный домашний канал.
MAX_PARALLEL = 2
CANCEL_WAIT = 15.0                            # S11: сколько запрос отмены ждёт, пока задача освободит ресурсы
SHUTDOWN_WAIT = 15.0                          # S11: сколько выход из lifespan ждёт остановки загрузок
ACTIVE = {"queued", "preparing", "downloading", "processing"}
COVER_HOSTS = ("googleusercontent.com", "ytimg.com", "ggpht.com", "sndcdn.com")   # откуда разрешено брать обложки
EXT = {"mp4": ".m4a", "mpeg": ".mp3", "webm": ".webm", "ogg": ".ogg"}
jobs: dict[str, dict] = {}


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


# ---------------------------------------------------------------------------
# Лимит параллельности (S11)
# ---------------------------------------------------------------------------
class _Slots:
    """
    Счётчик мест с очередью ожидания по порядку (FIFO). В отличие от asyncio.Semaphore, место можно занять
    синхронно (`try_take`) — тогда POST сразу честно отвечает «preparing» или «queued».

    Место передаётся ожидающему напрямую (`release` не уменьшает счётчик, если в очереди кто-то есть), поэтому
    новая задача не может «перехватить» место у ждущей. Отменённый ожидающий место не теряет и не занимает.
    """

    def __init__(self, limit: int):
        self.limit = limit
        self.busy = 0
        self.peak = 0                                       # наибольшее число занятых мест (для диагностики/тестов)
        self._waiters: deque[asyncio.Future] = deque()

    def _taken(self) -> None:
        self.peak = max(self.peak, self.busy)

    def try_take(self) -> bool:
        if self.busy < self.limit and not any(not w.done() for w in self._waiters):
            self.busy += 1
            self._taken()
            return True
        return False

    async def take(self) -> None:
        if self.try_take():
            return
        fut = asyncio.get_running_loop().create_future()
        self._waiters.append(fut)
        try:
            await fut                                       # место передано нам (busy уже учтён)
        except asyncio.CancelledError:
            if fut.done() and not fut.cancelled():          # место успели передать перед отменой — отдаём дальше
                self.release()
            raise

    def release(self) -> None:
        while self._waiters:
            fut = self._waiters.popleft()
            if not fut.done():
                fut.set_result(None)                        # передача места: busy не меняется
                return
        self.busy -= 1


@dataclass
class _Entry:
    task: asyncio.Task | None = None
    holds_slot: bool = False
    cancel_code: str = "cancelled"


class DownloadManager:
    """
    Единственный механизм запуска загрузок (S11). Создаётся в lifespan (объекты asyncio принадлежат циклу этого
    запуска, находка M1) и закрывается там же — раньше, чем HTTP-клиенты, которыми пользуются загрузки.

    * Не больше `limit` задач одновременно проходят этапы preparing → … → done; остальные ждут в состоянии queued.
      Место освобождается, когда задача закончилась любым образом: успех, ошибка, отмена (в том числе до старта).
    * Повторный запрос того же трека, пока его задача жива, возвращает ту же задачу.
    * Отмена дожидается, пока задача закроет файл, удалит свою папку `*.part` и освободит место; запись на диск,
      уже идущая в потоке, доводится до конца (download_io.run_in_thread), новая не начинается.
    * Фиксация результата (перенос папки на место и запись индекса) не прерывается: отмена, пришедшая в этот
      момент, получает итог done — трек уже сохранён (предсказуемый исход гонки «отмена против готовности»).
    """

    def __init__(self, limit: int = MAX_PARALLEL):
        self.loop = asyncio.get_running_loop()
        self.slots = _Slots(limit)
        self.closing = False
        self._entries: dict[str, _Entry] = {}

    def alive(self, key: str) -> bool:
        e = self._entries.get(key)
        return e is not None and e.task is not None and not e.task.done()

    def start(self, req: DownloadRequest, client, rs, hls_client) -> dict:
        key = f"{req.source}:{req.id}"
        if self.alive(key):
            return jobs[key]
        if self.closing:
            raise download_policy.refuse(503, "shutting_down", "Приложение закрывается — загрузка не начата.")
        entry = _Entry(holds_slot=self.slots.try_take())
        job = {"state": "preparing" if entry.holds_slot else "queued", "progress": 0, "error": None, "code": None,
               "received": 0, "total": None}
        jobs[key] = job
        entry.task = asyncio.create_task(self._job(key, job, entry, req, client, rs, hls_client))
        entry.task.add_done_callback(lambda t: self._finished(key, job, entry, t))
        self._entries[key] = entry
        return job

    async def _job(self, key, job, entry: _Entry, req, client, rs, hls_client) -> None:
        if not entry.holds_slot:
            await self.slots.take()                      # отмена здесь: место не занято, папок ещё нет
            entry.holds_slot = True
            job["state"] = "preparing"
        await _run(client, rs, req, hls_client, job, entry)

    def _finished(self, key: str, job: dict, entry: _Entry, task: asyncio.Task) -> None:
        if entry.holds_slot:
            entry.holds_slot = False
            self.slots.release()
        if self._entries.get(key) is entry:
            del self._entries[key]
        exc = None if task.cancelled() else task.exception()
        if job["state"] in ACTIVE:                       # задача кончилась, не записав итог: отменена до старта или в очереди
            if exc is None:
                job.update(state="cancelled", error=None, code=entry.cancel_code)
            else:
                log.error("Загрузка %s прервалась: %r", key, exc)
                job.update(state="error", error="Не удалось скачать трек. Попробуйте ещё раз.", code="internal")
            job["finished_at"] = time.time()
        elif exc is not None:
            log.error("Загрузка %s: ошибка после завершения: %r", key, exc)

    async def cancel(self, key: str, code: str = "cancelled") -> dict | None:
        """
        Отменяет задачу и ждёт, пока она освободит ресурсы. Возвращает итог задачи: cancelled; done, если трек
        уже сохраняется (точка фиксации пройдена) или сохранён; error — если задача успела закончиться ошибкой.
        None — задачи нет (никогда не было или итог уже убран из списка).
        """
        entry = self._entries.get(key)
        if entry is not None and entry.task is not None and not entry.task.done():
            entry.cancel_code = code
            entry.task.cancel()
            done, _ = await asyncio.wait({entry.task}, timeout=CANCEL_WAIT)
            if not done:
                log.warning("Загрузка %s не остановилась за %.0f с после отмены", key, CANCEL_WAIT)
        return jobs.get(key)

    async def aclose(self) -> None:
        """Выход из lifespan: новые загрузки не принимаются, текущие отменяются и дожидаются (с очисткой `.part`)."""
        self.closing = True
        tasks = []
        for e in self._entries.values():
            if e.task is not None and not e.task.done():
                e.cancel_code = "shutdown"
                e.task.cancel()
                tasks.append(e.task)
        if tasks:
            done, pending = await asyncio.wait(tasks, timeout=SHUTDOWN_WAIT)
            if pending:
                log.warning("При остановке не завершились загрузки: %d", len(pending))
            log.info("Остановка: прервано загрузок — %d", len(done))


def manager(request: Request) -> DownloadManager:
    return request.app.state.downloads


# ---------------------------------------------------------------------------
# Сама загрузка
# ---------------------------------------------------------------------------
async def _download_audio(client, rs, req: DownloadRequest, out: ThreadFile, job: dict) -> str:
    """
    Качает аудио порциями с Range в out; возвращает расширение файла.

    S8 (N2): байты пишутся по мере получения (без накопления кусков по 64 КБ), а обрыв источника посередине
    порции не губит загрузку: полученное сохраняется, следующая порция запрашивается с первого недостающего
    байта. Подряд допускается не больше MAX_BREAKS обрывов без единого нового байта — дальше ошибка.
    S11: запись — в потоке (out), пачками до WRITE_BATCH; всё полученное до обрыва записывается до следующего
    запроса, поэтому продолжение с первого недостающего байта не теряет и не дублирует данные.
    """
    pos, total, ext = 0, None, ".m4a"
    breaks = total_breaks = 0
    buf = bytearray()
    while total is None or pos < total:
        up = await _connect(client, rs, req.source, req.id, f"bytes={pos}-{pos + DL_CHUNK - 1}")
        got, whole, broken = 0, False, False
        try:
            if up.status_code == 206:
                m = CONTENT_RANGE_RE.fullmatch(up.headers.get("content-range", ""))
                if m and int(m.group(1)) != pos:
                    raise RuntimeError(f"Источник вернул не тот диапазон ({m.group(1)} вместо {pos})")
                total = int(m.group(3)) if m and m.group(3) != "*" else None
            elif up.status_code == 200:                 # источник проигнорировал Range: файл целиком
                if pos:                                 # продолжить с середины нельзя — файл испортился бы
                    raise RuntimeError("Источник перестал поддерживать Range посреди загрузки")
                total, whole = int(up.headers.get("content-length") or 0) or None, True
            else:
                raise RuntimeError(f"Источник вернул HTTP {up.status_code}")
            if pos == 0:
                ext = _ext(up.headers.get("content-type", ""))
            job["total"] = total
            try:
                async for chunk in up.aiter_raw():
                    buf += chunk
                    got += len(chunk)
                    job["received"] = pos + got
                    if total:
                        job["progress"] = min(99, int((pos + got) * 100 / total))
                    if len(buf) >= WRITE_BATCH:
                        await out.write(bytes(buf))
                        buf.clear()
            except httpx.HTTPError as exc:
                if whole:                               # без Range продолжить нельзя
                    raise
                broken = True
                log.info("Источник оборвал порцию загрузки %s:%s на %d байте: %s",
                         req.source, req.id, pos + got, type(exc).__name__)
        finally:
            await up.aclose()
        await out.write(bytes(buf))                     # всё полученное — на диск до следующего запроса
        buf.clear()
        pos += got
        if broken:
            breaks = 0 if got else breaks + 1
            total_breaks += 1
            if breaks >= MAX_BREAKS or total_breaks > MAX_TOTAL_BREAKS:
                raise RuntimeError("Источник раз за разом обрывает соединение")
            continue
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
        await run_in_thread((folder / name).write_bytes, r.content)
        return name
    except Exception as exc:
        log.info("Обложка не скачана: %r", exc)
        return None


async def _fetch_lyrics_safe(client, req: DownloadRequest) -> dict | None:
    """Текст необязателен: любой сбой сервиса текстов загрузку не отменяет."""
    try:
        return await fetch_lyrics(client, req.artist, req.title, req.duration)
    except Exception as exc:
        log.info("Текст для загрузки не получен: %r", exc)
        return None


def _fresh_dir(tmp: Path) -> None:
    """Пустая временная папка этой загрузки (в потоке). Остаток прошлого запуска с тем же именем удаляется."""
    if os.path.lexists(tmp) and not clear_part_dir(tmp):
        raise OSError(f"временная папка {tmp.name} занята")
    tmp.mkdir(parents=True)


def _write_text(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


class _Commit:
    """
    Точка фиксации (S11): старая неиндексированная папка трека убирается, временная переносится на место,
    в индекс пишется запись. Выполняется в потоке одной операцией, поэтому отмена не может разделить перенос
    папки и запись индекса. `done` — фиксация прошла целиком.
    """

    def __init__(self, tmp: Path, target: Path, row: dict):
        self.tmp, self.target, self.row = tmp, target, row
        self.done = False

    def run(self) -> None:
        shutil.rmtree(self.target, ignore_errors=True)
        os.replace(self.tmp, self.target)
        size = (self.target / self.row["audio"]).stat().st_size
        library.add(**self.row, size=size, created_at=time.time(), root=str(self.target.parent))
        self.done = True


async def _run(client, rs, req: DownloadRequest, hls_client, job: dict, entry: _Entry | None = None) -> None:
    key = f"{req.source}:{req.id}"
    name = library.safe_folder(req.artist, req.title, req.source, req.id)
    target = library.folder_path(name)
    tmp = target.with_name(target.name + ".part")
    out: ThreadFile | None = None
    outcome = {"state": "error", "error": "Не удалось скачать трек. Попробуйте ещё раз.", "code": "internal"}
    try:
        # preparing: поток получен штатно (тем же резолвером, что и для воспроизведения) и это не фрагмент
        res = await download_policy.check(rs, req.source, req.id)

        await run_in_thread(_fresh_dir, tmp)
        out = ThreadFile(tmp / "audio.part")
        await out.open()

        job["state"] = "downloading"
        if res.kind == HLS:                                  # S8: тот же HLS-поток, что играет плеер
            ext = await hls_download.download(hls_client, rs, req.source, req.id, out, job)
        else:
            ext = await _download_audio(client, rs, req, out, job)
        await out.close()
        audio = f"audio{ext}"
        await run_in_thread(os.replace, tmp / "audio.part", tmp / audio)

        job.update(state="processing", progress=99)
        cover = await _download_cover(client, req.cover, tmp)
        lyr = await _fetch_lyrics_safe(client, req)
        lyrics_file = None
        if lyr and (lyr["synced"] or lyr["plain"]):           # пустой файл «для вида» не создаём
            await run_in_thread(_write_text, tmp / "lyrics.json", json.dumps(lyr, ensure_ascii=False))
            lyrics_file = "lyrics.json"

        meta = {"id": req.id, "source": req.source, "title": req.title, "artist": req.artist,
                "album": req.album, "duration": req.duration, "source_url": SOURCES[req.source].page_url(req.id),
                "audio": audio, "cover": cover, "lyrics": lyrics_file}
        await run_in_thread(_write_text, tmp / "metadata.json", json.dumps(meta, ensure_ascii=False, indent=2))

        commit = _Commit(tmp, target, dict(source=req.source, track_id=req.id, title=req.title, artist=req.artist,
                                           album=req.album, duration=req.duration, folder=name, audio=audio,
                                           cover=cover, lyrics=lyrics_file))
        try:
            await run_in_thread(commit.run)
        except asyncio.CancelledError:
            if not commit.done:
                raise
            log.info("Отмена загрузки %s пришла во время сохранения: трек уже сохранён", key)
        outcome = {"state": "done", "progress": 100}
    except asyncio.CancelledError:
        # Отмена пользователем или остановка приложения: не ошибка источника. Задача заканчивается штатно,
        # итог записывается ниже — после того как временная папка удалена.
        outcome = {"state": "cancelled", "error": None, "code": entry.cancel_code if entry else "cancelled"}
        log.info("Загрузка %s отменена (%s)", key, outcome["code"])
    except HTTPException as exc:
        outcome = {"state": "error", "error": str(exc.detail), "code": (exc.headers or {}).get(ERROR_HEADER)}
    except Exception:
        log.exception("Загрузка %s не удалась", key)
    finally:
        # Файл закрывается и папка .part удаляется одной операцией в потоке — уже после того, как закончилась любая
        # запись в неё. Итог задачи публикуется последним: пока он «активный», повтор того же трека невозможен,
        # и новая загрузка не начнёт писать в папку, которую эта ещё удаляет.
        try:
            await run_in_thread(remove_partial, tmp, out)
        except asyncio.CancelledError:
            pass                                             # повторная отмена: удаление уже закончилось (run_in_thread)
        except Exception as exc:
            log.warning("Временная папка загрузки %s не очищена: %r", key, exc)
        job.update(outcome, finished_at=time.time())


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
    if library.get(req.source, req.id):
        return {"state": "done", "progress": 100}
    # HLS-путь — только клиентом с закреплением проверенных IP (netguard.make_client), без запасного варианта
    return manager(request).start(req, request.app.state.http, request.app.state.resolver, request.app.state.hls_http)


@router.delete("/downloads/jobs/{source}/{track_id}")
async def cancel_download(source: str, track_id: str, request: Request):
    """
    S11: отмена загрузки. Ответ — итог задачи после отмены (cancelled; done, если трек уже успел сохраниться;
    error — если задача закончилась ошибкой раньше). Повторная отмена возвращает тот же итог. 404 — такой задачи нет.
    Удаление уже скачанного трека — прежний DELETE /api/downloads/{source}/{id}.
    """
    _check(source, track_id)
    job = await manager(request).cancel(f"{source}:{track_id}")
    if job is None:
        raise download_policy.refuse(404, "no_job", "Этой загрузки уже нет.")
    return job


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
