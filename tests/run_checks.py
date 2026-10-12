"""
Все автоматические проверки репозитория одной командой.

    python tests/run_checks.py [--no-e2e]

Шаги:
  hygiene    — в git не отслеживаются базы, журналы, медиа, секреты, окружения, артефакты сборки;
  compileall — синтаксис Python (байткод пишется во временную папку, не в репозиторий);
  pyflakes   — статические ошибки Python;
  node       — синтаксис JavaScript фронтенда (node --check);
  e2e        — браузерный тест на одноразовом профиле (tests/run_e2e.py).

Статусы: PASS — выполнено и прошло; FAIL — выполнено и нашло дефект; BLOCKED — нет нужного
инструмента; NOT RUN — шаг пропущен по флагу. Код выхода: 1 при любом FAIL, 3 при BLOCKED
без FAIL, иначе 0.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY_DIRS = ["backend", "packaging", "tests"]

# Что не должно отслеживаться git (дублирует .gitignore на случай `git add -f`)
FORBIDDEN = re.compile(
    r"(^|/)(__pycache__|\.venv[^/]*|venv|node_modules|build|dist)/"
    r"|\.(pyc|pyo|db|db-journal|db-wal|db-shm|sqlite3?|log|pem|key|pfx|m4a|mp3|webm|ogg|opus|wav)$"
    r"|(^|/)\.env(\.[^/]*)?$"
    r"|(^|/)cookies[^/]*\.txt$",
    re.IGNORECASE,
)


def run(cmd: list[str], env: dict | None = None) -> tuple[int, str]:
    p = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
    return p.returncode, (p.stdout + p.stderr).strip()


def step_hygiene() -> tuple[str, str]:
    if not shutil.which("git"):
        return "BLOCKED", "git не найден"
    code, out = run(["git", "ls-files"])
    if code != 0:
        return "BLOCKED", out or "git ls-files не выполнился"
    bad = [f for f in out.splitlines() if FORBIDDEN.search(f)]
    return ("FAIL", "отслеживаются: " + ", ".join(bad)) if bad else ("PASS", f"{len(out.splitlines())} файлов проверено")


def step_compileall() -> tuple[str, str]:
    with tempfile.TemporaryDirectory(prefix="wave-pycache-") as cache:
        env = {**os.environ, "PYTHONPYCACHEPREFIX": cache}
        code, out = run([sys.executable, "-m", "compileall", "-q", *PY_DIRS], env=env)
    return ("PASS", "") if code == 0 else ("FAIL", out)


def step_pyflakes() -> tuple[str, str]:
    if importlib.util.find_spec("pyflakes") is None:
        return "BLOCKED", "pyflakes не установлен (requirements-dev.txt)"
    code, out = run([sys.executable, "-m", "pyflakes", *PY_DIRS], env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    return ("PASS", "") if code == 0 else ("FAIL", out)


def step_node() -> tuple[str, str]:
    node = shutil.which("node")
    if not node:
        return "BLOCKED", "node не найден"
    files = sorted(str(p.relative_to(ROOT)) for p in (ROOT / "frontend").rglob("*.js"))
    errors = []
    for f in files:
        code, out = run([node, "--check", f])
        if code != 0:
            errors.append(f"{f}: {out}")
    return ("FAIL", "\n".join(errors)) if errors else ("PASS", f"{len(files)} файл(ов)")


def step_e2e() -> tuple[str, str]:
    code = subprocess.run([sys.executable, str(ROOT / "tests" / "run_e2e.py")], cwd=ROOT).returncode
    return {0: ("PASS", ""), 3: ("BLOCKED", "см. вывод выше")}.get(code, ("FAIL", f"код выхода {code}"))


def main() -> int:
    ap = argparse.ArgumentParser(description="Проверки Wave")
    ap.add_argument("--no-e2e", action="store_true", help="пропустить браузерный тест (NOT RUN)")
    args = ap.parse_args()

    steps = [("hygiene", step_hygiene), ("compileall", step_compileall), ("pyflakes", step_pyflakes),
             ("node", step_node)]
    results = []
    for name, fn in steps:
        status, note = fn()
        results.append((name, status, note))
        print(f"[{status}] {name}" + (f": {note}" if note else ""), flush=True)
    if args.no_e2e:
        results.append(("e2e", "NOT RUN", "--no-e2e"))
    else:
        print("[....] e2e", flush=True)
        status, note = step_e2e()
        results.append(("e2e", status, note))

    print("\nИтог:")
    for name, status, _ in results:
        print(f"  {status:<8} {name}")
    statuses = {s for _, s, _ in results}
    return 1 if "FAIL" in statuses else 3 if "BLOCKED" in statuses else 0


if __name__ == "__main__":
    sys.exit(main())
