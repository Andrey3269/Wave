"""Каталоги Wave. Данные приложения и пользовательские загрузки хранятся раздельно."""
from __future__ import annotations

import os
import sys
from pathlib import Path


def resource_dir() -> Path:
    """
    Корень ресурсов приложения (папка frontend). Не зависит от рабочей директории:
    в сборке PyInstaller это каталог распаковки (_MEIPASS), в разработке корень репозитория.
    """
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent.parent


def data_dir() -> Path:
    """База данных, логи, кэш. На Windows: %LOCALAPPDATA%\\Wave (туда есть право записи)."""
    if env := os.getenv("WAVE_DATA_DIR"):
        p = Path(env)
    elif sys.platform == "win32":
        p = Path(os.getenv("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "Wave"
    else:
        p = Path.home() / ".local" / "share" / "wave"
    p.mkdir(parents=True, exist_ok=True)
    return p


def local_music_dir() -> Path:
    """
    Локальная музыка (S15): копии аудиофайлов, импортированных пользователем, — <data>/local. Файлы называются по id
    трека (backend/localmusic.py), пользовательских путей здесь нет. Создаётся при обращении.
    """
    p = data_dir() / "local"
    p.mkdir(parents=True, exist_ok=True)
    return p


_downloads_override: Path | None = None


def set_downloads_dir(p: Path | None) -> None:
    """Папка, выбранная пользователем в настройках (переменная WAVE_DOWNLOADS_DIR приоритетнее)."""
    global _downloads_override
    _downloads_override = p


def default_downloads_dir() -> Path:
    """Папка загрузок по умолчанию (без создания): ~/Downloads/Wave."""
    return Path.home() / "Downloads" / "Wave"


def current_downloads_dir() -> Path:
    """
    Текущая папка загрузок без создания (S13): для чтения и показа. Недоступная папка (отключённый диск, путь
    через файл) не роняет чтение индекса и настроек — её создаёт только downloads_dir() при записи.
    """
    return Path(os.getenv("WAVE_DOWNLOADS_DIR") or _downloads_override or default_downloads_dir())


def downloads_dir() -> Path:
    """Скачанные треки: ~/Downloads/Wave по умолчанию, либо папка из настроек. Создаётся при обращении (для записи)."""
    p = current_downloads_dir()
    p.mkdir(parents=True, exist_ok=True)
    return p