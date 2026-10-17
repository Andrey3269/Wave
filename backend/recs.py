"""
Рекомендации: детерминированный рейтинг с объяснимыми баллами (без ML).

Кандидаты берутся из реальных источников:
  * «радио» текущего трека (YouTube Music: get_watch_playlist, SoundCloud: tracks/{id}/related);
  * поиск по любимым и чаще всего слушаемым артистам;
  * если истории ещё нет: общая подборка по нескольким запросам.
Затем каждому кандидату считаются баллы (таблица W), дубли и недавно игравшее отбрасываются,
а итог собирается с ограничением на число треков одного артиста (разнообразие).
Жанров и тегов у источников нет в единообразном виде, поэтому по ним рейтинг не строится.

Ответ честно сообщает, что произошло (находка M4): поля `failed_sources` и `source_status` отличают
«источник не ответил» от «подходящих треков нет». `personalized` = в выдаче есть треки, отобранные
по истории или избранному; `radio` = в выдаче есть треки из «радио» текущего трека.

S14:
  * исключения пользователя (трек, исполнитель — recs_store.py, таблица rec_exclusions) не предлагаются никогда;
  * сброс сигналов: прослушивания, пропуски и избранное до отметки recs_reset_at не влияют на баллы (записи остаются);
  * сигналом «люблю исполнителя» считаются только полноценные прослушивания: запись-пропуск (S10) раньше добавляла
    исполнителю балл; исполнителя, которого только пропускают (ARTIST_SKIP_HIDE пропусков и ни одного
    прослушивания), лента не предлагает, частые пропуски исполнителя дают штраф;
  * разнообразие: не больше MAX_PER_ARTIST треков исполнителя и MAX_PER_ALBUM треков одного альбома, небольшой
    балл за нового исполнителя (когда есть история), недавно игравшая песня не предлагается и на другом видео;
  * причина считается по самому треку, а не по запросу, который его нашёл: «Любимый исполнитель» и «Из твоей
    истории» — только если это правда для исполнителя трека; `reason_kind` — то же машиночитаемо
    (radio | fav_artist | history_artist | artist_search | general).
"""
from __future__ import annotations

import asyncio
import logging
from collections import Counter

from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field, model_validator

import providers
import recs_store
import userdata
from domain import snapshot
from providers import youtube_music as yt_provider

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
    "artist_skipped": -10,      # S14: исполнителя часто пропускают (за каждый пропуск)...
    "artist_skipped_cap": -30,  # ...но не ниже этого
    "new_artist": 4,            # S14: исполнитель не встречался в истории и избранном (только когда они есть)
}
MAX_PER_ARTIST = 3
MAX_PER_ALBUM = 2               # S14: не больше двух треков одного альбома (по album_id, иначе по названию и артисту)
ARTIST_SKIP_MIN = 2             # S14: штраф за пропуски исполнителя — от стольких пропусков, если их больше прослушиваний
ARTIST_SKIP_HIDE = 3            # S14: столько пропусков и ни одного прослушивания — исполнителя не предлагаем
RECENT_EXCLUDE = 25             # последние N прослушанных не предлагаем
MAX_SEARCH = 100                # глубже одного поиска по артисту не листаем
# Общая подборка, когда истории ещё нет: на каждой странице ленты берутся следующие два запроса
COLD_START_QUERIES = ["лучшие хиты", "top hits", "новинки музыки", "chill", "lo-fi beats", "indie rock", "русский рэп",
                      "поп музыка", "электроника", "джаз", "рок классика", "r&b", "танцевальная музыка", "акустика",
                      "инди поп", "хип-хоп", "метал", "фонк", "лаунж", "саундтреки", "k-pop", "латино", "кантри", "регги"]


_name = recs_store.norm          # нормализованное имя (то же — у исключений исполнителей)


def _as_dict(t) -> dict:
    return t.model_dump() if hasattr(t, "model_dump") else t.dict()


def _key(d: dict) -> str:
    return f"{d['source']}:{d['id']}"


def _sig(d: dict) -> str:
    return f"{_name(d['artist'])}|{_name(d['title'])}"     # одна и та же песня на разных видео


# «Радио» YouTube Music (прежнее имя; с S7 — функция адаптера providers/youtube_music.py)
_yt_radio = yt_provider.radio_sync

# Сколько секунд ждём один запрос кандидатов (как до S7). Предел задаётся в вызове адаптера: так таймаут
# учитывается в состоянии источника (cooldown), а зависший запрос не занимает общий пул потоков (S7, M2).
JOB_TIMEOUT = 12


async def _radio(request: Request, source: str, tid: str, n: int) -> list:
    return await providers.get(source).radio(request.app.state, tid, n, timeout=JOB_TIMEOUT)


async def _search(request: Request, source: str, q: str, n: int, offset: int = 0) -> list:
    """Поиск треков: запрашиваем первые n и отдаём с позиции offset (так листаем глубже)."""
    if source == "yt":
        return await providers.yt.search_tracks(request.app.state, q, n, offset, timeout=JOB_TIMEOUT)
    return await providers.sc.search_tracks_plain(request.app.state, q, n, offset, timeout=JOB_TIMEOUT)


def _artists(d: dict) -> set[str]:
    """Все исполнители трека (основной и соавторы), нормализованные."""
    return {_name(d.get("artist"))} | {_name(a) for a in (d.get("artists") or [])} - {""}


def _album(d: dict) -> str | None:
    if d.get("album_id"):
        return f"id:{d['album_id']}"
    return f"{_name(d.get('album'))}|{_name(d.get('artist'))}" if _name(d.get("album")) else None


@router.get("/recommendations")
async def recommendations(request: Request, source: str | None = None, id: str | None = None, title: str = "",
                          exclude: str = "", limit: int = Query(24, ge=1, le=50),
                          page: int = Query(0, ge=0, le=500), salt: int = Query(0, ge=0)):
    plays = userdata.recent_plays(300)
    since = recs_store.reset_at()
    favs = userdata.favorite_tracks(since)                   # сигналы: только избранное после сброса
    liked = favs if since is None else userdata.favorite_tracks()
    pref = source if source in ("yt", "sc") else userdata.get_setting("default_source", "yt")
    banned_tracks, banned_artists = recs_store.excluded()

    # Сигналы персонализации — только записи после сброса. Защита от повторов (played, недавние) — по всей истории.
    played, skips, artist_plays, artist_skips, display = Counter(), Counter(), Counter(), Counter(), {}
    for p in plays:
        played[p["key"]] += 1
        if since is not None and p["played_at"] <= since:
            continue
        d = p["track"]
        a = _name(d["artist"])
        if p["skipped"]:
            skips[p["key"]] += 1
            artist_skips[a] += 1
        else:
            artist_plays[a] += 1                             # пропуск — не прослушивание (до S14 давал балл артисту)
            display[a] = d["artist"]
    fav_artists = Counter(_name(t["artist"]) for t in favs)
    for t in favs:
        display[_name(t["artist"])] = t["artist"]
    fav_keys = {_key(t) for t in liked}
    known = set(fav_artists) | set(artist_plays) | set(artist_skips)
    hidden = {a for a, n in artist_skips.items() if n >= ARTIST_SKIP_HIDE and not artist_plays[a] and a not in fav_artists}

    recent = plays[:RECENT_EXCLUDE]
    drop = {p["key"] for p in recent} | {k for k in exclude.split(",") if k} | banned_tracks
    recent_sigs = {_sig(p["track"]) for p in recent}         # та же песня на другом видео — тоже повтор
    if source and id:
        drop.add(f"{source}:{id}")

    # ---- 1. кандидаты ----
    # Задание: (корутина, текст причины, базовый балл, источник, вид, запрос). Вид: radio | personal (артист из
    # истории или избранного) | general (общая подборка при холодном старте).
    jobs: list[tuple] = []
    if source in ("yt", "sc") and id:
        jobs.append((_radio(request, source, id, 25), f"Похоже на «{title}»" if title else "Похоже на текущий трек",
                     W["radio"], source, "radio", ""))
    weight = Counter()
    for a, n in fav_artists.items():
        weight[a] += n * 3
    for a, n in artist_plays.items():
        weight[a] += n
    for a in banned_artists:                                 # исключённого исполнителя не ищем
        weight.pop(a, None)
    if 12 * page < MAX_SEARCH:                               # страница N: следующие 12 треков каждого любимого артиста
        for a, _ in weight.most_common(3):
            jobs.append((_search(request, pref, display[a], min(12 * (page + 1), MAX_SEARCH), 12 * page), "", 0,
                         pref, "personal", display[a]))
    if not weight:                                           # истории и избранного ещё нет (или они сброшены)
        for k in range(2):
            q = COLD_START_QUERIES[(salt + page * 2 + k) % len(COLD_START_QUERIES)]
            jobs.append((_search(request, pref, q, 15), "", 0, pref, "general", q))

    results = await asyncio.gather(*(j[0] for j in jobs), return_exceptions=True)

    # ---- состояние источников: ok — все запросы ответили, partial — часть, failed — ни один ----
    asked: Counter = Counter()
    failed: Counter = Counter()
    for res, job in zip(results, jobs):
        asked[job[3]] += 1
        if isinstance(res, BaseException):
            failed[job[3]] += 1
            log.warning("Источник кандидатов %s не ответил: %r", job[3], res)
    source_status = {src: "ok" if not failed[src] else "failed" if failed[src] == n else "partial"
                     for src, n in asked.items()}
    failed_sources = [src for src, st in source_status.items() if st != "ok"]

    # ---- 2. баллы и причина ----
    cands: dict[str, dict] = {}
    kinds: dict[str, set] = {}                               # ключ трека -> виды заданий, которые его нашли
    for res, (_, radio_reason, base, _src, kind, query) in zip(results, jobs):
        if isinstance(res, BaseException):
            continue
        for t in res:
            d = _as_dict(t)
            k = _key(d)
            names = _artists(d)
            if k in drop or _sig(d) in recent_sigs or names & banned_artists or names & hidden:
                continue
            a = _name(d["artist"])
            s = base
            if a in fav_artists:
                s += W["fav_artist"]
            s += min(W["history_artist_cap"], W["history_artist"] * artist_plays.get(a, 0))
            if artist_skips[a] >= ARTIST_SKIP_MIN and artist_skips[a] > artist_plays[a]:
                s += max(W["artist_skipped_cap"], W["artist_skipped"] * artist_skips[a])
            if known and a not in known:
                s += W["new_artist"]
            if d["source"] == pref:
                s += W["same_source"]
            if k in played:
                s += W["already_played"]
            s += max(W["skipped_cap"], W["skipped"] * skips.get(k, 0))
            if k in fav_keys:
                s += W["liked"]
            # Причина — по самому треку: «любимый» и «из истории» только если это так для его исполнителя
            if kind == "radio":
                d["reason"], d["reason_kind"] = radio_reason, "radio"
            elif kind == "general":
                d["reason"], d["reason_kind"] = f"Подборка по запросу «{query}»", "general"
            elif a in fav_artists:
                d["reason"], d["reason_kind"] = f"Любимый исполнитель: {d['artist']}", "fav_artist"
            elif artist_plays.get(a):
                d["reason"], d["reason_kind"] = f"Из твоей истории: {d['artist']}", "history_artist"
            else:
                d["reason"], d["reason_kind"] = f"Найдено поиском по исполнителю {query}", "artist_search"
            d["score"] = s
            kinds.setdefault(k, set()).add(kind)
            if a in fav_artists or artist_plays.get(a):      # балл за любимого или слушанного артиста
                kinds[k].add("personal")
            if k not in cands or s > cands[k]["score"]:
                cands[k] = d

    # ---- 3. разнообразие: жадный выбор со штрафом за повтор артиста, без дублей песен, с пределом на альбом ----
    pool = list(cands.values())
    out, per_artist, per_album, sigs = [], Counter(), Counter(), set()
    while pool and len(out) < limit:
        best = max(pool, key=lambda d: d["score"] + W["artist_repeat"] * per_artist[_name(d["artist"])])
        pool.remove(best)
        a, alb = _name(best["artist"]), _album(best)
        if per_artist[a] >= MAX_PER_ARTIST or _sig(best) in sigs or (alb and per_album[alb] >= MAX_PER_ALBUM):
            continue
        out.append(best)
        per_artist[a] += 1
        if alb:
            per_album[alb] += 1
        sigs.add(_sig(best))
    used = set().union(*(kinds[_key(d)] for d in out)) if out else set()
    return {"tracks": out, "personalized": "personal" in used, "radio": "radio" in used, "page": page,
            "failed_sources": failed_sources, "source_status": source_status}


# ---------------------------------------------------------------------------
# Управление рекомендациями (S14): исключения и сброс сигналов. Изменяющие запросы защищает OriginGuard (S13).
# ---------------------------------------------------------------------------
class ExclusionIn(BaseModel):
    """Исключение: трек (source + id в формате источника) или исполнитель (имя). title/artist — подпись в настройках."""
    kind: Literal["track", "artist"]
    source: Literal["yt", "sc"] | None = None
    id: str | None = None
    title: str = Field("", max_length=recs_store.LABEL_MAX)
    artist: str = Field("", max_length=recs_store.LABEL_MAX)

    @model_validator(mode="after")
    def _check(self):
        if self.kind == "track" and not (self.source and self.id and snapshot.valid_track(self.source, self.id)):
            raise ValueError("Некорректный идентификатор трека для этого источника")
        if self.kind == "artist" and not _name(self.artist):
            raise ValueError("Не указан исполнитель")
        return self


@router.get("/recommendations/exclusions")
def list_exclusions():
    return {"items": recs_store.exclusions(), "reset_at": recs_store.reset_at()}


@router.post("/recommendations/exclusions")
def add_exclusion(body: ExclusionIn):
    if body.kind == "track":
        value = f"{body.source}:{body.id}"
        label = " — ".join(x for x in (recs_store.clean_label(body.title), recs_store.clean_label(body.artist)) if x)
    else:
        value, label = _name(body.artist), body.artist
    try:
        return {"ok": True, "item": recs_store.add(body.kind, value, label)}
    except recs_store.TooMany:
        raise HTTPException(400, f"Слишком много исключений (предел {recs_store.EXCLUSIONS_MAX}). "
                                 "Верните часть в настройках.") from None


# `:path`: id трека и имя исполнителя приходят закодированными (encodeURIComponent)
@router.delete("/recommendations/exclusions/{kind}/{value:path}")
def remove_exclusion(kind: Literal["track", "artist"], value: str):
    recs_store.remove(kind, value)
    return {"ok": True}


@router.post("/recommendations/reset")
def reset_signals():
    """Рекомендации перестают учитывать историю, пропуски и избранное до этого момента. Ничего не удаляется."""
    return {"ok": True, "reset_at": recs_store.set_reset()}


@router.delete("/recommendations/reset")
def undo_reset():
    """Отмена сброса: прежние сигналы снова учитываются (они не удалялись)."""
    recs_store.clear_reset()
    return {"ok": True, "reset_at": None}
