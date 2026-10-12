"""
Рекомендации: детерминированный рейтинг с объяснимыми баллами (без ML).

Кандидаты берутся из реальных источников:
  * «радио» текущего трека (YouTube Music: get_watch_playlist, SoundCloud: tracks/{id}/related);
  * поиск по любимым и чаще всего слушаемым артистам;
  * если истории ещё нет: общая подборка по нескольким запросам.
Затем каждому кандидату считаются баллы (таблица W), дубли и недавно игравшее отбрасываются,
а итог собирается с ограничением на число треков одного артиста (разнообразие).
Жанров и тегов у источников нет в единообразном виде, поэтому по ним рейтинг не строится.
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
from collections import Counter

from fastapi import APIRouter, Query, Request

import catalog
import soundcloud as sc_api
import userdata
from catalog import _run

log = logging.getLogger("musicmeta.recs")
router = APIRouter(prefix="/api", tags=["recommendations"])

# Баллы. Меняются в одном месте, итог объясним: каждый балл можно проследить.
W = {
    "radio": 30,                # кандидат из «радио» текущего трека (похожий)
    "fav_artist": 20,           # артист есть в избранном
    "history_artist": 5,        # за каждое прослушивание артиста...
    "history_artist_cap": 15,   # ...но не больше этого
    "same_source": 5,           # из предпочитаемого источника
    "already_played": -15,      # трек уже слушали
    "skipped": -20,             # за каждый пропуск трека...
    "skipped_cap": -40,         # ...но не ниже этого
    "liked": -5,                # уже в избранном (его и так найдёшь)
    "artist_repeat": -8,        # штраф за каждый уже выбранный трек этого артиста (разнообразие)
}
MAX_PER_ARTIST = 3
RECENT_EXCLUDE = 25             # последние N прослушанных не предлагаем
MAX_SEARCH = 100                # глубже одного поиска по артисту не листаем
# Общая подборка, когда истории ещё нет: на каждой странице ленты берутся следующие два запроса
COLD_START_QUERIES = ["лучшие хиты", "top hits", "новинки музыки", "chill", "lo-fi beats", "indie rock", "русский рэп",
                      "поп музыка", "электроника", "джаз", "рок классика", "r&b", "танцевальная музыка", "акустика",
                      "инди поп", "хип-хоп", "метал", "фонк", "лаунж", "саундтреки", "k-pop", "латино", "кантри", "регги"]


def _name(s: str | None) -> str:
    return re.sub(r"\W+", " ", (s or "").lower()).strip()


def _as_dict(t) -> dict:
    return t.model_dump() if hasattr(t, "model_dump") else t.dict()


def _key(d: dict) -> str:
    return f"{d['source']}:{d['id']}"


def _sig(d: dict) -> str:
    return f"{_name(d['artist'])}|{_name(d['title'])}"     # одна и та же песня на разных видео


def _yt_radio(ytm, vid: str, n: int) -> list[dict]:
    out = []
    for it in ytm.get_watch_playlist(videoId=vid, limit=n).get("tracks") or []:
        it = dict(it)
        it.setdefault("thumbnails", it.get("thumbnail"))   # в этом ответе ключ «thumbnail»
        it["duration"] = it.get("length")
        out.append(it)
    return out


async def _radio(request: Request, source: str, tid: str, n: int) -> list:
    if source == "yt":
        raw = await _run(_yt_radio, request.app.state.ytm, tid, n)
        return [t for t in map(catalog._norm["yt"], raw) if t]
    data = await _run(sc_api.api_get, f"tracks/{tid}/related", {"limit": n})
    return [t for t in map(catalog._norm["sc"], (data or {}).get("collection") or []) if t]


async def _search(request: Request, source: str, q: str, n: int, offset: int = 0) -> list:
    """Поиск треков: запрашиваем первые n и отдаём с позиции offset (так листаем глубже)."""
    if source == "yt":
        raw = await _run(lambda: request.app.state.ytm.search(q, filter="songs", limit=n))
        return [t for t in map(catalog._norm["yt"], raw[offset:]) if t]
    data = await _run(sc_api.api_get, "search/tracks", {"q": q, "limit": n})
    return [t for t in map(catalog._norm["sc"], ((data or {}).get("collection") or [])[offset:]) if t]


@router.get("/recommendations")
async def recommendations(request: Request, source: str | None = None, id: str | None = None, title: str = "",
                          exclude: str = "", limit: int = Query(24, ge=1, le=50),
                          page: int = Query(0, ge=0, le=500), salt: int = Query(0, ge=0)):
    plays = userdata.recent_plays(300)
    favs = userdata.favorite_tracks()
    pref = source if source in ("yt", "sc") else userdata.get_setting("default_source", "yt")

    played, skips, artist_plays, display = Counter(), Counter(), Counter(), {}
    for p in plays:
        d = p["track"]
        played[p["key"]] += 1
        skips[p["key"]] += p["skipped"]
        artist_plays[_name(d["artist"])] += 1
        display[_name(d["artist"])] = d["artist"]
    fav_artists = Counter(_name(t["artist"]) for t in favs)
    for t in favs:
        display[_name(t["artist"])] = t["artist"]
    fav_keys = {_key(t) for t in favs}

    drop = {p["key"] for p in plays[:RECENT_EXCLUDE]} | {k for k in exclude.split(",") if k}
    if source and id:
        drop.add(f"{source}:{id}")

    # ---- 1. кандидаты ----
    jobs: list[tuple] = []
    if source in ("yt", "sc") and id:
        jobs.append((_radio(request, source, id, 25), f"Похоже на «{title}»" if title else "Похоже на текущий трек", W["radio"]))
    weight = Counter()
    for a, n in fav_artists.items():
        weight[a] += n * 3
    for a, n in artist_plays.items():
        weight[a] += n
    if 12 * page < MAX_SEARCH:                               # страница N: следующие 12 треков каждого любимого артиста
        for a, _ in weight.most_common(3):
            reason = f"Любимый исполнитель: {display[a]}" if a in fav_artists else f"Из твоей истории: {display[a]}"
            jobs.append((_search(request, pref, display[a], min(12 * (page + 1), MAX_SEARCH), 12 * page), reason, 0))
    if not weight:                                           # истории и избранного ещё нет
        for k in range(2):
            q = COLD_START_QUERIES[(salt + page * 2 + k) % len(COLD_START_QUERIES)]
            jobs.append((_search(request, pref, q, 15), "Подборка", 0))

    results = await asyncio.gather(*(asyncio.wait_for(j[0], 12) for j in jobs), return_exceptions=True)

    # ---- 2. баллы ----
    cands: dict[str, dict] = {}
    for res, (_, reason, base) in zip(results, jobs):
        if isinstance(res, BaseException):
            log.info("Источник кандидатов не ответил: %r", res)
            continue
        for t in res:
            d = _as_dict(t)
            k = _key(d)
            if k in drop:
                continue
            a = _name(d["artist"])
            s = base
            if a in fav_artists:
                s += W["fav_artist"]
            s += min(W["history_artist_cap"], W["history_artist"] * artist_plays.get(a, 0))
            if d["source"] == pref:
                s += W["same_source"]
            if k in played:
                s += W["already_played"]
            s += max(W["skipped_cap"], W["skipped"] * skips.get(k, 0))
            if k in fav_keys:
                s += W["liked"]
            d["reason"], d["score"] = reason, s
            if k not in cands or s > cands[k]["score"]:
                cands[k] = d

    # ---- 3. разнообразие: жадный выбор со штрафом за повтор артиста, без дублей песен ----
    pool = list(cands.values())
    out, per_artist, sigs = [], Counter(), set()
    while pool and len(out) < limit:
        best = max(pool, key=lambda d: d["score"] + W["artist_repeat"] * per_artist[_name(d["artist"])])
        pool.remove(best)
        a = _name(best["artist"])
        if per_artist[a] >= MAX_PER_ARTIST or _sig(best) in sigs:
            continue
        out.append(best)
        per_artist[a] += 1
        sigs.add(_sig(best))
    return {"tracks": out, "personalized": bool(weight or (source and id)), "page": page}