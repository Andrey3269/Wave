"""
Состояние воспроизведения между запусками (S10): очередь, текущий трек, позиция, перемешивание и повтор.

Хранение — одна строка `playback_state` (JSON) в существующей таблице settings того же wave.db. Новой таблицы
и новой версии схемы нет: база остаётся v1 и по-прежнему открывается версиями до S10 (они эту строку не
читают и не меняют). Это не настройка: через /api/settings строка не видна и не меняется (неизвестный ключ
там, как и раньше, — 400). Почему не отдельная таблица — docs/engineering/reports/S10.md.

Сохранённому не доверяем: при записи и при чтении проверяются структура, размеры, типы и значения.
* Адрес потока не хранится: его всегда вычисляет сервер (/api/stream/{source}/{id}), как у остальных треков.
* Обложка и публичная ссылка — только из разрешённых списков (S13, domain/snapshot.py: https:// с хостов обложек
  источников или /api/local/{source}/{id}/cover; ссылка — https:// на сайтах источников); остальное отбрасывается
  (None), а не попадает в интерфейс.
* Неизвестные поля трека и состояния отбрасываются.
* Строка, которая при чтении не проходит проверку (испорчена, неполная, от другой версии), считается
  отсутствующей: интерфейс начинает с пустой очереди, в журнале — предупреждение. Строку перезапишет
  следующее сохранение.

API:
  GET    /api/player/state → {"state": {...} | null}
  PUT    /api/player/state   полное состояние (очередь, текущий, позиция, режимы, история «Назад», круг перемешивания)
  PATCH  /api/player/state   только позиция: {"key": "source:id", "index": n, "position": s}; 409, если сохранённая
                         очередь не играет этот трек на этом месте (устаревшее обновление)
  DELETE /api/player/state   забыть состояние
  POST   /api/player/state, /api/player/state/position — то же, что PUT и PATCH, для navigator.sendBeacon при закрытии
                         окна (beacon умеет только POST; браузер доставляет его и после выгрузки страницы)
"""
from __future__ import annotations

import json
import logging
import math
import re
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request

import library
from domain import snapshot
from domain.models import album_id_or_none

log = logging.getLogger("musicmeta.playback")
router = APIRouter(prefix="/api", tags=["playback"])

KEY = "playback_state"              # строка в таблице settings
VERSION = 1
MAX_BODY = 1_000_000                # байт в запросе PUT
MAX_TRACKS = 500                    # длинную очередь фронтенд сохраняет окном вокруг текущего трека
MAX_HISTORY = 500
MAX_POSITION = 24 * 3600.0          # секунд
REPEAT = ("off", "all", "one")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_ARTIST_ID = re.compile(r"^[\w-]{1,128}$")


class BadState(ValueError):
    """Состояние не прошло проверку."""


def _text(v: Any, limit: int, *, required: bool = False) -> str | None:
    """Строка ≤ limit символов; управляющие символы заменяются пробелом. None — если поле необязательное и пустое."""
    if v is None and not required:
        return None
    if not isinstance(v, str) or len(v) > limit:
        raise BadState("text")
    v = _CONTROL.sub(" ", v).strip()
    if required and not v:
        raise BadState("text")
    return v or None


def _int(v: Any, lo: int, hi: int) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
        raise BadState("int")
    return v


def _track(t: Any) -> dict:
    """Трек очереди в форме Track. Обязательные поля проверяются строго, необязательные — мягко (плохое → None)."""
    if not isinstance(t, dict):
        raise BadState("track")
    source = t.get("source")
    tid = t.get("id")
    if not snapshot.valid_track(source, tid):
        raise BadState("track id")
    artists = t.get("artists")
    if not isinstance(artists, list) or len(artists) > 20:
        artists = []
    artist_ids = t.get("artist_ids")
    if not isinstance(artist_ids, list) or len(artist_ids) > 20:
        artist_ids = []
    duration = t.get("duration")
    if isinstance(duration, float) and math.isfinite(duration):
        duration = round(duration)
    plays = t.get("plays")
    released = t.get("released")
    artist_id = t.get("artist_id")
    out = {
        "id": tid,
        "source": source,
        "title": _text(t.get("title"), 500, required=True),
        "artist": _text(t.get("artist"), 500, required=True),
        "artists": [a for a in (_soft_text(x, 300) for x in artists) if a],
        "album": _soft_text(t.get("album"), 500),
        "album_id": album_id_or_none(t.get("album_id")),
        "duration": duration if isinstance(duration, int) and not isinstance(duration, bool) and 0 <= duration <= MAX_POSITION else None,
        "cover": snapshot.safe_cover(t.get("cover")),
        "explicit": t.get("explicit") is True,
        "stream_url": snapshot.stream_url(source, tid),       # вычисляет сервер, сохранённому не доверяем
        "artist_id": artist_id if isinstance(artist_id, str) and _ARTIST_ID.fullmatch(artist_id) else None,
        "artist_ids": [x if isinstance(x, str) and _ARTIST_ID.fullmatch(x) else None for x in artist_ids],
        "plays": plays if isinstance(plays, int) and not isinstance(plays, bool) and plays >= 0 else None,
        "released": released if isinstance(released, str) and len(released) <= 40 and not _CONTROL.search(released) else None,
        "url": snapshot.safe_link(t.get("url")),
    }
    return out


def _soft_text(v: Any, limit: int) -> str | None:
    try:
        return _text(v, limit)
    except BadState:
        return None


def clean_state(body: Any) -> dict:
    """Проверенное состояние для хранения. BadState — если структура или значения недопустимы."""
    if not isinstance(body, dict):
        raise BadState("state")
    tracks = body.get("tracks")
    if not isinstance(tracks, list) or not 0 < len(tracks) <= MAX_TRACKS:
        raise BadState("tracks")
    tracks = [_track(t) for t in tracks]
    n = len(tracks)
    index = _int(body.get("index"), -1, n - 1)
    position = body.get("position", 0)
    if isinstance(position, bool) or not isinstance(position, (int, float)) or not math.isfinite(position) \
            or not 0 <= position <= MAX_POSITION:
        raise BadState("position")
    repeat = body.get("repeat", "off")
    if repeat not in REPEAT:
        raise BadState("repeat")
    shuffle = body.get("shuffle", False)
    if not isinstance(shuffle, bool):
        raise BadState("shuffle")
    history = body.get("history", [])
    if not isinstance(history, list) or len(history) > MAX_HISTORY:
        raise BadState("history")
    history = [_int(k, 0, n - 1) for k in history]
    cursor = _int(body.get("cursor", len(history) - 1), -1, len(history) - 1)
    cycle = body.get("cycle", [])
    if not isinstance(cycle, list) or len(cycle) > n:
        raise BadState("cycle")
    cycle = [_int(k, 0, n - 1) for k in cycle]
    if len(set(cycle)) != len(cycle):
        raise BadState("cycle")
    return {"v": VERSION, "tracks": tracks, "index": index, "position": round(float(position), 2),
            "shuffle": shuffle, "repeat": repeat, "label": _soft_text(body.get("label"), 200) or "",
            "history": history, "cursor": cursor, "cycle": cycle}


def read_state() -> dict | None:
    """Сохранённое состояние или None (нет, испорчено, от другой версии)."""
    rows = library._q("SELECT value FROM settings WHERE key = ?", (KEY,))
    if not rows:
        return None
    return _parse(rows[0]["value"])


def _parse(raw: str | None) -> dict | None:
    try:
        data = json.loads(raw or "")
        if not isinstance(data, dict) or data.get("v") != VERSION:
            raise BadState("version")
        state = clean_state(data)
    except (ValueError, TypeError, RecursionError) as exc:      # BadState — подкласс ValueError
        log.warning("Сохранённое состояние воспроизведения не прочитано и пропущено: %s", type(exc).__name__)
        return None
    state["saved_at"] = data.get("saved_at") if isinstance(data.get("saved_at"), (int, float)) else None
    return state


def _store(db, state: dict) -> None:
    db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
               (KEY, json.dumps({**state, "saved_at": time.time()}, ensure_ascii=False, separators=(",", ":"))))


@router.get("/player/state")
def get_playback():
    return {"state": read_state()}


@router.put("/player/state")
@router.post("/player/state")                    # sendBeacon при закрытии окна
async def put_playback(request: Request):
    raw = await request.body()
    if len(raw) > MAX_BODY:
        raise HTTPException(413, "Слишком большая очередь для сохранения.")
    try:
        state = clean_state(json.loads(raw))
    except (ValueError, TypeError, RecursionError):
        raise HTTPException(400, "Недопустимое состояние воспроизведения.") from None
    with library.transaction() as db:
        _store(db, state)
    return {"ok": True}


@router.patch("/player/state")
@router.post("/player/state/position")           # sendBeacon при закрытии окна
def patch_position(body: dict[str, Any]):
    """Только позиция текущего трека (часто, маленьким запросом; при закрытии окна — keepalive)."""
    key, index, position = body.get("key"), body.get("index"), body.get("position")
    if (not isinstance(key, str) or isinstance(index, bool) or not isinstance(index, int)
            or isinstance(position, bool) or not isinstance(position, (int, float))
            or not math.isfinite(position) or not 0 <= position <= MAX_POSITION):
        raise HTTPException(400, "Недопустимая позиция воспроизведения.")
    with library.transaction() as db:
        row = db.execute("SELECT value FROM settings WHERE key = ?", (KEY,)).fetchone()
        state = _parse(row[0]) if row else None
        if state is None or not 0 <= index < len(state["tracks"]) or state["index"] != index:
            raise HTTPException(409, "Сохранённая очередь уже другая.")
        t = state["tracks"][index]
        if f"{t['source']}:{t['id']}" != key:
            raise HTTPException(409, "Сохранённая очередь уже другая.")
        state.pop("saved_at", None)
        state["position"] = round(float(position), 2)
        _store(db, state)
    return {"ok": True}


@router.delete("/player/state")
def delete_playback():
    library._q("DELETE FROM settings WHERE key = ?", (KEY,))
    return {"ok": True}
