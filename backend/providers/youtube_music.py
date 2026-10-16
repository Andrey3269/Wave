"""
Адаптер YouTube Music (S7): каталог через ytmusicapi.

Здесь всё, что зависит от формы ответов ytmusicapi: нормализация треков и карточек (перенесена из main.py
и catalog.py без изменения логики), сборка страниц артиста и альбома, «радио» трека. Наружу — только
Track, карточки и ProviderError. Аудиопоток YouTube резолвит stream.py (yt-dlp), не этот модуль.

Таймауты. ytmusicapi работает через requests и по умолчанию ждёт ответа до 30 с на каждый HTTP-запрос
(один вызов библиотеки может сделать несколько запросов). Клиент создаётся с собственной сессией requests,
у которой таймаут соединения и чтения короче (способ из документации ytmusicapi: `requests_session`).
Это настоящий предел: по нему зависший поток завершается сам. Таймаут ожидания в Provider.call — предел
для пользователя (см. providers/base.py).

Официального API YouTube Music нет: ytmusicapi эмулирует веб-клиент и может перестать работать при
изменениях YouTube (REPOSITORY_AUDIT §5). Сбои изолированы в этом адаптере.
"""
from __future__ import annotations

import json
import logging
import os
import re
import socket
from functools import partial

import requests
from ytmusicapi import YTMusic
from ytmusicapi.exceptions import YTMusicGatedError, YTMusicServerError, YTMusicUserError

from domain.models import Track, album_card, album_id_or_none, artist_card
from providers.base import Capabilities, Provider, ProviderError

log = logging.getLogger("musicmeta.providers.yt")

MAX_RESULTS = 100              # глубже одного запроса не листаем (как раньше в main.py и catalog.py)
ARTIST_SONGS_MAX = 500         # сколько песен артиста подгружаем максимум (было catalog.YT_ARTIST_MAX)
CONNECT_TIMEOUT = float(os.getenv("YTM_CONNECT_TIMEOUT", "5"))
READ_TIMEOUT = float(os.getenv("YTM_READ_TIMEOUT", "10"))


# ---------------------------------------------------------------------------
# Клиент
# ---------------------------------------------------------------------------
def make_session() -> requests.Session:
    """Сессия requests с таймаутом на каждый запрос (connect, read) — вместо 30 с по умолчанию в ytmusicapi."""
    s = requests.Session()
    s.request = partial(s.request, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))   # type: ignore[method-assign]
    return s


def create_client() -> YTMusic:
    """Клиент без авторизации (для каталога этого достаточно). Сеть при создании не используется."""
    return YTMusic(requests_session=make_session())


# ---------------------------------------------------------------------------
# Нормализация треков (перенесено из main.py без изменений)
# ---------------------------------------------------------------------------
_SIZE_RE = re.compile(r"=w\d+-h\d+")


def best_cover(thumbnails: list[dict] | None) -> str | None:
    """
    ytmusicapi отдаёт миниатюры в порядке возрастания размера (обычно 60px и 120px).
    Для крупной обложки (баннер главной) берём последнюю и, если это ссылка
    googleusercontent, подменяем размер на 544x544.
    """
    if not thumbnails:
        return None
    url = thumbnails[-1].get("url")
    return _SIZE_RE.sub("=w544-h544", url) if url else None


def parse_duration(item: dict) -> int | None:
    if isinstance(item.get("duration_seconds"), int):
        return item["duration_seconds"]
    raw = item.get("duration")  # формат "3:45" или "1:02:10"
    if not raw:
        return None
    try:
        parts = [int(p) for p in raw.split(":")]
    except ValueError:
        return None
    seconds = 0
    for p in parts:
        seconds = seconds * 60 + p
    return seconds


def normalize_track(item: dict) -> Track | None:
    video_id = item.get("videoId")
    if not video_id:  # в выдаче бывают элементы без видео (альбомы, плейлисты)
        return None

    artists = [a["name"] for a in (item.get("artists") or []) if a.get("name")]
    artist_id = next((a["id"] for a in (item.get("artists") or []) if a.get("name") and a.get("id")), None)
    artist_ids = [a.get("id") for a in (item.get("artists") or []) if a.get("name")]
    raw_album = item.get("album")       # в поиске это {"name": ..., "id": "MPRE…"}, а в get_album() просто строка с названием
    album = raw_album.get("name") if isinstance(raw_album, dict) else (raw_album if isinstance(raw_album, str) else None)
    album_id = album_id_or_none(raw_album.get("id")) if isinstance(raw_album, dict) else None   # S10

    return Track(
        id=video_id,
        source="yt",
        title=item.get("title") or "Без названия",
        artist=", ".join(artists) or "Неизвестный исполнитель",
        artists=artists,
        album=album,
        album_id=album_id,
        duration=parse_duration(item),
        cover=best_cover(item.get("thumbnails")),
        explicit=bool(item.get("isExplicit")),
        stream_url=f"/api/stream/yt/{video_id}",
        artist_id=artist_id,
        artist_ids=artist_ids,
        url=f"https://music.youtube.com/watch?v={video_id}",
    )


# ---------------------------------------------------------------------------
# Карточки (перенесено из catalog.py без изменений)
# ---------------------------------------------------------------------------
def card_image(thumbs) -> str | None:
    return _SIZE_RE.sub("=w544-h544", thumbs[-1]["url"]) if thumbs else None


def _album_from(x: dict, artist: str) -> dict:
    return album_card("yt", x["browseId"], x.get("title"), artist, x.get("year"), card_image(x.get("thumbnails")),
                      playlist_id=x.get("audioPlaylistId") or x.get("playlistId"))


# ---------------------------------------------------------------------------
# Блокирующие вызовы ytmusicapi (выполняются в пуле адаптера)
# ---------------------------------------------------------------------------
def search_songs_sync(ytm, query: str, limit: int, offset: int = 0) -> list[Track]:
    """Песни по запросу: первые `limit` (не больше MAX_RESULTS) результатов, с позиции `offset`."""
    raw = ytm.search(query, filter="songs", limit=min(limit, MAX_RESULTS))
    return [t for t in map(normalize_track, raw[offset:]) if t is not None]


def search_cards_sync(ytm, q: str, kind: str, n: int) -> list[dict]:
    out = []
    for it in ytm.search(q, filter=kind, limit=n):
        bid = it.get("browseId")
        if not bid:
            continue
        if kind == "artists":
            subs = it.get("subscribers")
            out.append(artist_card("yt", bid, it.get("artist") or "—", card_image(it.get("thumbnails")),
                                   f"{subs} подписчиков" if subs else None))
        else:
            who = ", ".join(a["name"] for a in it.get("artists") or [] if a.get("name"))
            out.append(album_card("yt", bid, it.get("title"), who, it.get("year"), card_image(it.get("thumbnails")),
                                  playlist_id=it.get("playlistId") or it.get("audioPlaylistId")))
    return out


def radio_sync(ytm, vid: str, n: int) -> list[dict]:
    """«Радио» трека (get_watch_playlist): сырые элементы, приведённые к форме выдачи поиска."""
    out = []
    for it in ytm.get_watch_playlist(videoId=vid, limit=n).get("tracks") or []:
        it = dict(it)
        it.setdefault("thumbnails", it.get("thumbnail"))   # в этом ответе ключ «thumbnail»
        it["duration"] = it.get("length")
        out.append(it)
    return out


# ---------------------------------------------------------------------------
# Адаптер
# ---------------------------------------------------------------------------
_HTTP_STATUS = re.compile(r"HTTP (\d{3})")


class YouTubeMusicProvider(Provider):
    id = "yt"
    label = "YouTube Music"
    capabilities = Capabilities(
        search_tracks=True, search_artists=True, search_albums=True, artist_page=True, album_page=True,
        radio=True,                 # get_watch_playlist (рекомендации)
        stream=True,                # stream.py: yt-dlp + JS-runtime (S3)
        download=True,              # ADR-10: сохраняется поток, который играет плеер (download_policy.py)
        attribution_required=False,
        mixing_allowed=True,        # поиск чередует источники, очередь общая
    )
    workers = int(os.getenv("YTM_WORKERS", "4"))
    max_pending = int(os.getenv("YTM_MAX_PENDING", "8"))
    # Пауза после сбоя (находка M3): короче, чем у SoundCloud (300 с), — YouTube Music основной источник,
    # и случайный сбой не должен надолго выключать поиск. Без паузы при «висящей» сети каждый поиск ждал
    # таймаут заново.
    cooldown = float(os.getenv("YT_SOURCE_COOLDOWN", "45"))

    def classify(self, exc: BaseException) -> ProviderError:
        if isinstance(exc, ProviderError):
            return exc
        if isinstance(exc, (requests.Timeout, socket.timeout, TimeoutError)):
            return self.error("timeout")
        if isinstance(exc, requests.RequestException):       # нет соединения, DNS, прокси, TLS
            return self.error("unavailable")
        if isinstance(exc, YTMusicGatedError):                # YouTube просит действие в диалоге (согласие и т.п.)
            return self.error("restricted")
        if isinstance(exc, YTMusicServerError):
            m = _HTTP_STATUS.search(str(exc))
            status = int(m.group(1)) if m else 0
            if status == 429:
                return self.error("rate_limited")
            if status in (400, 404):
                return self.error("not_found")
            if status in (401, 403):
                return self.error("restricted")
            if status >= 500:
                return self.error("unavailable")
            return self.error("internal")
        if isinstance(exc, YTMusicUserError):                 # неверный browseId и т.п.
            return self.error("not_found")
        if isinstance(exc, json.JSONDecodeError):             # вместо JSON пришла страница (защита, обслуживание)
            return self.error("unavailable")
        return super().classify(exc)

    # ---- поиск ----
    async def search_tracks(self, state, q: str, limit: int, offset: int = 0, *, timeout: float) -> list[Track]:
        return await self.call("search_tracks", search_songs_sync, state.ytm, q, limit, offset, timeout=timeout)

    async def search_cards(self, state, kind: str, q: str, limit: int, offset: int, *,
                           timeout: float) -> tuple[list[dict], bool]:
        raw = await self.call("search_cards", search_cards_sync, state.ytm, q, kind, min(offset + limit, MAX_RESULTS),
                              timeout=timeout)
        items = raw[offset:offset + limit]
        more = bool(items) and len(raw) >= offset + limit and offset + limit < MAX_RESULTS
        return items, more

    # ---- страница артиста ----
    async def artist_info(self, state, artist_id: str, *, timeout: float) -> tuple[dict, bool]:
        """Шапка, релизы, похожие артисты и первая порция песен. degraded — полный список релизов не получен."""
        ytm = state.ytm
        a = await self.call("artist_page", ytm.get_artist, artist_id, timeout=timeout)
        name, songs = a.get("name") or "—", a.get("songs") or {}
        albums = [_album_from(x, name)
                  for sec in ("albums", "singles") for x in ((a.get(sec) or {}).get("results") or []) if x.get("browseId")]
        degraded = False
        # На главной артиста только часть релизов: полный список альбомов и синглов берём отдельным запросом
        for sec in ("albums", "singles"):
            blk = a.get(sec) or {}
            if blk.get("browseId") and blk.get("params"):
                try:
                    more = await self.call("artist_page", ytm.get_artist_albums, blk["browseId"], blk["params"], 100,
                                           timeout=timeout, optional=True)
                except ProviderError as exc:
                    log.info("Полный список %s не получен: %r", sec, exc)
                    degraded = True
                    continue
                albums += [_album_from(x, name) for x in more if x.get("browseId")]
        seen: set = set()
        albums = [x for x in albums if not (x["id"] in seen or seen.add(x["id"]))]
        subs = a.get("subscribers")
        related = [artist_card("yt", x["browseId"], x.get("title") or "—", card_image(x.get("thumbnails")),
                               f"{x['subscribers']} подписчиков" if x.get("subscribers") else None)
                   for x in ((a.get("related") or {}).get("results") or []) if x.get("browseId")]
        info = {"name": name, "cover": card_image(a.get("thumbnails")), "subtitle": f"{subs} подписчиков" if subs else None,
                "songs_id": songs.get("browseId"), "songs_raw": songs.get("results") or [], "albums": albums,
                "related": related}
        return info, degraded

    async def artist_tracks(self, state, artist_id: str, info: dict, offset: int, limit: int, *,
                            timeout: float) -> tuple[list[Track], bool]:
        """Страница песен артиста. Кэш песен (в info) растёт по мере прокрутки: 40, 80, 160 … до ARTIST_SONGS_MAX."""
        need = offset + limit
        raw = info.get("songs_all")
        if info["songs_id"] and (raw is None or (need > len(raw) and len(raw) >= info["songs_lim"]
                                                 and info["songs_lim"] < ARTIST_SONGS_MAX)):
            lim = min(ARTIST_SONGS_MAX, max(40, need * 2))
            pl = await self.call("artist_page", state.ytm.get_playlist, info["songs_id"], lim, timeout=timeout)
            raw, info["songs_all"], info["songs_lim"] = pl.get("tracks") or [], pl.get("tracks") or [], lim
        elif raw is None:
            raw = info["songs_all"] = info["songs_raw"]
            info["songs_lim"] = len(raw)
        page = raw[offset:offset + limit]
        tracks = [t for t in map(normalize_track, page) if t]
        more = need < len(raw) or (len(raw) >= info["songs_lim"] and info["songs_lim"] < ARTIST_SONGS_MAX
                                   and bool(info["songs_id"]))
        return tracks, bool(page) and more

    # ---- страница альбома ----
    async def album(self, state, album_id: str, playlist_id: str | None, *,
                    timeout: float) -> tuple[dict, list[Track], int, bool]:
        """(карточка, треки, сколько недоступно, degraded). degraded — альбом собран запасным путём (как плейлист)."""
        ytm = state.ytm
        a: dict = {}
        degraded = False
        try:
            # С запасным путём (плейлист альбома) сбой get_album необязателен: страница соберётся без него
            a = await self.call("album_page", ytm.get_album, album_id, timeout=timeout,
                                optional=bool(playlist_id)) or {}
        except ProviderError as exc:                    # ytmusicapi иногда не может разобрать страницу альбома
            log.info("get_album %s не разобран: %r", album_id, exc)
            if not playlist_id:
                raise
            degraded = True
        raw, meta = a.get("tracks") or [], a
        pl_id = playlist_id or a.get("audioPlaylistId")
        if not raw and pl_id:                           # запасной путь: треки альбома как обычный плейлист
            p = await self.call("album_page", ytm.get_playlist, pl_id, 200, timeout=timeout)
            raw = p.get("tracks") or []
            meta = {**p, **{k: v for k, v in a.items() if v}}
            if not meta.get("artists") and (p.get("author") or {}).get("name"):
                meta["artists"] = [{"name": p["author"]["name"]}]
        cover = card_image(meta.get("thumbnails"))
        who = ", ".join(x["name"] for x in meta.get("artists") or [] if x.get("name"))
        tracks, skipped = [], 0
        for it in raw:
            t = normalize_track(it)
            if not t:                                   # трек недоступен (нет videoId)
                skipped += 1
                continue
            t.cover = t.cover or cover
            t.album = t.album or meta.get("title")
            t.album_id = t.album_id or album_id_or_none(album_id)          # S10: трек этой страницы альбома
            if not t.artists and who:
                t.artists, t.artist = [who], who
            tracks.append(t)
        card = album_card("yt", album_id, meta.get("title"), who, str(meta.get("year") or ""), cover,
                          meta.get("trackCount") or len(raw), pl_id)
        return card, tracks, skipped, degraded

    # ---- радио ----
    async def radio(self, state, track_id: str, n: int, *, timeout: float) -> list[Track]:
        raw = await self.call("radio", radio_sync, state.ytm, track_id, n, timeout=timeout)
        return [t for t in map(normalize_track, raw) if t]


PROVIDER = YouTubeMusicProvider()
