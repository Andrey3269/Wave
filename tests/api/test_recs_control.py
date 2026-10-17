"""
S14: управление рекомендациями и честность выдачи (backend/recs.py, backend/recs_store.py).

Исключения трека и исполнителя (таблица rec_exclusions), сброс сигналов персонализации без удаления истории и
избранного, сигналы пропусков, разнообразие (альбом, повтор песни на другом видео, новизна), причина рекомендации
по самому треку.
"""
from __future__ import annotations

import time

import pytest

import recs
import recs_store
from wavetest.fakes import track_payload

W = recs.W


def yt(vid: str, title: str, artist: str, album: str | None = None, album_id: str | None = None, artists=None) -> dict:
    item = {"videoId": vid, "title": title, "artists": [{"name": a, "id": None} for a in (artists or [artist])]}
    if album:
        item["album"] = {"name": album, "id": album_id}
    return item


def body_for(vid: str, artist: str, title: str | None = None, source: str = "yt") -> dict:
    return track_payload(source, vid, artist=artist, artists=[artist], title=title or f"t {vid}")


def play(api, vid, artist, title=None):
    assert api.post("/api/history", json=body_for(vid, artist, title)).status_code == 200


def skip(api, vid, artist, title=None):
    r = api.post("/api/history/skip", json={"source": "yt", "id": vid, "track": body_for(vid, artist, title)})
    assert r.status_code == 200


def keys(body) -> list[str]:
    return [f"{t['source']}:{t['id']}" for t in body["tracks"]]


def recommend(api, **params) -> dict:
    r = api.get("/api/recommendations", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def exclude_track(api, vid, title="Песня", artist="Артист", source="yt"):
    return api.post("/api/recommendations/exclusions",
                    json={"kind": "track", "source": source, "id": vid, "title": title, "artist": artist})


def exclude_artist(api, name):
    return api.post("/api/recommendations/exclusions", json={"kind": "artist", "artist": name})


# ---------------------------------------------------------------------------
# Исключения
# ---------------------------------------------------------------------------
def test_excluded_track_is_never_recommended_and_can_be_returned(api):
    api.ytm.songs["*"] = [yt("XXXXXXXXX01", "a", "A"), yt("XXXXXXXXX02", "b", "B")]
    api.ytm.watch["CURCURCUR01"] = {"tracks": [yt("XXXXXXXXX01", "a", "A"), yt("XXXXXXXXX03", "c", "C")]}
    r = exclude_track(api, "XXXXXXXXX01", "a", "A")
    assert r.status_code == 200
    assert r.json()["item"] == {"kind": "track", "value": "yt:XXXXXXXXX01", "label": "a — A",
                                "created_at": r.json()["item"]["created_at"]}
    assert keys(recommend(api)) == ["yt:XXXXXXXXX02"]
    assert "yt:XXXXXXXXX01" not in keys(recommend(api, source="yt", id="CURCURCUR01", title="cur"))

    assert api.delete("/api/recommendations/exclusions/track/yt:XXXXXXXXX01").json() == {"ok": True}
    assert "yt:XXXXXXXXX01" in keys(recommend(api))
    assert api.get("/api/recommendations/exclusions").json() == {"items": [], "reset_at": None}


def test_excluded_artist_matches_any_spelling_source_and_coartist(api):
    api.ytm.songs["*"] = [
        yt("ARTARTART01", "one", "Шум Волн"),
        yt("ARTARTART02", "two", "ШУМ  волн!"),                              # другое написание
        yt("ARTARTART03", "feat", "Гость", artists=["Гость", "Шум волн"]),   # соавтор
        yt("ARTARTART04", "ok", "Другой"),
    ]
    assert exclude_artist(api, "Шум волн").json()["item"]["value"] == "шум волн"
    assert keys(recommend(api)) == ["yt:ARTARTART04"]
    api.put("/api/settings", json={"default_source": "sc"})
    api.sc.responses["search/tracks"] = {"collection": [
        {"kind": "track", "id": 1000000071, "title": "sc", "user": {"username": "шум волн", "id": 5},
         "permalink_url": "https://soundcloud.com/x/y", "duration": 1000, "media": {"transcodings": []}},
    ]}
    assert recommend(api)["tracks"] == []                                      # тот же исполнитель на SoundCloud
    api.delete("/api/recommendations/exclusions/artist/шум волн")
    assert keys(recommend(api)) == ["sc:1000000071"]                           # без исключения трек был бы в выдаче


def test_excluded_artist_is_not_searched_even_if_favorite(api):
    api.post("/api/favorites", json=body_for("FAVFAVFAV01", "Любимый"))
    api.ytm.songs["Любимый"] = [yt("FAVFAVFAV02", "x", "Любимый")]
    api.ytm.songs["*"] = [yt("GENGENGEN01", "g", "Общий")]
    exclude_artist(api, "любимый")
    body = recommend(api)
    assert "Любимый" not in {c[1] for c in api.ytm.calls if c[0] == "search"}
    assert keys(body) == ["yt:GENGENGEN01"] and body["personalized"] is False
    # исключение не трогает пользовательские данные
    assert [f["id"] for f in api.get("/api/favorites").json()["items"]] == ["FAVFAVFAV01"]


def test_exclusion_does_not_touch_history_favorites_or_playlists(api):
    play(api, "KEEPKEEP001", "Артист")
    api.post("/api/favorites", json=body_for("KEEPKEEP001", "Артист"))
    pid = api.post("/api/playlists", json={"name": "P"}).json()["id"]
    api.post(f"/api/playlists/{pid}/tracks", json=body_for("KEEPKEEP001", "Артист"))
    exclude_track(api, "KEEPKEEP001")
    exclude_artist(api, "Артист")
    assert [t["id"] for t in api.get("/api/history").json()["tracks"]] == ["KEEPKEEP001"]
    assert [t["id"] for t in api.get("/api/favorites").json()["items"]] == ["KEEPKEEP001"]
    assert api.get(f"/api/playlists/{pid}").json()["playlist"]["count"] == 1


def test_repeated_exclusion_keeps_one_row_and_first_date(api):
    first = exclude_track(api, "DUPDUPDUP01", "old", "A").json()["item"]
    time.sleep(0.01)
    second = exclude_track(api, "DUPDUPDUP01", "new", "A").json()["item"]
    assert second["created_at"] == first["created_at"] and second["label"] == "new — A"
    assert api.profile.sql("SELECT COUNT(*) FROM rec_exclusions")[0][0] == 1


def test_exclusions_are_listed_newest_first_with_clean_labels(api):
    exclude_artist(api, "Первый\x07")
    time.sleep(0.01)
    exclude_track(api, "LBLLBLLBL01", "Название\nс переводом", "Исп")
    items = api.get("/api/recommendations/exclusions").json()["items"]
    assert [(i["kind"], i["value"], i["label"]) for i in items] == [
        ("track", "yt:LBLLBLLBL01", "Название с переводом — Исп"), ("artist", "первый", "Первый")]


def test_artist_with_encoded_name_can_be_removed(api):
    exclude_artist(api, "AC/DC & Co")
    value = api.get("/api/recommendations/exclusions").json()["items"][0]["value"]
    assert value == "ac dc co"
    assert api.delete("/api/recommendations/exclusions/artist/ac%20dc%20co").status_code == 200
    assert api.get("/api/recommendations/exclusions").json()["items"] == []
    assert api.delete("/api/recommendations/exclusions/artist/nothing").json() == {"ok": True}   # нет — не ошибка


@pytest.mark.parametrize("payload", [
    {"kind": "track", "source": "yt", "id": "short"},                       # не формат YouTube
    {"kind": "track", "source": "sc", "id": "abc"},                         # не число SoundCloud
    {"kind": "track", "source": "yt"},                                      # нет id
    {"kind": "track", "source": "xx", "id": "XXXXXXXXX01"},
    {"kind": "artist", "artist": "  !!  "},                                 # пустое после нормализации
    {"kind": "artist"},
    {"kind": "album", "artist": "A"},
    {"kind": "artist", "artist": "a" * (recs_store.LABEL_MAX + 1)},
])
def test_invalid_exclusion_is_rejected(api, payload):
    assert api.post("/api/recommendations/exclusions", json=payload).status_code == 422
    assert api.profile.sql("SELECT COUNT(*) FROM rec_exclusions")[0][0] == 0


def test_unknown_kind_on_delete_is_rejected(api):
    assert api.delete("/api/recommendations/exclusions/album/x").status_code == 422


def test_exclusion_limit(api, monkeypatch):
    monkeypatch.setattr(recs_store, "EXCLUSIONS_MAX", 2)
    assert exclude_artist(api, "a").status_code == 200
    assert exclude_artist(api, "b").status_code == 200
    r = exclude_artist(api, "c")
    assert r.status_code == 400 and "Слишком много исключений" in r.json()["detail"]
    assert exclude_artist(api, "a").status_code == 200                      # повтор существующего — не новый
    assert api.profile.sql("SELECT COUNT(*) FROM rec_exclusions")[0][0] == 2


# ---------------------------------------------------------------------------
# Сброс сигналов персонализации
# ---------------------------------------------------------------------------
def test_reset_ignores_old_signals_without_deleting_anything(api):
    play(api, "OLDOLDOLD01", "Старый")
    api.post("/api/favorites", json=body_for("OLDFAVOLD01", "Старый Любимый"))
    api.ytm.songs["Старый"] = [yt("STASTASTA01", "s", "Старый")]
    api.ytm.songs["Старый Любимый"] = [yt("STLSTLSTL01", "s", "Старый Любимый")]
    api.ytm.songs["*"] = [yt("GENGENGEN01", "g", "Общий")]
    assert recommend(api)["personalized"] is True
    time.sleep(0.01)

    r = api.post("/api/recommendations/reset").json()
    assert r["ok"] is True and isinstance(r["reset_at"], float)
    assert api.get("/api/recommendations/exclusions").json()["reset_at"] == r["reset_at"]
    api.ytm.calls.clear()
    body = recommend(api)
    assert body["personalized"] is False and keys(body) == ["yt:GENGENGEN01"]     # холодный старт
    assert {c[1] for c in api.ytm.calls if c[0] == "search"} <= set(recs.COLD_START_QUERIES)
    # история и избранное на месте
    assert [t["id"] for t in api.get("/api/history").json()["tracks"]] == ["OLDOLDOLD01"]
    assert [t["id"] for t in api.get("/api/favorites").json()["items"]] == ["OLDFAVOLD01"]

    time.sleep(0.01)
    play(api, "NEWNEWNEW01", "Новый")                                        # после сброса — снова сигнал
    api.ytm.songs["Новый"] = [yt("NEWNEWNEW02", "n", "Новый")]
    body = recommend(api)
    assert body["personalized"] is True and keys(body) == ["yt:NEWNEWNEW02"]

    assert api.delete("/api/recommendations/reset").json() == {"ok": True, "reset_at": None}
    assert {"yt:STASTASTA01", "yt:STLSTLSTL01"} <= set(keys(recommend(api)))      # прежние сигналы вернулись


def test_reset_keeps_repeat_protection_and_exclusions(api):
    play(api, "RECRECREC01", "Артист", "Песня")
    exclude_track(api, "EXCEXCEXC01")
    time.sleep(0.01)
    api.post("/api/recommendations/reset")
    api.ytm.songs["*"] = [yt("RECRECREC01", "Песня", "Артист"), yt("RECRECREC02", "Песня", "Артист"),
                          yt("EXCEXCEXC01", "e", "E"), yt("OKOKOKOKO01", "ok", "OK")]
    assert keys(recommend(api)) == ["yt:OKOKOKOKO01"]


def test_reset_marker_is_not_a_public_setting(api):
    api.post("/api/recommendations/reset")
    s = api.get("/api/settings").json()
    assert "recs_reset_at" not in s["values"]
    assert api.put("/api/settings", json={"recs_reset_at": "0"}).status_code == 400


def test_corrupt_reset_marker_means_no_reset(api):
    play(api, "SIGSIGSIG01", "Сигнал")
    api.ytm.songs["Сигнал"] = [yt("SIGSIGSIG02", "x", "Сигнал")]
    api.profile.sql("INSERT INTO settings (key, value) VALUES ('recs_reset_at', 'мусор')")
    assert recommend(api)["personalized"] is True
    assert api.get("/api/recommendations/exclusions").json()["reset_at"] is None


# ---------------------------------------------------------------------------
# Сигналы пропусков
# ---------------------------------------------------------------------------
def test_skip_records_are_not_artist_plays(api):
    """Запись-пропуск (S10) не делает исполнителя «любимым»: до S14 давала +5 и поиск по нему."""
    for i in range(2):
        skip(api, f"SKPSKPSKP{i:02d}", "Пропускаемый")
    api.ytm.songs["*"] = [yt("GENGENGEN01", "g", "Общий")]
    api.ytm.songs["Пропускаемый"] = [yt("SKPSKPSKP10", "s", "Пропускаемый")]
    body = recommend(api)
    assert "Пропускаемый" not in {c[1] for c in api.ytm.calls if c[0] == "search"}
    assert body["personalized"] is False


def test_legacy_skip_is_not_an_artist_play(api):
    """Фронтенд до S10 помечал прежнюю запись: она тоже не считается прослушиванием исполнителя."""
    play(api, "LEGLEGLEG01", "Старый Клиент")
    api.post("/api/history/skip", json={"source": "yt", "id": "LEGLEGLEG01"})
    api.ytm.songs["*"] = []
    api.ytm.songs["Старый Клиент"] = [yt("LEGLEGLEG02", "s", "Старый Клиент")]
    recommend(api)
    assert "Старый Клиент" not in {c[1] for c in api.ytm.calls if c[0] == "search"}


def test_artist_skipped_without_plays_is_not_offered(api):
    for i in range(recs.ARTIST_SKIP_HIDE):
        skip(api, f"HIDHIDHID{i:02d}", "Надоевший")
    api.ytm.watch["CURCURCUR01"] = {"tracks": [yt("HIDHIDHID10", "h", "Надоевший"), yt("NRMNRMNRM01", "n", "Норм")]}
    api.ytm.songs["*"] = []
    assert keys(recommend(api, source="yt", id="CURCURCUR01")) == ["yt:NRMNRMNRM01"]


def test_mostly_skipped_artist_gets_a_capped_penalty(api):
    play(api, "MSKMSKMSK00", "Спорный")
    for i in range(5):
        skip(api, f"MSKMSKMSK{i + 1:02d}", "Спорный")
    for i in range(recs.RECENT_EXCLUDE):                                       # убрать их из окна недавних
        play(api, f"FILFILFIL{i:02d}", "Наполнитель")
    api.ytm.watch["CURCURCUR01"] = {"tracks": [yt("MSKMSKMSK20", "m", "Спорный")]}
    api.ytm.songs["*"] = []
    t = recommend(api, source="yt", id="CURCURCUR01")["tracks"][0]
    assert t["score"] == W["radio"] + W["history_artist"] + W["artist_skipped_cap"] + W["same_source"]


def test_favorite_artist_is_never_hidden_by_skips(api):
    api.post("/api/favorites", json=body_for("FAVFAVFAV01", "Любимый"))
    for i in range(recs.ARTIST_SKIP_HIDE):
        skip(api, f"FSKFSKFSK{i:02d}", "Любимый")
    api.ytm.watch["CURCURCUR01"] = {"tracks": [yt("FSKFSKFSK10", "f", "Любимый")]}
    api.ytm.songs["*"] = []
    assert keys(recommend(api, source="yt", id="CURCURCUR01")) == ["yt:FSKFSKFSK10"]


# ---------------------------------------------------------------------------
# Разнообразие и повторы
# ---------------------------------------------------------------------------
def test_album_limit_by_album_id_across_artists(api):
    api.ytm.watch["CURCURCUR01"] = {"tracks": [
        yt(f"ALBALBALB{i:02d}", f"s{i}", f"Участник {i}", "Сборник", "MPREb_sbornik01") for i in range(5)]
        + [yt("SOLOSOLO001", "solo", "Соло", "Сборник", "MPREb_drugoi001")]}         # то же название, другой id
    api.ytm.songs["*"] = []
    body = recommend(api, source="yt", id="CURCURCUR01")
    assert sum(t["album_id"] == "MPREb_sbornik01" for t in body["tracks"]) == recs.MAX_PER_ALBUM
    assert "yt:SOLOSOLO001" in keys(body)


def test_album_limit_by_name_for_the_same_artist(api):
    api.ytm.watch["CURCURCUR01"] = {"tracks": [yt(f"ALNALNALN{i:02d}", f"s{i}", "Группа", "Пластинка") for i in range(3)]
                                    + [yt("ALNALNALN10", "x", "Другая", "Пластинка")]}
    api.ytm.songs["*"] = []
    body = recommend(api, source="yt", id="CURCURCUR01")
    assert sum(t["artist"] == "Группа" for t in body["tracks"]) == recs.MAX_PER_ALBUM
    assert "yt:ALNALNALN10" in keys(body)                                    # у другого артиста свой альбом


def test_recently_played_song_on_another_video_is_not_offered(api):
    play(api, "ORIORIORI01", "Певица", "Хит!")
    api.ytm.songs["*"] = [yt("COPCOPCOP01", "хит", "ПЕВИЦА"), yt("OTHOTHOTH01", "Другая", "Певица")]
    api.ytm.songs["Певица"] = api.ytm.songs["*"]
    assert keys(recommend(api)) == ["yt:OTHOTHOTH01"]


def test_new_artist_bonus_only_with_history(api):
    api.ytm.watch["CURCURCUR01"] = {"tracks": [yt("NEWARTIST01", "n", "Незнакомец")]}
    api.ytm.songs["*"] = []
    assert recommend(api, source="yt", id="CURCURCUR01")["tracks"][0]["score"] == W["radio"] + W["same_source"]
    play(api, "HISHISHIS01", "Знакомый")
    api.ytm.songs["Знакомый"] = []
    t = recommend(api, source="yt", id="CURCURCUR01")["tracks"][0]
    assert t["score"] == W["radio"] + W["new_artist"] + W["same_source"]
    assert t["reason"] == "Похоже на текущий трек"                         # причина — радио, а не «новое»


# ---------------------------------------------------------------------------
# Причина рекомендации
# ---------------------------------------------------------------------------
def test_reason_describes_the_track_not_the_query(api):
    """Поиск по слушанному исполнителю вернул чужой трек: это не «Из твоей истории»."""
    play(api, "HISHISHIS01", "Слушанный")
    api.post("/api/favorites", json=body_for("FAVFAVFAV01", "Любимый"))
    api.ytm.songs["Слушанный"] = [yt("COVCOVCOV01", "cover", "Каверщик"), yt("OWNOWNOWN01", "own", "Слушанный"),
                                  yt("FAVFAVFAV02", "fav", "Любимый")]
    api.ytm.songs["Любимый"] = []
    by_id = {t["id"]: t for t in recommend(api)["tracks"]}
    assert (by_id["COVCOVCOV01"]["reason"], by_id["COVCOVCOV01"]["reason_kind"]) == (
        "Найдено поиском по исполнителю Слушанный", "artist_search")
    assert (by_id["OWNOWNOWN01"]["reason"], by_id["OWNOWNOWN01"]["reason_kind"]) == (
        "Из твоей истории: Слушанный", "history_artist")
    assert (by_id["FAVFAVFAV02"]["reason"], by_id["FAVFAVFAV02"]["reason_kind"]) == (
        "Любимый исполнитель: Любимый", "fav_artist")
