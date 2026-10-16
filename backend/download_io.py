"""
Файловые операции загрузок вне event loop (S11, остаток M8).

Запись аудио, обложки, текста и metadata.json, перенос временной папки и удаление `.part` выполняются в потоках
(`asyncio.to_thread`), чтобы большой файл или медленный диск не останавливали event loop: пока идёт запись,
backend продолжает отдавать поток плееру и отвечать интерфейсу.

Главная тонкость — отмена. Отмена coroutine НЕ останавливает уже запущенный вызов в потоке: `await
asyncio.to_thread(...)` получает CancelledError сразу, а поток продолжает писать. Если после этого закрыть файл или
удалить временную папку, поток может писать в закрытый дескриптор или заново создать удалённый файл. Поэтому
`run_in_thread` при отмене дожидается конца уже запущенной операции и только потом пробрасывает отмену. Одна
операция короткая (одна порция записи, перенос папки), так что отмена задерживается на миллисекунды, а не на всю
загрузку. Следующая операция после отмены уже не начинается.

Владелец файла один: `ThreadFile` открывается, пишется и закрывается только задачей загрузки. Его дескриптор
присваивается внутри потока, поэтому даже открытие, прерванное отменой, оставляет файл, который можно закрыть.
"""
from __future__ import annotations

import asyncio
import logging
import os
import stat
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("musicmeta.downloads")


async def run_in_thread(fn: Callable[..., Any], *args: Any) -> Any:
    """
    fn(*args) в потоке. Если задачу отменили, пока fn выполняется, дожидается конца fn (поток не остановить)
    и затем пробрасывает CancelledError: после возврата управления fn гарантированно больше ничего не делает.
    Ошибку самой fn в этом случае только записывает в журнал — задача всё равно отменена.
    """
    fut = asyncio.ensure_future(asyncio.to_thread(fn, *args))
    cancelled = False
    while not fut.done():
        try:
            # asyncio.wait не отменяет fut при отмене ожидающего и не пробрасывает ошибку fut — разбор ниже
            await asyncio.wait({fut})
        except asyncio.CancelledError:
            cancelled = True                     # отменили нас: ждём, пока поток закончит, и повторяем отмену
    if cancelled:
        if not fut.cancelled() and (exc := fut.exception()) is not None:
            log.info("Файловая операция загрузки после отмены закончилась ошибкой: %r", exc)
        raise asyncio.CancelledError
    return fut.result()


def _write(f, data: bytes) -> None:
    """Одна запись в файл (в потоке). Вынесена отдельно, чтобы тесты могли проверить поток и порядок записей."""
    f.write(data)


class ThreadFile:
    """Файл загрузки, все операции с которым идут в потоках, строго по очереди (одна задача — один владелец)."""

    def __init__(self, path: Path):
        self.path = path
        self.written = 0
        self._f = None

    def _open(self) -> None:
        self._f = open(self.path, "wb")          # дескриптор запоминается в потоке: отмена открытия его не теряет

    async def open(self) -> None:
        await run_in_thread(self._open)

    async def write(self, data: bytes) -> None:
        if data:
            await run_in_thread(_write, self._f, data)
            self.written += len(data)

    def close_sync(self) -> None:
        f, self._f = self._f, None
        if f is not None:
            f.close()

    async def close(self) -> None:
        await run_in_thread(self.close_sync)

    @property
    def closed(self) -> bool:
        return self._f is None


def remove_partial(tmp: Path, file: ThreadFile | None = None) -> None:
    """
    Закрывает файл загрузки и удаляет временную папку `*.part` этой загрузки (в потоке, одной операцией: отмена не
    может разделить закрытие и удаление). Удаляются только файлы, которые туда пишет загрузчик, — папка без вложенных
    каталогов; ссылки удаляются как ссылки, без перехода по ним. Если папку удалить не удалось (файл занят), она
    остаётся до следующего запуска: там её уберёт очистка при старте.
    """
    if file is not None:
        try:
            file.close_sync()
        except OSError as exc:
            log.info("Файл загрузки не закрылся: %r", exc)
    clear_part_dir(tmp)


_REPARSE_POINT = 0x400                       # FILE_ATTRIBUTE_REPARSE_POINT: symlink или junction на Windows


def is_link(st: os.stat_result) -> bool:
    """Ссылка любого вида по результату lstat: symlink, а на Windows и junction (is_symlink() их не видит)."""
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & _REPARSE_POINT)


def clear_part_dir(tmp: Path) -> bool:
    """Удаляет папку `*.part`: файлы и ссылки в ней (без рекурсии), затем саму папку. True — папки больше нет."""
    try:
        st = os.lstat(tmp)
        if is_link(st):
            log.warning("Временная папка загрузки %s — ссылка, не удаляется", tmp.name)
            return False
        if not os.path.isdir(tmp):
            return False
        with os.scandir(tmp) as it:
            entries = list(it)
        for e in entries:
            link = is_link(e.stat(follow_symlinks=False))
            if not link and e.is_dir(follow_symlinks=False):
                continue                         # каталогов загрузчик не создаёт: чужое не трогаем
            try:
                os.unlink(e.path)                # для ссылки удаляется сама ссылка, не цель
            except OSError:
                if not link:
                    raise
                os.rmdir(e.path)                 # ссылка на каталог в Windows (symlink/junction) снимается rmdir
        os.rmdir(tmp)
        return True
    except FileNotFoundError:
        return True
    except OSError as exc:
        log.warning("Временная папка загрузки %s не удалена: %r", tmp.name, exc)
        return False
