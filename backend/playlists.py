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


def _exists(pid: int) -> None:
    if not library._q("SELECT 1 FROM playlists WHERE id = ?", (pid,)):
        raise HTTPException(404, "Плейлист не найден.")


def _touch(pid: int) -> None:
    library._q("UPDATE playlists SET updated_at = ? WHERE id = ?", (time.time(), pid))


@router.get("/playlists")
def list_playlists():
    rows = library._q(
        "SELECT p.id, p.name, p.updated_at, COUNT(t.id) AS n,"
        " (SELECT data FROM playlist_tracks WHERE playlist_id = p.id ORDER BY position LIMIT 1) AS first"
        " FROM playlists p LEFT JOIN playlist_tracks t ON t.playlist_id = p.id GROUP BY p.id ORDER BY p.updated_at DESC")
    return {"items": [{"id": r["id"], "name": r["name"], "count": r["n"],
                       "cover": json.loads(r["first"]).get("cover") if r["first"] else None} for r in rows]}


@router.post("/playlists", status_code=201)
def create_playlist(body: NameIn):
    name, now = _clean(body.name), time.time()
    with library.transaction() as db:
        pid = db.execute("INSERT INTO playlists (name, created_at, updated_at) VALUES (?, ?, ?)",
                         (name, now, now)).lastrowid
    return {"id": pid, "name": name, "count": 0, "cover": None}


@router.patch("/playlists/{pid}")
def rename_playlist(pid: int, body: NameIn):
    _exists(pid)
    library._q("UPDATE playlists SET name = ?, updated_at = ? WHERE id = ?", (_clean(body.name), time.time(), pid))
    return {"ok": True}


@router.delete("/playlists/{pid}")
def delete_playlist(pid: int):
    library._q("DELETE FROM playlist_tracks WHERE playlist_id = ?", (pid,))
    library._q("DELETE FROM playlists WHERE id = ?", (pid,))
    return {"ok": True}


@router.get("/playlists/{pid}")
def get_playlist(pid: int):
    rows = library._q("SELECT name FROM playlists WHERE id = ?", (pid,))
    if not rows:
        raise HTTPException(404, "Плейлист не найден.")
    tracks = [json.loads(r["data"]) for r in
              library._q("SELECT data FROM playlist_tracks WHERE playlist_id = ? ORDER BY position", (pid,))]
    return {"playlist": {"id": pid, "name": rows[0]["name"], "count": len(tracks),
                         "cover": next((t["cover"] for t in tracks if t.get("cover")), None)},
            "tracks": tracks}


@router.post("/playlists/{pid}/tracks")
def add_track(pid: int, t: userdata.TrackIn):
    _exists(pid)
    key = f"{t.source}:{t.id}"
    if library._q("SELECT 1 FROM playlist_tracks WHERE playlist_id = ? AND key = ?", (pid, key)):
        return {"added": False}                       # такой трек в плейлисте уже есть
    pos = library._q("SELECT COALESCE(MAX(position), -1) + 1 AS p FROM playlist_tracks WHERE playlist_id = ?", (pid,))[0]["p"]
    library._q("INSERT INTO playlist_tracks (playlist_id, position, key, data, added_at) VALUES (?, ?, ?, ?, ?)",
               (pid, pos, key, json.dumps(userdata._dump(t), ensure_ascii=False), time.time()))
    _touch(pid)
    return {"added": True}


@router.delete("/playlists/{pid}/tracks/{source}/{track_id}")
def remove_track(pid: int, source: str, track_id: str):
    library._q("DELETE FROM playlist_tracks WHERE playlist_id = ? AND key = ?", (pid, f"{source}:{track_id}"))
    _touch(pid)
    return {"ok": True}