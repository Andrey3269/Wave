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

log = logging.getLogger("musicmeta.lyrics")
router = APIRouter(prefix="/api", tags=["lyrics"])

_LRC = re.compile(r"\[(\d+):(\d+(?:\.\d+)?)\]\s*(.*)")
_cache: dict[tuple[str, str], dict] = {}


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
    data = r.json()
    return {"synced": _parse_lrc(data.get("syncedLyrics") or ""), "plain": data.get("plainLyrics"),
            "provider": "lrclib"}


@router.get("/lyrics")
async def lyrics(request: Request, artist: str, title: str, duration: int | None = None,
                 source: str | None = None, id: str | None = None):
    # Скачанный трек: берём сохранённый текст (работает офлайн)
    if source and id:
        p = library.lyrics_path(source, id)
        if p:
            return json.loads(p.read_text(encoding="utf-8"))

    key = (artist.strip().lower()[:200], title.strip().lower()[:200])
    if key in _cache:
        return _cache[key]
    out = await fetch_lyrics(request.app.state.http, artist, title, duration)
    if out is None:
        return {"synced": [], "plain": None, "provider": "lrclib"}   # временный сбой: не кэшируем
    _cache[key] = out
    return out