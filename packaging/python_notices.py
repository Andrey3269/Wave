"""
Лицензии Python-пакетов, которые PyInstaller действительно положил в сборку (S4). Вызывается из wave.spec.

Какие пакеты попали в сборку, определяется по результату анализа PyInstaller (a.pure — модули, a.binaries и
a.datas — файлы): верхний уровень имени модуля или пути сопоставляется с дистрибутивом через
importlib.metadata.packages_distributions(), а для файлов вне модулей — по списку файлов дистрибутива.
Инструменты сборки (PyInstaller, Pillow, setuptools и т.п.) в сборку не попадают и в перечень не входят.

Для каждого найденного дистрибутива в сборку копируются его файлы лицензий из метаданных (LICENSE*,
COPYING*, NOTICE*, AUTHORS* в .dist-info, в том числе .dist-info/licenses/ по PEP 639) в
licenses/python/<имя>-<версия>/, а в licenses/python/PACKAGES.txt пишется перечень: имя, версия, лицензия
из метаданных и какие файлы приложены. Сам Python: LICENSE.txt установки Python (в нём и уведомления
о встроенных в Python библиотеках: OpenSSL, libffi, SQLite, zlib, bzip2, xz, expat, mpdecimal и др.).
"""
from __future__ import annotations

import re
import sys
from importlib import metadata
from pathlib import Path, PurePosixPath

LICENSE_NAME = re.compile(r"^(licen[cs]e|copying|notice|authors?|copyright)([-_.].*)?$", re.I)
OUT_DIR = "licenses/python"            # внутри _internal


def top_level(name: str) -> str:
    return re.split(r"[./\\]", name, maxsplit=1)[0]


def distributions_for(module_names: list[str], file_names: list[str]) -> dict[str, metadata.Distribution]:
    """Дистрибутивы, к которым относятся модули и файлы сборки: {нормализованное имя: Distribution}."""
    by_top = metadata.packages_distributions()
    names: set[str] = set()
    for mod in module_names:
        names.update(by_top.get(top_level(mod), []))
    tops = {top_level(f.replace("\\", "/")) for f in file_names}
    for top in tops:
        names.update(by_top.get(top, []))
    found: dict[str, metadata.Distribution] = {}
    for n in names:
        try:
            d = metadata.distribution(n)
        except metadata.PackageNotFoundError:
            continue
        found[norm(d.metadata["Name"])] = d
    return found


def norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def license_expression(d: metadata.Distribution) -> str:
    m = d.metadata
    expr = m.get("License-Expression")
    if expr:
        return expr.strip()
    classifiers = [c.split("::")[-1].strip() for c in m.get_all("Classifier") or [] if c.startswith("License ::")]
    if classifiers:
        return "; ".join(classifiers)
    lic = (m.get("License") or "").strip()
    return lic.splitlines()[0][:80] if lic else "(не указана в метаданных)"


def license_files(d: metadata.Distribution) -> list[Path]:
    out = []
    for f in d.files or []:
        p = PurePosixPath(str(f))
        in_meta = p.parts and p.parts[0].endswith((".dist-info", ".egg-info"))
        if in_meta and (LICENSE_NAME.match(p.name) or "licenses" in p.parts[1:-1]):
            path = Path(d.locate_file(f))
            if path.is_file():
                out.append(path)
    return sorted(set(out))


def python_license() -> Path | None:
    for cand in (Path(sys.base_prefix) / "LICENSE.txt", Path(sys.base_prefix) / "LICENSE"):
        if cand.is_file():
            return cand
    return None


def collect(module_names: list[str], file_names: list[str], workdir: Path,
            extra: list[tuple[str, str, str]] = ()) -> list[tuple[str, str, str]]:
    """
    Возвращает элементы TOC (путь в сборке, исходный файл, 'DATA') для COLLECT и пишет PACKAGES.txt в workdir.
    extra — компоненты не из pip (имя, версия, примечание), например SDK внутри пакетов.
    """
    workdir = Path(workdir)
    dists = distributions_for(module_names, file_names)
    toc: list[tuple[str, str, str]] = []
    lines = ["Wave / «ВОЛНА»: Python и Python-пакеты в этой сборке",
             "=" * 60, "",
             "Перечень сформирован при сборке (packaging/python_notices.py) по фактическому составу сборки",
             "PyInstaller. Тексты лицензий — в папках рядом: <имя>-<версия>/.", ""]
    py = python_license()
    ver = ".".join(map(str, sys.version_info[:3]))
    lines.append(f"Python {ver} — PSF License; уведомления встроенных библиотек — в том же файле"
                 + (f": python-{ver}/{py.name}" if py else ": (LICENSE.txt установки Python не найден)"))
    if py:
        toc.append((f"{OUT_DIR}/python-{ver}/{py.name}", str(py), "DATA"))
    no_text = []
    for key in sorted(dists):
        d = dists[key]
        name, version = d.metadata["Name"], d.version
        files = license_files(d)
        folder = f"{OUT_DIR}/{norm(name)}-{version}"
        for f in files:
            toc.append((f"{folder}/{f.name}", str(f), "DATA"))
        shown = ", ".join(f.name for f in files) if files else "ФАЙЛОВ ЛИЦЕНЗИИ В ПАКЕТЕ НЕТ"
        if not files:
            no_text.append(name)
        lines.append(f"{name} {version} — {license_expression(d)}; файлы: {shown}")
    for name, version, note in extra:
        lines.append(f"{name} {version} — {note}")
    if no_text:
        lines += ["", "Без файла лицензии в пакете (требует ручной проверки): " + ", ".join(no_text)]
    workdir.mkdir(parents=True, exist_ok=True)
    summary = workdir / "PACKAGES.txt"
    summary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    toc.append((f"{OUT_DIR}/PACKAGES.txt", str(summary), "DATA"))
    return toc
