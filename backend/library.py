"""
Локальная библиотека: индекс скачанных треков в SQLite (%LOCALAPPDATA%\\Wave\\wave.db).

Индекс нужен, чтобы не сканировать папку загрузок при каждом запросе. Если файл
удалили вручную, запись при следующем обращении исчезает из индекса сама.
"""
from __future__ import annotations

import logging
import re
import shutil
import sqlite3
from pathlib import Path

import paths
from persistence import db

log = logging.getLogger("musicmeta.library")

# Схема и её версии — persistence/migrations.py; соединение, блокировка и транзакции — persistence/db.py.


def init() -> None:
    """Открывает wave.db (обновив схему до текущей версии, с резервной копией) — один раз на запуск backend."""
    data = paths.data_dir()
    db.open(data / "wave.db", data / "backups")


def _q(sql: str, args: tuple = ()) -> list[sqlite3.Row]:
    """Один запрос (адаптер на время перехода к persistence/*; новый код вызывает db.query)."""
    return db.query(sql, args)


# Несколько запросов как одна операция: всё или ничего (см. persistence.db.transaction).
transaction = db.transaction


# ---------- имена папок ----------
_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _clean(s: str | None, limit: int) -> str:
    return _BAD.sub("", s or "").strip(" .")[:limit].rstrip(" .") or "track"


def safe_folder(artist: str, title: str, source: str, track_id: str) -> str:
    """Безопасное для Windows имя папки; уникальность даёт хвост [источник-id]."""
    return f"{_clean(artist, 40)} - {_clean(title, 60)} [{source}-{_clean(track_id, 24)}]"


def folder_path(folder: str, root: str | None = None) -> Path:
    """Путь внутри каталога загрузок (своего у каждой записи); выход за его пределы (../) запрещён."""
    root = Path(root).resolve() if root else paths.downloads_dir().resolve()
    p = (root / folder).resolve()
    if not p.is_relative_to(root) or p == root:
        raise ValueError("Недопустимый путь")
    return p


# ---------- проверка путей из индекса (находка N3) ----------
# Строки индекса читаются из базы и не считаются доверенными: папка загрузки — одно имя внутри своего
# каталога загрузок, файлы — одно имя внутри этой папки. Строка, которая выводит за эти пределы
# (../, абсолютный путь, другой диск, ссылка наружу), пропускается при чтении и пишется в журнал;
# её файлы не читаются и не удаляются. Сама строка не удаляется: это данные пользователя.
_warned: set[str] = set()
_SEP = re.compile(r"[/\\:\x00]")


def _component(name) -> bool:
    """Ровно одно имя файла или папки: без разделителей, диска и ссылок на родителя."""
    return (isinstance(name, str) and name not in ("", ".", "..") and not _SEP.search(name)
            and Path(name).name == name)


def _unsafe(row: sqlite3.Row, why: str) -> None:
    if row["key"] not in _warned:
        _warned.add(row["key"])
        log.warning("Запись индекса загрузок %s пропущена: %s", row["key"], why)


def _row_dir(row: sqlite3.Row) -> Path | None:
    """Папка загрузки этой строки или None, если строка указывает за пределы своего каталога загрузок."""
    root = row["root"]
    if root and not Path(root).is_absolute():
        _unsafe(row, "каталог загрузок записан относительным путём")
        return None
    if not _component(row["folder"]):
        _unsafe(row, "имя папки выводит за пределы каталога загрузок")
        return None
    try:
        return folder_path(row["folder"], root)
    except (ValueError, OSError):
        _unsafe(row, "папка разрешается за пределы каталога загрузок")
        return None


def _row_file(row: sqlite3.Row, column: str) -> Path | None:
    name = row[column]
    if not name:
        return None
    folder = _row_dir(row)
    if folder is None:
        return None
    if not _component(name):
        _unsafe(row, f"имя файла ({column}) выводит за пределы папки загрузки")
        return None
    return folder / name


def owns_folder(row: sqlite3.Row) -> bool:
    """Папку создал загрузчик Wave именно для этого трека: только такую папку можно удалить целиком."""
    return row["folder"].endswith(f"[{row['source']}-{_clean(row['track_id'], 24)}]")


# ---------- запросы ----------
def _alive(row: sqlite3.Row | None) -> sqlite3.Row | None:
    """Строка индекса, только если она безопасна и аудиофайл реально лежит на диске."""
    if row is None:
        return None
    if _row_dir(row) is None:
        return None
    audio = _row_file(row, "audio")
    if audio is None:
        return None
    if audio.is_file():
        return row
    _q("DELETE FROM downloads WHERE key = ?", (row["key"],))
    return None


def get(source: str, track_id: str) -> sqlite3.Row | None:
    rows = _q("SELECT * FROM downloads WHERE key = ?", (f"{source}:{track_id}",))
    return _alive(rows[0]) if rows else None


def audio_path(source: str, track_id: str) -> Path | None:
    row = get(source, track_id)
    return _row_file(row, "audio") if row else None


def lyrics_path(source: str, track_id: str) -> Path | None:
    row = get(source, track_id)
    p = _row_file(row, "lyrics") if row else None
    return p if p and p.is_file() else None


def cover_path(source: str, track_id: str) -> Path | None:
    row = get(source, track_id)
    p = _row_file(row, "cover") if row else None
    return p if p and p.is_file() else None


def list_items() -> list[sqlite3.Row]:
    rows = _q("SELECT * FROM downloads ORDER BY created_at DESC")
    return [r for r in (_alive(r) for r in rows) if r]


def add(**f) -> None:
    _q(
        "INSERT OR REPLACE INTO downloads (key, source, track_id, title, artist, album, duration,"
        " folder, audio, cover, lyrics, size, created_at, root) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (f"{f['source']}:{f['track_id']}", f["source"], f["track_id"], f["title"], f["artist"], f["album"],
         f["duration"], f["folder"], f["audio"], f["cover"], f["lyrics"], f["size"], f["created_at"], f.get("root")),
    )


def verified_roots() -> list[Path]:
    """
    Каталоги загрузок из индекса, в которых Wave действительно сохранял треки (S11: там может остаться `*.part`
    после закрытия приложения посреди загрузки). Строки базы не доверенные (N3), поэтому каталог засчитывается,
    только если строка проходит те же проверки, что и при чтении (папка — одно имя внутри своего каталога,
    без выхода наружу), папку создал загрузчик Wave для этого трека, а её аудиофайл реально лежит на диске.
    Строка, указывающая, например, на C:\\Windows, каталогом загрузок не становится. Строки не удаляются.
    """
    roots: dict[str, Path] = {}
    for row in _q("SELECT * FROM downloads"):
        folder = _row_dir(row)
        if folder is None or not owns_folder(row):
            continue
        audio = _row_file(row, "audio")
        try:
            if audio is None or not audio.is_file():
                continue
        except OSError:                          # диск недоступен
            continue
        roots.setdefault(str(folder.parent), folder.parent)
    return list(roots.values())


class FolderKept(Exception):
    """Запись индекса удалена, а папку Wave не тронул: она не создана загрузчиком для этого трека."""

    def __init__(self, folder: Path | None):
        super().__init__(str(folder) if folder else "")
        self.folder = folder


def delete(source: str, track_id: str) -> bool:
    """
    Удаляет папку трека целиком (аудио, обложка, текст, metadata), затем запись индекса.

    Целиком удаляется только папка внутри каталога загрузок с хвостом имени [источник-id] этого трека.
    Если запись указывает куда-то ещё (повреждённая или чужая строка базы), удаляется только запись,
    файлы остаются на месте, а вызывающий получает FolderKept.
    """
    rows = _q("SELECT * FROM downloads WHERE key = ?", (f"{source}:{track_id}",))
    if not rows:
        return False
    row = rows[0]
    folder = _row_dir(row)
    if folder is None or not owns_folder(row):
        if folder is not None:
            _unsafe(row, "имя папки создано не загрузчиком Wave, удаляется только запись индекса")
        _q("DELETE FROM downloads WHERE key = ?", (row["key"],))
        raise FolderKept(folder)
    if folder.exists():
        shutil.rmtree(folder)          # при ошибке бросит OSError: запись индекса остаётся
    _q("DELETE FROM downloads WHERE key = ?", (row["key"],))
    return True


def to_track(row: sqlite3.Row) -> dict:
    """Скачанный трек в том же формате, что и результаты поиска (плеер играет его как обычный)."""
    s, tid = row["source"], row["track_id"]
    return {
        "id": tid, "source": s, "title": row["title"], "artist": row["artist"],
        "artists": [row["artist"]], "album": row["album"], "duration": row["duration"],
        "cover": f"/api/local/{s}/{tid}/cover" if row["cover"] else None,
        "explicit": False, "stream_url": f"/api/stream/{s}/{tid}",   # сервер отдаёт локальный файл
        "downloaded": True, "size": row["size"], "has_lyrics": bool(row["lyrics"]),
    }