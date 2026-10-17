"""
Загрузки /api/downloads (backend/downloads.py): трек, обложка, текст и metadata.json в отдельной папке,
сборка во временном *.part, индекс в SQLite, ошибки и очистка. CDN, обложки и LRCLIB подменены.
Какие треки скачиваются (поток штатно доступен, не фрагмент), отказы и SoundCloud — в test_download_policy.py.
"""
from __future__ import annotations

import json
import time

import httpx
import pytest

import downloads
import library
import stream
from wavetest.fakes import audio_bytes, load_fixture

YT = "AAAAAAAAAA1"
KEY = f"yt:{YT}"
CDN = f"/yt/{YT}"
COVER = "https://lh3.googleusercontent.com/cover=w544-h544"
DATA = audio_bytes(10_000, seed=5)
REQ = {"source": "yt", "id": YT, "title": "Northern Lights", "artist": "Aurora Wave", "album": "Polar Nights",
       "duration": 225, "cover": COVER}
FOLDER = "Aurora Wave - Northern Lights [yt-AAAAAAAAAA1]"


@pytest.fixture
def dl(api, monkeypatch):
    monkeypatch.setattr(downloads, "DL_CHUNK", 4096)        # несколько порций на маленьком файле
    api.net.add_file(CDN, DATA, "audio/mp4")
    api.net.images[COVER] = (b"\xff\xd8jpeg", "image/jpeg")
    api.net.lyrics = (200, load_fixture("lrclib/get_synced.json"))
    return api


def wait_job(api, key=KEY, timeout=5.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = api.get("/api/downloads/jobs").json().get(key)
        if job and job["state"] not in downloads.ACTIVE:
            return job
        time.sleep(0.02)
    raise AssertionError(f"загрузка {key} не завершилась: {api.get('/api/downloads/jobs').json()}")


def leftovers(api) -> list[str]:
    return sorted(p.name for p in api.profile.downloads.iterdir())


def test_full_download(dl):
    r = dl.post("/api/downloads", json=REQ)
    assert r.status_code == 202 and r.json()["state"] == "preparing"
    job = wait_job(dl)
    assert job["state"] == "done" and job["progress"] == 100 and job["error"] is None

    folder = dl.profile.downloads / FOLDER
    assert leftovers(dl) == [FOLDER]                                  # .part не осталось
    assert sorted(p.name for p in folder.iterdir()) == ["audio.m4a", "cover.jpg", "lyrics.json", "metadata.json"]
    assert (folder / "audio.m4a").read_bytes() == DATA
    assert (folder / "cover.jpg").read_bytes() == b"\xff\xd8jpeg"
    assert json.loads((folder / "lyrics.json").read_text(encoding="utf-8"))["synced"][0]["text"] == "Первая строка"
    meta = json.loads((folder / "metadata.json").read_text(encoding="utf-8"))
    assert meta == {"id": YT, "source": "yt", "title": "Northern Lights", "artist": "Aurora Wave", "album": "Polar Nights",
                    "duration": 225, "source_url": f"https://www.youtube.com/watch?v={YT}", "audio": "audio.m4a",
                    "cover": "cover.jpg", "lyrics": "lyrics.json"}

    ranges = [q.headers["range"] for q in dl.net.cdn_requests(CDN)]
    assert ranges == ["bytes=0-4095", "bytes=4096-8191", "bytes=8192-12287"]        # порциями с Range
    assert dl.resolver.calls[KEY] == 1
    assert dl.profile.sql("SELECT key, folder, audio, cover, lyrics, size, root FROM downloads") == [
        (KEY, FOLDER, "audio.m4a", "cover.jpg", "lyrics.json", len(DATA), str(dl.profile.downloads))]


def test_downloaded_track_is_listed_and_plays_offline(dl):
    dl.post("/api/downloads", json=REQ)
    wait_job(dl)
    listed = dl.get("/api/downloads").json()
    assert listed["dir"] == str(dl.profile.downloads)
    assert [(t["id"], t["downloaded"], t["has_lyrics"], t["cover"]) for t in listed["items"]] == [
        (YT, True, True, f"/api/local/yt/{YT}/cover")]
    requests_before = len(dl.net.requests)
    play = dl.get(f"/api/stream/yt/{YT}", headers={"Range": "bytes=100-199"})
    assert play.status_code == 206 and play.content == DATA[100:200]
    cover = dl.get(f"/api/local/yt/{YT}/cover")
    assert cover.status_code == 200 and cover.content == b"\xff\xd8jpeg"
    assert len(dl.net.requests) == requests_before                   # всё с диска


def test_upstream_without_range_support(dl):
    dl.net.support_ranges = False
    dl.net.files[CDN] = (DATA, "audio/mpeg")
    dl.post("/api/downloads", json=REQ)
    assert wait_job(dl)["state"] == "done"
    assert (dl.profile.downloads / FOLDER / "audio.mp3").read_bytes() == DATA
    assert len(dl.net.cdn_requests(CDN)) == 1


def test_progress_follows_received_bytes(dl):
    """Перед каждой порцией задача показывает уже полученные байты и честный процент; 100 — только после done."""
    seen: list[dict] = []

    def snapshot_then_serve(request):
        seen.append(dict(downloads.jobs[KEY]))
        return dl.net.serve_file(request)

    dl.net.queue(CDN, *[snapshot_then_serve] * 3)               # 10 000 байт порциями по 4096 → 3 запроса
    dl.post("/api/downloads", json=REQ)
    job = wait_job(dl)

    assert [(s["state"], s["received"], s["total"], s["progress"]) for s in seen] == [
        ("downloading", 0, None, 0),
        ("downloading", 4096, len(DATA), 40),
        ("downloading", 8192, len(DATA), 81),
    ]
    assert job["state"] == "done" and job["progress"] == 100
    assert job["received"] == job["total"] == len(DATA)


def test_unknown_total_size(dl):
    dl.net.unknown_total = True
    dl.post("/api/downloads", json=REQ)
    assert wait_job(dl)["state"] == "done"
    assert (dl.profile.downloads / FOLDER / "audio.m4a").read_bytes() == DATA


def test_stale_link_during_download_is_resolved_again(dl):
    dl.net.queue(CDN, dl.net.serve_file, 403)
    dl.post("/api/downloads", json=REQ)
    assert wait_job(dl)["state"] == "done"
    assert dl.resolver.calls[KEY] == 2
    assert (dl.profile.downloads / FOLDER / "audio.m4a").read_bytes() == DATA


def test_failure_mid_download_leaves_nothing(dl):
    dl.net.queue(CDN, dl.net.serve_file, 500)
    dl.post("/api/downloads", json=REQ)
    job = wait_job(dl)
    assert job["state"] == "error" and job["error"] == "Не удалось скачать трек. Попробуйте ещё раз."
    assert leftovers(dl) == []                                       # ни папки, ни .part
    assert dl.profile.sql("SELECT COUNT(*) FROM downloads") == [(0,)]


def test_upstream_break_mid_portion_resumes_without_loss(dl):
    """S8 (N2): CDN оборвал порцию посередине — полученные байты сохранены, загрузка продолжается с первого
    недостающего байта и завершается точной копией файла."""
    dl.net.chunk = 1000
    real = dl.net.serve_file

    def broken(request):                                            # 4096 байт порции, но обрыв после 2000
        dl.net.fail_after = 2
        try:
            return real(request)
        finally:
            dl.net.fail_after = None
    dl.net.queue(CDN, broken)
    dl.post("/api/downloads", json=REQ)
    job = wait_job(dl)
    assert job["state"] == "done", job
    assert (dl.profile.downloads / FOLDER / "audio.m4a").read_bytes() == DATA
    ranges = [q.headers["range"] for q in dl.net.cdn_requests(CDN)]
    assert ranges[:2] == ["bytes=0-4095", "bytes=2000-6095"]         # продолжение — с 2000-го байта


def test_repeated_breaks_without_progress_fail_cleanly(dl):
    def dead(request):
        return httpx.Response(206, headers={"Content-Range": f"bytes 0-4095/{len(DATA)}", "Content-Type": "audio/mp4"},
                              stream=_BrokenStream())
    dl.net.queue(CDN, *[dead] * (downloads.MAX_BREAKS + 1))
    dl.post("/api/downloads", json=REQ)
    job = wait_job(dl)
    assert job["state"] == "error" and job["code"] == "internal"
    assert leftovers(dl) == [] and dl.profile.sql("SELECT COUNT(*) FROM downloads") == [(0,)]
    assert len(dl.net.cdn_requests(CDN)) == downloads.MAX_BREAKS


def test_source_dropping_range_support_mid_download_is_an_error(dl):
    """Вторая порция пришла целиком (200 без Range): дописывать её в середину файла нельзя."""
    dl.net.queue(CDN, dl.net.serve_file, lambda req: httpx.Response(200, content=DATA, headers={
        "Content-Length": str(len(DATA)), "Content-Type": "audio/mp4"}))
    dl.post("/api/downloads", json=REQ)
    job = wait_job(dl)
    assert job["state"] == "error"
    assert leftovers(dl) == []


class _BrokenStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        raise httpx.ReadError("обрыв (тест)")
        yield b""                                                   # noqa: unreachable — делает метод генератором

    async def aclose(self):
        pass


def test_resolver_error_message_reaches_job(dl):
    dl.resolver.fail("yt", YT, stream.StreamUnavailable("hls only"))
    dl.post("/api/downloads", json=REQ)
    job = wait_job(dl)
    assert job["state"] == "error" and job["error"] == "Для этого трека нет прямого аудиопотока"
    assert leftovers(dl) == []


@pytest.mark.parametrize("cover", ["http://lh3.googleusercontent.com/x", "https://evil.example/cover.jpg",
                                   "https://googleusercontent.com.evil.example/x",
                                   "https://evilgoogleusercontent.com/x",       # S13: раньше проходило проверку по окончанию имени
                                   "https://user@lh3.googleusercontent.com/x", None])
def test_cover_only_from_allowed_https_hosts(dl, cover):
    dl.post("/api/downloads", json={**REQ, "cover": cover})
    assert wait_job(dl)["state"] == "done"
    assert not (dl.profile.downloads / FOLDER / "cover.jpg").exists()
    assert not [q for q in dl.net.requests if q.url.host.endswith("evil.example")]


# ---- S13: редиректы при скачивании обложки — каждый адрес цепочки проверяется заново ----
def _redirect(location: str, status: int = 302):
    return lambda request: httpx.Response(status, headers={"Location": location})


def _cover_requests(dl) -> list[str]:
    return [str(q.url) for q in dl.net.requests if q.url.host != "cdn.test" and q.url.host != "lrclib.net"]


@pytest.mark.parametrize("target", ["https://lh3.googleusercontent.com/final=w544-h544",     # тот же хост
                                    "/final=w544-h544",                                     # относительный Location
                                    "https://i1.sndcdn.com/artworks-x-t500x500.jpg",        # другой хост обложек
                                    "https://i.ytimg.com/vi/x/hqdefault.jpg"])
def test_cover_redirect_to_allowed_host_is_followed(dl, target):
    final = target if target.startswith("https://") else "https://lh3.googleusercontent.com" + target
    dl.net.images[COVER] = _redirect(target, 301)
    dl.net.images[final] = (b"\xff\xd8jpeg", "image/jpeg")
    dl.post("/api/downloads", json=REQ)
    assert wait_job(dl)["state"] == "done"
    assert (dl.profile.downloads / FOLDER / "cover.jpg").read_bytes() == b"\xff\xd8jpeg"
    assert _cover_requests(dl) == [COVER, final]


@pytest.mark.parametrize("target", [
    "https://evil.example/x.jpg",                          # посторонний хост
    "https://googleusercontent.com.evil.example/x.jpg",    # поддельный домен с именем хоста обложек
    "https://evilgoogleusercontent.com/x.jpg",
    "https://lh3.googleusercontent.com@evil.example/x.jpg",
    "http://lh3.googleusercontent.com/x.jpg",              # не https
    "https://lh3.googleusercontent.com:8443/x.jpg",
    "https://127.0.0.1/x.jpg",                             # локальные и частные адреса
    "https://localhost/x.jpg",
    "https://[::1]/x.jpg",
    "https://10.0.0.5/x.jpg",
    "https://169.254.169.254/latest/meta-data",
    "file:///etc/passwd",
])
def test_cover_redirect_to_foreign_or_local_address_is_refused(dl, target):
    dl.net.images[COVER] = _redirect(target)
    dl.post("/api/downloads", json=REQ)
    assert wait_job(dl)["state"] == "done"                 # трек сохранён, обложки нет
    assert not (dl.profile.downloads / FOLDER / "cover.jpg").exists()
    assert dl.profile.sql("SELECT cover FROM downloads") == [(None,)]
    assert _cover_requests(dl) == [COVER]                   # по адресу редиректа запроса не было
    dl.net.assert_no_unexpected()


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.0.0.5", "192.168.1.10", "169.254.169.254", "::1"])
def test_allowed_cover_host_resolving_to_local_address_is_refused(dl, ip):
    """Имя из разрешённого списка, но DNS ведёт в локальную сеть (подмена DNS): ни исходный адрес, ни редирект не запрашиваются."""
    dl.net.images[COVER] = _redirect("https://lh5.googleusercontent.com/x.jpg")
    dl.net.images["https://lh5.googleusercontent.com/x.jpg"] = (b"\xff\xd8jpeg", "image/jpeg")
    dl.dns["lh5.googleusercontent.com"] = [ip]
    dl.post("/api/downloads", json=REQ)
    assert wait_job(dl)["state"] == "done"
    assert not (dl.profile.downloads / FOLDER / "cover.jpg").exists()
    assert _cover_requests(dl) == [COVER]
    dl.dns["lh3.googleusercontent.com"] = [ip]
    dl.delete(f"/api/downloads/yt/{YT}")
    dl.net.requests.clear()
    dl.post("/api/downloads", json=REQ)
    assert wait_job(dl)["state"] == "done"
    assert _cover_requests(dl) == []


def test_cover_redirects_are_limited(dl):
    hops = [f"https://lh3.googleusercontent.com/hop{i}" for i in range(downloads.COVER_MAX_REDIRECTS + 2)]
    dl.net.images[COVER] = _redirect(hops[0])
    for a, b in zip(hops, hops[1:]):
        dl.net.images[a] = _redirect(b)
    dl.net.images[hops[-1]] = (b"\xff\xd8jpeg", "image/jpeg")
    dl.post("/api/downloads", json=REQ)
    assert wait_job(dl)["state"] == "done"
    assert not (dl.profile.downloads / FOLDER / "cover.jpg").exists()
    assert len(_cover_requests(dl)) == downloads.COVER_MAX_REDIRECTS + 1     # исходный запрос и 3 перехода


def test_cover_failure_does_not_fail_download(dl):
    dl.net.images.clear()                                            # 404 на обложку
    dl.post("/api/downloads", json=REQ)
    assert wait_job(dl)["state"] == "done"
    assert dl.profile.sql("SELECT cover FROM downloads") == [(None,)]


@pytest.mark.parametrize("lyr", [(404, {"message": "nf"}), (500, {"e": 1}), (200, {"plainLyrics": None, "syncedLyrics": None})])
def test_missing_lyrics_do_not_fail_download(dl, lyr):
    dl.net.lyrics = lyr
    dl.post("/api/downloads", json=REQ)
    assert wait_job(dl)["state"] == "done"
    assert not (dl.profile.downloads / FOLDER / "lyrics.json").exists()     # пустой файл «для вида» не создаётся
    assert dl.profile.sql("SELECT lyrics FROM downloads") == [(None,)]


def test_broken_lyrics_response_does_not_fail_download(dl):
    dl.net.lyrics = (200, "<html>oops</html>")
    dl.post("/api/downloads", json=REQ)
    assert wait_job(dl)["state"] == "done"
    assert (dl.profile.downloads / FOLDER / "audio.m4a").is_file()


def test_already_downloaded_returns_done_immediately(dl):
    dl.post("/api/downloads", json=REQ)
    wait_job(dl)
    r = dl.post("/api/downloads", json=REQ)
    assert r.status_code == 202 and r.json() == {"state": "done", "progress": 100}
    assert dl.resolver.calls[KEY] == 1


def test_repeated_request_while_active_returns_same_job(dl):
    import threading
    dl.resolver.gate = threading.Event()
    try:
        first = dl.post("/api/downloads", json=REQ).json()
        second = dl.post("/api/downloads", json=REQ).json()
        assert first["state"] == "preparing" and second["state"] in downloads.ACTIVE
        assert len(downloads.jobs) == 1
    finally:
        dl.resolver.gate.set()
    assert wait_job(dl)["state"] == "done"
    assert dl.resolver.calls[KEY] == 1


@pytest.mark.parametrize("body, status", [
    ({**REQ, "id": "short"}, 400),
    ({**REQ, "source": "sc", "id": "abc"}, 400),
    ({**REQ, "source": "spotify"}, 422),
    ({k: v for k, v in REQ.items() if k != "title"}, 422),
])
def test_validation(dl, body, status):
    assert dl.post("/api/downloads", json=body).status_code == status
    assert not downloads.jobs and dl.resolver.total() == 0


def test_finished_jobs_expire_from_list(dl):
    downloads.jobs["yt:old"] = {"state": "done", "progress": 100, "error": None, "finished_at": time.time() - 61}
    downloads.jobs["yt:new"] = {"state": "done", "progress": 100, "error": None, "finished_at": time.time()}
    assert set(dl.get("/api/downloads/jobs").json()) == {"yt:new"}


def test_delete_download(dl):
    dl.post("/api/downloads", json=REQ)
    wait_job(dl)
    assert dl.delete(f"/api/downloads/yt/{YT}").json() == {"ok": True}
    assert leftovers(dl) == []
    assert dl.get("/api/downloads").json()["items"] == []
    assert dl.delete(f"/api/downloads/yt/{YT}").status_code == 200          # повторное удаление — не ошибка
    assert dl.delete("/api/downloads/yt/bad").status_code == 400


def test_delete_failure_keeps_index(dl, monkeypatch):
    dl.post("/api/downloads", json=REQ)
    wait_job(dl)

    def locked(path, *a, **kw):
        raise PermissionError("файл занят другим процессом")

    monkeypatch.setattr(library.shutil, "rmtree", locked)
    r = dl.delete(f"/api/downloads/yt/{YT}")
    assert r.status_code == 500 and r.json()["detail"] == "Не удалось удалить локальную копию. Попробуйте ещё раз."
    assert dl.profile.sql("SELECT COUNT(*) FROM downloads") == [(1,)]


def test_local_cover_endpoint_validation(api):
    assert api.get(f"/api/local/yt/{YT}/cover").status_code == 404
    assert api.get("/api/local/yt/bad/cover").status_code == 400


def test_partial_folders_are_cleaned_on_startup(harness):
    """Приложение закрыли во время загрузки: на следующем запуске *.part удаляются, остальное не трогается."""
    (harness.profile.downloads / "X - Y [yt-AAAAAAAAAA2].part").mkdir()
    (harness.profile.downloads / "X - Y [yt-AAAAAAAAAA2].part" / "audio.part").write_bytes(b"half")
    (harness.profile.downloads / "Kept [yt-AAAAAAAAAA3]").mkdir()
    with harness.client():
        pass
    assert sorted(p.name for p in harness.profile.downloads.iterdir()) == ["Kept [yt-AAAAAAAAAA3]"]


def test_redownload_replaces_unindexed_folder(dl):
    stale = dl.profile.downloads / FOLDER
    stale.mkdir()
    (stale / "old.txt").write_text("old")
    dl.post("/api/downloads", json=REQ)
    assert wait_job(dl)["state"] == "done"
    assert not (stale / "old.txt").exists() and (stale / "audio.m4a").is_file()


@pytest.mark.parametrize("artist, title", [("../../..", "../escape"), ("..\\..\\x", "C:\\Windows\\evil"),
                                           ("/etc", "passwd"), ("..", "..")])
def test_track_names_cannot_write_outside_downloads(dl, artist, title):
    """Название и исполнитель приходят от клиента: папка загрузки всё равно создаётся внутри каталога загрузок."""
    before = sorted(p.name for p in dl.profile.root.iterdir())
    dl.post("/api/downloads", json={**REQ, "artist": artist, "title": title})
    assert wait_job(dl)["state"] == "done"
    assert sorted(p.name for p in dl.profile.root.iterdir()) == before          # рядом с профилем ничего нового
    [folder] = dl.profile.downloads.iterdir()
    assert folder.name.endswith(f"[yt-{YT}]") and not set("/\\:") & set(folder.name)
    assert dl.profile.sql("SELECT folder FROM downloads") == [(folder.name,)]


def test_existing_downloads_survive_restart(harness):
    """Скачанное в прошлом запуске остаётся в индексе и играет с диска после перезапуска приложения."""
    with harness.client() as h:
        h.net.add_file(CDN, DATA, "audio/mp4")
        h.post("/api/downloads", json=REQ)
        assert wait_job(h)["state"] == "done"
    with harness.client() as h:
        assert [t["id"] for t in h.get("/api/downloads").json()["items"]] == [YT]
        before = h.resolver.total()
        r = h.get(f"/api/stream/yt/{YT}", headers={"Range": "bytes=0-99"})
        assert r.status_code == 206 and r.content == DATA[:100]
        assert h.resolver.total() == before                                     # без обращения к источнику
