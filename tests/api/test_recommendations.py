"""
Рекомендации /api/recommendations (backend/recs.py): детерминированный рейтинг по таблице W,
разнообразие, исключения, холодный старт, изоляция сбоев источников кандидатов.
"""
from __future__ import annotations

import time

import pytest

import recs
from wavetest.fakes import load_fixture, track_payload

W = recs.W


def yt(vid: str, title: str, artist: str) -> dict:
    """Элемент ответа ytmusicapi.search(filter="songs")."""
    return {"videoId": vid, "title": title, "artists": [{"name": artist, "id": None}]}


def play(api, vid, artist, skipped=False):
    api.post("/api/history", json=track_payload(id_=vid, artist=artist, artists=[artist], title=f"t {vid}"))
    if skipped:
        api.post("/api/history/skip", json={"source": "yt", "id": vid})
    time.sleep(0.002)


def keys(body) -> list[str]:
    return [f"{t['source']}:{t['id']}" for t in body["tracks"]]


# ---------------------------------------------------------------------------
# Холодный старт
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("params, queries", [
    ({}, ["лучшие хиты", "top hits"]),
    ({"page": 1}, ["новинки музыки", "chill"]),
    ({"salt": 1}, ["top hits", "новинки музыки"]),
    ({"page": 11, "salt": 3}, [recs.COLD_START_QUERIES[(3 + 22) % 24], recs.COLD_START_QUERIES[(3 + 23) % 24]]),
])
def test_cold_start_queries(api, params, queries):
    api.ytm.songs["*"] = [yt("CSCSCSCSC01", "Hit", "Pop Star")]
    r = api.get("/api/recommendations", params=params)
    assert r.status_code == 200
    body = r.json()
    assert sorted(c[1] for c in api.ytm.calls if c[0] == "search") == sorted(queries)
    assert body["personalized"] is False and body["page"] == params.get("page", 0)
    assert keys(body) == ["yt:CSCSCSCSC01"]                       # дубль из двух запросов — один раз
    assert body["tracks"][0]["reason"] == "Подборка"
    assert body["tracks"][0]["score"] == W["same_source"]


# ---------------------------------------------------------------------------
# Баллы: каждый можно проследить по таблице W
# ---------------------------------------------------------------------------
def test_scoring_is_traceable(api):
    play(api, "HHHHHHHHH01", "Hist Artist", skipped=True)            # слушали и пропустили
    for i in range(recs.RECENT_EXCLUDE):                              # 25 более свежих прослушиваний
        play(api, f"OOOOOOOOO{i:02d}", "Other Artist")
    api.post("/api/favorites", json=track_payload(id_="FFFFFFFFF01", artist="Fav Artist", artists=["Fav Artist"]))
    api.ytm.songs["Fav Artist"] = [yt("FFFFFFFFF01", "Fav One", "Fav Artist"), yt("FFFFFFFFF02", "Fav Two", "Fav Artist")]
    api.ytm.songs["Hist Artist"] = [yt("HHHHHHHHH01", "Old One", "Hist Artist"), yt("HHHHHHHHH02", "New One", "Hist Artist")]
    api.ytm.songs["Other Artist"] = [yt("OOOOOOOOO00", "recent", "Other Artist")]       # недавнее: исключается

    body = api.get("/api/recommendations").json()
    scores = {t["id"]: t["score"] for t in body["tracks"]}
    assert scores == {
        "FFFFFFFFF02": W["fav_artist"] + W["same_source"],                                       # 25
        "FFFFFFFFF01": W["fav_artist"] + W["same_source"] + W["liked"],                          # 20
        "HHHHHHHHH02": W["history_artist"] * 1 + W["same_source"],                               # 10
        "HHHHHHHHH01": W["history_artist"] + W["same_source"] + W["already_played"] + W["skipped"],   # -25
    }
    assert [t["id"] for t in body["tracks"]] == ["FFFFFFFFF02", "FFFFFFFFF01", "HHHHHHHHH02", "HHHHHHHHH01"]
    reasons = {t["id"]: t["reason"] for t in body["tracks"]}
    assert reasons["FFFFFFFFF02"] == "Любимый исполнитель: Fav Artist"
    assert reasons["HHHHHHHHH02"] == "Из твоей истории: Hist Artist"
    assert body["personalized"] is True
    queried = {c[1] for c in api.ytm.calls if c[0] == "search"}
    assert queried == {"Fav Artist", "Hist Artist", "Other Artist"}           # три самых весомых артиста


def test_history_artist_bonus_is_capped(api):
    for i in range(5):
        play(api, f"HHHHHHHHH{i:02d}", "Loved")
    api.ytm.songs["Loved"] = [yt("NNNNNNNNN01", "fresh", "Loved")]
    t = api.get("/api/recommendations").json()["tracks"][0]
    assert t["score"] == W["history_artist_cap"] + W["same_source"]


def test_max_per_artist_and_diversity(api):
    api.post("/api/favorites", json=track_payload(id_="FFFFFFFFF00", artist="Solo", artists=["Solo"]))
    api.ytm.songs["Solo"] = [yt(f"SSSSSSSSS{i:02d}", f"song {i}", "Solo") for i in range(6)]
    body = api.get("/api/recommendations").json()
    assert len(body["tracks"]) == recs.MAX_PER_ARTIST


def test_same_song_on_different_videos_is_deduplicated(api):
    api.post("/api/favorites", json=track_payload(id_="FFFFFFFFF00", artist="Band", artists=["Band"]))
    api.ytm.songs["Band"] = [yt("DDDDDDDDD01", "Song!", "Band"), yt("DDDDDDDDD02", "song", "BAND")]
    assert len(api.get("/api/recommendations").json()["tracks"]) == 1


def test_exclude_parameter(api):
    api.ytm.songs["*"] = [yt("XXXXXXXXX01", "a", "A"), yt("XXXXXXXXX02", "b", "B")]
    body = api.get("/api/recommendations", params={"exclude": "yt:XXXXXXXXX01,,sc:1"}).json()
    assert keys(body) == ["yt:XXXXXXXXX02"]


def test_limit(api):
    api.ytm.songs["*"] = [yt(f"LLLLLLLLL{i:02d}", f"t{i}", f"Artist {i}") for i in range(15)]
    assert len(api.get("/api/recommendations", params={"limit": 5}).json()["tracks"]) == 5
    assert api.get("/api/recommendations", params={"limit": 51}).status_code == 422


# ---------------------------------------------------------------------------
# Радио текущего трека
# ---------------------------------------------------------------------------
def test_radio_of_current_track(api):
    api.ytm.watch["AAAAAAAAAA1"] = load_fixture("ytmusic/watch_playlist.json")
    body = api.get("/api/recommendations", params={"source": "yt", "id": "AAAAAAAAAA1", "title": "Northern Lights"}).json()
    assert keys(body) == ["yt:RRRRRRRRRR1", "yt:RRRRRRRRRR2"]           # сам текущий трек не предлагается
    assert all(t["reason"] == "Похоже на «Northern Lights»" for t in body["tracks"])
    assert [t["score"] for t in body["tracks"]] == [W["radio"] + W["same_source"]] * 2
    assert ("get_watch_playlist", "AAAAAAAAAA1", 25) in api.ytm.calls
    assert body["personalized"] is True                               # M13: «персонально» уже при текущем треке


def test_radio_soundcloud_uses_related_endpoint(api):
    api.sc.responses["tracks/1000000001/related"] = load_fixture("soundcloud/search_tracks.json")
    api.ytm.songs["*"] = []
    body = api.get("/api/recommendations", params={"source": "sc", "id": "1000000001"}).json()
    assert ("api_get", "tracks/1000000001/related", {"limit": 25}) in api.sc.calls
    assert {"sc:1000000002", "sc:1000000006"} <= set(keys(body))
    assert all(t["reason"] == "Похоже на текущий трек" for t in body["tracks"] if t["source"] == "sc")


def test_preferred_source_from_settings(api):
    api.put("/api/settings", json={"default_source": "sc"})
    api.sc.responses["search/tracks"] = load_fixture("soundcloud/search_tracks.json")
    body = api.get("/api/recommendations").json()
    assert not [c for c in api.ytm.calls if c[0] == "search"]
    assert all(t["source"] == "sc" for t in body["tracks"]) and body["tracks"]


# ---------------------------------------------------------------------------
# Сбои источников кандидатов
# ---------------------------------------------------------------------------
def test_radio_failure_keeps_other_candidates(api):
    api.ytm.fail("get_watch_playlist")
    api.post("/api/favorites", json=track_payload(id_="FFFFFFFFF00", artist="Fav", artists=["Fav"]))
    api.ytm.songs["Fav"] = [yt("FFFFFFFFF01", "x", "Fav")]
    body = api.get("/api/recommendations", params={"source": "yt", "id": "AAAAAAAAAA1"}).json()
    assert keys(body) == ["yt:FFFFFFFFF01"]


def test_all_candidate_sources_failed_returns_empty_200(api):
    """M4 (фиксируем текущее поведение): сбой всех источников неотличим от «нечего предложить»."""
    api.ytm.fail("search")
    r = api.get("/api/recommendations")
    assert r.status_code == 200
    assert r.json() == {"tracks": [], "personalized": False, "page": 0}


@pytest.mark.xfail(strict=True, reason="M4: рекомендации прячут сбой источников (нет failed_sources), план S2c")
def test_all_candidate_sources_failed_is_reported(api):
    api.ytm.fail("search")
    body = api.get("/api/recommendations").json()
    assert body.get("failed_sources") == ["yt"]


def test_deep_pages_stop_artist_searches(api):
    api.post("/api/favorites", json=track_payload(id_="FFFFFFFFF00", artist="Fav", artists=["Fav"]))
    api.ytm.songs["Fav"] = [yt("FFFFFFFFF01", "x", "Fav")]
    body = api.get("/api/recommendations", params={"page": 9}).json()           # 12 * 9 >= MAX_SEARCH
    assert body["tracks"] == [] and not api.ytm.calls
