"""
Офлайн-оценка рекомендаций (S14) на детерминированных фикстурах истории.

Каждый сценарий — история прослушиваний и пропусков, избранное, при необходимости текущий трек и заранее заданные
ответы источника (поиск YouTube Music по запросу, «радио» трека). Сценарий проигрывается через настоящий API
(/api/history, /api/history/skip, /api/favorites, /api/recommendations) на временном профиле с двойником
ytmusicapi, затем по выдаче первой страницы считаются метрики:

* new_artist_share — доля треков исполнителей, которых нет ни в истории (прослушивания и пропуски), ни в избранном
                     (новизна, больше — лучше; пропускаемый исполнитель — не новый);
* repeat_share     — доля повторов: трек или та же песня на другом видео среди последних RECENT прослушиваний
                     (включая пропуски) или уже выше в этой же выдаче (меньше — лучше);
* skip_share       — доля треков, которые пользователь сценария пропускает: трек уже пропускали или исполнитель из
                     списка `dislikes` (его в истории пропускали много раз) — меньше — лучше;
* count            — сколько треков в выдаче (защита от «улучшения» метрик пустой выдачей).

Определения метрик не зависят от кода рекомендаций: имена нормализуются здесь же, известные исполнители и
повторы берутся из сценария. Базовая линия — метрики алгоритма до S14 (коммит 37781f8), записаны в
tests/fixtures/recs_eval/baseline.json; tests/api/test_recs_eval.py сверяет с ней текущий алгоритм.
"""
from __future__ import annotations

import re

RECENT = 25            # окно «недавних повторов» (совпадает с RECENT_EXCLUDE алгоритма до S14)
LIMIT = 24             # размер первой страницы ленты в оценке


def norm(s: str | None) -> str:
    return re.sub(r"\W+", " ", (s or "").lower()).strip()


def vid(prefix: str, n: int) -> str:
    """Идентификатор видео YouTube в формате источника (11 символов)."""
    return f"{prefix}{n:0{11 - len(prefix)}d}"


def song(v: str, title: str, artist: str, album: str | None = None, album_id: str | None = None) -> dict:
    """Элемент ответа ytmusicapi (search filter="songs" и get_watch_playlist имеют те же поля)."""
    item = {"videoId": v, "title": title, "artists": [{"name": artist, "id": None}]}
    if album:
        item["album"] = {"name": album, "id": album_id}
    return item


# ---------------------------------------------------------------------------
# Сценарии. Записи истории идут от старых к новым: ("play" | "skip", id, название, исполнитель).
# ---------------------------------------------------------------------------
def _loyal() -> dict:
    """Постоянный слушатель трёх исполнителей; поиск возвращает недавно игравшие песни на других видео и каверы."""
    hist = []
    for i in range(8):
        hist.append(("play", vid("SV", i % 4), f"Северная песня {i % 4}", "Северный Ветер"))
    for i in range(4):
        hist.append(("play", vid("LC", i), f"Городская {i}", "Луна Сити"))
    for i in range(14):                                     # свежие прослушивания, которые нельзя предлагать снова
        hist.append(("play", vid("RC", i), f"Свежая {i}", "Северный Ветер" if i % 2 else "Луна Сити"))
    favs = [(vid("TD", 0), "Дом 0", "Тёплый Дом"), (vid("TD", 1), "Дом 1", "Тёплый Дом")]
    songs = {
        "Северный Ветер": [song(vid("RX", i), f"Свежая {i}", "Северный Ветер", "Север", "MPREb_sever0001")
                           for i in range(1, 14, 2)]       # те же песни на других видео
        + [song(vid("SN", i), f"Новая северная {i}", "Северный Ветер", "Север", "MPREb_sever0001") for i in range(5)]
        + [song(vid("CV", i), f"Кавер {i}", "Каверы Плюс") for i in range(3)],
        "Тёплый Дом": [song(vid("TD", i), f"Дом {i}", "Тёплый Дом", "Очаг", "MPREb_ochag00001") for i in range(6)]
        + [song(vid("NB", i), f"Соседи {i}", "Новые Берега") for i in range(3)],
        "Луна Сити": [song(vid("RY", i), f"Свежая {i}", "Луна Сити") for i in range(0, 14, 2)]
        + [song(vid("LN", i), f"Ночная {i}", "Луна Сити", "Ночь", "MPREb_noch000001") for i in range(5)]
        + [song(vid("ZV", i), f"Звёзды {i}", "Звёздный Путь") for i in range(3)],
    }
    return {"name": "loyal", "history": hist, "favorites": favs, "songs": songs, "dislikes": []}


def _skipper() -> dict:
    """Пользователь раз за разом пропускает одного исполнителя; радио текущего трека его предлагает."""
    hist = []
    for i in range(6):
        hist.append(("play", vid("LU", i), f"Луч {i}", "Луч"))
        hist.append(("skip", vid("SH", i), f"Шум {i}", "Шум"))
    for i in range(4):
        hist.append(("play", vid("MO", i), f"Мост {i}", "Мост"))
        hist.append(("skip", vid("SH", 10 + i), f"Шум {10 + i}", "Шум"))
    radio = [song(vid("SR", i), f"Шум радио {i}", "Шум") for i in range(6)] \
        + [song(vid("NR", i), f"Рядом {i}", f"Соседний {i}") for i in range(8)] \
        + [song(vid("LU", 10 + i), f"Луч новый {i}", "Луч") for i in range(4)]
    songs = {
        "Шум": [song(vid("SS", i), f"Шум поиск {i}", "Шум") for i in range(8)],
        "Луч": [song(vid("LS", i), f"Луч поиск {i}", "Луч") for i in range(6)],
        "Мост": [song(vid("MS", i), f"Мост поиск {i}", "Мост") for i in range(6)],
    }
    return {"name": "skipper", "history": hist, "favorites": [], "songs": songs, "dislikes": ["Шум"],
            "current": (vid("LU", 0), "Луч 0"), "radio": radio}


def _radio_album() -> dict:
    """Без истории: радио текущего трека перегружено одним сборником и дублями песен."""
    radio = [song(vid("CA", i), f"Сборник {i}", f"Участник {i}", "Лето сборник", "MPREb_leto000001") for i in range(9)] \
        + [song(vid("DU", i), "Одна песня", "Дуэт") for i in range(4)] \
        + [song(vid("OT", i), f"Другая {i}", f"Исполнитель {i}") for i in range(10)]
    return {"name": "radio_album", "history": [], "favorites": [], "songs": {"*": []}, "dislikes": [],
            "current": (vid("CU", 0), "Начало"), "radio": radio}


def _cold() -> dict:
    """Холодный старт: общая подборка по запросам, в ответе дубль песни."""
    items = [song(vid("CS", i), f"Хит {i}", f"Звезда {i % 9}") for i in range(20)] \
        + [song(vid("CD", 0), "Хит 0", "Звезда 0")]
    return {"name": "cold", "history": [], "favorites": [], "songs": {"*": items}, "dislikes": []}


SCENARIOS = [_loyal, _skipper, _radio_album, _cold]


# ---------------------------------------------------------------------------
def _payload(v: str, title: str, artist: str) -> dict:
    return {"id": v, "source": "yt", "title": title, "artist": artist, "artists": [artist], "album": None,
            "duration": 200, "cover": None, "explicit": False, "stream_url": f"/api/stream/yt/{v}",
            "artist_id": None, "artist_ids": [], "url": None}


def load(api, sc: dict) -> None:
    """Записывает историю и избранное сценария через API и настраивает ответы двойника источника."""
    for kind, v, title, artist in sc["history"]:
        body = _payload(v, title, artist)
        if kind == "play":
            assert api.post("/api/history", json=body).status_code == 200
        else:                                               # пропуск с S10: отдельная запись со снимком трека
            assert api.post("/api/history/skip", json={"source": "yt", "id": v, "track": body}).status_code == 200
    for v, title, artist in sc["favorites"]:
        assert api.post("/api/favorites", json=_payload(v, title, artist)).status_code == 200
    api.ytm.songs.update(sc["songs"])
    if sc.get("current"):
        api.ytm.watch[sc["current"][0]] = {"tracks": sc["radio"]}


def request(api, sc: dict) -> dict:
    params = {"limit": LIMIT}
    if sc.get("current"):
        params.update(source="yt", id=sc["current"][0], title=sc["current"][1])
    r = api.get("/api/recommendations", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def metrics(sc: dict, tracks: list[dict]) -> dict:
    hist = sc["history"]
    known = {norm(a) for _, _, _, a in hist} | {norm(a) for _, _, a in sc["favorites"]}
    recent = hist[-RECENT:]
    recent_keys = {v for _, v, _, _ in recent}
    recent_sigs = {f"{norm(a)}|{norm(t)}" for _, _, t, a in recent}
    skipped = {v for kind, v, _, _ in hist if kind == "skip"}
    dislikes = {norm(a) for a in sc["dislikes"]}
    new = rep = skip = 0
    seen: set[str] = set()
    for t in tracks:
        a, sig = norm(t["artist"]), f"{norm(t['artist'])}|{norm(t['title'])}"
        new += a not in known
        rep += t["id"] in recent_keys or sig in recent_sigs or sig in seen
        skip += t["id"] in skipped or a in dislikes
        seen.add(sig)
    n = len(tracks)
    share = (lambda x: round(x / n, 4)) if n else (lambda x: 0.0)
    return {"count": n, "new_artist_share": share(new), "repeat_share": share(rep), "skip_share": share(skip)}


def run(api, sc: dict) -> dict:
    load(api, sc)
    return metrics(sc, request(api, sc)["tracks"])
