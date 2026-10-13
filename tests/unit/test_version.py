"""Единый источник версии (S4): backend/version.py, его потребители и согласованность с упаковкой."""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

import version

ROOT = Path(__file__).resolve().parents[2]


def test_version_has_strict_three_part_format():
    assert re.fullmatch(r"\d+\.\d+\.\d+", version.__version__)
    assert version.version_tuple() == (*map(int, version.__version__.split(".")), 0)


@pytest.mark.parametrize("bad", ["", "1", "1.2", "1.2.3.4", "1.2.3-beta", "v1.2.3", "01.2.3", " 1.2.3", "1.2.70000"])
def test_bad_versions_are_rejected(bad):
    with pytest.raises(ValueError):
        version.version_tuple(bad)


def test_cli_prints_version_for_build_scripts():
    """build_windows.bat и CI получают версию командой `python backend/version.py`."""
    out = subprocess.run([sys.executable, str(ROOT / "backend" / "version.py")], capture_output=True, text=True,
                         check=True).stdout
    assert out.strip() == version.__version__


def test_product_metadata_is_present():
    for value in (version.PRODUCT_NAME, version.PUBLISHER, version.DESCRIPTION):
        assert value.strip() == value and value
    assert version.EXE_NAME == "Wave.exe"


def test_spec_takes_version_resource_from_version_module():
    """Версия Wave.exe не задаётся в spec вручную: только из backend/version.py."""
    spec = (ROOT / "packaging" / "wave.spec").read_text(encoding="utf-8")
    assert "app_version.version_tuple()" in spec and "version=VERSION_INFO" in spec
    assert version.__version__ not in spec


def test_no_other_hardcoded_app_version_in_backend():
    """Других литералов версии приложения в backend нет (раньше было FastAPI(version="0.1.0"))."""
    for path in (ROOT / "backend").glob("*.py"):
        if path.name == "version.py":
            continue
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"""version\s*=\s*["']\d+\.\d+""", text), path.name
