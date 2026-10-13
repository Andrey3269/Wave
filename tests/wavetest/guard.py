"""
Защита тестовой среды Wave. Импортируется из tests/conftest.py ОДИН раз, до любого импорта backend.

  1. временный профиль: WAVE_DATA_DIR / WAVE_DOWNLOADS_DIR и HOME / USERPROFILE / LOCALAPPDATA / XDG_*
     указывают во временный каталог сессии; секреты (YTDLP_COOKIES, YTDLP_PROXY) убраны из окружения;
  2. аудит-хук Python блокирует запись, mkdir, rename, remove, rmtree и sqlite3.connect вне системного
     временного каталога и любые сетевые соединения, кроме loopback. Нарушение записывается в список,
     и фикстура в conftest роняет тест, даже если код приложения перехватил исключение.
"""
from __future__ import annotations

import ipaddress
import os
import sys
import tempfile
from pathlib import Path

# ---------------------------------------------------------------------------
# 1. Временный профиль: до импорта backend
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND = ROOT / "backend"
TESTS = ROOT / "tests"


def _norm(p: str | os.PathLike) -> str:
    return os.path.normcase(os.path.realpath(os.fspath(p)))


def _real_user_locations() -> dict[str, Path]:
    """Где лежали бы НАСТОЯЩИЕ данные пользователя (по логике backend/paths.py), до подмены окружения."""
    home = Path.home()
    if os.getenv("WAVE_DATA_DIR"):
        data = Path(os.environ["WAVE_DATA_DIR"])
    elif sys.platform == "win32":
        data = Path(os.getenv("LOCALAPPDATA") or home / "AppData" / "Local") / "Wave"
    else:
        data = home / ".local" / "share" / "wave"
    downloads = Path(os.getenv("WAVE_DOWNLOADS_DIR") or home / "Downloads" / "Wave")
    return {"data": data, "downloads": downloads}


def _snapshot(locations: dict[str, Path]) -> dict[str, tuple]:
    """Состояние реальных каталогов: существует ли, размер и время изменения wave.db. Ничего не создаёт."""
    out = {}
    for name, p in locations.items():
        db = p / "wave.db"
        stat = db.stat() if db.is_file() else None
        out[name] = (p.exists(), stat.st_size if stat else None, stat.st_mtime_ns if stat else None)
    return out


REAL_LOCATIONS = _real_user_locations()
REAL_SNAPSHOT = _snapshot(REAL_LOCATIONS)
SYSTEM_TMP = Path(os.path.realpath(tempfile.gettempdir()))
SESSION_ROOT = Path(tempfile.mkdtemp(prefix="wave-pytest-")).resolve()

for _k in ("YTDLP_COOKIES", "YTDLP_PROXY", "SEARCH_SOURCES", "SEARCH_TIMEOUT", "SOURCE_COOLDOWN",
           "SEARCH_CACHE_TTL", "STREAM_CHUNK_KB", "ALLOWED_ORIGINS", "SSL_CERT_FILE", "SSL_CERT_DIR"):
    os.environ.pop(_k, None)
# Прокси убираем: иначе HTTP-библиотеки шли бы через локальный прокси (loopback разрешён) и защита
# от сети стала бы дырявой. Без прокси любое внешнее соединение упирается в аудит-хук.
# Сохраняем их только для live-тестов (им нужна настоящая сеть, возможно через прокси).
PROXY_ENV = {k: os.environ.pop(k) for k in [k for k in os.environ
                                            if k.lower() in ("http_proxy", "https_proxy", "all_proxy", "ftp_proxy",
                                                             "ws_proxy", "wss_proxy", "no_proxy")]}

_fake_home = SESSION_ROOT / "home"
_env = {
    "HOME": _fake_home,
    "USERPROFILE": _fake_home,
    "LOCALAPPDATA": SESSION_ROOT / "localappdata",
    "APPDATA": SESSION_ROOT / "appdata",
    "XDG_CACHE_HOME": SESSION_ROOT / "xdg-cache",
    "XDG_DATA_HOME": SESSION_ROOT / "xdg-data",
    "XDG_CONFIG_HOME": SESSION_ROOT / "xdg-config",
    "WAVE_DATA_DIR": SESSION_ROOT / "data",
    "WAVE_DOWNLOADS_DIR": SESSION_ROOT / "downloads",
}
for _k, _v in _env.items():
    _v.mkdir(parents=True, exist_ok=True)
    os.environ[_k] = str(_v)

sys.dont_write_bytecode = True          # в backend/ не появится __pycache__
sys.path.insert(0, str(BACKEND))        # модули backend импортируются как в uvicorn --app-dir backend
sys.path.insert(0, str(TESTS))          # вспомогательный пакет tests/wavetest


# ---------------------------------------------------------------------------
# 2. Аудит-хук: запись вне временного каталога и внешняя сеть запрещены
# ---------------------------------------------------------------------------
class GuardViolation(OSError):
    """Тест попытался писать вне временного каталога или выйти в сеть."""


_GUARD = {"active": False, "network_allowed": False, "violations": []}
_ALLOWED_ROOTS = [_norm(SYSTEM_TMP), _norm(SESSION_ROOT)]
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC
_LOOPBACK_NAMES = {"localhost", "localhost.localdomain", "ip6-localhost"}
# Устройства, а не файлы профиля (S3): os.devnull — subprocess.DEVNULL при запуске JS-runtime;
# CONOUT$/CONIN$ — консоль Windows, которую открывает YoutubeDL (yt_dlp.utils.windows_enable_vt_mode).
_DEVICES = {os.devnull, "CONOUT$", "CONIN$"} if os.name == "nt" else {os.devnull}


def _inside_allowed(path) -> bool:
    if isinstance(path, int):            # файловый дескриптор: путь уже проверен при открытии
        return True
    if isinstance(path, bytes):
        path = os.fsdecode(path)
    if path in (":memory:", "") or str(path).startswith("file::memory:"):
        return True
    if os.fspath(path) in _DEVICES:      # не файлы: см. _DEVICES
        return True
    p = _norm(os.path.abspath(os.fspath(path)))
    return any(p == r or p.startswith(r + os.sep) for r in _ALLOWED_ROOTS)


def _is_loopback(host) -> bool:
    if host is None:
        return True                      # getaddrinfo(None, port) — локальный адрес для bind
    if isinstance(host, bytes):
        host = host.decode(errors="replace")
    host = str(host).strip("[]").lower()
    if host in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host.split("%")[0]).is_loopback
    except ValueError:
        return False


def _violate(kind: str, detail) -> None:
    msg = f"{kind}: {detail!r}"
    _GUARD["violations"].append(msg)
    raise GuardViolation(f"Защита тестов: {msg}")


def _audit(event: str, args: tuple) -> None:
    if not _GUARD["active"]:
        return
    if event == "open":
        path, mode, flags = args
        writing = (isinstance(mode, str) and any(c in mode for c in "wax+")) or bool((flags or 0) & _WRITE_FLAGS)
        if writing and not _inside_allowed(path):
            _violate("запись файла вне временного каталога", path)
    elif event in ("os.mkdir", "os.remove", "os.rmdir", "os.truncate"):
        path, dir_fd = args[0], args[-1]
        if dir_fd in (None, -1) and not _inside_allowed(path):
            _violate(f"{event} вне временного каталога", path)
    elif event == "os.rename":
        src, dst, src_fd, dst_fd = args
        for p, fd in ((src, src_fd), (dst, dst_fd)):
            if fd in (None, -1) and not _inside_allowed(p):
                _violate("переименование вне временного каталога", p)
    elif event == "shutil.rmtree":
        if not _inside_allowed(args[0]):
            _violate("удаление дерева вне временного каталога", args[0])
    elif event == "sqlite3.connect":
        if not _inside_allowed(args[0]):
            _violate("SQLite вне временного каталога", args[0])
    elif event in ("socket.connect", "socket.getaddrinfo", "socket.sendto"):
        if _GUARD["network_allowed"]:
            return
        if event == "socket.getaddrinfo":
            host = args[0]
        else:
            address = args[1] if event == "socket.connect" else args[-1]
            if not isinstance(address, tuple):       # AF_UNIX и т.п.: локально
                return
            host = address[0]
        if not _is_loopback(host):
            _violate("сетевое соединение", host)


sys.addaudithook(_audit)


def guard_violations() -> list[str]:
    return list(_GUARD["violations"])


def take_guard_violations() -> list[str]:
    """Для тестов самой защиты: забрать (и очистить) записанные нарушения."""
    out = list(_GUARD["violations"])
    _GUARD["violations"].clear()
    return out
