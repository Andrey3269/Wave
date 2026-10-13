"""
Получение закреплённого JS-runtime Deno для сборки Wave (этап S3).

    python packaging/fetch_runtime.py [--platform windows-x86_64] [--check]

Читает packaging/runtime/deno.lock, скачивает официальный архив релиза Deno, сверяет SHA-256 ДО распаковки,
извлекает исполняемый файл, сверяет и его SHA-256 и кладёт в packaging/runtime/deno/ (оттуда его забирает
packaging/wave.spec). Без проверенного хэша файл не используется никогда: отсутствие хэша в lock, несовпадение
хэша, ошибка загрузки — это ошибка с понятным сообщением и ненулевым кодом, а не «продолжим без проверки».

Повторный запуск ничего не скачивает, если готовый файл уже проверен (метка .wave-runtime.json и SHA-256).
Скачанный архив хранится в packaging/runtime/downloads/ и повторно используется только после сверки хэша.
Каталоги deno/ и downloads/ в git не попадают (.gitignore).

--check: только проверить, что готовый runtime на месте и совпадает с lock (без сети). Использует wave.spec.

Коды выхода: 0 — готово; 2 — ошибка загрузки; 3 — хэш не совпал; 4 — lock-файл неполный или неверный;
5 — архив повреждён; 6 — (--check) runtime не подготовлен.
Только стандартная библиотека Python: скрипт запускается до установки зависимостей.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable

HERE = Path(__file__).resolve().parent
LOCK = HERE / "runtime" / "deno.lock"
DEST = HERE / "runtime" / "deno"
CACHE = HERE / "runtime" / "downloads"
MARKER = ".wave-runtime.json"
URL_RE = re.compile(r"^https://github\.com/denoland/deno/releases/download/v(?P<v>[0-9][0-9A-Za-z.\-]*)/[A-Za-z0-9._\-]+\.zip$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
MEMBER_RE = re.compile(r"^[A-Za-z0-9._\-]+$")       # только имя файла в корне архива, без путей
CHUNK = 1024 * 1024
TIMEOUT = 60


class FetchError(Exception):
    """Ошибка подготовки runtime с кодом выхода."""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


def default_platform() -> str:
    machine = platform.machine().lower()
    arch = "x86_64" if machine in ("amd64", "x86_64") else machine
    return f"{'windows' if sys.platform == 'win32' else sys.platform}-{arch}"


def load_lock(path: Path = LOCK) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise FetchError(4, f"Не удалось прочитать {path}: {exc}") from exc
    if not isinstance(data, dict) or data.get("schema") != 1 or data.get("name") != "deno":
        raise FetchError(4, f"{path}: ожидается schema=1 и name=deno")
    if not isinstance(data.get("version"), str) or not re.fullmatch(r"\d+\.\d+\.\d+", data["version"]):
        raise FetchError(4, f"{path}: version должна быть вида X.Y.Z")
    if not isinstance(data.get("platforms"), dict) or not data["platforms"]:
        raise FetchError(4, f"{path}: нет списка platforms")
    return data


def platform_entry(lock: dict, name: str) -> dict:
    entry = lock["platforms"].get(name)
    if not isinstance(entry, dict):
        raise FetchError(4, f"В deno.lock нет платформы {name!r} (есть: {', '.join(lock['platforms'])})")
    m = URL_RE.fullmatch(str(entry.get("url", "")))
    if not m or m.group("v") != lock["version"]:
        raise FetchError(4, f"{name}: url должен указывать на официальный релиз Deno v{lock['version']} на GitHub")
    for key in ("sha256", "member_sha256"):
        if not SHA_RE.fullmatch(str(entry.get(key, ""))):
            raise FetchError(4, f"{name}: нет проверенного {key} (64 шестнадцатеричных символа в нижнем регистре). "
                                   "Без хэша runtime не используется")
    if entry.get("archive") != "zip":
        raise FetchError(4, f"{name}: поддерживается только archive=zip")
    if not MEMBER_RE.fullmatch(str(entry.get("member", ""))) or entry["member"] in (".", ".."):
        raise FetchError(4, f"{name}: member — имя файла в корне архива")
    for key in ("size", "member_size"):
        if not isinstance(entry.get(key), int) or entry[key] <= 0:
            raise FetchError(4, f"{name}: {key} — положительное целое")
    return entry


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_sha256(path: Path, expected: str) -> None:
    actual = sha256_file(path)
    if actual != expected:
        raise FetchError(3, f"SHA-256 не совпадает для {path.name}: ожидался {expected}, получен {actual}")


def http_download(url: str, target: Path) -> None:
    """Загрузка через urllib (учитывает HTTPS_PROXY). Пишет прямо в target, вызывающий отвечает за удаление."""
    req = urllib.request.Request(url, headers={"User-Agent": "wave-build/fetch_runtime"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r, open(target, "wb") as f:
        shutil.copyfileobj(r, f, CHUNK)


def _marker(dest: Path) -> dict | None:
    try:
        return json.loads((dest / MARKER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def is_ready(lock: dict, name: str, dest: Path = DEST) -> bool:
    """Готовый runtime совпадает с lock: метка версии и SHA-256 самого исполняемого файла."""
    entry = platform_entry(lock, name)
    exe = dest / entry["member"]
    m = _marker(dest)
    return (exe.is_file() and m is not None and m.get("version") == lock["version"] and m.get("platform") == name
            and m.get("member_sha256") == entry["member_sha256"] and sha256_file(exe) == entry["member_sha256"])


def _archive(entry: dict, cache: Path, download: Callable[[str, Path], None], log: Callable[[str], None]) -> Path:
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / entry["url"].rsplit("/", 1)[1]
    if archive.is_file():
        try:
            verify_sha256(archive, entry["sha256"])
            log(f"Архив уже скачан и проверен: {archive.name}")
            return archive
        except FetchError:
            log(f"Скачанный ранее {archive.name} не совпадает с lock — загружаем заново")
            archive.unlink()
    part = archive.with_name(archive.name + ".part")
    log(f"Загрузка {entry['url']}")
    try:
        download(entry["url"], part)
    except Exception as exc:                       # noqa: BLE001 — любая ошибка загрузки = остановка
        part.unlink(missing_ok=True)
        raise FetchError(2, f"Не удалось скачать {entry['url']}: {exc}") from exc
    try:
        verify_sha256(part, entry["sha256"])       # до любого использования содержимого
    except FetchError:
        part.unlink(missing_ok=True)
        raise
    os.replace(part, archive)
    log(f"SHA-256 архива совпал: {entry['sha256']}")
    return archive


def _extract(archive: Path, entry: dict, dest: Path) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    exe = dest / entry["member"]
    fd, tmp_name = tempfile.mkstemp(prefix=".extract-", dir=dest)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as out, zipfile.ZipFile(archive) as z:
            try:
                info = z.getinfo(entry["member"])
            except KeyError as exc:
                raise FetchError(5, f"В архиве нет файла {entry['member']}") from exc
            if info.file_size != entry["member_size"]:
                raise FetchError(5, f"{entry['member']}: размер {info.file_size}, ожидался {entry['member_size']}")
            with z.open(info) as src:
                shutil.copyfileobj(src, out, CHUNK)
        verify_sha256(tmp, entry["member_sha256"])
        if os.name != "nt":
            tmp.chmod(tmp.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        os.replace(tmp, exe)
    except zipfile.BadZipFile as exc:
        raise FetchError(5, f"Архив повреждён: {exc}") from exc
    finally:
        tmp.unlink(missing_ok=True)
    return exe


def fetch(lock: dict, name: str, dest: Path = DEST, cache: Path = CACHE,
          download: Callable[[str, Path], None] = http_download, log: Callable[[str], None] = print) -> Path:
    """Готовит runtime для платформы name. Возвращает путь к проверенному исполняемому файлу."""
    entry = platform_entry(lock, name)
    exe = dest / entry["member"]
    if is_ready(lock, name, dest):
        log(f"Deno {lock['version']} ({name}) уже подготовлен и проверен: {exe}")
        return exe
    (dest / MARKER).unlink(missing_ok=True)        # метка только после полной проверки
    archive = _archive(entry, cache, download, log)
    exe = _extract(archive, entry, dest)
    (dest / MARKER).write_text(json.dumps({"version": lock["version"], "platform": name, "sha256": entry["sha256"],
                                           "member_sha256": entry["member_sha256"]}, indent=2), encoding="utf-8")
    log(f"Готово: Deno {lock['version']} ({name}) -> {exe} ({exe.stat().st_size // (1024 * 1024)} МБ), "
        f"SHA-256 {entry['member_sha256']}")
    return exe


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Загрузка и проверка закреплённого Deno для сборки Wave")
    ap.add_argument("--platform", default="windows-x86_64",
                    help=f"платформа из deno.lock (по умолчанию windows-x86_64; эта машина: {default_platform()})")
    ap.add_argument("--lock", type=Path, default=LOCK)
    ap.add_argument("--dest", type=Path, default=DEST)
    ap.add_argument("--cache", type=Path, default=CACHE)
    ap.add_argument("--check", action="store_true", help="только проверить готовый runtime (без сети)")
    args = ap.parse_args(argv)
    try:
        lock = load_lock(args.lock)
        if args.check:
            if is_ready(lock, args.platform, args.dest):
                print(f"Deno {lock['version']} ({args.platform}) подготовлен и совпадает с lock")
                return 0
            print(f"Deno {lock['version']} ({args.platform}) не подготовлен или не совпадает с lock. "
                  f"Выполните: python packaging/fetch_runtime.py --platform {args.platform}", file=sys.stderr)
            return 6
        fetch(lock, args.platform, args.dest, args.cache)
        return 0
    except FetchError as exc:
        print(f"ОШИБКА: {exc}", file=sys.stderr)
        return exc.code


if __name__ == "__main__":
    sys.exit(main())
