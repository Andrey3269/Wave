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
