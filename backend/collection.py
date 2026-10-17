"""
Библиотека пользователя (S15): избранные альбомы и исполнители, экспорт и импорт библиотеки в JSON.

Избранные треки по-прежнему живут в таблице favorites (/api/favorites, формат не менялся). Альбомы и исполнители —
в favorite_items (схема v3): карточка каталога в том же виде, в каком её отдают поиск и страницы артиста/альбома
(domain/models.py: artist_card, album_card). Карточке от клиента не доверяем: id — в формате каталога, обложка — из
разрешённого списка (domain/snapshot.safe_cover), строки — ограниченной длины без управляющих символов.

Экспорт (GET /api/library/export, POST /api/library/export/file): избранное (треки, альбомы, исполнители) с датами
добавления, плейлисты с порядком треков и датами, настройки. Папка загрузок (путь на этом компьютере), служебные
строки и состояние плеера не экспортируются. Импорт (POST /api/library/import):

* тело — тот же JSON (формат FORMAT, версия VERSION), не больше IMPORT_MAX_BYTES;
* сначала проверяется ВЕСЬ файл (формат, типы, пределы, дубли, каждый трек — как в TrackIn, каждая настройка — как
  в PUT /api/settings); при любой ошибке — 400 и ни одна строка базы не меняется;
* затем всё пишется одной транзакцией: ошибка посреди записи — откат, данные прежние;
* слияние с текущим профилем: уже избранное остаётся как есть, плейлист пропускается, если такой же (имя и треки по
  порядку) уже есть, настройки из файла заменяют текущие. Локальные треки (источник local) импортируются, только если
  этот файл уже импортирован в Wave на этом компьютере: сами аудиофайлы в экспорт не входят.
На чистом профиле экспорт → импорт восстанавливает избранное, плейлисты (с порядком и датами) и настройки 1:1.
"""
from __future__ import annotations

import json
import logging
import math
import re
import time
from typing import Any, Literal

import anyio
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

import library
import paths
import userdata
from domain import snapshot
from domain.models import SOURCE_LOCAL
from version import __version__

log = logging.getLogger("musicmeta.library")
router = APIRouter(prefix="/api/library", tags=["library"])

FORMAT = "wave-library"
VERSION = 1
IMPORT_MAX_BYTES = 20 * 1024 * 1024
MAX_FAVORITES = 100_000
MAX_ITEMS = 20_000
MAX_PLAYLISTS = 2_000
MAX_PLAYLIST_TRACKS = 10_000
MAX_TOTAL_PLAYLIST_TRACKS = 200_000
TEXT_MAX = 300
CARD_ID_RE = re.compile(r"^[\w-]{1,64}$")          # как catalog._ID_RE: id артиста и альбома в каталоге
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_YEAR = re.compile(r"^\d{4}$")
TS_MAX = 4_102_444_800                              # 2100-01-01: дата из файла должна быть правдоподобной


def _text(v: Any, required: bool = False) -> str | None:
    if v is None and not required:
        return None
    if not isinstance(v, str) or _CONTROL.search(v):
        raise ValueError("недопустимая строка")
    v = v.strip()
    if len(v) > TEXT_MAX or (required and not v):
        raise ValueError("пустая или слишком длинная строка")
    return v or None


# ---------------------------------------------------------------------------
# Карточки альбомов и исполнителей
# ---------------------------------------------------------------------------
class _Card(BaseModel):
    model_config = ConfigDict(extra="ignore")
    source: Literal["yt", "sc"]
    id: str

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not CARD_ID_RE.fullmatch(v):
            raise ValueError("некорректный id")
        return v


class AlbumIn(_Card):
    title: str
    artist: str | None = None
    year: str | None = None
    cover: str | None = None
    track_count: int | None = Field(None, ge=0, le=100_000)
    playlist_id: str | None = None

    @field_validator("title", mode="before")
    @classmethod
    def _title(cls, v):
        return _text(v, required=True)

    @field_validator("artist", mode="before")
    @classmethod
    def _artist(cls, v):
        return _text(v)

    @field_validator("year", mode="before")
    @classmethod
    def _year(cls, v):
        if v in (None, ""):
            return None
        if not isinstance(v, str) or not _YEAR.fullmatch(v):
            raise ValueError("год — четыре цифры")
        return v

    @field_validator("playlist_id")
    @classmethod
    def _pl(cls, v):
        if v is not None and not CARD_ID_RE.fullmatch(v):
            raise ValueError("некорректный id плейлиста альбома")
        return v

    def card(self) -> dict:
        return {"id": self.id, "source": self.source, "kind": "album", "title": self.title, "artist": self.artist,
                "year": self.year, "cover": snapshot.safe_cover(self.cover, local=False),
                "track_count": self.track_count, "playlist_id": self.playlist_id}


class ArtistIn(_Card):
    name: str
    cover: str | None = None
    subtitle: str | None = None

    @field_validator("name", mode="before")
    @classmethod
    def _name(cls, v):
        return _text(v, required=True)

    @field_validator("subtitle", mode="before")
    @classmethod
    def _sub(cls, v):
        return _text(v)

    def card(self) -> dict:
        return {"id": self.id, "source": self.source, "kind": "artist", "name": self.name,
                "cover": snapshot.safe_cover(self.cover, local=False), "subtitle": self.subtitle}


MODELS = {"album": AlbumIn, "artist": ArtistIn}


def read_card(kind: str, raw: Any) -> dict | None:
    """Карточка из базы для ответа клиенту или None, если строка испорчена (она остаётся в базе, как у снимков)."""
    try:
        data = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
        return MODELS[kind].model_validate(data).card()
    except (ValueError, TypeError, RecursionError, ValidationError):
        return None


def _items(kind: str) -> list[dict]:
    rows = library._q("SELECT data, added_at FROM favorite_items WHERE kind = ? ORDER BY added_at DESC, rowid DESC", (kind,))
    return [c for c in (read_card(kind, r["data"]) for r in rows) if c is not None]


def _add(kind: str, item: _Card) -> dict:
    card = item.card()
    cur = library.db.execute(
        "INSERT OR IGNORE INTO favorite_items (kind, source, item_id, data, added_at) VALUES (?, ?, ?, ?, ?)",
        (kind, item.source, item.id, json.dumps(card, ensure_ascii=False), time.time()))
    return {"ok": True, "added": cur.rowcount == 1}


def _remove(kind: str, source: str, item_id: str) -> dict:
    if len(source) > 16 or len(item_id) > 128:
        raise HTTPException(400, "Некорректный запрос")
    library.db.execute("DELETE FROM favorite_items WHERE kind = ? AND source = ? AND item_id = ?", (kind, source, item_id))
    return {"ok": True}


@router.get("/albums")
def list_albums():
    return {"items": _items("album")}


@router.post("/albums")
def add_album(body: AlbumIn):
    return _add("album", body)


@router.delete("/albums/{source}/{item_id}")
def remove_album(source: str, item_id: str):
    return _remove("album", source, item_id)


@router.get("/artists")
def list_artists():
    return {"items": _items("artist")}


@router.post("/artists")
def add_artist(body: ArtistIn):
    return _add("artist", body)


@router.delete("/artists/{source}/{item_id}")
def remove_artist(source: str, item_id: str):
    return _remove("artist", source, item_id)


# ---------------------------------------------------------------------------
# Экспорт
# ---------------------------------------------------------------------------
EXPORT_SETTINGS = tuple(k for k in userdata.VALIDATORS)       # без downloads_dir (путь этого компьютера)


def build_export() -> dict:
    favs = library._q("SELECT key, data, added_at FROM favorites ORDER BY added_at DESC, rowid DESC")
    items = library._q("SELECT kind, data, added_at FROM favorite_items ORDER BY added_at DESC, rowid DESC")
    pls = library._q("SELECT id, name, created_at, updated_at FROM playlists ORDER BY updated_at DESC, id DESC")
    out_pls = []
    for p in pls:
        rows = library._q("SELECT key, data, added_at FROM playlist_tracks WHERE playlist_id = ? ORDER BY position", (p["id"],))
        tracks = [{"added_at": r["added_at"], "track": t} for r in rows
                  if (t := snapshot.read_snapshot(r["data"], "playlist_tracks", r["key"])) is not None]
        out_pls.append({"name": p["name"], "created_at": p["created_at"], "updated_at": p["updated_at"], "tracks": tracks})
    values = userdata._public()["values"]
    return {
        "format": FORMAT, "version": VERSION, "app_version": __version__,
        "exported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "favorites": {
            "tracks": [{"added_at": r["added_at"], "track": t} for r in favs
                       if (t := snapshot.read_snapshot(r["data"], "favorites", r["key"])) is not None],
            "albums": [{"added_at": r["added_at"], "item": c} for r in items if r["kind"] == "album"
                       and (c := read_card("album", r["data"])) is not None],
            "artists": [{"added_at": r["added_at"], "item": c} for r in items if r["kind"] == "artist"
                        and (c := read_card("artist", r["data"])) is not None],
        },
        "playlists": out_pls,
        "settings": {k: values[k] for k in EXPORT_SETTINGS if k in values},
    }


def _export_name() -> str:
    return time.strftime("wave-library-%Y%m%d-%H%M%S.json")


@router.get("/export")
def export_library():
    return JSONResponse(build_export(), headers={"Content-Disposition": f'attachment; filename="{_export_name()}"',
                                                 "Cache-Control": "no-store"})


@router.post("/export/file")
def export_to_file():
    """
    Экспорт в файл в папке загрузок (окно Wave — WebView2 — не сохраняет скачивания браузера). Имя выбирает сервер:
    wave-library-<дата>.json; существующий файл не перезаписывается. Ответ: {"path": полный путь}.
    """
    data = json.dumps(build_export(), ensure_ascii=False, indent=1).encode("utf-8")
    try:
        root = paths.downloads_dir()
        target = root / _export_name()
        n = 1
        while target.exists():
            n += 1
            target = root / time.strftime(f"wave-library-%Y%m%d-%H%M%S-{n}.json")
        with open(target, "xb") as f:
            f.write(data)
    except OSError as exc:
        log.warning("Экспорт библиотеки не записан: %r", exc)
        raise HTTPException(500, "Не удалось записать файл экспорта в папку загрузок. Проверь папку в настройках.") from None
    return {"path": str(target), "size": len(data)}


# ---------------------------------------------------------------------------
# Импорт
# ---------------------------------------------------------------------------
def _ts(v: Any) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not 0 <= v <= TS_MAX:
        raise ValueError("недопустимая дата")
    return float(v)


class _Dated(BaseModel):
    model_config = ConfigDict(extra="forbid")
    added_at: float

    @field_validator("added_at", mode="before")
    @classmethod
    def _at(cls, v):
        return _ts(v)


class TrackEntry(_Dated):
    track: userdata.TrackIn


class AlbumEntry(_Dated):
    item: AlbumIn


class ArtistEntry(_Dated):
    item: ArtistIn


class FavoritesIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tracks: list[TrackEntry] = Field(default_factory=list, max_length=MAX_FAVORITES)
    albums: list[AlbumEntry] = Field(default_factory=list, max_length=MAX_ITEMS)
    artists: list[ArtistEntry] = Field(default_factory=list, max_length=MAX_ITEMS)


class PlaylistIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    created_at: float
    updated_at: float
    tracks: list[TrackEntry] = Field(default_factory=list, max_length=MAX_PLAYLIST_TRACKS)

    @field_validator("name", mode="before")
    @classmethod
    def _name(cls, v):
        if not isinstance(v, str) or _CONTROL.search(v):
            raise ValueError("недопустимое название")
        name = " ".join(v.split())
        if not name or len(name) > 80:
            raise ValueError("название пустое или длиннее 80 символов")
        return name

    @field_validator("created_at", "updated_at", mode="before")
    @classmethod
    def _at(cls, v):
        return _ts(v)


class ExportIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    format: Literal["wave-library"]
    version: Literal[1]
    app_version: str | None = Field(None, max_length=64)
    exported_at: str | None = Field(None, max_length=64)
    favorites: FavoritesIn = Field(default_factory=FavoritesIn)
    playlists: list[PlaylistIn] = Field(default_factory=list, max_length=MAX_PLAYLISTS)
    settings: dict[str, Any] = Field(default_factory=dict)


def _reject_constant(name: str):
    raise ValueError(f"недопустимое число {name}")


def _where(err: dict) -> str:
    return ".".join(str(p) for p in err.get("loc", ()))[:120] or "файл"


def parse_import(raw: bytes) -> tuple[ExportIn, dict[str, str]]:
    """Полная проверка файла до записи. Возвращает модель и проверенные значения настроек (как пишет PUT /api/settings)."""
    try:
        data = json.loads(raw.decode("utf-8-sig"), parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise HTTPException(400, "Файл не является JSON-экспортом библиотеки Wave.") from None
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise HTTPException(400, "Это не файл экспорта библиотеки Wave.")
    if data.get("version") != VERSION:
        raise HTTPException(400, f"Версия файла экспорта ({str(data.get('version'))[:20]}) не поддерживается этой версией Wave.")
    try:
        doc = ExportIn.model_validate(data)
    except ValidationError as exc:
        errs = exc.errors()
        raise HTTPException(400, f"Файл экспорта повреждён: ошибка в поле {_where(errs[0])}. Ничего не изменено.") from None
    if sum(len(p.tracks) for p in doc.playlists) > MAX_TOTAL_PLAYLIST_TRACKS:
        raise HTTPException(400, "В файле слишком много треков в плейлистах. Ничего не изменено.")
    seen: set[str] = set()
    for e in doc.favorites.tracks:
        k = f"{e.track.source}:{e.track.id}"
        if k in seen:
            raise HTTPException(400, f"Трек {k} указан в избранном дважды. Ничего не изменено.")
        seen.add(k)
    for kind, entries in (("album", doc.favorites.albums), ("artist", doc.favorites.artists)):
        ids = [(e.item.source, e.item.id) for e in entries]
        if len(set(ids)) != len(ids):
            raise HTTPException(400, f"В избранном повторяется {'альбом' if kind == 'album' else 'исполнитель'}. Ничего не изменено.")
    for p in doc.playlists:
        keys = [f"{e.track.source}:{e.track.id}" for e in p.tracks]
        if len(set(keys)) != len(keys):
            raise HTTPException(400, f"В плейлисте «{p.name}» трек повторяется. Ничего не изменено.")
    settings: dict[str, str] = {}
    for key, value in doc.settings.items():
        if key not in userdata.VALIDATORS:
            raise HTTPException(400, f"Неизвестная настройка в файле: {str(key)[:40]}. Ничего не изменено.")
        try:
            settings[key] = userdata.VALIDATORS[key](value)
        except userdata.BadValue:
            raise HTTPException(400, f"Недопустимое значение настройки {key}. Ничего не изменено.") from None
    return doc, settings


def _keep(t: userdata.TrackIn, local_ids: set[str]) -> bool:
    """Локальный трек импортируется, только если этот файл есть в Wave на этом компьютере."""
    return t.source != SOURCE_LOCAL or t.id in local_ids


def apply_import(doc: ExportIn, settings: dict[str, str]) -> dict:
    """Запись проверенного файла — одной транзакцией. Возвращает счётчики для ответа."""
    imported = dict.fromkeys(("tracks", "albums", "artists", "playlists", "playlist_tracks", "settings"), 0)
    skipped = dict.fromkeys(("existing", "duplicate_playlists", "missing_local"), 0)
    dump = lambda t: json.dumps(userdata._dump(t), ensure_ascii=False)       # noqa: E731
    with library.transaction() as db:
        local_ids = {r[0] for r in db.execute("SELECT id FROM local_tracks")}
        # Записи — от старых к новым (в файле новые сверху): при равных датах порядок по rowid остаётся прежним
        for e in reversed(doc.favorites.tracks):
            if not _keep(e.track, local_ids):
                skipped["missing_local"] += 1
                continue
            cur = db.execute("INSERT OR IGNORE INTO favorites (key, data, added_at) VALUES (?, ?, ?)",
                             (f"{e.track.source}:{e.track.id}", dump(e.track), e.added_at))
            imported["tracks"] += cur.rowcount
            skipped["existing"] += 1 - cur.rowcount
        for kind, entries in (("album", doc.favorites.albums), ("artist", doc.favorites.artists)):
            for e in reversed(entries):
                cur = db.execute("INSERT OR IGNORE INTO favorite_items (kind, source, item_id, data, added_at)"
                                 " VALUES (?, ?, ?, ?, ?)", (kind, e.item.source, e.item.id,
                                                             json.dumps(e.item.card(), ensure_ascii=False), e.added_at))
                imported[kind + "s"] += cur.rowcount
                skipped["existing"] += 1 - cur.rowcount
        existing = {}
        for p in db.execute("SELECT id, name FROM playlists").fetchall():
            # как в экспорте: только читаемые треки — иначе плейлист со строкой, которую GET пропускает, считался бы другим
            rows = db.execute("SELECT key, data FROM playlist_tracks WHERE playlist_id = ? ORDER BY position", (p[0],)).fetchall()
            keys = tuple(r[0] for r in rows if snapshot.read_snapshot(r[1], "playlist_tracks", r[0]) is not None)
            existing.setdefault(p[1], set()).add(keys)
        for p in reversed(doc.playlists):            # в файле — новые сверху; создаём в исходном порядке
            entries = [e for e in p.tracks if _keep(e.track, local_ids)]
            skipped["missing_local"] += len(p.tracks) - len(entries)
            keys = tuple(f"{e.track.source}:{e.track.id}" for e in entries)
            if keys in existing.get(p.name, set()):
                skipped["duplicate_playlists"] += 1
                continue
            pid = db.execute("INSERT INTO playlists (name, created_at, updated_at) VALUES (?, ?, ?)",
                             (p.name, p.created_at, p.updated_at)).lastrowid
            db.executemany("INSERT INTO playlist_tracks (playlist_id, position, key, data, added_at) VALUES (?, ?, ?, ?, ?)",
                           [(pid, i, k, dump(e.track), e.added_at) for i, (k, e) in enumerate(zip(keys, entries))])
            existing.setdefault(p.name, set()).add(keys)
            imported["playlists"] += 1
            imported["playlist_tracks"] += len(entries)
        if settings:
            db.executemany("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", settings.items())
            imported["settings"] = len(settings)
            if "history_limit" in settings:
                userdata._trim(db, int(settings["history_limit"]))
    return {"ok": True, "imported": imported, "skipped": skipped}


@router.post("/import")
async def import_library(request: Request):
    """Импорт JSON-экспорта (тело запроса — содержимое файла). Всё или ничего: см. описание модуля."""
    declared = request.headers.get("content-length")
    if declared is not None and (not declared.isdigit() or int(declared) > IMPORT_MAX_BYTES):
        raise HTTPException(413, "Файл экспорта слишком большой.")
    raw = bytearray()
    async for chunk in request.stream():
        raw += chunk
        if len(raw) > IMPORT_MAX_BYTES:
            raise HTTPException(413, "Файл экспорта слишком большой.")
    doc, settings = await anyio.to_thread.run_sync(parse_import, bytes(raw))
    result = await anyio.to_thread.run_sync(apply_import, doc, settings)
    log.info("Импорт библиотеки: %s, пропущено %s", result["imported"], result["skipped"])
    return {**result, "settings": userdata._public()}
