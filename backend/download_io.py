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
import threading
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("musicmeta.downloads")


async def run_in_thread(fn: Callable[..., Any], *args: Any) -> Any:
    """
    fn(*args) в потоке. Если задачу отменили, пока fn выполняется, дожидается конца fn (поток не остановить)
    и затем пробрасывает CancelledError: после возврата управления fn гарантированно больше ничего не делает.
    Ошибку самой fn в этом случае только записывает в журнал — задача всё равно отменена.

    Ждём сам поток, а не asyncio-обёртку: при остановке цикла (asyncio.run отменяет все задачи) обёртка может
    оказаться отменённой, пока поток ещё пишет. Поэтому поток сам отмечает начало и конец fn, а fn, которая к
    моменту такой отмены ещё не началась, уже не начнётся никогда (флаг abandoned под блокировкой).
    """
    lock, finished = threading.Lock(), threading.Event()
    flags = {"started": False, "abandoned": False}

    def call():
        with lock:
            if flags["abandoned"]:
                return None
            flags["started"] = True
        try:
            return fn(*args)
        finally:
            finished.set()

    fut = asyncio.ensure_future(asyncio.to_thread(call))
    cancelled = False
    while True:
        if fut.done():
            if not fut.cancelled():
                break                                    # fn закончилась (или поток не запустился — ошибка в fut)
            with lock:                                   # обёртку отменили снаружи: fn могла уже начаться
                if not flags["started"]:
                    flags["abandoned"] = True
                running = flags["started"] and not finished.is_set()
            if not running:
                cancelled = True
                break
        try:
            if fut.done():
                await asyncio.sleep(0.005)               # обёртки уже нет, а поток ещё пишет — ждём сам поток
            else:
                # asyncio.wait не отменяет fut при отмене ожидающего и не пробрасывает ошибку fut — разбор ниже
                await asyncio.wait({fut})
        except asyncio.CancelledError:
            cancelled = True                             # отменили нас: ждём, пока поток закончит, и повторяем отмену
    if cancelled:
        if fut.done() and not fut.cancelled() and (exc := fut.exception()) is not None:
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


_REPARSE_POINT = 0x400                       # FILE_ATTRIBUTE_REPARSE_POINT
_NAME_SURROGATE = 0x20000000                 # бит тега: точка указывает на другое имя (symlink, junction, LX-ссылка)
# Удаление относительно открытого дескриптора каталога (POSIX): подмена папки ссылкой между проверкой и удалением
# ничего не даёт — открыт уже проверенный каталог, а ссылка при открытии не разыменовывается (O_NOFOLLOW)
_DIR_FD = (hasattr(os, "O_NOFOLLOW") and hasattr(os, "O_DIRECTORY") and os.scandir in os.supports_fd
           and os.unlink in os.supports_dir_fd)


def is_link(st) -> bool:
    """
    Ссылка любого вида по результату lstat: symlink, а на Windows и junction (is_symlink() её не видит). Прочие
    точки повторной обработки (плейсхолдеры OneDrive, дедупликация) — обычные файлы и папки: это не ссылки на
    другое место, их можно удалять. Если тег неизвестен, точку на всякий случай считаем ссылкой.
    """
    if stat.S_ISLNK(st.st_mode):
        return True
    if getattr(st, "st_file_attributes", 0) & _REPARSE_POINT:
        tag = getattr(st, "st_reparse_tag", None)
        return tag is None or bool(tag & _NAME_SURROGATE)
    return False


def _entries(it) -> list[tuple[str, bool]]:
    """(имя, удалять ли) для содержимого .part: файлы и ссылки — да, настоящие подкаталоги — нет."""
    out = []
    for e in it:
        try:
            link = is_link(e.stat(follow_symlinks=False))
            isdir = e.is_dir(follow_symlinks=False)
        except FileNotFoundError:
            continue
        out.append((e.name, link or not isdir))
    return out


def clear_part_dir(tmp: Path) -> bool:
    """
    Удаляет папку `*.part`: файлы и ссылки в ней (без рекурсии и без перехода по ссылкам), затем саму папку.
    True — папки больше нет. Исчезнувший по дороге файл (антивирус, синхронизация) очистку не прерывает.
    """
    try:
        st = os.lstat(tmp)
    except FileNotFoundError:
        return True
    except OSError as exc:
        log.warning("Временная папка загрузки %s недоступна: %r", tmp.name, exc)
        return False
    if is_link(st):
        log.warning("Временная папка загрузки %s — ссылка, не удаляется", tmp.name)
        return False
    if not stat.S_ISDIR(st.st_mode):
        return False
    try:
        if _DIR_FD:
            fd = os.open(tmp, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                if not os.path.samestat(os.fstat(fd), st):
                    log.warning("Временная папка загрузки %s подменена во время удаления", tmp.name)
                    return False
                with os.scandir(fd) as it:
                    entries = _entries(it)
                for name, remove in entries:
                    if remove:
                        try:
                            os.unlink(name, dir_fd=fd)       # для ссылки удаляется сама ссылка, не цель
                        except FileNotFoundError:
                            pass
            finally:
                os.close(fd)
        else:                                            # Windows: тот же каталог до и после чтения списка
            with os.scandir(tmp) as it:
                entries = _entries(it)
            again = os.lstat(tmp)
            if is_link(again) or not os.path.samestat(again, st):
                log.warning("Временная папка загрузки %s подменена во время удаления", tmp.name)
                return False
            for name, remove in entries:
                if not remove:
                    continue
                path = os.path.join(tmp, name)
                try:
                    os.unlink(path)
                except FileNotFoundError:
                    pass
                except OSError:
                    if not is_link(os.lstat(path)):
                        raise
                    os.rmdir(path)                       # ссылка на каталог (symlink/junction) снимается rmdir
        os.rmdir(tmp)
        return True
    except FileNotFoundError:
        return True
    except OSError as exc:
        log.warning("Временная папка загрузки %s не удалена: %r", tmp.name, exc)
        return False
