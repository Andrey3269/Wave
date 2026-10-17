"""
Проверка снимков трека и адресов, которые приходят от клиента или читаются из базы (S13, находки M6 и M7).

Снимок трека (избранное, история, плейлисты, очередь плеера) хранится как JSON и попадает обратно в интерфейс:
обложка — в <img src>, публичная ссылка — в «Поделиться», stream_url — в <audio src>. Клиенту и старым строкам
базы здесь не доверяем:

* `stream_url` всегда вычисляет сервер: /api/stream/{source}/{id} (так же, как для поиска и скачанных треков).
* Обложка — только https:// с хостов обложек источников (COVER_HOSTS и их поддомены, без логина и чужого
  порта) или адрес локальной обложки скачанного трека /api/local/{source}/{id}/cover. Остальное → None:
  трек сохраняется без обложки, а не отклоняется (новый CDN источника не должен ломать «Нравится»).
* Публичная ссылка — только https:// на сайтах источников (LINK_HOSTS), иначе None.
* id обязан соответствовать формату своего источника (TRACK_ID_RE); новые записи с другим id отклоняются (422),
  старые такие строки при чтении пропускаются (воспроизвести их всё равно нельзя: поток проверяет тот же формат).
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any
from urllib.parse import urlsplit

from domain.models import TRACK_ID_RE

log = logging.getLogger("musicmeta.snapshot")

# Откуда интерфейс показывает обложки и откуда загрузчик их скачивает (раньше — downloads.COVER_HOSTS)
COVER_HOSTS = ("googleusercontent.com", "ytimg.com", "ggpht.com", "sndcdn.com")
LINK_HOSTS = frozenset({"music.youtube.com", "www.youtube.com", "youtube.com", "m.youtube.com", "youtu.be",
                        "soundcloud.com", "www.soundcloud.com", "m.soundcloud.com", "on.soundcloud.com"})
MAX_URL = 2048
_UNSAFE_CHARS = re.compile(r"[\x00-\x20\x7f\\]")          # управляющие символы, пробел и обратная косая черта
_LOCAL_COVER = re.compile(r"^/api/local/(yt|sc)/([^/?#]+)/cover$")
_WARN_MAX = 256
_warned: set[str] = set()


def valid_track(source: Any, track_id: Any) -> bool:
    """Пара источник + id допустима: известный источник и id в его формате (videoId YouTube, число SoundCloud)."""
    rx = TRACK_ID_RE.get(source) if isinstance(source, str) else None
    return rx is not None and isinstance(track_id, str) and bool(rx.fullmatch(track_id))


def stream_url(source: str, track_id: str) -> str:
    return f"/api/stream/{source}/{track_id}"


def cover_host_allowed(host: str | None) -> bool:
    """Хост из COVER_HOSTS или его поддомен (lh3.googleusercontent.com), но не «evilgoogleusercontent.com»."""
    host = (host or "").lower().rstrip(".")
    return any(host == h or host.endswith("." + h) for h in COVER_HOSTS)


def _https(v: Any, host_ok) -> str | None:
    if not isinstance(v, str) or not v or len(v) > MAX_URL or _UNSAFE_CHARS.search(v):
        return None
    try:
        u = urlsplit(v)
        port = u.port
    except ValueError:
        return None
    if u.scheme != "https" or u.username is not None or u.password is not None or port not in (None, 443):
        return None
    return v if host_ok(u.hostname) else None


def safe_cover(v: Any, *, local: bool = True) -> str | None:
    """Адрес обложки из разрешённого списка или None. local=False — только удалённые (для загрузчика)."""
    if local and isinstance(v, str):
        m = _LOCAL_COVER.fullmatch(v)
        if m:
            return v if valid_track(m.group(1), m.group(2)) else None
    return _https(v, cover_host_allowed)


def safe_link(v: Any) -> str | None:
    """Публичная ссылка на трек (https:// на сайте источника) или None."""
    return _https(v, lambda h: (h or "").lower() in LINK_HOSTS)


def _skip(where: str, key: Any, why: str) -> None:
    tag = f"{where}:{key}"
    if tag not in _warned and len(_warned) < _WARN_MAX:
        _warned.add(tag)
        log.warning("Сохранённый трек (%s, %s) пропущен при чтении: %s", where, str(key)[:80], why)


def read_snapshot(raw: Any, where: str = "", key: Any = None) -> dict | None:
    """
    Снимок трека из базы для ответа клиенту или None, если строка недопустима (испорчена, не объект,
    неизвестный источник, id не в формате источника). Строка в базе не меняется и не удаляется: это данные
    пользователя; DELETE по ключу по-прежнему её удаляет.

    Остальные поля возвращаются как есть (старые снимки без новых полей читаются без изменений), кроме
    вычисляемых и проверяемых: stream_url — всегда от сервера, cover и url (если есть) — по разрешённым спискам.
    """
    try:
        data = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
    except (ValueError, RecursionError):
        _skip(where, key, "не JSON")
        return None
    if not isinstance(data, dict):
        _skip(where, key, "не объект")
        return None
    source, tid = data.get("source"), data.get("id")
    if not valid_track(source, tid):
        _skip(where, key, "источник или id трека недопустимы")
        return None
    data["stream_url"] = stream_url(source, tid)
    if "cover" in data:
        data["cover"] = safe_cover(data["cover"])
    if "url" in data:
        data["url"] = safe_link(data["url"])
    return data
