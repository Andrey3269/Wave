"""
Согласованность зависимостей воспроизведения YouTube (S3).

yt-dlp и yt-dlp-ejs обновляются только вместе: yt-dlp принимает скрипты решателя из пакета
yt-dlp-ejs, только если их версия совпадает с той, под которую он собран (vendor._info.VERSION).
Иначе он молча откатывается на встроенные скрипты, и сборка перестаёт соответствовать файлам
зависимостей. Эти тесты ловят расхождение до сборки.
"""
from __future__ import annotations

import re
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _pins(path: Path) -> dict[str, str]:
    """Точные закрепления `имя==версия` из файла требований (без комментариев и маркеров)."""
    pins = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        m = re.fullmatch(r"([A-Za-z0-9_.\-\[\]]+)==([^\s;]+)", line)
        if m:
            pins[re.sub(r"\[.*\]", "", m.group(1)).lower().replace("_", "-")] = m.group(2)
    return pins


def _default_extra_ejs_requirement() -> str:
    """Версия yt-dlp-ejs, которую установленный yt-dlp объявляет в extra [default]."""
    for req in metadata.requires("yt-dlp") or []:
        m = re.match(r"yt-dlp-ejs\s*==\s*([^\s;]+)\s*;.*extra\s*==\s*['\"]default['\"]", req)
        if m:
            return m.group(1)
    raise AssertionError("yt-dlp не объявляет yt-dlp-ejs в extra [default]")


def test_ejs_version_matches_yt_dlp_default_extra():
    import yt_dlp_ejs

    assert yt_dlp_ejs.version == _default_extra_ejs_requirement()


def test_ejs_version_matches_yt_dlp_builtin_solver_version():
    """Совпадает с версией, под которую собран yt-dlp: иначе пакетные скрипты отвергаются."""
    import yt_dlp_ejs
    from yt_dlp.extractor.youtube.jsc._builtin.vendor import VERSION

    assert yt_dlp_ejs.version == VERSION


def test_requirements_pin_installed_versions():
    """requirements.txt и constraints.txt закрепляют ровно установленные версии yt-dlp и yt-dlp-ejs."""
    req = _pins(ROOT / "backend" / "requirements.txt")
    con = _pins(ROOT / "constraints.txt")
    for name in ("yt-dlp", "yt-dlp-ejs"):
        installed = metadata.version(name)
        assert req.get(name) == installed, f"backend/requirements.txt: {name}=={req.get(name)}, установлено {installed}"
        assert con.get(name) == installed, f"constraints.txt: {name}=={con.get(name)}, установлено {installed}"


def test_gpl_dependency_is_not_pulled_in():
    """mutagen (GPL-2.0-or-later) из extra [default] не нужен Wave и не должен попадать в сборку."""
    active = [line.split("#", 1)[0].strip()
              for line in (ROOT / "backend" / "requirements.txt").read_text(encoding="utf-8").splitlines()]
    assert not [line for line in active if "[default]" in line or line.startswith("mutagen")]
    assert "mutagen" not in _pins(ROOT / "constraints.txt")


# ---------------------------------------------------------------------------
# S4: воспроизводимая сборка — хэш-лок packaging/requirements-build.lock для Windows x64, Python 3.12
# ---------------------------------------------------------------------------
LOCK = ROOT / "packaging" / "requirements-build.lock"


def _lock_entries() -> dict[str, tuple[str, list[str]]]:
    """Пакеты лока: имя -> (версия, хэши). Каждый пакет закреплён точно и хотя бы одним sha256."""
    entries: dict[str, tuple[str, list[str]]] = {}
    current = None
    for raw in LOCK.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].rstrip().rstrip("\\").strip()
        if not line:
            continue
        m = re.fullmatch(r"([A-Za-z0-9_.\-]+)==([^\s;]+)", line)
        if m:
            current = m.group(1).lower().replace("_", "-")
            entries[current] = (m.group(2), [])
            continue
        h = re.fullmatch(r"--hash=sha256:([0-9a-f]{64})", line)
        assert h and current, f"непонятная строка лока: {raw!r}"
        entries[current][1].append(h.group(1))
    return entries


def test_build_lock_pins_everything_with_hashes():
    entries = _lock_entries()
    assert entries, "лок пуст"
    assert not [n for n, (_, hashes) in entries.items() if not hashes], "пакеты без хэша"


def test_build_lock_agrees_with_direct_build_pins_and_runtime_constraints():
    """Лок содержит ровно закреплённые версии: прямые зависимости сборки и общий набор constraints.txt."""
    entries = _lock_entries()
    build = _pins(ROOT / "packaging" / "requirements-build.txt")
    assert set(build) == {"pyinstaller", "pywebview", "pillow"}
    for name, ver in build.items():
        assert entries.get(name, (None,))[0] == ver, f"{name}: requirements-build {ver}, лок {entries.get(name)}"
    con = _pins(ROOT / "constraints.txt")
    mismatched = {n: (con[n], v) for n, (v, _) in entries.items() if n in con and con[n] != v}
    assert not mismatched, f"лок расходится с constraints.txt: {mismatched}"


def test_build_lock_covers_runtime_requirements():
    """Всё, что нужно приложению (backend/requirements.txt), есть в локе сборки."""
    entries = _lock_entries()
    names = set()
    for line in (ROOT / "backend" / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        m = re.match(r"([A-Za-z0-9_.\-]+)", line)
        if m:
            names.add(m.group(1).lower().replace("_", "-"))
    assert names and not names - set(entries), f"нет в локе: {names - set(entries)}"
    assert "mutagen" not in entries


def test_build_script_installs_from_the_hashed_lock():
    """build_windows.bat ставит зависимости сборки только из лока с проверкой хэшей; CI собирает этим же скриптом."""
    bat = (ROOT / "build_windows.bat").read_text(encoding="utf-8")
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "--require-hashes --no-deps -r packaging\\requirements-build.lock" in bat
    assert "call build_windows.bat" in ci
    assert "pip install -r backend/requirements.txt -r packaging/requirements-build.txt" not in ci
