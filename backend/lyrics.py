"""
Тексты песен через LRCLIB (бесплатно, без ключа).

Если сервис отдал синхронизированный текст (LRC), возвращаем строки с реальными
таймкодами. Иначе отдаём обычный текст без синхронизации: таймкоды не выдумываем.
Для скачанных треков текст берётся из локального файла (работает без интернета).
Genius не подключён: его API не отдаёт сам текст, а парсинг страниц запрещён их условиями.
"""
from __future__ import annotations

import json
import logging
import re

from fastapi import APIRouter, Request

import library
from providers.cache import TTLCache

log = logging.getLogger("musicmeta.lyrics")
router = APIRouter(prefix="/api", tags=["lyrics"])

_LRC = re.compile(r"\[(\d+):(\d+(?:\.\d+)?)\]\s*(.*)")
# Найденные тексты (и «текста нет») в памяти: не больше 512 записей, сутки (S7, находка M9: раньше без
# лимита и срока). Временный сбой LRCLIB не кэшируется. Очистка — «Очистить кэш» в настройках.
_cache: TTLCache[dict] = TTLCache(maxsize=512, ttl=24 * 3600)


def _parse_lrc(text: str) -> list[dict]:
    rows = []
    for line in text.splitlines():
        m = _LRC.match(line.strip())
        if m and m.group(3):
            rows.append({"t": int(m.group(1)) * 60 + float(m.group(2)), "text": m.group(3)})
    return rows


async def fetch_lyrics(client, artist: str, title: str, duration: int | None = None) -> dict | None:
    """dict (поля могут быть пустыми = текста нет) или None при временном сбое сервиса."""
    params = {"artist_name": artist[:200], "track_name": title[:200]}
    if duration:
        params["duration"] = duration
    try:
        r = await client.get("https://lrclib.net/api/get", params=params, timeout=8,
                             headers={"User-Agent": "Wave/0.1"})
    except Exception as exc:
        log.info("LRCLIB недоступен: %r", exc)
        return None
    if r.status_code == 404:
        return {"synced": [], "plain": None, "provider": "lrclib"}
    if r.status_code != 200:
        return None
    try:
        data = r.json()
    except ValueError:                       # страница-заглушка вместо JSON (обслуживание, защита от ботов)
        log.info("LRCLIB ответил не JSON (%s)", r.headers.get("content-type", "?"))
        return None
    if not isinstance(data, dict):
        log.info("LRCLIB ответил JSON неожиданного вида: %s", type(data).__name__)
        return None
    synced, plain = data.get("syncedLyrics"), data.get("plainLyrics")
    return {"synced": _parse_lrc(synced) if isinstance(synced, str) else [],
            "plain": plain if isinstance(plain, str) and plain else None, "provider": "lrclib"}


def _saved_lyrics(source: str, track_id: str) -> dict | None:
    """
    Текст скачанного трека из его папки или None (S13, N3): файл, который не читается, не JSON, не объект или
    не того вида, что пишет загрузчик, не даёт HTTP 500 — берём текст из сети, как для нескачанного трека.
    """
    try:
        p = library.lyrics_path(source, track_id)
        if not p:
            return None
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError) as exc:       # UnicodeDecodeError и JSONDecodeError — подклассы ValueError
        log.info("Сохранённый текст %s:%s не прочитан: %s", source, track_id, type(exc).__name__)
        return None
    synced, plain = (data.get("synced"), data.get("plain")) if isinstance(data, dict) else (None, None)
    if not isinstance(synced, list) or not (plain is None or isinstance(plain, str)):
        log.info("Сохранённый текст %s:%s неожиданного вида, пропущен", source, track_id)
        return None
    return data


@router.get("/lyrics")
async def lyrics(request: Request, artist: str, title: str, duration: int | None = None,
                 source: str | None = None, id: str | None = None):
    # Скачанный трек: берём сохранённый текст (работает офлайн)
    if source and id:
        saved = _saved_lyrics(source, id)
        if saved is not None:
            return saved

    key = (artist.strip().lower()[:200], title.strip().lower()[:200])
    hit = _cache.get(key)
    if hit is not None:
        return hit
    out = await fetch_lyrics(request.app.state.http, artist, title, duration)
    if out is None:
        return {"synced": [], "plain": None, "provider": "lrclib"}   # временный сбой: не кэшируем
    _cache.set(key, out)
    return out