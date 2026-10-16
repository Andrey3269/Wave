"""
S11 (остаток M8): менеджер загрузок — лимит параллельности и очередь, отмена (API и реальные точки отмены в Python),
гонки отмены с фиксацией, остановка в lifespan до закрытия HTTP-клиентов, запись файлов вне event loop,
очистка `*.part` во всех проверенных каталогах загрузок при старте.

Сети нет: CDN, резолвер, тексты и обложки подменены (wavetest.fakes). Загрузки «удерживаются» на нужном этапе
событиями asyncio/threading, поэтому тесты детерминированы и не зависят от скорости машины.
"""
from __future__ import annotations

import asyncio
import os
import threading
import time
from pathlib import Path

import httpx
import pytest

import download_io
import downloads
import library
import paths
from streaming import hls_download
from wavetest.fakes import audio_bytes

DATA = audio_bytes(10_000, seed=11)
IDS = [f"AAAAAAAAAA{i}" for i in range(1, 6)]          # 11 символов — корректные id YouTube


def req(tid: str, **over) -> dict:
    body = {"source": "yt", "id": tid, "title": f"Песня {tid[-1]}", "artist": "Исполнитель", "album": None,
            "duration": 100, "cover": None}
    body.update(over)
    return body


def folder(tid: str) -> str:
    return library.safe_folder("Исполнитель", f"Песня {tid[-1]}", "yt", tid)


async def until(cond, timeout: float = 5.0, what: str = "условие"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        v = cond()
        if v:
            return v
        await asyncio.sleep(0.005)
    raise AssertionError(f"не дождались: {what}")


def job(key: str) -> dict | None:
    return downloads.jobs.get(key)


def state(tid: str) -> str | None:
    j = job(f"yt:{tid}")
    return j and j["state"]


def finished(tid: str) -> bool:
    return state(tid) not in downloads.ACTIVE and state(tid) is not None


def symlink(link: Path, target: Path, is_dir: bool = False) -> None:
    try:
        os.symlink(target, link, target_is_directory=is_dir)
    except (OSError, NotImplementedError):
        pytest.skip("символические ссылки недоступны в этой среде")


def names(p: Path) -> list[str]:
    return sorted(x.name for x in p.iterdir())


class Hold:
    """
    CDN, который отвечает на первую порцию каждого трека «удерживаемым» телом: тело отдаёт данные только после
    release(tid). Считает одновременно открытые тела (это и есть идущие загрузки) и их максимум.
    """

    def __init__(self, net, ids):
        self.net, self.open, self.peak = net, set(), 0
        self.events: dict[str, asyncio.Event] = {}
        self.streams: list = []
        for tid in ids:
            net.add_file(f"/yt/{tid}", DATA, "audio/mp4")
            net.queue(f"/yt/{tid}", self._responder(tid))

    def release(self, tid: str) -> None:
        self.events.setdefault(tid, asyncio.Event()).set()

    def release_all(self) -> None:
        for tid in list(self.events):
            self.release(tid)

    def _responder(self, tid):
        hold = self

        class Body(httpx.AsyncByteStream):
            closed = False

            async def __aiter__(self):
                hold.open.add(tid)
                hold.peak = max(hold.peak, len(hold.open))
                try:
                    await hold.events.setdefault(tid, asyncio.Event()).wait()
                    yield DATA
                finally:
                    hold.open.discard(tid)

            async def aclose(self):
                Body.closed = True
                hold.open.discard(tid)

        def respond(request):
            body = Body()
            hold.streams.append(body)
            return httpx.Response(200, headers={"Content-Type": "audio/mp4", "Content-Length": str(len(DATA))},
                                  stream=body)
        return respond


# ---------------------------------------------------------------------------
# 1–3, 6. Лимит, очередь, повторный запрос, освобождение места
# ---------------------------------------------------------------------------
def test_parallel_downloads_never_exceed_the_limit(harness):
    """Пять загрузок сразу: идут не больше MAX_PARALLEL, остальные честно «в очереди»; в итоге скачаны все."""
    assert downloads.MAX_PARALLEL == 2
    hold = Hold(harness.net, IDS)

    async def scenario(c):
        first = [(await c.post("/api/downloads", json=req(t))).json()["state"] for t in IDS]
        assert first == ["preparing", "preparing", "queued", "queued", "queued"]
        await until(lambda: len(hold.open) == 2, what="две загрузки дошли до CDN")
        await asyncio.sleep(0.05)                                  # дать «лишним» шанс стартовать, если бы могли
        assert [state(t) for t in IDS] == ["downloading", "downloading", "queued", "queued", "queued"]
        assert harness.resolver.total() == 2                       # preparing (резолв) тоже внутри лимита
        assert harness.app.state.downloads.slots.busy == 2
        jobs = (await c.get("/api/downloads/jobs")).json()
        assert sorted(j["state"] for j in jobs.values()) == ["downloading"] * 2 + ["queued"] * 3
        hold.release_all()
        for t in IDS:
            hold.release(t)                                        # и тем, кто ещё в очереди
        await until(lambda: all(finished(t) for t in IDS), what="все загрузки закончились")
        return harness.app.state.downloads.slots

    slots = harness.run(scenario)
    assert hold.peak == 2 and slots.peak == 2
    assert [state(t) for t in IDS] == ["done"] * 5
    assert names(harness.profile.downloads) == sorted(folder(t) for t in IDS)


def test_queued_download_starts_when_a_slot_frees(harness):
    hold = Hold(harness.net, IDS[:3])

    async def scenario(c):
        for t in IDS[:3]:
            await c.post("/api/downloads", json=req(t))
        await until(lambda: len(hold.open) == 2, what="две загрузки идут")
        assert state(IDS[2]) == "queued" and harness.resolver.calls[f"yt:{IDS[2]}"] == 0
        hold.release(IDS[0])                                       # первая закончилась — место у третьей
        await until(lambda: state(IDS[0]) == "done", what="первая готова")
        await until(lambda: IDS[2] in hold.open, what="третья дошла до CDN")
        assert state(IDS[1]) == "downloading" and state(IDS[2]) == "downloading"
        hold.release(IDS[1])
        hold.release(IDS[2])
        await until(lambda: finished(IDS[1]) and finished(IDS[2]))

    harness.run(scenario)
    assert [state(t) for t in IDS[:3]] == ["done"] * 3
    assert hold.peak == 2


def test_repeated_request_for_a_queued_track_returns_the_same_job(harness):
    hold = Hold(harness.net, IDS[:3])

    async def scenario(c):
        for t in IDS[:3]:
            await c.post("/api/downloads", json=req(t))
        await until(lambda: len(hold.open) == 2)
        again = (await c.post("/api/downloads", json=req(IDS[2]))).json()
        again_active = (await c.post("/api/downloads", json=req(IDS[0]))).json()
        assert again["state"] == "queued" and again_active["state"] == "downloading"
        assert len(downloads.jobs) == 3 and len(harness.app.state.downloads._entries) == 3
        for t in IDS[:3]:
            hold.release(t)
        await until(lambda: all(finished(t) for t in IDS[:3]))

    harness.run(scenario)
    assert all(harness.resolver.calls[f"yt:{t}"] == 1 for t in IDS[:3])       # по одному резолву на трек
    assert len(harness.net.cdn_requests(f"/yt/{IDS[2]}")) == 1


def test_failed_download_frees_its_slot(harness):
    """Ошибка источника на этапе preparing освобождает место: ожидающая загрузка идёт дальше."""
    import stream
    hold = Hold(harness.net, IDS[:3])
    harness.resolver.fail("yt", IDS[0], stream.StreamUnavailable("нет потока"))

    async def scenario(c):
        for t in IDS[:3]:
            await c.post("/api/downloads", json=req(t))
        await until(lambda: state(IDS[0]) == "error", what="первая — ошибка")
        await until(lambda: len(hold.open) == 2, what="вторая и третья идут")
        assert sorted(hold.open) == sorted(IDS[1:3])
        hold.release(IDS[1])
        hold.release(IDS[2])
        await until(lambda: finished(IDS[1]) and finished(IDS[2]))

    harness.run(scenario)
    assert [state(t) for t in IDS[:3]] == ["error", "done", "done"]
    assert harness.app.state.downloads.slots.busy == 0


# ---------------------------------------------------------------------------
# 4–6. Отмена на каждом этапе
# ---------------------------------------------------------------------------
def assert_clean(h, tid: str) -> None:
    """После отмены: нет ни .part, ни папки трека, ни строки индекса, ни трека в «Скачанных»."""
    assert not [p for p in h.profile.downloads.iterdir() if tid in p.name], names(h.profile.downloads)
    assert h.profile.sql("SELECT COUNT(*) FROM downloads WHERE key = ?", (f"yt:{tid}",)) == [(0,)]


def test_cancel_while_preparing(api):
    """Резолв «висит» (поток yt-dlp): отмена отвечает сразу, папок нет, общий резолв плеера не прерван."""
    tid = IDS[0]
    api.net.add_file(f"/yt/{tid}", DATA, "audio/mp4")
    api.resolver.gate = threading.Event()
    try:
        assert api.post("/api/downloads", json=req(tid)).json()["state"] == "preparing"
        deadline = time.monotonic() + 5
        while api.resolver.calls[f"yt:{tid}"] == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        r = api.delete(f"/api/downloads/jobs/yt/{tid}")
        assert r.status_code == 200
        body = r.json()
        assert body["state"] == "cancelled" and body["code"] == "cancelled" and body["error"] is None
        assert body["finished_at"]
        assert_clean(api, tid)
    finally:
        api.resolver.gate.set()
    assert api.get("/api/downloads").json()["items"] == []
    # тот же трек можно скачать снова
    api.resolver.gate = None
    assert api.post("/api/downloads", json=req(tid)).json()["state"] == "preparing"
    deadline = time.monotonic() + 5
    while state(tid) in downloads.ACTIVE and time.monotonic() < deadline:
        time.sleep(0.01)
    assert state(tid) == "done"
    assert (api.profile.downloads / folder(tid) / "audio.m4a").read_bytes() == DATA


def test_cancel_while_receiving_audio_closes_the_upstream_and_removes_part(harness):
    tid = IDS[0]
    hold = Hold(harness.net, [tid])

    async def scenario(c):
        await c.post("/api/downloads", json=req(tid))
        await until(lambda: tid in hold.open, what="загрузка идёт")
        part = harness.profile.downloads / (folder(tid) + ".part")
        assert part.is_dir() and (part / "audio.part").exists()     # временная папка есть, пока идёт загрузка
        body = (await c.delete(f"/api/downloads/jobs/yt/{tid}")).json()
        assert body["state"] == "cancelled"
        assert not part.exists()                                     # к ответу на отмену .part уже удалён
        assert harness.app.state.downloads.slots.busy == 0
        assert all(s.closed for s in hold.streams)                   # соединение с CDN закрыто

    harness.run(scenario)
    assert_clean(harness, tid)


def test_cancel_during_processing(harness, monkeypatch):
    """Аудио уже скачано (audio.m4a в .part), идёт обработка: отмена не оставляет ни файла, ни строки индекса."""
    tid = IDS[0]
    harness.net.add_file(f"/yt/{tid}", DATA, "audio/mp4")
    entered = {}

    async def slow_lyrics(client, r):
        entered["yes"] = True
        await asyncio.Event().wait()

    monkeypatch.setattr(downloads, "_fetch_lyrics_safe", slow_lyrics)

    async def scenario(c):
        await c.post("/api/downloads", json=req(tid))
        await until(lambda: entered.get("yes"), what="этап обработки")
        assert state(tid) == "processing"
        part = harness.profile.downloads / (folder(tid) + ".part")
        assert (part / "audio.m4a").read_bytes() == DATA
        body = (await c.delete(f"/api/downloads/jobs/yt/{tid}")).json()
        assert body["state"] == "cancelled" and not part.exists()
        lst = (await c.get("/api/downloads")).json()["items"]
        assert lst == []

    harness.run(scenario)
    assert_clean(harness, tid)


def test_cancel_of_a_queued_download_never_starts_it(harness):
    hold = Hold(harness.net, IDS[:3])

    async def scenario(c):
        for t in IDS[:3]:
            await c.post("/api/downloads", json=req(t))
        await until(lambda: len(hold.open) == 2)
        body = (await c.delete(f"/api/downloads/jobs/yt/{IDS[2]}")).json()
        assert body["state"] == "cancelled" and body["code"] == "cancelled"
        assert harness.app.state.downloads.slots.busy == 2           # чужие места не тронуты
        hold.release(IDS[0])
        hold.release(IDS[1])
        await until(lambda: finished(IDS[0]) and finished(IDS[1]))

    harness.run(scenario)
    assert harness.resolver.calls[f"yt:{IDS[2]}"] == 0 and not harness.net.cdn_requests(f"/yt/{IDS[2]}")
    assert [state(t) for t in IDS[:3]] == ["done", "done", "cancelled"]
    assert_clean(harness, IDS[2])


def test_cancelling_a_hung_download_frees_the_slot_for_the_queue(harness):
    """Зависшая загрузка не держит место навсегда: после её отмены ожидающая начинается."""
    hold = Hold(harness.net, IDS[:3])

    async def scenario(c):
        for t in IDS[:3]:
            await c.post("/api/downloads", json=req(t))
        await until(lambda: len(hold.open) == 2)
        await c.delete(f"/api/downloads/jobs/yt/{IDS[0]}")
        await until(lambda: IDS[2] in hold.open, what="ожидающая пошла после отмены")
        assert harness.app.state.downloads.slots.busy == 2
        hold.release(IDS[1])
        hold.release(IDS[2])
        await until(lambda: finished(IDS[1]) and finished(IDS[2]))

    harness.run(scenario)
    assert [state(t) for t in IDS[:3]] == ["cancelled", "done", "done"]
    assert hold.peak == 2


def test_cancelled_track_can_be_downloaded_again(harness):
    tid = IDS[0]
    hold = Hold(harness.net, [tid])

    async def scenario(c):
        await c.post("/api/downloads", json=req(tid))
        await until(lambda: tid in hold.open)
        await c.delete(f"/api/downloads/jobs/yt/{tid}")
        again = (await c.post("/api/downloads", json=req(tid))).json()     # очередь CDN пуста: обычный файл
        assert again["state"] == "preparing"
        await until(lambda: finished(tid))

    harness.run(scenario)
    assert state(tid) == "done"
    assert (harness.profile.downloads / folder(tid) / "audio.m4a").read_bytes() == DATA
    assert harness.profile.sql("SELECT COUNT(*) FROM downloads") == [(1,)]


def test_cancel_api_contract(api):
    """Неизвестная задача — 404 с кодом; плохой id — 400; повторная отмена и отмена готовой — тот же итог."""
    tid = IDS[0]
    r = api.delete(f"/api/downloads/jobs/yt/{tid}")
    assert r.status_code == 404 and r.headers["X-Wave-Error"] == "no_job" and r.json()["detail"]
    assert api.delete("/api/downloads/jobs/yt/bad").status_code == 400
    assert api.delete("/api/downloads/jobs/spotify/AAAAAAAAAA1").status_code == 400

    api.net.add_file(f"/yt/{tid}", DATA, "audio/mp4")
    api.post("/api/downloads", json=req(tid))
    deadline = time.monotonic() + 5
    while state(tid) in downloads.ACTIVE and time.monotonic() < deadline:
        time.sleep(0.01)
    done = api.delete(f"/api/downloads/jobs/yt/{tid}")
    assert done.status_code == 200 and done.json()["state"] == "done"            # отмена готовой ничего не трогает
    assert (api.profile.downloads / folder(tid) / "audio.m4a").is_file()
    assert api.delete(f"/api/downloads/yt/{tid}").json() == {"ok": True}       # удаление копии — прежний маршрут


def test_repeated_cancel_returns_the_same_result(harness):
    tid = IDS[0]
    hold = Hold(harness.net, [tid])

    async def scenario(c):
        await c.post("/api/downloads", json=req(tid))
        await until(lambda: tid in hold.open)
        a, b = await asyncio.gather(c.delete(f"/api/downloads/jobs/yt/{tid}"),
                                    c.delete(f"/api/downloads/jobs/yt/{tid}"))
        third = await c.delete(f"/api/downloads/jobs/yt/{tid}")
        return [r.json()["state"] for r in (a, b, third)]

    assert harness.run(scenario) == ["cancelled"] * 3
    assert_clean(harness, tid)


def test_cancel_of_a_failed_download_reports_the_error(api):
    import stream
    tid = IDS[0]
    api.resolver.fail("yt", tid, stream.StreamUnavailable("нет потока"))
    api.post("/api/downloads", json=req(tid))
    deadline = time.monotonic() + 5
    while state(tid) in downloads.ACTIVE and time.monotonic() < deadline:
        time.sleep(0.01)
    body = api.delete(f"/api/downloads/jobs/yt/{tid}").json()
    assert body["state"] == "error" and body["error"] == "Для этого трека нет прямого аудиопотока"


# ---------------------------------------------------------------------------
# 7. Гонка «отмена против фиксации»
# ---------------------------------------------------------------------------
def test_cancel_during_commit_ends_as_done(harness, monkeypatch):
    """Отмена пришла, когда папка уже переносится на место и пишется индекс: итог — done, трек целиком сохранён."""
    tid = IDS[0]
    harness.net.add_file(f"/yt/{tid}", DATA, "audio/mp4")
    entered, release = threading.Event(), threading.Event()
    real = downloads._Commit.run

    def slow_commit(self):
        entered.set()
        assert release.wait(10)
        real(self)

    monkeypatch.setattr(downloads._Commit, "run", slow_commit)

    async def scenario(c):
        await c.post("/api/downloads", json=req(tid))
        await until(entered.is_set, what="фиксация началась")
        cancel = asyncio.create_task(c.delete(f"/api/downloads/jobs/yt/{tid}"))
        await asyncio.sleep(0.05)
        assert not cancel.done()                                     # отмена ждёт конца фиксации
        release.set()
        return (await cancel).json()

    body = harness.run(scenario)
    assert body["state"] == "done" and body["progress"] == 100
    assert (harness.profile.downloads / folder(tid) / "audio.m4a").read_bytes() == DATA
    assert names(harness.profile.downloads) == [folder(tid)]                     # .part нет
    assert harness.profile.sql("SELECT key FROM downloads") == [(f"yt:{tid}",)]


def test_cancel_just_before_commit_leaves_nothing(harness, monkeypatch):
    """Отмена во время записи metadata.json (последний шаг перед фиксацией): итог cancelled, ничего не сохранено."""
    tid = IDS[0]
    harness.net.add_file(f"/yt/{tid}", DATA, "audio/mp4")
    entered, release = threading.Event(), threading.Event()
    real = downloads._write_text

    def slow_meta(path, text):
        if path.name == "metadata.json":
            entered.set()
            assert release.wait(10)
        real(path, text)

    monkeypatch.setattr(downloads, "_write_text", slow_meta)

    async def scenario(c):
        await c.post("/api/downloads", json=req(tid))
        await until(entered.is_set)
        cancel = asyncio.create_task(c.delete(f"/api/downloads/jobs/yt/{tid}"))
        await asyncio.sleep(0.05)
        assert not cancel.done()                                     # запись в потоке ещё идёт — ждём её
        release.set()
        return (await cancel).json()

    assert harness.run(scenario)["state"] == "cancelled"
    assert_clean(harness, tid)


# ---------------------------------------------------------------------------
# 9–10. Запись вне event loop, отмена посреди записи
# ---------------------------------------------------------------------------
class SpyOpen:
    """Подменяет open() в модулях загрузок: запоминает, в каком потоке шла каждая запись аудио."""

    def __init__(self):
        self.threads: list[int] = []
        self.on_write = None

    def __call__(self, path, mode="r", *a, **kw):
        f = open(path, mode, *a, **kw)
        if "w" not in mode or Path(path).name != "audio.part":
            return f
        spy = self

        class Spy:
            def write(self, data):
                spy.threads.append(threading.get_ident())
                if spy.on_write:
                    spy.on_write()
                return f.write(data)

            def close(self):
                f.close()

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                f.close()

        return Spy()


@pytest.fixture
def spy_open(monkeypatch):
    spy = SpyOpen()
    for mod in (downloads, hls_download, download_io):
        monkeypatch.setattr(mod, "open", spy, raising=False)
    return spy


def test_audio_is_written_outside_the_event_loop_thread(harness, spy_open):
    tid = IDS[0]
    harness.net.add_file(f"/yt/{tid}", DATA, "audio/mp4")

    async def scenario(c):
        await c.post("/api/downloads", json=req(tid))
        await until(lambda: finished(tid))
        return threading.get_ident()

    loop_thread = harness.run(scenario)
    assert state(tid) == "done"
    assert spy_open.threads and loop_thread not in spy_open.threads


def test_event_loop_keeps_serving_while_a_write_is_blocked(harness, spy_open):
    """Пока запись на диск «висит» (медленный диск), backend отвечает на запросы (event loop свободен)."""
    tid = IDS[0]
    harness.net.add_file(f"/yt/{tid}", DATA, "audio/mp4")
    entered, release, answered = threading.Event(), threading.Event(), {}

    def first_write_blocks():
        if not entered.is_set():
            entered.set()
            answered["released_in_time"] = release.wait(5)      # True — отпустил тест, т.е. loop работал

    spy_open.on_write = first_write_blocks

    async def scenario(c):
        await c.post("/api/downloads", json=req(tid))
        await until(entered.is_set, what="запись началась")
        assert (await c.get("/api/health")).status_code == 200
        release.set()
        await until(lambda: finished(tid))

    harness.run(scenario)
    assert answered == {"released_in_time": True} and state(tid) == "done"


BIG = audio_bytes(60_000, seed=3)


def small_batches(monkeypatch, harness):
    """Пачки записи по 1000 байт, порции CDN по 4096: на BIG приходится больше 60 записей — есть что «не записать»."""
    monkeypatch.setattr(downloads, "WRITE_BATCH", 1000)
    monkeypatch.setattr(downloads, "DL_CHUNK", 4096)
    harness.net.chunk = 500


def test_cancel_waits_for_a_running_write_and_no_write_follows(harness, spy_open, monkeypatch):
    """Отмена посреди записи в потоке: .part удаляется только после конца записи и не возникает заново."""
    tid = IDS[0]
    small_batches(monkeypatch, harness)
    harness.net.add_file(f"/yt/{tid}", BIG, "audio/mp4")
    entered, release = threading.Event(), threading.Event()

    def block_first():
        if not entered.is_set():
            entered.set()
            release.wait(10)

    spy_open.on_write = block_first

    async def scenario(c):
        await c.post("/api/downloads", json=req(tid))
        await until(entered.is_set)
        part = harness.profile.downloads / (folder(tid) + ".part")
        cancel = asyncio.create_task(c.delete(f"/api/downloads/jobs/yt/{tid}"))
        await asyncio.sleep(0.05)
        assert not cancel.done() and part.is_dir()                  # запись идёт: папку удалять нельзя
        assert state(tid) == "downloading"
        release.set()
        body = (await cancel).json()
        writes = len(spy_open.threads)
        await asyncio.sleep(0.05)
        return body, writes, part

    body, writes, part = harness.run(scenario)
    assert body["state"] == "cancelled"
    assert writes == len(spy_open.threads) == 1                     # после отмены не было ни одной новой записи
    assert not part.exists()
    assert_clean(harness, tid)


# ---------------------------------------------------------------------------
# 8. Завершение приложения
# ---------------------------------------------------------------------------
def test_lifespan_cancels_downloads_before_closing_http_clients(harness, monkeypatch):
    import main
    from streaming import netguard
    hold = Hold(harness.net, IDS[:3])
    seen: dict[str, list] = {"http": [], "hls": []}

    def tracking(name, factory):
        def make():
            client = factory()
            real_close = client.aclose

            async def aclose():
                seen[name].append({t: state(t) for t in IDS[:3]})
                seen[name].append(sorted(p.name for p in harness.profile.downloads.iterdir()))
                await real_close()
            client.aclose = aclose
            return client
        return make

    monkeypatch.setattr(main, "make_http_client", tracking("http", harness.net.client))
    monkeypatch.setattr(netguard, "make_client", tracking("hls", harness.net.client))

    async def scenario(c):
        for t in IDS[:3]:
            await c.post("/api/downloads", json=req(t))
        await until(lambda: len(hold.open) == 2)
        return harness.app.state.downloads

    mgr = harness.run(scenario)                                      # выход из lifespan при живых загрузках
    for name in ("http", "hls"):
        states, files = seen[name]
        assert states == {t: "cancelled" for t in IDS[:3]}, name     # к закрытию клиентов загрузки уже остановлены
        assert files == [], name                                     # и их .part удалены
    assert all(downloads.jobs[f"yt:{t}"]["code"] == "shutdown" for t in IDS[:3])
    assert mgr.closing and mgr.slots.busy == 0 and not mgr._entries
    assert all(s.closed for s in hold.streams)


def test_no_new_downloads_while_shutting_down(harness):
    async def scenario(c):
        harness.app.state.downloads.closing = True
        return await c.post("/api/downloads", json=req(IDS[0]))

    r = harness.run(scenario)
    assert r.status_code == 503 and r.headers["X-Wave-Error"] == "shutting_down"
    assert job(f"yt:{IDS[0]}") is None and harness.resolver.total() == 0


def test_shutdown_mid_write_waits_for_the_write(harness, spy_open, monkeypatch):
    """Остановка во время записи в потоке: lifespan ждёт конца записи, затем удаляет .part; после — ни одной записи."""
    tid = IDS[0]
    small_batches(monkeypatch, harness)
    harness.net.add_file(f"/yt/{tid}", BIG, "audio/mp4")
    entered = threading.Event()

    def slow_first():
        if not entered.is_set():
            entered.set()
            time.sleep(0.3)

    spy_open.on_write = slow_first

    async def scenario(c):
        await c.post("/api/downloads", json=req(tid))
        await until(entered.is_set)

    harness.run(scenario)
    writes = len(spy_open.threads)
    time.sleep(0.4)
    assert writes == len(spy_open.threads) == 1
    assert state(tid) == "cancelled" and job(f"yt:{tid}")["code"] == "shutdown"
    assert names(harness.profile.downloads) == []


def test_restart_after_shutdown_mid_download_is_clean(harness):
    """Закрыли посреди загрузки — следующий запуск: недокачанного нет, готовые загрузки на месте, трек качается."""
    with harness.client() as h:
        h.net.add_file(f"/yt/{IDS[1]}", DATA, "audio/mp4")
        h.post("/api/downloads", json=req(IDS[1]))
        deadline = time.monotonic() + 5
        while state(IDS[1]) in downloads.ACTIVE and time.monotonic() < deadline:
            time.sleep(0.01)
    hold = Hold(harness.net, [IDS[0]])

    async def scenario(c):
        await c.post("/api/downloads", json=req(IDS[0]))
        await until(lambda: IDS[0] in hold.open)

    harness.run(scenario)
    with harness.client() as h:
        items = h.get("/api/downloads").json()["items"]
        assert [t["id"] for t in items] == [IDS[1]]                  # недокачанный не считается скачанным
        assert names(h.profile.downloads) == [folder(IDS[1])]
        h.post("/api/downloads", json=req(IDS[0]))
        deadline = time.monotonic() + 5
        while state(IDS[0]) in downloads.ACTIVE and time.monotonic() < deadline:
            time.sleep(0.01)
        assert state(IDS[0]) == "done"


# ---------------------------------------------------------------------------
# HLS: отмена и запись тем же путём
# ---------------------------------------------------------------------------
def test_hls_download_writes_off_loop_and_cancels_cleanly(harness, spy_open, monkeypatch):
    """HLS-only трек SoundCloud: сегменты пишутся вне event loop; отмена между сегментами удаляет .part."""
    from wavetest.hlsfix import MP3_FRAME_SEC, media_playlist, mp3_frames
    sc = "1000000001"
    segs = [mp3_frames(40), mp3_frames(35), mp3_frames(20), mp3_frames(20)]
    names_ = [f"seg{i}.mp3" for i in range(len(segs))]
    for name, data in zip(names_, segs):
        harness.net.add_file(f"/sc/{sc}/{name}", data, "audio/mpeg")
    durs = [round(len(d) / 417 * MP3_FRAME_SEC, 6) for d in segs]
    harness.net.add_file(f"/sc/{sc}/main.m3u8", media_playlist(names_, durs).encode(), "application/vnd.apple.mpegurl")
    harness.resolver.hls[f"sc:{sc}"] = "mp3"
    entered = threading.Event()
    real_fetch = hls_download._fetch
    calls = {"n": 0}

    async def gated_fetch(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 3:                                          # третий сегмент «висит»
            entered.set()
            await asyncio.Event().wait()
        return await real_fetch(*a, **kw)

    monkeypatch.setattr(hls_download, "_fetch", gated_fetch)

    async def scenario(c):
        await c.post("/api/downloads", json={"source": "sc", "id": sc, "title": "Волна", "artist": "Автор"})
        await until(entered.is_set)
        assert job(f"sc:{sc}")["received"] > 0                       # два сегмента уже записаны
        body = (await c.delete(f"/api/downloads/jobs/sc/{sc}")).json()
        return threading.get_ident(), body

    loop_thread, body = harness.run(scenario)
    assert body["state"] == "cancelled"
    assert len(spy_open.threads) == 2 and loop_thread not in spy_open.threads
    assert names(harness.profile.downloads) == []
    assert harness.profile.sql("SELECT COUNT(*) FROM downloads") == [(0,)]


# ---------------------------------------------------------------------------
# 11–12. Очистка .part при старте во всех проверенных каталогах
# ---------------------------------------------------------------------------
def make_download(root: Path, tid: str, title: str = "Песня") -> str:
    """Готовая загрузка Wave в root: папка с аудио + строка индекса (как после настоящей загрузки)."""
    name = library.safe_folder("Исполнитель", title, "yt", tid)
    (root / name).mkdir(parents=True)
    (root / name / "audio.m4a").write_bytes(b"audio")
    (root / name / "metadata.json").write_text("{}", encoding="utf-8")
    library.add(source="yt", track_id=tid, title=title, artist="Исполнитель", album=None, duration=1, folder=name,
                audio="audio.m4a", cover=None, lyrics=None, size=5, created_at=time.time(), root=str(root))
    return name


def make_part(root: Path, name: str, files=("audio.part",)) -> Path:
    p = root / name
    p.mkdir(parents=True)
    for f in files:
        (p / f).write_bytes(b"half")
    return p


def test_startup_cleanup_covers_every_verified_root(harness, tmp_path, monkeypatch):
    other = tmp_path / "Старая папка загрузок" / "Музыка Wave"              # кириллица в пути
    default = tmp_path / "home-downloads" / "Wave"
    default.mkdir(parents=True)
    monkeypatch.setattr(paths, "default_downloads_dir", lambda: default, raising=False)
    # обычный запуск: папка загрузок выбрана в настройках, WAVE_DOWNLOADS_DIR не задан
    monkeypatch.delenv("WAVE_DOWNLOADS_DIR")
    paths.set_downloads_dir(harness.profile.downloads)
    with harness.client():
        kept_current = make_download(harness.profile.downloads, IDS[0], "Текущая")
        kept_other = make_download(other, IDS[1], "Прошлая")
    cur = harness.profile.downloads
    parts = [
        make_part(cur, "Исполнитель - Обрыв [yt-AAAAAAAAAA7].part"),
        make_part(other, "Исполнитель - Обрыв [yt-AAAAAAAAAA8].part", files=("audio.part", "cover.jpg", "metadata.json")),
        make_part(default, "Исполнитель - По умолчанию [sc-123].part"),
        make_part(cur, "Длинное имя исполнителя Ёжик - " + "Очень длинное название " * 3 + "[yt-AAAAAAAAAA9].part"),
    ]
    # не наше: имя не по шаблону загрузчика, файл с суффиксом .part, обычная папка
    (cur / "notes.part").mkdir()
    (cur / "Чужая папка.part").mkdir()
    (cur / "Исполнитель - Файл [yt-AAAAAAAAAA6].part").write_bytes(b"file, not folder")
    (cur / "Мои заметки").mkdir()
    with harness.client():
        pass
    assert not any(p.exists() for p in parts)
    assert sorted(names(cur)) == sorted([kept_current, "notes.part", "Чужая папка.part",
                                         "Исполнитель - Файл [yt-AAAAAAAAAA6].part", "Мои заметки"])
    assert names(other) == [kept_other] and names(default) == []
    assert (other / kept_other / "audio.m4a").read_bytes() == b"audio"
    with harness.client() as h:                                                   # повторный старт — без изменений
        assert sorted(t["id"] for t in h.get("/api/downloads").json()["items"]) == sorted(IDS[:2])
    assert names(other) == [kept_other]


def test_startup_cleanup_ignores_roots_from_untrusted_index_rows(harness, tmp_path):
    """Строки индекса с выходом за каталог, с чужой папкой или без аудиофайла каталогами загрузок не становятся."""
    victim = tmp_path / "victim"
    victim.mkdir()
    stranger = make_part(victim, "Исполнитель - Чужое [yt-AAAAAAAAAA7].part")          # «.part» в чужом каталоге
    (victim / "Исполнитель - Пусто [yt-AAAAAAAAAA1]").mkdir()                       # папка без аудиофайла
    (victim / "Не Wave").mkdir()
    (victim / "Не Wave" / "audio.m4a").write_bytes(b"x")
    rows = [
        ("AAAAAAAAAA1", "Исполнитель - Пусто [yt-AAAAAAAAAA1]", str(victim)),       # аудио нет
        ("AAAAAAAAAA2", "Не Wave", str(victim)),                                    # папку создал не Wave
        ("AAAAAAAAAA3", "../victim/x [yt-AAAAAAAAAA3]", str(harness.profile.downloads)),   # выход наружу
        ("AAAAAAAAAA4", "x [yt-AAAAAAAAAA4]", "relative/root"),                     # относительный каталог
        ("AAAAAAAAAA5", "x [yt-AAAAAAAAAA5]", str(victim / "нет такого диска")),    # недоступный каталог
        ("AAAAAAAAAA6", "Без метаданных [yt-AAAAAAAAAA6]", str(victim)),            # нет metadata.json
    ]
    (victim / "Без метаданных [yt-AAAAAAAAAA6]").mkdir()
    (victim / "Без метаданных [yt-AAAAAAAAAA6]" / "audio.m4a").write_bytes(b"x")
    # Находка ревью: строка с выдуманным источником и id, совпадающими с обычной папкой пользователя
    (victim / "Фото [2020-лето]").mkdir()
    (victim / "Фото [2020-лето]" / "audio.m4a").write_bytes(b"jpeg")
    (victim / "Фото [2020-лето]" / "metadata.json").write_text("{}")
    archive = make_part(victim, "Архив [yt-backup].part", files=("важное.docx",))     # не id YouTube — не наше имя
    with harness.client():
        for tid, name, root in rows:
            library.add(source="yt", track_id=tid, title="t", artist="a", album=None, duration=1, folder=name,
                        audio="audio.m4a", cover=None, lyrics=None, size=1, created_at=time.time(), root=root)
        library.add(source="2020", track_id="лето", title="t", artist="a", album=None, duration=1,
                    folder="Фото [2020-лето]", audio="audio.m4a", cover=None, lyrics=None, size=1,
                    created_at=time.time(), root=str(victim))
        assert library.verified_roots(downloads._valid_track) == []   # ни одна строка не подтверждает каталог
    with harness.client() as h:
        assert h.get("/api/health").status_code == 200
    assert stranger.is_dir() and (stranger / "audio.part").read_bytes() == b"half"
    assert (archive / "важное.docx").read_bytes() == b"half"
    assert sorted(names(victim)) == sorted(["Исполнитель - Чужое [yt-AAAAAAAAAA7].part", "Архив [yt-backup].part",
                                            "Исполнитель - Пусто [yt-AAAAAAAAAA1]", "Не Wave", "Фото [2020-лето]",
                                            "Без метаданных [yt-AAAAAAAAAA6]"])


def test_part_names_must_carry_a_valid_source_id():
    ok = ["A - B [yt-AAAAAAAAAA1].part", "A - B [sc-123456789].part", "Ёж - Ж [yt-a_b-C_d-E_f].part"]
    bad = ["Архив [yt-backup].part", "A - B [yt-AAAAAAAAAA12].part", "A - B [sc-12a].part", "A - B [xx-1].part",
           "A - B [yt-AAAAAAAAAA1]", "notes.part", "A - B [sc-1234567890123456].part"]
    assert all(downloads.PART_RE.fullmatch(n) for n in ok)
    assert not any(downloads.PART_RE.fullmatch(n) for n in bad)


def test_default_folder_is_left_alone_with_an_explicit_downloads_dir(harness, tmp_path, monkeypatch):
    """С WAVE_DOWNLOADS_DIR (тесты, e2e, отладка) настоящий ~/Downloads/Wave не трогается (находка ревью)."""
    default = tmp_path / "home-downloads" / "Wave"
    default.mkdir(parents=True)
    monkeypatch.setattr(paths, "default_downloads_dir", lambda: default)
    real = make_part(default, "Исполнитель - Идёт в настоящей Wave [yt-AAAAAAAAAA1].part")
    with harness.client():
        pass
    assert (real / "audio.part").read_bytes() == b"half"


def test_startup_cleanup_does_not_follow_links(harness, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "precious.txt").write_text("не трогать")
    cur = harness.profile.downloads
    link = cur / "Исполнитель - Ссылка [yt-AAAAAAAAAA1].part"
    symlink(link, outside, is_dir=True)                                             # .part — ссылка наружу
    inner = make_part(cur, "Исполнитель - Внутри [yt-AAAAAAAAAA2].part")
    symlink(inner / "audio.part.link", outside / "precious.txt")                     # ссылка внутри .part
    (inner / "подкаталог").mkdir()                                                  # загрузчик каталогов не делает
    (inner / "подкаталог" / "file").write_text("x")
    with harness.client():
        pass
    assert (outside / "precious.txt").read_text() == "не трогать"
    assert link.is_symlink()                                                        # ссылку не трогаем вовсе
    assert inner.is_dir() and names(inner) == ["подкаталог"]                       # файлы и ссылка удалены, каталог — нет
    assert (inner / "подкаталог" / "file").exists()


@pytest.mark.skipif(os.name != "nt", reason="junction есть только в Windows")
def test_startup_cleanup_does_not_follow_windows_junctions(harness, tmp_path):
    """Windows: .part-junction наружу (is_symlink() её не видит) не обходится и не удаляется вместе с целью."""
    import _winapi
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "precious.txt").write_text("не трогать")
    junction = harness.profile.downloads / "Исполнитель - Junction [yt-AAAAAAAAAA1].part"
    _winapi.CreateJunction(str(outside), str(junction))
    inner = make_part(harness.profile.downloads, "Исполнитель - Внутри [yt-AAAAAAAAAA2].part")
    _winapi.CreateJunction(str(outside), str(inner / "dir-junction"))
    with harness.client():
        pass
    assert (outside / "precious.txt").read_text() == "не трогать"
    assert junction.exists() and not inner.exists()             # внутренняя junction снята как ссылка


def test_cleanup_survives_unavailable_roots(harness, tmp_path, monkeypatch):
    other = tmp_path / "other"
    with harness.client():
        make_download(other, IDS[1])
    part = make_part(other, "Исполнитель - Обрыв [yt-AAAAAAAAAA8].part")
    real_scandir = os.scandir

    def flaky_scandir(p="."):
        if isinstance(p, (str, os.PathLike)) and Path(p) == harness.profile.downloads:   # fd каталога — как есть
            raise PermissionError("нет доступа (тест)")
        return real_scandir(p)

    monkeypatch.setattr(downloads.os, "scandir", flaky_scandir)
    with harness.client() as h:                                    # текущий каталог недоступен — старт не падает
        assert h.get("/api/health").status_code == 200
    assert not part.exists()                                       # другой каталог всё равно очищен

    monkeypatch.setattr(downloads.os, "scandir", real_scandir)

    def broken_dir():
        raise OSError("диск отключён (тест)")

    def broken_index():
        raise RuntimeError("база недоступна (тест)")

    part2 = make_part(other, "Исполнитель - Ещё [yt-AAAAAAAAAA9].part")
    monkeypatch.setattr(downloads.paths, "downloads_dir", broken_dir)
    downloads.cleanup_partial()                                    # не бросает
    assert not part2.exists()
    monkeypatch.setattr(downloads.library, "verified_roots", broken_index)
    downloads.cleanup_partial()                                    # не бросает и без базы


def test_cleanup_handles_a_very_long_root_path(harness, tmp_path, monkeypatch):
    deep = tmp_path
    for i in range(6):
        deep = deep / (f"Папка уровня {i} " + "ж" * 30)
    try:
        deep.mkdir(parents=True)
        (deep / "probe").write_bytes(b"")
    except OSError:
        pytest.skip("длинные пути недоступны в этой среде (Windows без LongPathsEnabled)")
    (deep / "probe").unlink()
    assert len(str(deep)) > 260
    monkeypatch.setenv("WAVE_DOWNLOADS_DIR", str(deep))
    part = make_part(deep, "Исполнитель - Длинный путь [yt-AAAAAAAAAA1].part")
    with harness.client():
        pass
    assert not part.exists() and deep.is_dir()


# ---------------------------------------------------------------------------
# Находки независимого ревью (S11)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("batch, fail_after", [(700, 3), (1000, 5), (4096, 2), (1500, 1)])
def test_batched_writes_keep_every_byte_across_cdn_breaks(harness, monkeypatch, batch, fail_after):
    """Запись пачками (WRITE_BATCH) и обрывы CDN посреди порции: файл — точная копия, без потерь и дублей (N2)."""
    tid = IDS[0]
    monkeypatch.setattr(downloads, "WRITE_BATCH", batch)
    monkeypatch.setattr(downloads, "DL_CHUNK", 4096)
    harness.net.chunk = 500
    harness.net.add_file(f"/yt/{tid}", BIG, "audio/mp4")
    real = harness.net.serve_file

    def broken(request):
        harness.net.fail_after = fail_after
        try:
            return real(request)
        finally:
            harness.net.fail_after = None
    harness.net.queue(f"/yt/{tid}", real, broken, real, broken)

    async def scenario(c):
        await c.post("/api/downloads", json=req(tid))
        await until(lambda: finished(tid))

    harness.run(scenario)
    assert state(tid) == "done"
    assert (harness.profile.downloads / folder(tid) / "audio.m4a").read_bytes() == BIG


def test_jobs_left_active_by_a_crashed_run_are_marked_interrupted(harness):
    """Сторож перезапустил backend в том же процессе без штатной остановки: «активные» задачи прошлого запуска
    не выглядят идущими — ни в списке, ни в ответе на отмену (иначе интерфейс опрашивал бы их вечно)."""
    downloads.jobs[f"yt:{IDS[0]}"] = {"state": "downloading", "progress": 40, "error": None, "code": None,
                                      "received": 4, "total": 10}

    async def scenario(c):
        listed = (await c.get("/api/downloads/jobs")).json()[f"yt:{IDS[0]}"]
        downloads.jobs[f"yt:{IDS[1]}"] = {"state": "queued", "progress": 0, "error": None, "code": None}
        cancelled = (await c.delete(f"/api/downloads/jobs/yt/{IDS[1]}")).json()
        return listed, cancelled

    listed, cancelled = harness.run(scenario)
    assert listed["state"] == "cancelled" and listed["code"] == "shutdown" and listed["finished_at"]
    assert cancelled["state"] == "cancelled" and cancelled["code"] == "shutdown"


def test_slow_startup_cleanup_does_not_block_start_or_touch_live_downloads(harness, monkeypatch):
    """Недоступный сетевой каталог: старт ждёт очистку не дольше CLEANUP_STARTUP_WAIT, интерфейс отвечает, а
    оставшаяся в фоне очистка не удаляет папку загрузки, начатой за это время."""
    tid = IDS[0]
    monkeypatch.setattr(downloads, "CLEANUP_STARTUP_WAIT", 0.2)
    stuck, release = threading.Event(), threading.Event()
    real_cleanup = downloads.cleanup_partial

    def slow_cleanup():
        stuck.set()
        release.wait(10)                                          # «сетевой диск не отвечает»
        real_cleanup()

    monkeypatch.setattr(downloads, "cleanup_partial", slow_cleanup)
    hold = Hold(harness.net, [tid])

    async def scenario(c):
        t0 = time.monotonic()
        assert (await c.get("/api/health")).status_code == 200
        await c.post("/api/downloads", json=req(tid))
        await until(lambda: tid in hold.open)
        part = harness.profile.downloads / (folder(tid) + ".part")
        release.set()                                             # очистка доходит до каталога с живой загрузкой
        await asyncio.sleep(0.2)
        assert part.is_dir() and (part / "audio.part").exists()  # папку идущей загрузки очистка не тронула
        hold.release(tid)
        await until(lambda: finished(tid))
        return t0

    harness.run(scenario)
    assert stuck.is_set() and state(tid) == "done"


def test_cleanup_skips_parts_of_live_downloads_and_waits_for_cleaning(harness):
    part = make_part(harness.profile.downloads, "Исполнитель - Живая [yt-AAAAAAAAAA1].part")
    key = downloads._part_key(part)
    with downloads._parts_lock:
        downloads._live_parts.add(key)
    try:
        downloads.cleanup_partial()
        assert part.is_dir()
    finally:
        with downloads._parts_lock:
            downloads._live_parts.discard(key)
    downloads.cleanup_partial()
    assert not part.exists()
