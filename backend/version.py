"""
Единственный источник версии приложения Wave / «ВОЛНА» (S4).

Отсюда версию берут:
  * backend: FastAPI (`/openapi.json`), `GET /api/health`, `GET /api/diagnostics`, `Wave.exe --diagnostics`;
  * packaging/wave.spec: version-resource файла `Wave.exe` (свойства файла в Проводнике);
  * build_windows.bat и CI: `/DAppVersion=<версия>` для Inno Setup и имя `Wave-Setup-<версия>.exe`
    (команда `python backend/version.py` печатает версию);
  * installer/wave.iss: при компиляции сверяет `/DAppVersion` с версией внутри собранного `Wave.exe`
    и останавливается при расхождении.

Формат — три неотрицательных целых числа `MAJOR.MINOR.PATCH` без суффиксов: так версия однозначно
переводится в четырёхчастную версию Windows (`MAJOR.MINOR.PATCH.0`), которую показывают свойства
файла и «Установленные приложения». Меняется отдельным коммитом при подготовке выпуска.
"""
from __future__ import annotations

import re

__version__ = "0.1.0"

# Сведения о продукте для version-resource `Wave.exe`. Установщик (installer/wave.iss) читает их
# из собранного `Wave.exe`, поэтому название, издатель и описание задаются только здесь.
PRODUCT_NAME = "ВОЛНА (Wave)"
PUBLISHER = "Andrey3269"                    # владелец репозитория github.com/Andrey3269/Wave
DESCRIPTION = "ВОЛНА — музыкальный плеер для Windows"
EXE_NAME = "Wave.exe"

_FORMAT = re.compile(r"(0|[1-9]\d{0,4})\.(0|[1-9]\d{0,4})\.(0|[1-9]\d{0,4})")


def version_tuple(version: str = __version__) -> tuple[int, int, int, int]:
    """Четырёхчастная версия Windows: "0.1.0" -> (0, 1, 0, 0). Неверный формат — ValueError."""
    m = _FORMAT.fullmatch(version)
    if not m:
        raise ValueError(f"Версия приложения должна иметь вид MAJOR.MINOR.PATCH, получено {version!r}")
    parts = tuple(int(p) for p in m.groups())
    if any(p > 65535 for p in parts):                 # каждая часть версии Windows — 16 бит
        raise ValueError(f"Часть версии больше 65535: {version!r}")
    return (*parts, 0)


if __name__ == "__main__":                            # python backend/version.py -> 0.1.0
    version_tuple()                                   # неверный формат — ошибка и ненулевой код
    print(__version__)
