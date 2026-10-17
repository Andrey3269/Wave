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
    assert body["personalized"] is False and body["radio"] is False and body["page"] == params.get("page", 0)
    assert body["failed_sources"] == [] and body["source_status"] == {"yt": "ok"}
    assert keys(body) == ["yt:CSCSCSCSC01"]                       # дубль из двух запросов — один раз
    # S14: причина называет запрос общей подборки, который нашёл трек (до S14 — просто «Подборка»)
    assert body["tracks"][0]["reason"] == f"Подборка по запросу «{queries[0]}»"
    assert body["tracks"][0]["reason_kind"] == "general"
    assert body["tracks"][0]["score"] == W["same_source"]


# ---------------------------------------------------------------------------
# Баллы: каждый можно проследить по таблице W
# ---------------------------------------------------------------------------
def test_scoring_is_traceable(api):
    # слушали и пропустили. S14: пропуск — отдельная запись со снимком (контракт фронтенда с S10); пометка прежней
    # записи (до S10) больше не считается прослушиванием исполнителя — см. test_legacy_skip_is_not_an_artist_play
    play(api, "HHHHHHHHH01", "Hist Artist")
    api.post("/api/history/skip", json={"source": "yt", "id": "HHHHHHHHH01", "track": track_payload(
        id_="HHHHHHHHH01", artist="Hist Artist", artists=["Hist Artist"], title="t HHHHHHHHH01")})
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
    assert {t["id"]: t["reason_kind"] for t in body["tracks"]} == {
        "FFFFFFFFF02": "fav_artist", "FFFFFFFFF01": "fav_artist", "HHHHHHHHH02": "history_artist", "HHHHHHHHH01": "history_artist"}
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
    assert all(t["reason"] == "Похоже на «Northern Lights»" and t["reason_kind"] == "radio" for t in body["tracks"])
    assert [t["score"] for t in body["tracks"]] == [W["radio"] + W["same_source"]] * 2
    assert ("get_watch_playlist", "AAAAAAAAAA1", 25) in api.ytm.calls
    # M13/M4: радио трека без истории и избранного — не «персональная» подборка (до S2c здесь было True)
    assert body["personalized"] is False and body["radio"] is True


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
    """Частичный сбой: радио не ответило, поиск по любимому артисту — да. Результат есть, сбой виден."""
    api.ytm.fail("get_watch_playlist")
    api.post("/api/favorites", json=track_payload(id_="FFFFFFFFF00", artist="Fav", artists=["Fav"]))
    api.ytm.songs["Fav"] = [yt("FFFFFFFFF01", "x", "Fav")]
    body = api.get("/api/recommendations", params={"source": "yt", "id": "AAAAAAAAAA1"}).json()
    assert keys(body) == ["yt:FFFFFFFFF01"]
    assert body["failed_sources"] == ["yt"] and body["source_status"] == {"yt": "partial"}
    assert body["personalized"] is True and body["radio"] is False


def test_all_candidate_sources_failed_is_reported(api):
    """M4: полный сбой — 200 с пустым списком, но failed_sources/source_status говорят правду."""
    api.ytm.fail("search")
    r = api.get("/api/recommendations")
    assert r.status_code == 200
    assert r.json() == {"tracks": [], "personalized": False, "radio": False, "page": 0,
                        "failed_sources": ["yt"], "source_status": {"yt": "failed"}}


def test_success_without_suitable_tracks_is_not_a_failure(api):
    """Источник ответил, но подходящего нет: пусто, без failed_sources — это «нечего предложить»."""
    api.ytm.songs["*"] = []
    body = api.get("/api/recommendations").json()
    assert body["tracks"] == [] and body["failed_sources"] == [] and body["source_status"] == {"yt": "ok"}
    assert body["personalized"] is False


def test_everything_filtered_out_is_not_a_failure(api):
    api.ytm.songs["*"] = [yt("XXXXXXXXX01", "a", "A")]
    body = api.get("/api/recommendations", params={"exclude": "yt:XXXXXXXXX01"}).json()
    assert body["tracks"] == [] and body["failed_sources"] == []


def test_soundcloud_failure_is_named(api):
    """
    Все задания одного запроса идут в один источник (радио — в источник трека, поиск — в него же).
    Сбой SoundCloud называется своим именем, YouTube Music при этом не опрашивается.
    """
    api.sc.responses["tracks/1000000001/related"] = RuntimeError("SoundCloud недоступен (тест)")
    api.sc.responses["search/tracks"] = RuntimeError("SoundCloud недоступен (тест)")
    api.post("/api/favorites", json=track_payload(id_="FFFFFFFFF00", artist="Fav", artists=["Fav"]))
    body = api.get("/api/recommendations", params={"source": "sc", "id": "1000000001"}).json()
    assert body["tracks"] == []
    assert body["failed_sources"] == ["sc"] and body["source_status"] == {"sc": "failed"}
    assert not api.ytm.calls


def test_personalized_only_when_history_actually_shaped_the_result(api):
    """
    История есть, но поиск по её артистам упал, а радио вернуло незнакомых артистов: выдача не персональная.
    До S2c `personalized` был True при любой истории или текущем треке.
    """
    play(api, "HHHHHHHHH01", "Hist Artist")
    api.ytm.fail("search")
    api.ytm.watch["AAAAAAAAAA1"] = load_fixture("ytmusic/watch_playlist.json")
    body = api.get("/api/recommendations", params={"source": "yt", "id": "AAAAAAAAAA1"}).json()
    assert body["tracks"]
    assert body["personalized"] is False and body["radio"] is True
    assert body["source_status"] == {"yt": "partial"}


def test_radio_track_by_a_played_artist_counts_as_personal(api):
    """Трек из радио получил балл за слушанного артиста — значит, история повлияла на выдачу."""
    api.ytm.watch["AAAAAAAAAA1"] = load_fixture("ytmusic/watch_playlist.json")
    artist = load_fixture("ytmusic/watch_playlist.json")["tracks"][1]["artists"][0]["name"]
    play(api, "HHHHHHHHH01", artist)
    api.ytm.songs["*"] = []
    body = api.get("/api/recommendations", params={"source": "yt", "id": "AAAAAAAAAA1"}).json()
    assert body["personalized"] is True and body["radio"] is True


def test_deep_pages_stop_artist_searches(api):
    api.post("/api/favorites", json=track_payload(id_="FFFFFFFFF00", artist="Fav", artists=["Fav"]))
    api.ytm.songs["Fav"] = [yt("FFFFFFFFF01", "x", "Fav")]
    body = api.get("/api/recommendations", params={"page": 9}).json()           # 12 * 9 >= MAX_SEARCH
    assert body["tracks"] == [] and not api.ytm.calls
    assert body["failed_sources"] == [] and body["source_status"] == {}          # спрашивать было некого
