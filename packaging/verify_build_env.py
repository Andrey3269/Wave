"""
Проверка окружения сборки Wave (S4): Python 3.12 x64 и пакеты ровно из packaging/requirements-build.lock.

    python packaging/verify_build_env.py            # build_windows.bat и CI после установки лока

Что проверяется:
  * интерпретатор — CPython 3.12, 64-битный (под него собран хэш-лок, см. requirements-build.txt);
  * `pip freeze --all` совпадает с локом пакет в пакет: ни лишних, ни недостающих, ни других версий
    (сам pip — инструмент установки, не сравнивается).
Код возврата 0 — окружение воспроизводимо; 1 — расхождение (список выводится); 2 — неверный Python.
"""
from __future__ import annotations

import argparse
import re
import struct
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCK = ROOT / "packaging" / "requirements-build.lock"
PYTHON = (3, 12)
IGNORED = {"pip"}


def norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def lock_pins(path: Path = LOCK) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    return {norm(m.group(1)): m.group(2) for m in re.finditer(r"^([A-Za-z0-9_.\-]+)==(\S+)", text, re.M)}


def freeze_pins(lines: list[str]) -> dict[str, str]:
    pins = {}
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, sep, ver = line.partition("==")
        pins[norm(name)] = ver if sep else line            # «name @ file://...» и т.п. — заведомо не совпадёт
    return {k: v for k, v in pins.items() if k not in IGNORED}


def differences(lock: dict[str, str], env: dict[str, str]) -> dict[str, tuple[str | None, str | None]]:
    return {n: (lock.get(n), env.get(n)) for n in sorted(set(lock) | set(env)) if lock.get(n) != env.get(n)}


def python_problem(version_info=None, bits: int | None = None, impl: str | None = None) -> str | None:
    """Почему этот интерпретатор не годится для сборки (None — годится)."""
    version_info = version_info or sys.version_info
    bits = bits or struct.calcsize("P") * 8
    impl = impl or sys.implementation.name
    if tuple(version_info[:2]) != PYTHON or bits != 64 or impl != "cpython":
        return (f"сборка требует CPython {PYTHON[0]}.{PYTHON[1]} 64-бит, запущен "
                f"{impl} {'.'.join(map(str, version_info[:3]))} {bits}-бит")
    return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--freeze-file", help="вывод pip freeze из файла (для тестов) вместо текущего окружения")
    ap.add_argument("--skip-python-check", action="store_true", help="не проверять версию Python (для тестов)")
    args = ap.parse_args(argv)

    if not args.skip_python_check and (problem := python_problem()):
        print(f"ОШИБКА: {problem}")
        return 2

    if args.freeze_file:
        lines = Path(args.freeze_file).read_text(encoding="utf-8").splitlines()
    else:
        out = subprocess.run([sys.executable, "-m", "pip", "freeze", "--all"], capture_output=True, text=True,
                             check=True).stdout
        lines = out.splitlines()
    lock = lock_pins()
    diff = differences(lock, freeze_pins(lines))
    if diff:
        print("ОШИБКА: окружение сборки отличается от packaging/requirements-build.lock (пакет: лок -> окружение):")
        for name, (want, have) in diff.items():
            print(f"  {name}: {want or '—'} -> {have or '—'}")
        return 1
    print(f"Окружение сборки: Python {sys.version.split()[0]}, {len(lock)} пакетов ровно как в requirements-build.lock")
    return 0


if __name__ == "__main__":
    sys.exit(main())
