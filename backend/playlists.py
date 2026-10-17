"""
Локальные плейлисты: создание, переименование, удаление, добавление и удаление треков.
Хранятся в wave.db рядом с избранным; треки лежат как снимок (название, артист, обложка, ссылка),
поэтому плейлист открывается мгновенно и не зависит от источника.
"""
from __future__ import annotations

import json
import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import library
import userdata
from domain import snapshot

router = APIRouter(prefix="/api", tags=["playlists"])
# Таблицы playlists и playlist_tracks создаёт persistence/migrations.py (версия схемы v1).


class NameIn(BaseModel):
    name: str


def init() -> None:
    """Схему создаёт library.init() (миграции); отдельной инициализации плейлистам не нужно."""


def _clean(name: str) -> str:
    name = " ".join((name or "").split())[:80]
    if not name:
        raise HTTPException(400, "Введите название плейлиста.")
    return name


def _require(db, pid: int) -> None:
    """Плейлист есть (проверка внутри транзакции: он не исчезнет до конца операции), иначе 404."""
    if db.execute("SELECT 1 FROM playlists WHERE id = ?", (pid,)).fetchone() is None:
        raise HTTPException(404, "Плейлист не найден.")


@router.get("/playlists")
def list_playlists():
    rows = library._q(
        "SELECT p.id, p.name, p.updated_at, COUNT(t.id) AS n,"
        " (SELECT data FROM playlist_tracks WHERE playlist_id = p.id ORDER BY position LIMIT 1) AS first"
        " FROM playlists p LEFT JOIN playlist_tracks t ON t.playlist_id = p.id GROUP BY p.id ORDER BY p.updated_at DESC")
    return {"items": [{"id": r["id"], "name": r["name"], "count": r["n"], "cover": _first_cover(r["first"])} for r in rows]}


def _first_cover(raw: str | None) -> str | None:
    """Обложка первого трека плейлиста — только проверенная (S13: старые строки базы не доверенные)."""
    t = snapshot.read_snapshot(raw, "playlist_tracks") if raw else None
    return t.get("cover") if t else None


@router.post("/playlists", status_code=201)
def create_playlist(body: NameIn):
    name, now = _clean(body.name), time.time()
    with library.transaction() as db:
        pid = db.execute("INSERT INTO playlists (name, created_at, updated_at) VALUES (?, ?, ?)",
                         (name, now, now)).lastrowid
    return {"id": pid, "name": name, "count": 0, "cover": None}


@router.patch("/playlists/{pid}")
def rename_playlist(pid: int, body: NameIn):
    with library.transaction() as db:
        _require(db, pid)                            # как раньше: сначала 404, потом проверка имени
        db.execute("UPDATE playlists SET name = ?, updated_at = ? WHERE id = ?", (_clean(body.name), time.time(), pid))
    return {"ok": True}


@router.delete("/playlists/{pid}")
def delete_playlist(pid: int):
    with library.transaction() as db:                 # треки и сам плейлист — вместе или никак
        db.execute("DELETE FROM playlist_tracks WHERE playlist_id = ?", (pid,))
        db.execute("DELETE FROM playlists WHERE id = ?", (pid,))
    return {"ok": True}


@router.get("/playlists/{pid}")
def get_playlist(pid: int):
    rows = library._q("SELECT name FROM playlists WHERE id = ?", (pid,))
    if not rows:
        raise HTTPException(404, "Плейлист не найден.")
    stored = library._q("SELECT key, data FROM playlist_tracks WHERE playlist_id = ? ORDER BY position", (pid,))
    tracks = userdata._snapshots(stored, "playlist_tracks")     # недопустимые строки пропускаются (S13)
    return {"playlist": {"id": pid, "name": rows[0]["name"], "count": len(tracks),
                         "cover": next((t["cover"] for t in tracks if t.get("cover")), None)},
            "tracks": tracks}


@router.post("/playlists/{pid}/tracks")
def add_track(pid: int, t: userdata.TrackIn):
    """
    Проверка плейлиста, позиция, вставка и updated_at — одна транзакция (находка M5). Дубль отсекает
    UNIQUE (playlist_id, key) через INSERT OR IGNORE: повторное и одновременное добавление одного трека
    дают {"added": false}, а не IntegrityError и HTTP 500. Порядок: новый трек всегда в конец.
    """
    key = f"{t.source}:{t.id}"
    data = json.dumps(userdata._dump(t), ensure_ascii=False)
    with library.transaction() as db:
        _require(db, pid)
        cur = db.execute(
            "INSERT OR IGNORE INTO playlist_tracks (playlist_id, position, key, data, added_at)"
            " SELECT ?, COALESCE(MAX(position), -1) + 1, ?, ?, ? FROM playlist_tracks WHERE playlist_id = ?",
            (pid, key, data, time.time(), pid))
        if cur.rowcount == 0:
            return {"added": False}                   # такой трек в плейлисте уже есть
        db.execute("UPDATE playlists SET updated_at = ? WHERE id = ?", (time.time(), pid))
    return {"added": True}


# `:path` — как у избранного: трек с «/» в id (сохранённый до проверки формата) тоже удаляется.
@router.delete("/playlists/{pid}/tracks/{source}/{track_id:path}")
def remove_track(pid: int, source: str, track_id: str):
    with library.transaction() as db:                 # удаление и updated_at — вместе
        db.execute("DELETE FROM playlist_tracks WHERE playlist_id = ? AND key = ?", (pid, f"{source}:{track_id}"))
        db.execute("UPDATE playlists SET updated_at = ? WHERE id = ?", (time.time(), pid))
    return {"ok": True}