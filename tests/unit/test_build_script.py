"""
build_windows.bat и packaging/verify_build_env.py (S4) — без Windows.

Сам скрипт сборки целиком выполняется в CI на windows-latest (job installer). Здесь закреплено то,
что ломалось или легко сломать правкой: порядок шагов (каталоги до загрузок, диагностика до установщика),
единая версия, отказ от «любого Python 3», понятные ошибки и ненулевой код при сбое.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BAT = ROOT / "build_windows.bat"
VERIFY = ROOT / "packaging" / "verify_build_env.py"

sys.path.insert(0, str(ROOT / "packaging"))
import verify_build_env  # noqa: E402


@pytest.fixture(scope="module")
def bat() -> str:
    raw = BAT.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf"), "BOM ломает первую строку .bat"
    return raw.decode("utf-8")


def _code(text: str) -> str:
    """Текст без комментариев и echo (с теми же позициями): порядок шагов ищется только в командах."""
    return "\n".join(" " * len(ln) if ln.lstrip().lower().startswith(("rem ", "echo ")) else ln
                     for ln in text.split("\n"))


def _pos(text: str, needle: str) -> int:
    i = _code(text).find(needle)
    assert i >= 0, f"нет среди команд build_windows.bat: {needle}"
    return i


def test_steps_are_in_order(bat):
    steps = [
        'mkdir "%%d"',                                              # каталоги build\webview2, dist\installer
        "%BASE_PY% -m venv .venv-build",
        "--require-hashes --no-deps -r packaging\\requirements-build.lock",
        "packaging\\verify_build_env.py",
        "packaging\\fetch_runtime.py --platform windows-x86_64",
        "-m PyInstaller --noconfirm --clean packaging\\wave.spec",
        'start "" /wait "dist\\Wave\\Wave.exe" --diagnostics',
        "backend\\version.py",
        "packaging\\fetch_webview2.ps1",
        '"/DAppVersion=%WAVE_VERSION%"',
    ]
    positions = [_pos(bat, s) for s in steps]
    assert positions == sorted(positions), "шаги build_windows.bat идут не в том порядке"


def test_directories_are_created_before_downloads(bat):
    m = re.search(r"for %%d in \(([^)]*)\) do", bat)
    assert m and {"build", "build\\webview2", "dist\\installer"} <= set(m.group(1).split())
    assert _pos(bat, 'mkdir "%%d"') < _pos(bat, "fetch_webview2.ps1")


def test_no_fallback_to_another_python(bat):
    assert "py -3 " not in bat and 'set "BASE_PY=py -3.12"' in bat
    assert "sys.version_info[:2] == (3, 12)" in bat and "Python 3.12" in bat


def test_failures_return_nonzero_and_success_requires_the_file(bat):
    assert re.search(r":fail\r?\n(?:.*\r?\n){1,4}exit /b 1", bat)
    for cmd in ("PyInstaller", "fetch_runtime.py", "fetch_webview2.ps1", "verify_build_env.py", "installer\\wave.iss"):
        lines = [ln for ln in bat.splitlines() if cmd in ln and not ln.lstrip().lower().startswith(("rem", "echo"))]
        assert lines and all("|| goto :fail" in ln for ln in lines), cmd
    assert 'if not exist "%OUT%"' in bat and 'set "OUT=dist\\installer\\Wave-Setup-%WAVE_VERSION%.exe"' in bat
    assert _pos(bat, 'if not exist "%OUT%"') < _pos(bat, "exit /b 0")
    assert "if errorlevel 1 (" in bat[_pos(bat, "--diagnostics"):_pos(bat, "backend\\version.py")]


def test_iscc_is_checked_with_instructions(bat):
    assert "Inno Setup 6" in bat and "jrsoftware.org/isdl.php" in bat and 'findstr /c:"Inno Setup 6"' in bat
    for place in ("%ProgramFiles(x86)%\\Inno Setup 6\\ISCC.exe", "%LOCALAPPDATA%\\Programs\\Inno Setup 6\\ISCC.exe"):
        assert place in bat


def test_version_of_exe_is_compared_with_version_module(bat):
    assert "VersionInfo.ProductVersion" in bat and 'if not "%EXE_VERSION%"=="%WAVE_VERSION%"' in bat


def test_signing_is_optional(bat):
    assert "if defined WAVE_SIGN_COMMAND (" in bat
    unsigned = bat[_pos(bat, ") else ("):]
    assert '"%ISCC%" /Qp "/DAppVersion=%WAVE_VERSION%" installer\\wave.iss' in unsigned


def test_no_dangerous_cleanup(bat):
    """Удаляется только своё: окружение сборки, старый отчёт диагностики и старый установщик."""
    removed = re.findall(r"(?:rmdir|rd|del)\s+(?:/[sq]\s+)*\"([^\"]+)\"", bat, re.I)
    assert sorted(removed) == sorted([".venv-build", "build\\diagnostics.json", "%OUT%"])


# ---------------------------------------------------------------------------
# packaging/verify_build_env.py
# ---------------------------------------------------------------------------
def _run(freeze: str, tmp_path) -> subprocess.CompletedProcess:
    f = tmp_path / "freeze.txt"
    f.write_text(freeze, encoding="utf-8")
    return subprocess.run([sys.executable, str(VERIFY), "--freeze-file", str(f), "--skip-python-check"],
                          capture_output=True, text=True, encoding="utf-8")


def _lock_freeze() -> list[str]:
    return [f"{n}=={v}" for n, v in verify_build_env.lock_pins().items()]


def test_exact_environment_passes(tmp_path):
    p = _run("\n".join(_lock_freeze() + ["pip==25.0.1"]), tmp_path)
    assert p.returncode == 0, p.stdout


def test_extra_missing_or_other_version_fails(tmp_path):
    lines = _lock_freeze()
    changed = [ln.replace("pyinstaller==6.22.3", "pyinstaller==6.10.0") for ln in lines[1:]] + ["mutagen==1.47.0"]
    p = _run("\n".join(changed), tmp_path)
    assert p.returncode == 1
    assert "pyinstaller: 6.22.3 -> 6.10.0" in p.stdout and "mutagen: — -> 1.47.0" in p.stdout
    assert f"{lines[0].split('==')[0]}:" in p.stdout                        # недостающий пакет


def test_name_normalization():
    assert verify_build_env.norm("Typing_Extensions") == verify_build_env.norm("typing-extensions")
    assert verify_build_env.freeze_pins(["PyYAML==6.0.3", "pip==25"]) == {"pyyaml": "6.0.3"}


@pytest.mark.parametrize("info, bits, impl, ok", [
    ((3, 12, 10), 64, "cpython", True),
    ((3, 14, 3), 64, "cpython", False),                  # так S3-сборку однажды собрали на 3.14
    ((3, 11, 9), 64, "cpython", False),
    ((3, 12, 10), 32, "cpython", False),
    ((3, 12, 10), 64, "pypy", False),
])
def test_python_check(info, bits, impl, ok):
    problem = verify_build_env.python_problem(info, bits, impl)
    assert (problem is None) == ok
    if not ok:
        assert "CPython 3.12 64-бит" in problem
