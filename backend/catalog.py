"""
Каталог: поиск артистов и альбомов, страницы артиста и альбома.

YouTube Music идёт через ytmusicapi (search с filter, get_artist, get_album, get_playlist),
SoundCloud через API v2 (search/users, search/albums, users/{id}/tracks, playlists/{id}).
Всё приводится к единым карточкам: artist {id, source, kind, name, cover, subtitle}
и album {id, source, kind, title, artist, year, cover, track_count}.
Нормализаторы треков передаёт main.py через configure() (так нет циклического импорта).
"""
from __future__ import annotations

import asyncio
import logging
import re
import time

from fastapi import APIRouter, HTTPException, Query, Request

import soundcloud as sc_api

log = logging.getLogger("musicmeta.catalog")
router = APIRouter(prefix="/api", tags=["catalog"])

MAX_RESULTS = 100
YT_ARTIST_MAX = 500            # сколько песен артиста YouTube Music подгружаем максимум
_ID_RE = re.compile(r"^[\w-]{1,64}$")
_norm: dict = {}
_artist_cache: dict[tuple[str, str], tuple[float, dict]] = {}


def configure(yt_norm, sc_norm) -> None:
    _norm["yt"], _norm["sc"] = yt_norm, sc_norm


# ---------- мелкие помощники ----------
_SIZE = re.compile(r"=w\d+-h\d+")
_LARGE = re.compile(r"-large\.")


def _yt_img(thumbs) -> str | None:
    return _SIZE.sub("=w544-h544", thumbs[-1]["url"]) if thumbs else None


def _sc_img(url: str | None) -> str | None:
    return _LARGE.sub("-t500x500.", url) if url else None


def _artist_card(source, id_, name, cover, subtitle=None) -> dict:
    return {"id": str(id_), "source": source, "kind": "artist", "name": name, "cover": cover, "subtitle": subtitle}


def _album_card(source, id_, title, artist, year, cover, count=None, playlist_id=None) -> dict:
    return {"id": str(id_), "source": source, "kind": "album", "title": title or "Без названия",
            "artist": artist, "year": (year or "")[:4] or None, "cover": cover, "track_count": count,
            "playlist_id": playlist_id}


async def _run(fn, *args):
    return await asyncio.wait_for(asyncio.to_thread(fn, *args), 25)


def _check(source: str, id_: str) -> None:
    if source not in ("yt", "sc") or not _ID_RE.fullmatch(id_):
        raise HTTPException(400, "Некорректный запрос")


def _fail(exc: Exception) -> HTTPException:
    if isinstance(exc, HTTPException):
        return exc
    log.warning("Каталог: %r", exc)
    return HTTPException(502, "Источник временно недоступен. Попробуйте позже.")


# ---------- YouTube Music ----------
def _yt_search(ytm, q: str, kind: str, n: int) -> list[dict]:
    out = []
    for it in ytm.search(q, filter=kind, limit=n):
        bid = it.get("browseId")
        if not bid:
            continue
        if kind == "artists":
            subs = it.get("subscribers")
            out.append(_artist_card("yt", bid, it.get("artist") or "—", _yt_img(it.get("thumbnails")),
                                    f"{subs} подписчиков" if subs else None))
        else:
            who = ", ".join(a["name"] for a in it.get("artists") or [] if a.get("name"))
            out.append(_album_card("yt", bid, it.get("title"), who, it.get("year"), _yt_img(it.get("thumbnails")),
                                   playlist_id=it.get("playlistId") or it.get("audioPlaylistId")))
    return out


# ---------- SoundCloud ----------
def _sc_artist(u: dict) -> dict:
    n = u.get("followers_count")
    return _artist_card("sc", u["id"], u.get("username") or "—", _sc_img(u.get("avatar_url")),
                        f"{n:,} подписчиков".replace(",", " ") if n else None)


def _sc_album(p: dict) -> dict:
    user = p.get("user") or {}
    year = p.get("release_date") or p.get("display_date") or p.get("created_at")
    return _album_card("sc", p["id"], p.get("title"), user.get("username"), year,
                       _sc_img(p.get("artwork_url") or user.get("avatar_url")), p.get("track_count"))


# ---------- поиск артистов и альбомов ----------
@router.get("/search/{kind}")
async def search_cards(request: Request, kind: str, q: str = Query(..., min_length=1, max_length=200),
                       source: str = "yt", offset: int = Query(0, ge=0, le=MAX_RESULTS),
                       limit: int = Query(20, ge=1, le=50)):
    if kind not in ("artists", "albums") or source not in ("yt", "sc"):
        raise HTTPException(400, "Некорректный запрос")
    try:
        if source == "yt":
            raw = await _run(_yt_search, request.app.state.ytm, q.strip(), kind, min(offset + limit, MAX_RESULTS))
            items = raw[offset:offset + limit]
            more = bool(items) and len(raw) >= offset + limit and offset + limit < MAX_RESULTS
        else:
            data = await _run(sc_api.api_get, "search/users" if kind == "artists" else "search/albums",
                              {"q": q.strip(), "limit": limit, "offset": offset, "linked_partitioning": 1})
            conv = _sc_artist if kind == "artists" else _sc_album
            items = [conv(x) for x in data.get("collection") or [] if x.get("id")]
            more = bool(data.get("next_href")) and offset + limit < MAX_RESULTS
    except Exception as exc:
        raise _fail(exc) from exc
    return {"items": items, "has_more": more}


# ---------- страница артиста ----------
async def _artist_info(request: Request, source: str, id_: str) -> dict:
    hit = _artist_cache.get((source, id_))
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    if source == "yt":
        a = await _run(request.app.state.ytm.get_artist, id_)
        name, songs = a.get("name") or "—", a.get("songs") or {}
        albums = [_album_card("yt", x["browseId"], x.get("title"), name, x.get("year"), _yt_img(x.get("thumbnails")),
                             playlist_id=x.get("audioPlaylistId") or x.get("playlistId"))
                  for sec in ("albums", "singles") for x in ((a.get(sec) or {}).get("results") or []) if x.get("browseId")]
        # На главной артиста только часть релизов: полный список альбомов и синглов берём отдельным запросом
        ytm = request.app.state.ytm
        for sec in ("albums", "singles"):
            blk = a.get(sec) or {}
            if blk.get("browseId") and blk.get("params"):
                try:
                    more = await _run(ytm.get_artist_albums, blk["browseId"], blk["params"], 100)
                except Exception as exc:
                    log.info("Полный список %s не получен: %r", sec, exc)
                    continue
                albums += [_album_card("yt", x["browseId"], x.get("title"), name, x.get("year"), _yt_img(x.get("thumbnails")),
                                       playlist_id=x.get("audioPlaylistId") or x.get("playlistId"))
                           for x in more if x.get("browseId")]
        seen: set = set()
        albums = [x for x in albums if not (x["id"] in seen or seen.add(x["id"]))]
        subs = a.get("subscribers")
        related = [_artist_card("yt", x["browseId"], x.get("title") or "—", _yt_img(x.get("thumbnails")),
                                f"{x['subscribers']} подписчиков" if x.get("subscribers") else None)
                   for x in ((a.get("related") or {}).get("results") or []) if x.get("browseId")]
        info = {"name": name, "cover": _yt_img(a.get("thumbnails")), "subtitle": f"{subs} подписчиков" if subs else None,
                "songs_id": songs.get("browseId"), "songs_raw": songs.get("results") or [], "albums": albums, "related": related}
    else:
        u = await _run(sc_api.api_get, f"users/{id_}", {})
        try:
            alb = await _run(sc_api.api_get, f"users/{id_}/albums", {"limit": 30, "linked_partitioning": 1})
            albums = [_sc_album(p) for p in alb.get("collection") or [] if p.get("id")]
        except Exception as exc:                     # у многих артистов альбомов нет: это не ошибка страницы
            log.info("Альбомы SC недоступны: %r", exc)
            albums = []
        n = u.get("followers_count")
        info = {"name": u.get("username") or "—", "cover": _sc_img(u.get("avatar_url")),
                "subtitle": f"{n:,} подписчиков".replace(",", " ") if n else None, "albums": albums}
    _artist_cache[(source, id_)] = (time.time(), info)
    return info


async def _artist_tracks(request: Request, source: str, id_: str, offset: int, limit: int):
    info = await _artist_info(request, source, id_)
    if source == "yt":
        need = offset + limit
        raw = info.get("songs_all")
        # Кэш песен артиста растёт по мере прокрутки (40, 80, 160 ... до YT_ARTIST_MAX), страницы режутся из него
        if info["songs_id"] and (raw is None or (need > len(raw) and len(raw) >= info["songs_lim"] and info["songs_lim"] < YT_ARTIST_MAX)):
            lim = min(YT_ARTIST_MAX, max(40, need * 2))
            pl = await _run(request.app.state.ytm.get_playlist, info["songs_id"], lim)
            raw, info["songs_all"], info["songs_lim"] = pl.get("tracks") or [], pl.get("tracks") or [], lim
        elif raw is None:
            raw = info["songs_all"] = info["songs_raw"]
            info["songs_lim"] = len(raw)
        page = raw[offset:offset + limit]
        tracks = [t for t in map(_norm["yt"], page) if t]
        more = need < len(raw) or (len(raw) >= info["songs_lim"] and info["songs_lim"] < YT_ARTIST_MAX and bool(info["songs_id"]))
        return tracks, bool(page) and more
    data = await _run(sc_api.api_get, f"users/{id_}/tracks", {"limit": limit, "offset": offset, "linked_partitioning": 1})
    tracks = [t for t in map(_norm["sc"], data.get("collection") or []) if t]
    return tracks, bool(data.get("next_href"))


@router.get("/artist/{source}/{artist_id}")
async def artist_page(request: Request, source: str, artist_id: str):
    _check(source, artist_id)
    try:
        info = await _artist_info(request, source, artist_id)
        tracks, more = await _artist_tracks(request, source, artist_id, 0, 20)
    except Exception as exc:
        raise _fail(exc) from exc
    head = {k: info[k] for k in ("name", "cover", "subtitle")}
    return {"artist": head, "albums": info["albums"], "related": info.get("related", []), "tracks": tracks, "has_more": more}


@router.get("/artist/{source}/{artist_id}/tracks")
async def artist_tracks(request: Request, source: str, artist_id: str,
                        offset: int = Query(0, ge=0, le=5000), limit: int = Query(20, ge=1, le=50)):
    _check(source, artist_id)
    try:
        tracks, more = await _artist_tracks(request, source, artist_id, offset, limit)
    except Exception as exc:
        raise _fail(exc) from exc
    return {"tracks": tracks, "has_more": more}


# ---------- страница альбома ----------
async def _yt_album(request: Request, album_id: str, pl: str | None):
    ytm = request.app.state.ytm
    a: dict = {}
    try:
        a = await _run(ytm.get_album, album_id) or {}
    except Exception as exc:                        # ytmusicapi иногда не может разобрать страницу альбома
        log.info("get_album %s не разобран: %r", album_id, exc)
        if not pl:
            raise
    raw, meta = a.get("tracks") or [], a
    pl_id = pl or a.get("audioPlaylistId")
    if not raw and pl_id:                           # запасной путь: треки альбома как обычный плейлист
        p = await _run(ytm.get_playlist, pl_id, 200)
        raw = p.get("tracks") or []
        meta = {**p, **{k: v for k, v in a.items() if v}}
        if not meta.get("artists") and (p.get("author") or {}).get("name"):
            meta["artists"] = [{"name": p["author"]["name"]}]
    cover = _yt_img(meta.get("thumbnails"))
    who = ", ".join(x["name"] for x in meta.get("artists") or [] if x.get("name"))
    tracks, skipped = [], 0
    for it in raw:
        t = _norm["yt"](it)
        if not t:                                   # трек недоступен (нет videoId)
            skipped += 1
            continue
        t.cover = t.cover or cover
        t.album = t.album or meta.get("title")
        if not t.artists and who:
            t.artists, t.artist = [who], who
        tracks.append(t)
    card = _album_card("yt", album_id, meta.get("title"), who, str(meta.get("year") or ""), cover,
                       meta.get("trackCount") or len(raw), pl_id)
    return card, tracks, skipped


async def _sc_album_page(album_id: str):
    p = await _run(sc_api.api_get, f"playlists/{album_id}", {})
    raw = p.get("tracks") or []
    if not raw and p.get("track_count"):            # в ответе нет списка треков: пробуем полное представление
        try:
            raw = (await _run(sc_api.api_get, f"playlists/{album_id}", {"representation": "full"})).get("tracks") or []
        except Exception as exc:
            log.info("playlists/%s full: %r", album_id, exc)
    stubs = [x["id"] for x in raw if not x.get("title")]      # SoundCloud отдаёт полные данные только первых треков
    full: dict = {}
    for i in range(0, len(stubs), 30):
        batch = await _run(sc_api.api_get, "tracks", {"ids": ",".join(map(str, stubs[i:i + 30]))})
        full.update({x["id"]: x for x in batch or []})
    items = [x if x.get("title") else full.get(x["id"]) for x in raw]
    tracks = [t for t in (_norm["sc"](x) for x in items if x) if t]
    # Не попали в список: превью Go+ (30 секунд) и треки, закрытые в регионе
    return _sc_album({**p, "id": p.get("id") or album_id}), tracks, max(len(raw), p.get("track_count") or 0) - len(tracks)


@router.get("/album/{source}/{album_id}")
async def album_page(request: Request, source: str, album_id: str, pl: str | None = Query(None)):
    _check(source, album_id)
    if pl is not None and not _ID_RE.fullmatch(pl):
        raise HTTPException(400, "Некорректный запрос")
    try:
        card, tracks, skipped = await (_yt_album(request, album_id, pl) if source == "yt" else _sc_album_page(album_id))
    except Exception as exc:
        raise _fail(exc) from exc
    return {"album": card, "tracks": tracks, "unavailable": max(0, skipped)}