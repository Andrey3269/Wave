"""
Пользовательские данные: избранное, история прослушиваний, настройки.
Всё хранится локально в wave.db (рядом с индексом загрузок) и никуда не отправляется.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Callable, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

import library
import paths

log = logging.getLogger("musicmeta.user")
router = APIRouter(prefix="/api", tags=["user"])

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS favorites (key TEXT PRIMARY KEY, data TEXT NOT NULL, added_at REAL NOT NULL)",
    "CREATE TABLE IF NOT EXISTS history (id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT NOT NULL,"
    " data TEXT NOT NULL, played_at REAL NOT NULL, skipped INTEGER NOT NULL DEFAULT 0)",
    "CREATE INDEX IF NOT EXISTS history_key ON history(key)",
    "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
)
_cache_clearers: list[Callable[[], None]] = []


def register_cache_clear(fn: Callable[[], None]) -> None:
    _cache_clearers.append(fn)


class TrackIn(BaseModel):
    id: str
    source: Literal["yt", "sc"]
    title: str
    artist: str
    artists: list[str] = []
    album: str | None = None
    duration: int | None = None
    cover: str | None = None
    stream_url: str
    artist_id: str | None = None
    artist_ids: list[str | None] = []
    url: str | None = None


class KeyIn(BaseModel):
    source: Literal["yt", "sc"]
    id: str


def _dump(t: TrackIn) -> dict:
    return t.model_dump() if hasattr(t, "model_dump") else t.dict()


def _q(sql: str, args: tuple = ()):
    return library._q(sql, args)


# ---------- настройки ----------
DEFAULTS = {"default_source": "yt", "autoplay": "1", "theme": "auto"}
ALLOWED = {"default_source": {"yt", "sc"}, "autoplay": {"0", "1"}, "theme": {"auto", "dark", "light"}}


def get_setting(key: str, default: str | None = None) -> str | None:
    rows = _q("SELECT value FROM settings WHERE key = ?", (key,))
    return rows[0]["value"] if rows else DEFAULTS.get(key, default)


def init() -> None:
    for sql in _SCHEMA:
        _q(sql)
    saved = get_setting("downloads_dir")
    if saved:                                   # возвращаем выбранную пользователем папку загрузок
        try:
            paths.set_downloads_dir(Path(saved))
        except OSError as exc:
            log.warning("Папка загрузок недоступна: %r", exc)


def _public() -> dict:
    return {
        "values": {"default_source": get_setting("default_source"), "autoplay": get_setting("autoplay") == "1",
                   "theme": get_setting("theme")},
        "paths": {"downloads": str(paths.downloads_dir()), "data": str(paths.data_dir()),
                  "downloads_locked": bool(os.getenv("WAVE_DOWNLOADS_DIR"))},
    }


@router.get("/settings")
def read_settings():
    return _public()


@router.put("/settings")
def write_settings(body: dict[str, str | bool]):
    for key, value in body.items():
        if key == "downloads_dir":
            if os.getenv("WAVE_DOWNLOADS_DIR"):
                raise HTTPException(400, "Папка загрузок задана переменной окружения WAVE_DOWNLOADS_DIR.")
            p = Path(str(value)).expanduser()
            if not p.is_absolute():
                raise HTTPException(400, "Укажите полный путь, например C:\\Users\\Имя\\Music\\Wave")
            try:
                p.mkdir(parents=True, exist_ok=True)
                probe = p / ".wave_write_test"
                probe.write_text("x")
                probe.unlink()
            except OSError:
                raise HTTPException(400, "В эту папку нельзя записывать. Выберите другую.") from None
            paths.set_downloads_dir(p.resolve())
            _q("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(p.resolve())))
        elif key in ALLOWED:
            val = "1" if value is True else "0" if value is False else str(value)
            if val not in ALLOWED[key]:
                raise HTTPException(400, "Недопустимое значение настройки.")
            _q("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, val))
        else:
            raise HTTPException(400, "Неизвестная настройка.")
    return _public()


@router.post("/settings/clear-history")
def clear_history_setting():
    _q("DELETE FROM history")
    return {"ok": True}


@router.post("/settings/clear-cache")
def clear_cache():
    """Сбрасывает кэши в памяти (поиск, артисты, тексты, прогретые треки). Загрузки не трогает."""
    for fn in _cache_clearers:
        fn()
    return {"ok": True}


# ---------- избранное ----------
@router.get("/favorites")
def list_favorites():
    return {"items": [json.loads(r["data"]) for r in _q("SELECT data FROM favorites ORDER BY added_at DESC")]}


@router.post("/favorites")
def add_favorite(t: TrackIn):
    _q("INSERT OR REPLACE INTO favorites (key, data, added_at) VALUES (?, ?, ?)",
       (f"{t.source}:{t.id}", json.dumps(_dump(t), ensure_ascii=False), time.time()))
    return {"ok": True}


@router.delete("/favorites/{source}/{track_id}")
def remove_favorite(source: str, track_id: str):
    _q("DELETE FROM favorites WHERE key = ?", (f"{source}:{track_id}",))
    return {"ok": True}


def favorite_tracks() -> list[dict]:
    return [json.loads(r["data"]) for r in _q("SELECT data FROM favorites ORDER BY added_at DESC")]


# ---------- история ----------
@router.post("/history")
def add_history(t: TrackIn):
    _q("INSERT INTO history (key, data, played_at) VALUES (?, ?, ?)",
       (f"{t.source}:{t.id}", json.dumps(_dump(t), ensure_ascii=False), time.time()))
    return {"ok": True}


@router.post("/history/skip")
def mark_skip(k: KeyIn):
    """Трек переключили почти сразу: последняя запись о нём считается пропуском (учитывается в рекомендациях)."""
    _q("UPDATE history SET skipped = 1 WHERE id = (SELECT MAX(id) FROM history WHERE key = ?)", (f"{k.source}:{k.id}",))
    return {"ok": True}


@router.get("/history")
def list_history(limit: int = Query(20, ge=1, le=50), offset: int = Query(0, ge=0, le=2000)):
    rows = _q("SELECT data FROM history GROUP BY key ORDER BY MAX(played_at) DESC LIMIT ? OFFSET ?", (limit, offset))
    return {"tracks": [json.loads(r["data"]) for r in rows], "has_more": len(rows) == limit}


@router.delete("/history")
def clear_history():
    _q("DELETE FROM history")
    return {"ok": True}


def recent_plays(n: int = 300) -> list[dict]:
    rows = _q("SELECT key, data, skipped FROM history ORDER BY id DESC LIMIT ?", (n,))
    return [{"key": r["key"], "track": json.loads(r["data"]), "skipped": bool(r["skipped"])} for r in rows]