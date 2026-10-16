"""
S11: запись загрузок вне event loop (backend/download_io.py) и очередь мест менеджера загрузок (downloads._Slots).

Главное — отмена: отменённая задача не продолжает работать с файлом, но и не бросает уже начатую запись
посередине (поток не остановить), поэтому удаление временной папки всегда идёт после конца записи.
"""
from __future__ import annotations

import asyncio
import os
import threading
import time
from types import SimpleNamespace

import pytest

import download_io
import downloads


# ---------------------------------------------------------------------------
# run_in_thread
# ---------------------------------------------------------------------------
def test_run_in_thread_returns_and_raises():
    async def main():
        assert await download_io.run_in_thread(lambda a, b: a + b, 2, 3) == 5
        with pytest.raises(ZeroDivisionError):
            await download_io.run_in_thread(lambda: 1 / 0)
        return threading.get_ident()

    assert asyncio.run(main())


def test_run_in_thread_runs_off_the_loop_thread():
    seen = {}

    async def main():
        seen["loop"] = threading.get_ident()
        await download_io.run_in_thread(lambda: seen.setdefault("fn", threading.get_ident()))

    asyncio.run(main())
    assert seen["fn"] != seen["loop"]


def test_cancel_waits_for_the_running_operation_then_propagates():
    started, release = threading.Event(), threading.Event()
    log: list[str] = []

    def slow():
        started.set()
        release.wait(5)
        log.append("fn finished")

    async def main():
        task = asyncio.create_task(download_io.run_in_thread(slow))
        while not started.is_set():
            await asyncio.sleep(0.005)
        task.cancel()
        await asyncio.sleep(0.05)
        assert not task.done()                     # поток ещё работает — отмена ждёт его
        task.cancel()                              # повторная отмена тоже ждёт
        await asyncio.sleep(0.02)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        log.append("cancel delivered")

    asyncio.run(main())
    assert log == ["fn finished", "cancel delivered"]


def test_error_after_cancel_is_logged_not_lost(caplog):
    started, release = threading.Event(), threading.Event()

    def fails():
        started.set()
        release.wait(5)
        raise OSError("диск отключён (тест)")

    async def main():
        task = asyncio.create_task(download_io.run_in_thread(fails))
        while not started.is_set():
            await asyncio.sleep(0.005)
        task.cancel()
        await asyncio.sleep(0.01)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    with caplog.at_level("INFO", logger="musicmeta.downloads"):
        asyncio.run(main())
    assert "диск отключён" in caplog.text


# ---------------------------------------------------------------------------
# ThreadFile
# ---------------------------------------------------------------------------
def test_thread_file_writes_in_order(tmp_path):
    path = tmp_path / "audio.part"

    async def main():
        f = download_io.ThreadFile(path)
        await f.open()
        for i in range(50):
            await f.write(bytes([i]) * 1000)
        await f.write(b"")
        await f.close()
        return f

    f = asyncio.run(main())
    assert f.closed and f.written == 50_000
    assert path.read_bytes() == b"".join(bytes([i]) * 1000 for i in range(50))


def test_open_interrupted_by_cancel_still_yields_a_closable_file(tmp_path, monkeypatch):
    part = tmp_path / "A - B [yt-AAAAAAAAAA1].part"
    part.mkdir()
    path = part / "audio.part"
    started, release = threading.Event(), threading.Event()
    real_open = open

    def slow_open(p, mode="r", *a, **kw):
        started.set()
        release.wait(5)
        return real_open(p, mode, *a, **kw)

    monkeypatch.setattr(download_io, "open", slow_open, raising=False)
    f = download_io.ThreadFile(path)

    async def main():
        task = asyncio.create_task(f.open())
        while not started.is_set():
            await asyncio.sleep(0.005)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())
    assert not f.closed                            # дескриптор не потерян: его можно закрыть
    download_io.remove_partial(part, f)
    assert f.closed and not part.exists()


# ---------------------------------------------------------------------------
# Удаление временной папки
# ---------------------------------------------------------------------------
def symlink(link, target, is_dir: bool = False) -> None:
    try:
        os.symlink(target, link, target_is_directory=is_dir)
    except (OSError, NotImplementedError):
        pytest.skip("символические ссылки недоступны в этой среде")


def test_clear_part_dir_removes_files_and_links_but_not_targets_or_subdirs(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("x")
    part = tmp_path / "A - B [yt-AAAAAAAAAA1].part"
    part.mkdir()
    (part / "audio.part").write_bytes(b"half")
    (part / "cover.jpg").write_bytes(b"img")
    symlink(part / "file-link", outside / "keep.txt")
    symlink(part / "dir-link", outside, is_dir=True)
    assert download_io.clear_part_dir(part) is True
    assert not part.exists() and (outside / "keep.txt").read_text() == "x"

    part.mkdir()
    (part / "sub").mkdir()
    (part / "audio.part").write_bytes(b"half")
    assert download_io.clear_part_dir(part) is False              # чужой подкаталог — папка остаётся
    assert sorted(p.name for p in part.iterdir()) == ["sub"]


def test_clear_part_dir_refuses_a_link_and_accepts_missing(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("x")
    link = tmp_path / "A - B [yt-AAAAAAAAAA1].part"
    symlink(link, outside, is_dir=True)
    assert download_io.clear_part_dir(link) is False
    assert link.is_symlink() and (outside / "keep.txt").exists()
    assert download_io.clear_part_dir(tmp_path / "missing.part") is True


def test_is_link_recognises_windows_reparse_points():
    """Junction в Windows не видна как symlink, но помечена атрибутом reparse point."""
    plain = SimpleNamespace(st_mode=0o040755, st_file_attributes=0x10)
    junction = SimpleNamespace(st_mode=0o040755, st_file_attributes=0x10 | 0x400)
    posix_link = SimpleNamespace(st_mode=0o120777)
    assert not download_io.is_link(plain)
    assert download_io.is_link(junction) and download_io.is_link(posix_link)


# ---------------------------------------------------------------------------
# Очередь мест
# ---------------------------------------------------------------------------
def test_slots_fifo_and_handover():
    async def main():
        s = downloads._Slots(2)
        assert s.try_take() and s.try_take() and not s.try_take()
        order: list[str] = []

        async def waiter(name):
            await s.take()
            order.append(name)

        a = asyncio.create_task(waiter("a"))
        await asyncio.sleep(0)
        b = asyncio.create_task(waiter("b"))
        await asyncio.sleep(0)
        assert not s.try_take()                    # новая задача не обгоняет ожидающих
        s.release()
        await a
        assert order == ["a"] and s.busy == 2      # место передано, а не освобождено
        s.release()
        await b
        assert order == ["a", "b"]
        s.release()
        s.release()
        assert s.busy == 0 and s.peak == 2

    asyncio.run(main())


def test_cancelled_waiter_neither_takes_nor_loses_a_slot():
    async def main():
        s = downloads._Slots(1)
        assert s.try_take()
        t1 = asyncio.create_task(s.take())
        t2 = asyncio.create_task(s.take())
        await asyncio.sleep(0)
        t1.cancel()                                 # отменён, пока ждал
        await asyncio.sleep(0)
        s.release()                                 # место — второму, а не отменённому
        await asyncio.wait_for(t2, 1)
        assert s.busy == 1
        s.release()
        assert s.busy == 0

    asyncio.run(main())


def test_waiter_cancelled_right_after_handover_passes_the_slot_on():
    async def main():
        s = downloads._Slots(1)
        assert s.try_take()
        t1 = asyncio.create_task(s.take())
        t2 = asyncio.create_task(s.take())
        await asyncio.sleep(0)
        s.release()                                 # место передано t1 …
        t1.cancel()                                 # … но t1 отменили раньше, чем он проснулся
        with pytest.raises(asyncio.CancelledError):
            await t1
        await asyncio.wait_for(t2, 1)               # место не потерялось — у t2
        assert s.busy == 1
        s.release()
        assert s.busy == 0

    asyncio.run(main())


def test_slots_under_random_cancellation_never_exceed_the_limit():
    """Много задач, часть отменяется в случайный момент: занято мест не больше лимита, в конце — ноль."""
    import random
    rnd = random.Random(7)

    async def main():
        s = downloads._Slots(3)
        inside = {"n": 0, "max": 0}

        async def worker():
            await s.take()
            try:
                inside["n"] += 1
                inside["max"] = max(inside["max"], inside["n"])
                await asyncio.sleep(rnd.random() / 200)
            finally:
                inside["n"] -= 1
                s.release()

        tasks = [asyncio.create_task(worker()) for _ in range(60)]
        for _ in range(25):
            await asyncio.sleep(rnd.random() / 400)
            rnd.choice(tasks).cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        return s, inside

    start = time.monotonic()
    s, inside = asyncio.run(main())
    assert inside["max"] <= 3 and s.busy == 0 and inside["n"] == 0
    assert time.monotonic() - start < 10
