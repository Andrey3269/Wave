"""
Пользовательские данные: избранное, история прослушиваний, настройки.
Всё хранится локально в wave.db (рядом с индексом загрузок) и никуда не отправляется.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import time
from pathlib import Path
from typing import Any, Callable, Literal

from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import BaseModel

import library
import paths

log = logging.getLogger("musicmeta.user")
router = APIRouter(prefix="/api", tags=["user"])

# Таблицы favorites, history, settings создаёт persistence/migrations.py (версия схемы v1).
_cache_clearers: list[Callable[[], None]] = []


def register_cache_clear(fn: Callable[[], None]) -> None:
    _cache_clearers.append(fn)


class TrackIn(BaseModel):
    """Снимок трека для избранного, истории и плейлистов. Поля и типы — как у main.Track (находка M6)."""
    id: str
    source: Literal["yt", "sc"]
    title: str
    artist: str
    artists: list[str] = []
    album: str | None = None
    duration: int | None = None
    cover: str | None = None
    explicit: bool = False
    stream_url: str
    artist_id: str | None = None
    artist_ids: list[str | None] = []
    plays: int | None = None                 # прослушивания (есть только у SoundCloud)
    released: str | None = None              # дата выхода ISO (есть только у SoundCloud)
    url: str | None = None


class KeyIn(BaseModel):
    source: Literal["yt", "sc"]
    id: str


def _dump(t: TrackIn) -> dict:
    return t.model_dump() if hasattr(t, "model_dump") else t.dict()


def _q(sql: str, args: tuple = ()):
    return library._q(sql, args)


# ---------- настройки ----------
# Все настройки — строки в таблице settings (ключ → значение). Отсутствие строки = значение по умолчанию.
# Настройки интерфейса (громкость, тексты, имя профиля, обложки миксов) раньше жили в localStorage окна.
# Он привязан к origin http://127.0.0.1:<порт>, а порт меняется при каждом запуске, поэтому значения терялись
# (находка H3). Теперь источник правды — эта таблица; localStorage переносится сюда один раз (migrate-local).
# Ретеншн истории (S6): хранятся последние history_limit прослушиваний, более старые удаляются при записи
# нового. Пределы выбраны так, чтобы рекомендациям (recent_plays, 300 последних) и странице истории
# (до 2050 уникальных треков) всегда хватало данных, а база не росла без ограничений.
HISTORY_LIMIT_DEFAULT = 10_000
HISTORY_LIMIT_MIN = 1_000
HISTORY_LIMIT_MAX = 100_000

DEFAULTS = {"default_source": "yt", "autoplay": "1", "theme": "auto",
            "volume": "1", "last_volume": "1", "lyrics_visible": "1", "profile_name": "", "mix_covers": "{}",
            "history_limit": str(HISTORY_LIMIT_DEFAULT)}
ALLOWED = {"default_source": {"yt", "sc"}, "autoplay": {"0", "1"}, "theme": {"auto", "dark", "light"}}
UI_KEYS = ("volume", "last_volume", "lyrics_visible", "profile_name", "mix_covers")
MIGRATED_KEY = "ui_local_migrated"      # служебная отметка: перенос из localStorage уже был
PROFILE_NAME_MAX = 24
MIX_COVERS_MAX = 16
_COVER_PREFIXES = ("https://", "http://", "/api/local/")
_THEME_ID = re.compile(r"[a-z][a-z0-9_-]{0,31}")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_DIGITS = re.compile(r"[0-9]{1,7}")                 # только ASCII-цифры: «²» и «٥» int() не примет


class BadValue(ValueError):
    """Значение настройки не прошло проверку."""


def _v_choice(key: str):
    def check(value) -> str:
        val = "1" if value is True else "0" if value is False else value
        if not isinstance(val, str) or val not in ALLOWED[key]:
            raise BadValue(key)
        return val
    return check


def _v_level(value) -> str:
    """Громкость: число 0…1 (bool не число, хотя в Python это подкласс int)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1:
        raise BadValue("volume")
    return repr(round(float(value), 3))


def _v_flag(value) -> str:
    if value is True or value == "1":
        return "1"
    if value is False or value == "0":
        return "0"
    raise BadValue("flag")


def _v_profile_name(value) -> str:
    if not isinstance(value, str):
        raise BadValue("profile_name")
    name = value.strip()
    if len(name) > PROFILE_NAME_MAX or _CONTROL.search(name):
        raise BadValue("profile_name")
    return name


def _clean_covers(value, lenient: bool) -> dict[str, str]:
    """Обложки миксов: {id микса: URL}. lenient=True (перенос из localStorage) — плохие пары отбрасываются."""
    if not isinstance(value, dict) or len(value) > MIX_COVERS_MAX:
        raise BadValue("mix_covers")
    out: dict[str, str] = {}
    for k, v in value.items():
        ok = (isinstance(k, str) and _THEME_ID.fullmatch(k) and isinstance(v, str) and len(v) <= 2048
              and v.startswith(_COVER_PREFIXES) and not _CONTROL.search(v))
        if ok:
            out[k] = v
        elif v is not None and not lenient:          # None = «обложки нет», это не ошибка
            raise BadValue("mix_covers")
    return out


def _v_mix_covers(value, lenient: bool = False) -> str:
    return json.dumps(_clean_covers(value, lenient), ensure_ascii=False, separators=(",", ":"))


def _v_history_limit(value) -> str:
    """Целое число записей в пределах HISTORY_LIMIT_MIN…MAX (число или строка из цифр; bool — не число)."""
    if isinstance(value, str) and _DIGITS.fullmatch(value.strip()):
        value = int(value.strip())
    if isinstance(value, bool) or not isinstance(value, int) or not HISTORY_LIMIT_MIN <= value <= HISTORY_LIMIT_MAX:
        raise BadValue("history_limit")
    return str(value)


def _read_history_limit(raw: str | None) -> int:
    try:
        return int(_v_history_limit(raw))
    except BadValue:
        return HISTORY_LIMIT_DEFAULT


VALIDATORS: dict[str, Callable] = {
    "default_source": _v_choice("default_source"),
    "autoplay": _v_choice("autoplay"),
    "theme": _v_choice("theme"),
    "volume": _v_level,
    "last_volume": _v_level,
    "lyrics_visible": _v_flag,
    "profile_name": _v_profile_name,
    "mix_covers": _v_mix_covers,
    "history_limit": _v_history_limit,
}


def get_setting(key: str, default: str | None = None) -> str | None:
    rows = _q("SELECT value FROM settings WHERE key = ?", (key,))
    return rows[0]["value"] if rows else DEFAULTS.get(key, default)


def _stored() -> dict[str, str]:
    return {r["key"]: r["value"] for r in _q("SELECT key, value FROM settings")}


def _read_level(raw: str | None, default: float) -> float:
    try:
        v = float(raw) if raw is not None else default
    except ValueError:
        return default
    return v if math.isfinite(v) and 0 <= v <= 1 else default


def _read_covers(raw: str | None) -> dict[str, str]:
    try:
        return _clean_covers(json.loads(raw or "{}"), lenient=True)
    except (ValueError, BadValue):
        return {}


def init() -> None:
    """Вызывается после library.init(): база уже открыта и обновлена до текущей схемы."""
    trim_history()                              # лимит могли уменьшить, или база от версии без ретеншна
    saved = get_setting("downloads_dir")
    if saved:                                   # возвращаем выбранную пользователем папку загрузок
        try:
            paths.set_downloads_dir(Path(saved))
        except OSError as exc:
            log.warning("Папка загрузок недоступна: %r", exc)


def _public() -> dict:
    s = {**DEFAULTS, **_stored()}
    theme = s["theme"] if s["theme"] in ALLOWED["theme"] else DEFAULTS["theme"]
    return {
        "values": {
            "default_source": s["default_source"], "autoplay": s["autoplay"] == "1", "theme": theme,
            # интерфейс (H3): раньше localStorage
            "volume": _read_level(s["volume"], 1.0), "last_volume": _read_level(s["last_volume"], 1.0),
            "lyrics_visible": s["lyrics_visible"] != "0", "profile_name": s["profile_name"],
            "mix_covers": _read_covers(s["mix_covers"]),
            "history_limit": _read_history_limit(s["history_limit"]),           # S6
        },
        "paths": {"downloads": str(paths.downloads_dir()), "data": str(paths.data_dir()),
                  "downloads_locked": bool(os.getenv("WAVE_DOWNLOADS_DIR"))},
        "meta": {"local_ui_migrated": s.get(MIGRATED_KEY) == "1"},
    }


@router.get("/settings")
def read_settings():
    return _public()


def _check_downloads_dir(value) -> Path:
    """Проверяет папку загрузок (создаёт её и пробует записать). Возвращает итоговый путь."""
    if os.getenv("WAVE_DOWNLOADS_DIR"):
        raise HTTPException(400, "Папка загрузок задана переменной окружения WAVE_DOWNLOADS_DIR.")
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(400, "Недопустимое значение настройки.")
    p = Path(value).expanduser()
    if not p.is_absolute():
        raise HTTPException(400, "Укажите полный путь, например C:\\Users\\Имя\\Music\\Wave")
    try:
        p.mkdir(parents=True, exist_ok=True)
        probe = p / ".wave_write_test"
        probe.write_text("x")
        probe.unlink()
    except OSError:
        raise HTTPException(400, "В эту папку нельзя записывать. Выберите другую.") from None
    return p.resolve()


@router.put("/settings")
def write_settings(body: dict[str, Any]):
    """
    Атомарно: сначала проверяются все ключи, затем всё пишется одной транзакцией. Если хоть один ключ
    неизвестен или с плохим значением — 400, и ни одна настройка не меняется (находка N1).
    """
    updates: dict[str, str] = {}
    new_dir = None
    for key, value in body.items():
        if key == "downloads_dir":
            new_dir = value                          # проверка с записью на диск — после остальных ключей
        elif key in VALIDATORS:
            try:
                updates[key] = VALIDATORS[key](value)
            except BadValue:
                raise HTTPException(400, "Недопустимое значение настройки.") from None
        else:
            raise HTTPException(400, "Неизвестная настройка.")
    target = _check_downloads_dir(new_dir) if "downloads_dir" in body else None
    if target is not None:
        updates["downloads_dir"] = str(target)
    with library.transaction() as db:
        db.executemany("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", updates.items())
        if "history_limit" in updates:                # уменьшенный лимит действует сразу
            _trim(db, int(updates["history_limit"]))
    if target is not None:
        paths.set_downloads_dir(target)              # только после успешной записи в базу
    return _public()


@router.post("/settings/migrate-local")
def migrate_local(body: dict[str, Any]):
    """
    Однократный перенос настроек интерфейса из localStorage окна (как было до H3).

    * Выполняется один раз на профиль: после первого вызова (даже с пустым телом) повторные — без изменений.
    * Пишутся только ключи интерфейса, которых ещё нет в базе: перенос не затирает значения,
      сохранённые через PUT /api/settings.
    * Плохие значения пропускаются по одному (в localStorage мог остаться мусор), а не роняют перенос целиком.
    Ответ: обычные настройки + списки `migrated` и `skipped`.
    """
    migrated: list[str] = []
    skipped: list[str] = []
    with library.transaction() as db:
        done = db.execute("SELECT 1 FROM settings WHERE key = ?", (MIGRATED_KEY,)).fetchone()
        if not done:
            have = {r[0] for r in db.execute("SELECT key FROM settings")}
            for key, value in body.items():
                if key not in UI_KEYS:
                    skipped.append(key)
                    continue
                try:
                    val = _v_mix_covers(value, lenient=True) if key == "mix_covers" else VALIDATORS[key](value)
                except BadValue:
                    skipped.append(key)
                    continue
                if key in have:
                    skipped.append(key)
                    continue
                db.execute("INSERT INTO settings (key, value) VALUES (?, ?)", (key, val))
                migrated.append(key)
            db.execute("INSERT INTO settings (key, value) VALUES (?, ?)", (MIGRATED_KEY, "1"))
        else:
            skipped = list(body)
    if migrated or skipped:
        log.info("Перенос настроек из localStorage: перенесено %s, пропущено %s", migrated, skipped)
    return {**_public(), "migrated": migrated, "skipped": skipped}


_THEME_SCRIPT = (
    "/* Wave: тема до первой отрисовки (значение из базы, а не из localStorage окна) */"
    "(function(m){try{document.documentElement.dataset.theme=m==='auto'?"
    "(matchMedia('(prefers-color-scheme: light)').matches?'light':'dark'):m}catch(e){}})(%s);"
)


@router.get("/boot.js", include_in_schema=False)
def boot_script():
    """Маленький синхронный скрипт в <head>: ставит тему до отрисовки, чтобы не было вспышки."""
    theme = get_setting("theme")
    if theme not in ALLOWED["theme"]:
        theme = DEFAULTS["theme"]
    return Response(_THEME_SCRIPT % json.dumps(theme), media_type="text/javascript",
                    headers={"Cache-Control": "no-store"})


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


# `:path`: id мог сохраниться с «/» (до проверки формата id, находка M6) — такую запись тоже можно удалить.
# Фронтенд кодирует id (encodeURIComponent), Starlette декодирует %2F до маршрутизации.
@router.delete("/favorites/{source}/{track_id:path}")
def remove_favorite(source: str, track_id: str):
    _q("DELETE FROM favorites WHERE key = ?", (f"{source}:{track_id}",))
    return {"ok": True}


def favorite_tracks() -> list[dict]:
    return [json.loads(r["data"]) for r in _q("SELECT data FROM favorites ORDER BY added_at DESC")]


# ---------- история ----------
def _trim(db, limit: int) -> int:
    """Удаляет из истории всё, кроме последних `limit` записей (по порядку записи). Другие таблицы не трогает."""
    cur = db.execute("DELETE FROM history WHERE id <= (SELECT id FROM history ORDER BY id DESC LIMIT 1 OFFSET ?)",
                     (limit,))
    return cur.rowcount


def history_limit(db=None) -> int:
    if db is None:
        return _read_history_limit(get_setting("history_limit"))
    row = db.execute("SELECT value FROM settings WHERE key = 'history_limit'").fetchone()
    return _read_history_limit(row[0] if row else None)


def trim_history() -> int:
    """Применяет лимит истории; возвращает число удалённых записей."""
    with library.transaction() as db:
        removed = _trim(db, history_limit(db))
    if removed:
        log.info("История: удалено %d старых записей сверх лимита", removed)
    return removed


@router.post("/history")
def add_history(t: TrackIn):
    """Запись прослушивания и обрезка истории до лимита — одной транзакцией."""
    with library.transaction() as db:
        db.execute("INSERT INTO history (key, data, played_at) VALUES (?, ?, ?)",
                   (f"{t.source}:{t.id}", json.dumps(_dump(t), ensure_ascii=False), time.time()))
        _trim(db, history_limit(db))
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