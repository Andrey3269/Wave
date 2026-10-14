"""
Все автоматические проверки репозитория одной командой.

    python tests/run_checks.py [--no-pytest] [--no-e2e]

Шаги:
  hygiene    — в git не отслеживаются базы, журналы, медиа, секреты, окружения, артефакты сборки;
  compileall — синтаксис Python (байткод пишется во временную папку, не в репозиторий);
  pyflakes   — статические ошибки Python;
  node       — синтаксис JavaScript фронтенда (node --check);
  jstest     — unit-тесты фронтенда без браузера: node --test tests/js (модель очереди, форматирование,
               граф ES-модулей). Нет node — BLOCKED; нет тестов или упал хотя бы один — FAIL;
  pytest     — unit- и API-тесты без сети и на временном профиле (python -m pytest, live выключены);
  e2e        — браузерный тест на одноразовом профиле (tests/run_e2e.py);
  worktree   — после всех шагов в рабочем каталоге не появилось новых неигнорируемых файлов.

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


JS_TESTS = ROOT / "tests" / "js"


def step_jstest() -> tuple[str, str]:
    node = shutil.which("node")
    if not node:
        return "BLOCKED", "node не найден"
    files = sorted(JS_TESTS.glob("*.test.mjs")) if JS_TESTS.is_dir() else []
    if not files:                          # пустой набор не выдаём за успех
        return "FAIL", f"нет тестов {JS_TESTS.relative_to(ROOT)}/*.test.mjs"
    # Явный список файлов: одинаково работает во всех версиях Node 20+ (разбор каталога в --test менялся).
    # TAP — стабильный формат для разбора. Файл без тестов Node показывает одним «пройденным» тестом с именем
    # самого файла — такой файл считаем ошибкой, а не успехом.
    names = [str(f.relative_to(ROOT)) for f in files]
    code, out = run([node, "--test", "--test-reporter=tap", *names])
    if code != 0:
        return "FAIL", out[-4000:]
    if not re.search(r"^# fail 0$", out, re.M):
        return "FAIL", "node --test не сообщил итог:\n" + out[-2000:]
    top = [t.strip() for t in re.findall(r"^# Subtest: (.+)$", out, re.M)]
    # Имя «теста-файла» Node пишет по-разному (относительный или полный путь; в TAP на Windows `\` экранируется),
    # поэтому сравниваем по имени файла в конце строки
    empty = [str(f.relative_to(ROOT)) for f in files if any(t == f.name or t.endswith(("/" + f.name, "\\" + f.name)) for t in top)]
    if empty:
        return "FAIL", "в файлах нет ни одного теста: " + ", ".join(empty)
    if not top:
        return "FAIL", "node --test не нашёл тестов:\n" + out[-2000:]
    return "PASS", f"{len(files)} файл(ов), тестов: {len(top)}"


def step_pytest() -> tuple[str, str]:
    if importlib.util.find_spec("pytest") is None:
        return "BLOCKED", "pytest не установлен (requirements-dev.txt)"
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    code = subprocess.run([sys.executable, "-m", "pytest"], cwd=ROOT, env=env).returncode
    return ("PASS", "") if code == 0 else ("FAIL", f"код выхода {code}")


def git_status() -> str | None:
    if not shutil.which("git"):
        return None
    code, out = run(["git", "status", "--porcelain", "--untracked-files=all"])
    return out if code == 0 else None


def step_worktree(before: str | None) -> tuple[str, str]:
    after = git_status()
    if before is None or after is None:
        return "BLOCKED", "git status недоступен"
    new = sorted(set(after.splitlines()) - set(before.splitlines()))
    return ("FAIL", "проверки изменили рабочий каталог: " + "; ".join(new)) if new else ("PASS", "")


def step_e2e() -> tuple[str, str]:
    code = subprocess.run([sys.executable, str(ROOT / "tests" / "run_e2e.py")], cwd=ROOT).returncode
    return {0: ("PASS", ""), 3: ("BLOCKED", "см. вывод выше")}.get(code, ("FAIL", f"код выхода {code}"))


def main() -> int:
    ap = argparse.ArgumentParser(description="Проверки Wave")
    ap.add_argument("--no-pytest", action="store_true", help="пропустить unit- и API-тесты (NOT RUN)")
    ap.add_argument("--no-e2e", action="store_true", help="пропустить браузерный тест (NOT RUN)")
    args = ap.parse_args()

    before = git_status()
    steps = [("hygiene", step_hygiene), ("compileall", step_compileall), ("pyflakes", step_pyflakes),
             ("node", step_node), ("jstest", step_jstest)]
    results = []

    def record(name: str, status: str, note: str) -> None:
        results.append((name, status, note))
        print(f"[{status}] {name}" + (f": {note}" if note else ""), flush=True)

    for name, fn in steps:
        record(name, *fn())
    for name, skip, fn in (("pytest", args.no_pytest, step_pytest), ("e2e", args.no_e2e, step_e2e)):
        if skip:
            record(name, "NOT RUN", f"--no-{name}")
        else:
            print(f"[....] {name}", flush=True)
            record(name, *fn())
    record("worktree", *step_worktree(before))

    print("\nИтог:")
    for name, status, _ in results:
        print(f"  {status:<8} {name}")
    statuses = {s for _, s, _ in results}
    return 1 if "FAIL" in statuses else 3 if "BLOCKED" in statuses else 0


if __name__ == "__main__":
    sys.exit(main())
