"""
S15: локальная музыка — источник `local` (backend/localmusic.py, streaming/router.py).

Импорт — копия файла в <data>/local под именем, которое выбирает сервер; путь на компьютере пользователя API не
принимает. Проверяются: сигнатура формата, расширение, размер, обход каталогов в имени, повторный импорт, Range
200/206/416 и HEAD, /playback, удаление с уборкой избранного и плейлистов, изоляция от YouTube Music и SoundCloud.
"""
from __future__ import annotations

import struct

import pytest

import localmusic
from wavetest.fakes import track_payload


def wav(seconds: float = 1.0, rate: int = 8000, seed: int = 0) -> bytes:
    n = int(seconds * rate)
    pcm = bytes((i * 7 + seed) % 251 for i in range(n))
    return (b"RIFF" + struct.pack("<I", 36 + n) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate, 1, 8)
            + b"data" + struct.pack("<I", n) + pcm)


MP3 = b"ID3\x04\x00\x00\x00\x00\x00\x00" + bytes(range(200)) * 5
MP3_SYNC = b"\xff\xfb\x90\x64" + bytes(500)                      # кадр MPEG-1 Layer III без ID3
FLAC = b"fLaC" + bytes(300)
OGG = b"OggS" + bytes(300)
M4A = b"\x00\x00\x00\x20ftypM4A " + bytes(300)
WEBM = b"\x1aE\xdf\xa3" + bytes(300)


def upload(api, data: bytes, name: str = "Артист - Песня.wav", duration: int | None = 1, **headers):
    params = {"name": name}
    if duration is not None:
        params["duration"] = duration
    return api.post("/api/library/local", params=params, content=data,
                    headers={"Content-Type": "application/octet-stream", **headers})


def _files(api) -> list[str]:
    root = api.profile.data / "local"
    return sorted(p.name for p in root.iterdir()) if root.is_dir() else []


# ---------------------------------------------------------------------------
def test_import_list_and_track_shape(api):
    r = upload(api, wav(), "C:\\Users\\Иван\\Music\\Кино - Группа крови.wav", duration=245)
    assert r.status_code == 200, r.text
    body = r.json()
    t = body["track"]
    assert body["added"] is True
    assert set(t) == {"id", "source", "title", "artist", "artists", "album", "album_id", "duration", "cover",
                      "explicit", "stream_url", "artist_id", "artist_ids", "plays", "released", "url"}
    assert (t["source"], t["title"], t["artist"], t["artists"], t["duration"]) == ("local", "Группа крови", "Кино", ["Кино"], 245)
    assert localmusic.ID_RE.fullmatch(t["id"]) and t["stream_url"] == f"/api/stream/local/{t['id']}"
    assert t["cover"] is None and t["url"] is None
    assert _files(api) == [f"{t['id']}.wav"]                                 # имя выбрал сервер, папок нет
    assert api.get("/api/library/local").json() == {"items": [t]}
    row = api.profile.sql("SELECT ext, size, sha256 FROM local_tracks")[0]
    assert row[0] == ".wav" and row[1] == len(wav()) and len(row[2]) == 64


@pytest.mark.parametrize("data, name", [(MP3, "a.mp3"), (MP3_SYNC, "b.MP3"), (FLAC, "c.flac"), (OGG, "d.ogg"),
                                        (OGG, "e.opus"), (M4A, "f.m4a"), (WEBM, "g.webm"), (wav(), "h.wav")])
def test_supported_formats(api, data, name):
    r = upload(api, data, name)
    assert r.status_code == 200, r.text
    assert r.json()["track"]["artist"] == localmusic.UNKNOWN_ARTIST
    assert r.json()["track"]["title"] == name.rsplit(".", 1)[0]


@pytest.mark.parametrize("data, name, code", [
    (b"MZ\x90\x00" + bytes(100), "virus.mp3", 415),            # не аудио, расширение mp3
    (wav(), "song.mp3", 415),                                  # WAV под видом mp3
    (MP3, "song.exe", 415),                                    # расширение не из списка
    (MP3, "song", 415),                                        # без расширения
    (b"\xff\xf1\x50\x80" + bytes(100), "x.mp3", 415),          # ADTS AAC — не MPEG audio
    (b"", "empty.wav", 400),                                   # пустой файл
    (wav(), "a\u0000b.wav", 400),                              # управляющий символ в имени
])
def test_rejected_files_leave_nothing(api, data, name, code):
    r = upload(api, data, name)
    assert r.status_code == code, r.text
    assert api.get("/api/library/local").json() == {"items": []}
    assert _files(api) == []                                                # временный файл убран


def test_name_too_long_or_missing(api):
    assert upload(api, wav(), "x" * 300 + ".wav").status_code == 422
    assert api.post("/api/library/local", content=wav()).status_code == 422
    assert upload(api, wav(), duration=0).status_code == 422


@pytest.mark.parametrize("name", ["../../evil.wav", "..\\..\\Windows\\evil.wav", "/etc/passwd.wav", "C:evil.wav"])
def test_paths_in_name_are_only_labels(api, name):
    r = upload(api, wav(), name)
    assert r.status_code == 200
    t = r.json()["track"]
    assert "/" not in t["title"] and "\\" not in t["title"] and ".." not in t["title"]
    assert _files(api) == [f"{t['id']}.wav"]
    assert not (api.profile.root / "evil.wav").exists() and not (api.profile.data / "evil.wav").exists()


def test_size_limit(api, monkeypatch):
    monkeypatch.setattr(localmusic, "MAX_BYTES", 1000)
    assert upload(api, wav(seconds=1)).status_code == 413                    # объявленный размер больше лимита
    assert _files(api) == []
    assert upload(api, wav(seconds=0.01)).status_code == 200                 # меньше лимита — принят


def test_size_limit_without_content_length(api, monkeypatch):
    monkeypatch.setattr(localmusic, "MAX_BYTES", 1000)

    def gen():
        yield wav(seconds=0.05)
        yield bytes(2000)

    r = api.post("/api/library/local", params={"name": "a.wav"}, content=gen())
    assert r.status_code == 413
    assert _files(api) == [] and api.get("/api/library/local").json() == {"items": []}


def test_same_file_is_imported_once(api):
    first = upload(api, wav(), "Один.wav").json()
    again = upload(api, wav(), "Другое имя.wav").json()
    assert again == {"track": first["track"], "added": False}
    assert len(_files(api)) == 1
    other = upload(api, wav(seed=3), "Другой.wav").json()
    assert other["added"] is True and other["track"]["id"] != first["track"]["id"]


# ---------------------------------------------------------------------------
# Воспроизведение
# ---------------------------------------------------------------------------
def test_playback_and_ranges(api):
    data = wav()
    tid = upload(api, data).json()["track"]["id"]
    url = f"/api/stream/local/{tid}"
    assert api.get(f"{url}/playback").json() == {"kind": "local", "url": url, "preview": False}
    full = api.get(url)
    assert full.status_code == 200 and full.content == data and full.headers["content-type"] == "audio/wav"
    part = api.get(url, headers={"Range": "bytes=10-19"})
    assert part.status_code == 206 and part.content == data[10:20]
    assert part.headers["content-range"] == f"bytes 10-19/{len(data)}"
    tail = api.get(url, headers={"Range": "bytes=-5"})
    assert tail.status_code == 206 and tail.content == data[-5:]
    assert api.get(url, headers={"Range": f"bytes={len(data) + 10}-"}).status_code == 416
    assert api.get(url, headers={"Range": "bytes=abc"}).status_code == 200          # битый Range игнорируется
    head = api.http.head(url)
    assert head.status_code == 200 and int(head.headers["content-length"]) == len(data) and not head.content
    assert api.get(url, params={"warm": 1}).status_code == 204
    assert not api.resolver.calls                                            # yt-dlp не вызывался


@pytest.mark.parametrize("tid, code", [("0123456789abcdef", 404), ("../../../etc", 404), ("ABCDEF0123456789", 400),
                                       ("0123456789abcde", 400), ("0123456789abcdef0", 400)])
def test_unknown_or_bad_local_ids(api, tid, code):
    upload(api, wav())
    r = api.get(f"/api/stream/local/{tid}")
    assert r.status_code == code
    assert api.get(f"/api/stream/local/{tid}/playback").status_code in (code, 404)


def test_file_removed_from_disk_gives_honest_404(api):
    tid = upload(api, wav()).json()["track"]["id"]
    (api.profile.data / "local" / f"{tid}.wav").unlink()
    r = api.get(f"/api/stream/local/{tid}")
    assert r.status_code == 404 and "не найден" in r.json()["detail"]


def test_tampered_extension_in_database_is_not_followed(api):
    tid = upload(api, wav()).json()["track"]["id"]
    import library
    library._q("UPDATE local_tracks SET ext = '/../../wave.db' WHERE id = ?", (tid,))
    assert api.get(f"/api/stream/local/{tid}").status_code == 404
    assert localmusic.audio_path(tid) is None


def test_symlink_is_not_served(api, tmp_path):
    tid = upload(api, wav()).json()["track"]["id"]
    f = api.profile.data / "local" / f"{tid}.wav"
    secret = tmp_path / "secret.wav"
    secret.write_bytes(wav(seed=9))
    f.unlink()
    try:
        f.symlink_to(secret)
    except (OSError, NotImplementedError):
        pytest.skip("символические ссылки недоступны")
    assert api.get(f"/api/stream/local/{tid}").status_code == 404


# ---------------------------------------------------------------------------
# Избранное, плейлисты, история, удаление
# ---------------------------------------------------------------------------
def test_local_track_in_favorites_playlists_history_and_delete(api):
    t = upload(api, wav()).json()["track"]
    other = track_payload(id_="AAAAAAAAAA1")
    assert api.post("/api/favorites", json=t).status_code == 200
    pid = api.post("/api/playlists", json={"name": "Смешанный"}).json()["id"]
    assert api.post(f"/api/playlists/{pid}/tracks", json=t).json() == {"added": True}
    api.post(f"/api/playlists/{pid}/tracks", json=other)
    assert api.post("/api/history", json=t).status_code == 200
    assert api.get("/api/favorites").json()["items"][0]["stream_url"] == t["stream_url"]
    assert [x["source"] for x in api.get(f"/api/playlists/{pid}").json()["tracks"]] == ["local", "yt"]

    assert api.delete(f"/api/library/local/{t['id']}").json() == {"ok": True}
    assert _files(api) == [] and api.get("/api/library/local").json() == {"items": []}
    assert api.get("/api/favorites").json()["items"] == []
    assert [x["id"] for x in api.get(f"/api/playlists/{pid}").json()["tracks"]] == ["AAAAAAAAAA1"]
    assert api.get("/api/history").json()["tracks"][0]["id"] == t["id"]      # история — запись прошлого, остаётся
    assert api.get(f"/api/stream/local/{t['id']}").status_code == 404
    assert api.delete(f"/api/library/local/{t['id']}").status_code == 404
    assert api.delete("/api/library/local/..%2F..").status_code in (400, 404, 405)   # не id — ничего не удаляется
    assert (api.profile.data / "wave.db").is_file()


def test_local_track_cannot_be_downloaded_or_excluded_by_id(api):
    t = upload(api, wav()).json()["track"]
    assert api.post("/api/downloads", json=t).status_code == 422
    assert api.post("/api/recommendations/exclusions", json={"kind": "track", "source": "local", "id": t["id"]}).status_code == 422


def test_foreign_local_id_in_snapshot_is_accepted_but_not_playable(api):
    fake = track_payload("local", "fedcba9876543210", cover=None)
    assert api.post("/api/favorites", json=fake).status_code == 200
    assert api.get("/api/stream/local/fedcba9876543210").status_code == 404
    bad = track_payload("local", "../../x", cover=None)
    assert api.post("/api/favorites", json=bad).status_code == 422


def test_recommendations_ignore_local_tracks_without_artist(api):
    t = upload(api, wav(), "просто файл.wav").json()["track"]
    for _ in range(3):
        api.post("/api/history", json=t)
    api.post("/api/favorites", json=t)
    r = api.get("/api/recommendations")
    assert r.status_code == 200
    queries = [c for c in api.ytm.calls if c[0] == "search"]
    assert all(localmusic.UNKNOWN_ARTIST not in str(c) for c in queries)


def test_youtube_and_soundcloud_keep_working_next_to_local(api):
    upload(api, wav())
    assert api.get("/api/stream/yt/!!!/playback").status_code == 400
    assert api.get("/api/stream/zz/0123456789abcdef").status_code == 404
    assert api.get("/api/sources").status_code == 200


def test_startup_removes_interrupted_imports_and_orphans(harness, wave_profile):
    with harness.client() as api:
        keep = upload(api, wav()).json()["track"]["id"]
    root = wave_profile.data / "local"
    (root / ".import-0123456789abcdef.part").write_bytes(b"x")
    (root / "fedcba9876543210.mp3").write_bytes(MP3)                         # копия без записи в базе
    (root / "notes.txt").write_text("чужой файл")                            # не наш формат имени — не трогаем
    from persistence import db as store
    store.close()
    with harness.client() as api:
        assert sorted(p.name for p in root.iterdir()) == sorted([f"{keep}.wav", "notes.txt"])
        assert api.get(f"/api/stream/local/{keep}").status_code == 200
