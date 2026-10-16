"""
S11 (остаток M8): менеджер загрузок — лимит параллельности и очередь, отмена (API и реальные точки отмены в Python),
гонки отмены с фиксацией, остановка в lifespan до закрытия HTTP-клиентов, запись файлов вне event loop.

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


def test_cancel_waits_for_a_running_write_and_no_write_follows(harness, spy_open):
    """Отмена посреди записи в потоке: .part удаляется только после конца записи и не возникает заново."""
    tid = IDS[0]
    harness.net.add_file(f"/yt/{tid}", DATA, "audio/mp4")
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


def test_shutdown_mid_write_waits_for_the_write(harness, spy_open):
    """Остановка во время записи в потоке: lifespan ждёт конца записи, затем удаляет .part; после — ни одной записи."""
    tid = IDS[0]
    harness.net.add_file(f"/yt/{tid}", DATA, "audio/mp4")
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
