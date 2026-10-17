"""
Локальная музыка (S15): аудиофайлы, которые пользователь импортировал в Wave, — источник `local`.

Как это устроено (минимально сложная безопасная интеграция с текущим плеером):

* Импорт — копия файла. Интерфейс выбирает файл обычным окном выбора (<input type="file">, в WebView2 — системный
  диалог) и отправляет его байты: POST /api/library/local?name=<имя файла>&duration=<секунды>. Пути к файлам на
  компьютере пользователя API не принимает вообще, поэтому нечего проверять на обход каталогов: backend сам пишет
  копию в <data>/local/<id><ext>, где id — 16 случайных hex-символов, ext — из белого списка.
* Имя файла клиента используется только как подпись: из него берутся название (и исполнитель, если имя вида
  «Исполнитель - Название») и расширение. Каталоги в имени отбрасываются.
* Содержимое проверяется по сигнатуре формата (ID3/MPEG, ftyp, OggS, fLaC, RIFF/WAVE, EBML): расширение должно
  совпадать с форматом, иначе 415. Размер — не больше MAX_BYTES (413). Пустой файл — 400.
* Повторный импорт того же файла (по SHA-256) возвращает уже импортированный трек, вторую копию не создаёт.
* Воспроизведение — тем же адресом, что у всех треков: /api/stream/local/{id} (Range — Starlette FileResponse,
  как у скачанных треков); /playback отвечает kind = local. Путь к файлу из базы не читается: только id и
  расширение, оба проверяются при каждом обращении.
* Удаление трека убирает его копию с диска и из избранного и плейлистов (одной транзакцией); история не меняется.

Метаданные из тегов файла не читаются (новая зависимость для этого не добавлялась): длительность сообщает браузер,
обложки у локальных треков нет.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import secrets
import sqlite3
import time
from pathlib import Path

import anyio
from fastapi import APIRouter, HTTPException, Query, Request
from starlette.requests import ClientDisconnect

import paths
from domain.models import SOURCE_LOCAL, TRACK_ID_RE
from domain.snapshot import stream_url
from persistence import db

log = logging.getLogger("musicmeta.local")
router = APIRouter(prefix="/api/library/local", tags=["library"])

MAX_BYTES = 1024 * 1024 * 1024          # 1 ГБ: час несжатого WAV — около 600 МБ
NAME_MAX = 255
TITLE_MAX = 200
UNKNOWN_ARTIST = "Неизвестный исполнитель"
ID_RE = TRACK_ID_RE[SOURCE_LOCAL]
_CHUNK_FLUSH = 1024 * 1024               # пишем на диск порциями по 1 МБ (в потоке, не в event loop)
_HEAD = 64

# Формат → допустимые расширения. Сигнатуры — в sniff().
FORMATS: dict[str, frozenset[str]] = {
    "mp3": frozenset({".mp3"}),
    "mp4": frozenset({".m4a"}),
    "ogg": frozenset({".ogg", ".oga", ".opus"}),
    "flac": frozenset({".flac"}),
    "wav": frozenset({".wav"}),
    "webm": frozenset({".webm", ".weba"}),
}
EXTENSIONS = frozenset().union(*FORMATS.values())
ACCEPT = ",".join(sorted(EXTENSIONS))     # для подсказки в ошибке
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_PART_RE = re.compile(r"^\.import-[0-9a-f]{16}\.part$")
_FILE_RE = re.compile(r"^([0-9a-f]{16})(\.[a-z0-9]{2,5})$")


def sniff(head: bytes) -> str | None:
    """Формат по первым байтам файла или None. Только аудиоформаты, которые играет <audio> в Chromium/WebView2."""
    if head[:3] == b"ID3":
        return "mp3"
    if len(head) >= 2 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0:
        version, layer = (head[1] >> 3) & 3, (head[1] >> 1) & 3
        if version != 1 and layer != 0:          # 01 — зарезервированная версия; layer 00 — это ADTS AAC, не MPEG
            return "mp3"
        return None
    if head[4:8] == b"ftyp":
        return "mp4"
    if head[:4] == b"OggS":
        return "ogg"
    if head[:4] == b"fLaC":
        return "flac"
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "wav"
    if head[:4] == b"\x1aE\xdf\xa3":
        return "webm"
    return None


def parse_name(name: str) -> tuple[str, str, str]:
    """
    (название, исполнитель, расширение) из имени файла клиента. Каталоги (C:\\..., ../) отбрасываются: имя — только
    подпись. «Исполнитель - Название.mp3» → исполнитель и название; иначе исполнитель неизвестен.
    """
    if not isinstance(name, str) or _CONTROL.search(name):
        raise HTTPException(400, "Недопустимое имя файла.")
    base = re.split(r"[\\/]", name)[-1].strip()
    stem, dot, ext = base.rpartition(".")
    ext = f".{ext.lower()}" if dot else ""
    if ext not in EXTENSIONS:
        raise HTTPException(415, f"Этот формат не поддерживается. Можно импортировать: {ACCEPT.replace(',', ', ')}.")
    stem = " ".join(stem.split())
    artist, sep, title = stem.partition(" - ")
    if not sep or not artist.strip() or not title.strip():
        artist, title = UNKNOWN_ARTIST, stem
    title = title.strip()[:TITLE_MAX] or "Без названия"
    return title, artist.strip()[:TITLE_MAX], ext


def track_of(row) -> dict:
    """Строка local_tracks → объект Track (те же поля, что у треков YouTube Music и SoundCloud)."""
    return {"id": row["id"], "source": SOURCE_LOCAL, "title": row["title"], "artist": row["artist"],
            "artists": [row["artist"]], "album": row["album"], "album_id": None, "duration": row["duration"],
            "cover": None, "explicit": False, "stream_url": stream_url(SOURCE_LOCAL, row["id"]), "artist_id": None,
            "artist_ids": [], "plays": None, "released": None, "url": None}


def _dir() -> Path:
    return paths.local_music_dir()


def exists(track_id: str) -> bool:
    return bool(ID_RE.fullmatch(track_id or "")) and bool(db.query("SELECT 1 FROM local_tracks WHERE id = ?", (track_id,)))


def audio_path(track_id: str) -> Path | None:
    """
    Файл локального трека или None (нет такого трека, файл удалён вручную). Имя строится из id и расширения:
    оба проверяются заново — строке базы не доверяем, путь из неё не берётся.
    """
    if not isinstance(track_id, str) or not ID_RE.fullmatch(track_id):
        return None
    rows = db.query("SELECT ext FROM local_tracks WHERE id = ?", (track_id,))
    if not rows or rows[0]["ext"] not in EXTENSIONS:
        return None
    root = _dir()
    p = root / f"{track_id}{rows[0]['ext']}"
    try:
        if p.is_symlink() or not p.is_file() or p.resolve().parent != root.resolve():
            return None
    except OSError:
        return None
    return p


def init() -> None:
    """После library.init(): убирает оборванные импорты (.part) и копии, которых нет в базе (удаление прервалось)."""
    root = _dir()
    known = {r["id"] for r in db.query("SELECT id FROM local_tracks")}
    for p in root.iterdir():
        try:
            m = _FILE_RE.match(p.name)
            orphan = m is not None and m.group(1) not in known and m.group(2) in EXTENSIONS
            if (orphan or _PART_RE.match(p.name)) and p.is_file() and not p.is_symlink():
                p.unlink()
                log.info("Локальная музыка: удалён лишний файл %s", p.name)
        except OSError as exc:
            log.warning("Локальная музыка: файл %s не удалён: %r", p.name, exc)


# ---------------------------------------------------------------------------
@router.get("")
def list_local():
    rows = db.query("SELECT * FROM local_tracks ORDER BY created_at DESC, rowid DESC")
    return {"items": [track_of(r) for r in rows]}


async def _receive(request: Request, part: Path) -> tuple[int, str, bytes]:
    """Тело запроса → файл part. Возвращает (размер, sha256, первые байты). Больше MAX_BYTES — 413."""
    digest, size, head, buf = hashlib.sha256(), 0, b"", bytearray()
    f = await anyio.to_thread.run_sync(lambda: open(part, "xb"))      # noqa: SIM115 — закрывается ниже
    try:
        async for chunk in request.stream():
            if not chunk:
                continue
            size += len(chunk)
            if size > MAX_BYTES:
                raise HTTPException(413, f"Файл больше {MAX_BYTES // (1024 * 1024)} МБ.")
            if len(head) < _HEAD:
                head += chunk[:_HEAD - len(head)]
            digest.update(chunk)
            buf += chunk
            if len(buf) >= _CHUNK_FLUSH:
                data, buf = bytes(buf), bytearray()
                await anyio.to_thread.run_sync(f.write, data)
        if buf:
            await anyio.to_thread.run_sync(f.write, bytes(buf))
        await anyio.to_thread.run_sync(f.flush)
        await anyio.to_thread.run_sync(os.fsync, f.fileno())
    finally:
        await anyio.to_thread.run_sync(f.close)
    return size, digest.hexdigest(), head


def _existing(sha: str) -> dict | None:
    rows = db.query("SELECT * FROM local_tracks WHERE sha256 = ?", (sha,))
    return track_of(rows[0]) if rows else None


@router.post("")
async def import_local(request: Request, name: str = Query(..., min_length=1, max_length=NAME_MAX),
                       duration: int | None = Query(None, ge=1, le=86_400)):
    """
    Импорт одного аудиофайла: тело запроса — байты файла (application/octet-stream), name — имя файла (только для
    подписи и расширения), duration — длительность в секундах, если её определил браузер. Ответ:
    {"track": Track, "added": bool}; added = false — этот файл уже импортирован (тот же SHA-256).
    """
    declared = request.headers.get("content-length")
    if declared is not None and (not declared.isdigit() or int(declared) > MAX_BYTES):
        raise HTTPException(413, f"Файл больше {MAX_BYTES // (1024 * 1024)} МБ.")
    title, artist, ext = parse_name(name)
    root = _dir()
    part = root / f".import-{secrets.token_hex(8)}.part"
    target: Path | None = None
    try:
        try:
            size, sha, head = await _receive(request, part)
        except ClientDisconnect:
            raise HTTPException(400, "Передача файла прервана.") from None
        if size == 0:
            raise HTTPException(400, "Файл пустой.")
        fmt = sniff(head)
        if fmt is None or ext not in FORMATS[fmt]:
            raise HTTPException(415, "Содержимое файла не похоже на аудио этого формата.")
        if (found := _existing(sha)) is not None:
            return {"track": found, "added": False}
        tid = secrets.token_hex(8)
        target = root / f"{tid}{ext}"
        await anyio.to_thread.run_sync(os.replace, part, target)
        try:
            db.query("INSERT INTO local_tracks (id, title, artist, album, duration, ext, size, sha256, created_at)"
                     " VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?)", (tid, title, artist, duration, ext, size, sha, time.time()))
        except sqlite3.IntegrityError:          # тот же файл импортировали параллельно
            target.unlink(missing_ok=True)
            target = None
            found = _existing(sha)
            if found is None:
                raise
            return {"track": found, "added": False}
        target = None                            # файл принадлежит записи базы
        log.info("Локальная музыка: импортирован трек %s (%s, %d байт)", tid, ext, size)
        return {"track": track_of(db.query("SELECT * FROM local_tracks WHERE id = ?", (tid,))[0]), "added": True}
    finally:
        for leftover in (part, target):
            if leftover is not None:
                try:
                    leftover.unlink(missing_ok=True)
                except OSError as exc:
                    log.warning("Локальная музыка: временный файл %s не удалён: %r", leftover.name, exc)


@router.delete("/{track_id}")
def delete_local(track_id: str):
    """Убирает трек из Wave: запись, избранное и плейлисты — одной транзакцией, затем копию файла с диска."""
    if not ID_RE.fullmatch(track_id):
        raise HTTPException(400, "Некорректный идентификатор трека.")
    key = f"{SOURCE_LOCAL}:{track_id}"
    with db.transaction() as con:
        row = con.execute("SELECT ext FROM local_tracks WHERE id = ?", (track_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "Локальный трек не найден.")
        con.execute("DELETE FROM local_tracks WHERE id = ?", (track_id,))
        con.execute("DELETE FROM favorites WHERE key = ?", (key,))
        con.execute("DELETE FROM playlist_tracks WHERE key = ?", (key,))
    if row["ext"] in EXTENSIONS:
        try:
            (_dir() / f"{track_id}{row['ext']}").unlink(missing_ok=True)
        except OSError as exc:                   # файл занят: уберёт init() при следующем запуске
            log.warning("Локальная музыка: файл трека %s не удалён: %r", track_id, exc)
    return {"ok": True}
