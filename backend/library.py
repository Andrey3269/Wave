"""
Локальная библиотека: индекс скачанных треков в SQLite (%LOCALAPPDATA%\\Wave\\wave.db).

Индекс нужен, чтобы не сканировать папку загрузок при каждом запросе. Если файл
удалили вручную, запись при следующем обращении исчезает из индекса сама.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import sqlite3
from pathlib import Path

import paths
from domain.snapshot import valid_track
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
# S13: каталог загрузок (колонка root) тоже приходит из базы, поэтому читать можно только папку, которую создал
# загрузчик Wave для этого трека: имя оканчивается на «[источник-id]» (owns_folder), ключ строки совпадает с её
# источником и id, а id — в формате источника. Строка с root=C:\Users\Имя и folder="Documents" больше не даёт
# прочитать файлы из Documents. Файл — не ссылка и лежит прямо в этой папке.
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
    """
    Папка загрузки этой строки или None, если строка указывает за пределы своего каталога загрузок или на папку,
    которую загрузчик Wave не создавал для этого трека.
    """
    source, track_id = row["source"], row["track_id"]
    if not valid_track(source, track_id) or row["key"] != f"{source}:{track_id}":
        _unsafe(row, "источник или id трека недопустимы либо не совпадают с ключом")
        return None
    root = row["root"]
    try:
        if root and not Path(root).is_absolute():
            _unsafe(row, "каталог загрузок записан относительным путём")
            return None
    except (TypeError, ValueError):
        _unsafe(row, "каталог загрузок записан недопустимым значением")
        return None
    if not _component(row["folder"]):
        _unsafe(row, "имя папки выводит за пределы каталога загрузок")
        return None
    if not owns_folder(row):
        _unsafe(row, "папка создана не загрузчиком Wave для этого трека")
        return None
    try:
        # без root — текущий каталог загрузок, не создавая его (S13: недоступная папка не роняет чтение)
        return folder_path(row["folder"], root or str(paths.current_downloads_dir()))
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
    p = folder / name
    try:
        if p.is_symlink() or (p.exists() and p.resolve().parent != folder):
            _unsafe(row, f"файл ({column}) — ссылка или разрешается за пределы папки загрузки")
            return None
    except (OSError, ValueError):
        return None
    return p


def owns_folder(row: sqlite3.Row) -> bool:
    """Папку создал загрузчик Wave именно для этого трека: только такую папку можно читать и удалить целиком."""
    folder, track_id = row["folder"], row["track_id"]
    return (isinstance(folder, str) and isinstance(track_id, str)
            and folder.endswith(f"[{row['source']}-{_clean(track_id, 24)}]"))


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


def verified_roots(valid_track) -> list[Path]:
    """
    Каталоги загрузок из индекса, в которых Wave действительно сохранял треки (S11: там может остаться `*.part`
    после закрытия приложения посреди загрузки). Строки базы не доверенные (N3), поэтому каталог засчитывается,
    только если строка подтверждает его целиком:
      * источник и id трека допустимы — valid_track(source, track_id), те же правила, что у POST /api/downloads;
      * строка проходит проверки чтения (папка — одно имя внутри своего каталога, без выхода наружу);
      * имя папки оканчивается на «[источник-id]» этого трека (её создал загрузчик Wave);
      * в папке лежат аудиофайл строки и metadata.json (их пишет загрузчик любой версии Wave).
    Строка, указывающая, например, на C:\\Windows или на «Фото [2020-лето]», каталогом загрузок не становится.
    Доступность каталога проверяется один раз на каталог (недоступный сетевой диск отвечает долго). Строки не удаляются.
    """
    roots: dict[str, Path] = {}
    available: dict[str | None, bool] = {}
    for row in _q("SELECT * FROM downloads"):
        root = row["root"]
        if not valid_track(row["source"], row["track_id"]):
            continue
        if root not in available:
            try:
                available[root] = root is None or (Path(root).is_absolute() and os.path.isdir(root))
            except (OSError, ValueError):
                available[root] = False
        if not available[root]:
            continue
        folder = _row_dir(row)
        if folder is None or not owns_folder(row) or str(folder.parent) in roots:
            continue
        audio = _row_file(row, "audio")
        try:
            if audio is None or not audio.is_file() or not (folder / "metadata.json").is_file():
                continue
        except OSError:                          # диск недоступен
            continue
        roots[str(folder.parent)] = folder.parent
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
    folder = _row_dir(row)                     # с S13 None и для папки, созданной не загрузчиком Wave для этого трека
    if folder is None:
        _q("DELETE FROM downloads WHERE key = ?", (row["key"],))
        raise FolderKept(None)
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