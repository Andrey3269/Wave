"""
Адаптер SoundCloud (S7): каталог через API v2 SoundCloud (находка M10 — изолирована, не устранена).

Как это работает (как и до S7). yt-dlp умеет искать (scsearch:), но возвращает только ссылки без
названий и обложек. Поэтому Wave вызывает API v2 SoundCloud сам, а client_id веб-клиента SoundCloud
(SoundCloud его периодически меняет) берёт у экстрактора yt-dlp: тот обновляет его автоматически и хранит
в своём дисковом кэше. Используются ВНУТРЕННИЕ методы yt-dlp (get_info_extractor, _call_api, а для очень
старых версий — _download_json, _update_client_id). API v2 — неофициальный интерфейс сайта.

Почему не официальный API (проверено 2026-10-13, developers.soundcloud.com): регистрация приложения и
получение ключей доступны только с подпиской Artist Pro; все клиенты считаются конфиденциальными, секрет
клиента нельзя встраивать в распространяемое приложение; условия API запрещают сохранение контента и
офлайн-доступ, а также воспроизведение вместе с контентом других сервисов (например, YouTube). Это
противоречит принятым решениям владельца (ADR-10: загрузки, общая очередь), поэтому замена требует
отдельного решения. Подробности — docs/engineering/reports/S7.md, раздел M10.

Что сделано в S7 вместо замены — изоляция:
  * все вызовы идут через Provider.call: свой пул (2 потока + 4 в очереди), таймаут, cooldown 300 с;
  * исчезновение или изменение внутренних методов yt-dlp (AttributeError и т.п.) — ошибка этого
    адаптера (internal), YouTube Music и локальная библиотека продолжают работать;
  * вместо одного экземпляра yt-dlp под глобальной блокировкой (все запросы SoundCloud шли строго по
    одному, и зависший запрос держал остальные) — свой экземпляр в каждом потоке пула;
  * socket_timeout yt-dlp ограничивает каждый HTTP-запрос: по нему зависший поток завершается сам.

Аудиопоток SoundCloud резолвит stream.py (yt-dlp), не этот модуль.
"""
from __future__ import annotations

import logging
import os
import re
import socket
import threading

from yt_dlp import YoutubeDL
from yt_dlp.networking.exceptions import HTTPError as YdlHTTPError, TransportError
from yt_dlp.utils import ExtractorError

import stream
from domain.models import Track, album_card, artist_card
from providers.base import Capabilities, Provider, ProviderError

log = logging.getLogger("musicmeta.providers.sc")

API_BASE = "https://api-v2.soundcloud.com/"
API_SEARCH = API_BASE + "search/tracks"
SOCKET_TIMEOUT = float(os.getenv("SC_SOCKET_TIMEOUT", "7"))
_NOTE = "Поиск на SoundCloud"


# ---------------------------------------------------------------------------
# Клиент API v2 (перенесено из backend/soundcloud.py)
# ---------------------------------------------------------------------------
def _api_get(ie, url: str, query: dict) -> dict:
    """
    Запрос к API SoundCloud с client_id.

    В современном yt-dlp метод _call_api сам добавляет client_id и при ответе 401/403
    скачивает свежий. Метод _download_json client_id НЕ добавляет, и без него
    SoundCloud отвечает 401 Unauthorized.
    """
    call_api = getattr(ie, "_call_api", None)
    if call_api is not None:
        return call_api(url, "soundcloud-search", note=_NOTE, query=query)

    # Запасной путь для очень старых версий yt-dlp: подставляем client_id сами
    for attempt in range(2):
        try:
            return ie._download_json(
                url, "soundcloud-search", note=_NOTE, query={**query, "client_id": ie._CLIENT_ID}
            )
        except ExtractorError as exc:
            status = getattr(exc.cause, "status", None) or getattr(exc.cause, "code", None)
            if attempt == 0 and status in (401, 403):
                ie._update_client_id()   # скачать свежий client_id и повторить
                continue
            raise
    return {}


def ydl_options() -> dict:
    """Настройки yt-dlp для запросов к API: общие (прокси, cookies, IPv4) + свой таймаут сокета."""
    opts = stream.ydl_opts()
    opts["socket_timeout"] = SOCKET_TIMEOUT
    return opts


# Свой экземпляр экстрактора в каждом потоке пула: client_id и соединения остаются в памяти потока,
# поэтому повторные запросы не тратят время на инициализацию. Блокировка между потоками не нужна.
_tls = threading.local()


def _get_ie():
    ie = getattr(_tls, "ie", None)
    if ie is None:
        ydl = YoutubeDL(ydl_options())
        ie = ydl.get_info_extractor("Soundcloud")
        ie.initialize()  # подтягивает client_id из кэша (или обновляет)
        _tls.ie, _tls.ydl = ie, ydl
    return ie


def _drop_ie() -> None:
    """При сбое следующий запрос этого потока создаст клиент заново."""
    ydl = getattr(_tls, "ydl", None)
    _tls.ie = _tls.ydl = None
    if ydl is not None:
        try:
            ydl.close()
        except Exception:
            pass


def search_raw(query: str, limit: int) -> list[dict]:
    """Блокирующий вызов (в пуле адаптера). «Сырые» объекты треков API (как раньше soundcloud.search_tracks)."""
    try:
        data = _api_get(_get_ie(), API_SEARCH,
                        {"q": query, "limit": min(limit, 100), "offset": 0, "linked_partitioning": 1})
    except Exception:
        _drop_ie()
        raise
    return (data or {}).get("collection") or []


def api_get(path: str, query: dict | None = None):
    """Любой GET к API v2 SoundCloud (users/{id}, playlists/{id}, search/users ...). Блокирующий."""
    try:
        return _api_get(_get_ie(), API_BASE + path, query or {})
    except Exception:
        _drop_ie()
        raise


# ---------------------------------------------------------------------------
# Нормализация (перенесено из main.py и catalog.py без изменений)
# ---------------------------------------------------------------------------
_ART_RE = re.compile(r"-large\.")


def normalize_track(item: dict) -> Track | None:
    if item.get("kind", "track") != "track" or not item.get("id"):
        return None
    # BLOCK = недоступен в регионе, SNIP = только 30-секундный превью (Go+): пропускаем,
    # иначе трек оборвётся на 30-й секунде.
    if item.get("policy") in ("BLOCK", "SNIP") or item.get("streamable") is False:
        return None

    track_id = str(item["id"])
    user = item.get("user") or {}
    meta = item.get("publisher_metadata") or {}
    artist = meta.get("artist") or user.get("username") or "Неизвестный исполнитель"

    millis = item.get("full_duration") or item.get("duration")
    art = item.get("artwork_url") or user.get("avatar_url")

    return Track(
        id=track_id,
        source="sc",
        title=item.get("title") or "Без названия",
        artist=artist,
        artists=[artist],
        album=meta.get("album_title"),
        duration=round(millis / 1000) if millis else None,
        # SoundCloud отдаёт 100x100 ("-large"); просим 500x500
        cover=_ART_RE.sub("-t500x500.", art) if art else None,
        explicit=bool(meta.get("explicit")),
        stream_url=f"/api/stream/sc/{track_id}",
        artist_id=str(user["id"]) if user.get("id") else None,   # страница артиста = страница загрузившего
        artist_ids=[str(user["id"])] if user.get("id") else [],
        plays=item.get("playback_count"),
        released=item.get("display_date") or item.get("created_at"),
        url=item.get("permalink_url"),
    )


def card_image(url: str | None) -> str | None:
    return _ART_RE.sub("-t500x500.", url) if url else None


def artist_from_user(u: dict) -> dict:
    n = u.get("followers_count")
    return artist_card("sc", u["id"], u.get("username") or "—", card_image(u.get("avatar_url")),
                       f"{n:,} подписчиков".replace(",", " ") if n else None)


def album_from_playlist(p: dict) -> dict:
    user = p.get("user") or {}
    year = p.get("release_date") or p.get("display_date") or p.get("created_at")
    return album_card("sc", p["id"], p.get("title"), user.get("username"), year,
                      card_image(p.get("artwork_url") or user.get("avatar_url")), p.get("track_count"))


def _tracks(items) -> list[Track]:
    return [t for t in map(normalize_track, items or []) if t]


# ---------------------------------------------------------------------------
# Адаптер
# ---------------------------------------------------------------------------
def _causes(exc: BaseException):
    """Цепочка причин: ExtractorError.cause, __cause__, __context__ (без зацикливания)."""
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        yield exc
        exc = getattr(exc, "cause", None) or exc.__cause__ or exc.__context__


class SoundCloudProvider(Provider):
    id = "sc"
    label = "SoundCloud"
    capabilities = Capabilities(
        search_tracks=True, search_artists=True, search_albums=True, artist_page=True, album_page=True,
        radio=True,                 # tracks/{id}/related
        stream=True,                # stream.py: только прогрессивный поток
        download=True,              # ADR-10 (download_policy.py); 30-секундный фрагмент Go+ не сохраняется
        attribution_required=True,  # условия SoundCloud; Wave показывает автора и ссылку на трек (url), логотипа нет
        mixing_allowed=True,        # так ведёт себя Wave (решение владельца); условия API SoundCloud это запрещают
    )
    workers = int(os.getenv("SC_WORKERS", "2"))
    max_pending = int(os.getenv("SC_MAX_PENDING", "4"))
    cooldown = float(os.getenv("SOURCE_COOLDOWN", "300"))   # имя переменной — как до S7

    def classify(self, exc: BaseException) -> ProviderError:
        if isinstance(exc, ProviderError):
            return exc
        for c in _causes(exc):
            if isinstance(c, YdlHTTPError):
                status = c.status
                if status == 429:
                    return self.error("rate_limited")
                if status in (401, 403):        # client_id не принят даже после обновления
                    return self.error("auth_required")
                if status in (404, 410):
                    return self.error("not_found")
                if status >= 500:
                    return self.error("unavailable")
                return self.error("internal")
            if isinstance(c, (socket.timeout, TimeoutError)):
                return self.error("timeout")
            if isinstance(c, TransportError):
                return self.error("timeout" if "timed out" in str(c).lower() else "unavailable")
        return super().classify(exc)

    # ---- поиск ----
    async def search_tracks(self, state, q: str, limit: int, offset: int = 0, *, timeout: float) -> list[Track]:
        raw = await self.call("search_tracks", search_raw, q, limit, timeout=timeout)
        return _tracks(raw[offset:])

    async def search_tracks_plain(self, state, q: str, n: int, offset: int = 0, *, timeout: float) -> list[Track]:
        """Поиск треков запросом GET search/tracks {q, limit} — так его делали рекомендации (recs.py) до S7."""
        data = await self.call("search_tracks", api_get, "search/tracks", {"q": q, "limit": n}, timeout=timeout)
        return _tracks(((data or {}).get("collection") or [])[offset:])

    async def search_cards(self, state, kind: str, q: str, limit: int, offset: int, *,
                           timeout: float) -> tuple[list[dict], bool]:
        data = await self.call("search_cards", api_get, "search/users" if kind == "artists" else "search/albums",
                               {"q": q, "limit": limit, "offset": offset, "linked_partitioning": 1}, timeout=timeout)
        conv = artist_from_user if kind == "artists" else album_from_playlist
        items = [conv(x) for x in data.get("collection") or [] if x.get("id")]
        more = bool(data.get("next_href")) and offset + limit < 100
        return items, more

    # ---- страница артиста ----
    async def artist_info(self, state, artist_id: str, *, timeout: float) -> tuple[dict, bool]:
        u = await self.call("artist_page", api_get, f"users/{artist_id}", {}, timeout=timeout)
        degraded = False
        try:
            alb = await self.call("artist_page", api_get, f"users/{artist_id}/albums",
                                  {"limit": 30, "linked_partitioning": 1}, timeout=timeout, optional=True)
            albums = [album_from_playlist(p) for p in alb.get("collection") or [] if p.get("id")]
        except ProviderError as exc:        # страница артиста показывается и без альбомов
            log.info("Альбомы SC недоступны: %r", exc)
            albums = []
            degraded = exc.kind != "not_found"      # альбомов нет — это не сбой
        n = u.get("followers_count")
        info = {"name": u.get("username") or "—", "cover": card_image(u.get("avatar_url")),
                "subtitle": f"{n:,} подписчиков".replace(",", " ") if n else None, "albums": albums}
        return info, degraded

    async def artist_tracks(self, state, artist_id: str, info: dict, offset: int, limit: int, *,
                            timeout: float) -> tuple[list[Track], bool]:
        data = await self.call("artist_page", api_get, f"users/{artist_id}/tracks",
                               {"limit": limit, "offset": offset, "linked_partitioning": 1}, timeout=timeout)
        return _tracks(data.get("collection")), bool(data.get("next_href"))

    # ---- страница альбома ----
    async def album(self, state, album_id: str, playlist_id: str | None = None, *,
                    timeout: float) -> tuple[dict, list[Track], int, bool]:
        p = await self.call("album_page", api_get, f"playlists/{album_id}", {}, timeout=timeout)
        raw = p.get("tracks") or []
        degraded = False
        if not raw and p.get("track_count"):            # в ответе нет списка треков: пробуем полное представление
            try:
                raw = (await self.call("album_page", api_get, f"playlists/{album_id}", {"representation": "full"},
                                       timeout=timeout, optional=True)).get("tracks") or []
            except ProviderError as exc:
                log.info("playlists/%s full: %r", album_id, exc)
                degraded = True
        stubs = [x["id"] for x in raw if not x.get("title")]      # SoundCloud отдаёт полные данные только первых треков
        full: dict = {}
        for i in range(0, len(stubs), 30):
            batch = await self.call("album_page", api_get, "tracks", {"ids": ",".join(map(str, stubs[i:i + 30]))},
                                    timeout=timeout)
            full.update({x["id"]: x for x in batch or []})
        items = [x if x.get("title") else full.get(x["id"]) for x in raw]
        tracks = _tracks(x for x in items if x)
        # Не попали в список: превью Go+ (30 секунд) и треки, закрытые в регионе
        skipped = max(len(raw), p.get("track_count") or 0) - len(tracks)
        return album_from_playlist({**p, "id": p.get("id") or album_id}), tracks, skipped, degraded

    # ---- радио ----
    async def radio(self, state, track_id: str, n: int, *, timeout: float) -> list[Track]:
        data = await self.call("radio", api_get, f"tracks/{track_id}/related", {"limit": n}, timeout=timeout)
        return _tracks((data or {}).get("collection"))


PROVIDER = SoundCloudProvider()
