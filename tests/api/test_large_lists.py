"""
S16: длинные списки. «Нравится» и плейлист отдаются целиком одним ответом, как раньше (фронтенд рисует их порциями,
frontend/js/views/list.js), а сериализация идёт без jsonable_encoder: тело ответа должно остаться тем же. История
и поиск ограничены постранично. Абсолютные тайминги здесь не проверяются (они зависят от машины) — только объём,
порядок и контракт; замеры — tests/perf/measure.py.
"""
from __future__ import annotations

import json
import time

N_FAV = 3000
N_PL = 1500


def _track(i: int, prefix: str = "f") -> dict:
    vid = f"{prefix}{i:010d}"[-11:]
    return {"id": vid, "source": "yt", "title": f"Трек {i}", "artist": f"Исполнитель {i % 7}", "artists": [f"Исполнитель {i % 7}"],
            "album": None, "duration": 100 + i % 50, "cover": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
            "explicit": False, "stream_url": f"/api/stream/yt/{vid}"}


def _import(api, favorites: int, playlist: int) -> None:
    now = time.time()
    doc = {"format": "wave-library", "version": 1,
           "favorites": {"tracks": [{"added_at": now - i, "track": _track(i)} for i in range(favorites)]},
           "playlists": [{"name": "Большой", "created_at": now, "updated_at": now,
                          "tracks": [{"added_at": now, "track": _track(i, "p")} for i in range(playlist)]}],
           "settings": {}}
    r = api.post("/api/library/import", content=json.dumps(doc), headers={"Content-Type": "application/json"})
    assert r.status_code == 200, r.text


def test_long_favorites_returned_whole_in_order_with_same_body(api):
    _import(api, N_FAV, 0)
    r = api.get("/api/favorites")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/json")
    items = r.json()["items"]
    assert len(items) == N_FAV
    assert [t["id"] for t in items[:3]] == [_track(i)["id"] for i in range(3)]          # новые сверху
    # Тело — компактный JSON без экранирования кириллицы, как у обычного ответа FastAPI
    assert r.content == json.dumps({"items": items}, ensure_ascii=False, separators=(",", ":")).encode()
    assert all(t["stream_url"] == f"/api/stream/yt/{t['id']}" for t in items)


def test_long_playlist_keeps_order_and_contract(api):
    _import(api, 0, N_PL)
    pid = api.get("/api/playlists").json()["items"][0]["id"]
    d = api.get(f"/api/playlists/{pid}").json()
    assert d["playlist"]["count"] == N_PL and d["playlist"]["name"] == "Большой"
    assert [t["id"] for t in d["tracks"]] == [_track(i, "p")["id"] for i in range(N_PL)]
    assert d["playlist"]["cover"] == d["tracks"][0]["cover"]
    assert api.get("/api/playlists/999999").status_code == 404


def test_history_is_paged_and_bounded(api, backend):
    rows = [(f"yt:{_track(i, 'h')['id']}", json.dumps(_track(i, "h"), ensure_ascii=False), 1000.0 + i) for i in range(500)]
    with backend["library"].transaction() as con:
        con.executemany("INSERT INTO history (key, data, played_at) VALUES (?, ?, ?)", rows)
    page = api.get("/api/history?limit=50&offset=0").json()
    assert len(page["tracks"]) == 50 and page["has_more"] is True
    assert page["tracks"][0]["id"] == _track(499, "h")["id"]                             # последние сверху
    assert api.get("/api/history?limit=51").status_code == 422                         # страница не больше 50
    assert api.get("/api/history?offset=2001").status_code == 422                       # и не глубже 2000
