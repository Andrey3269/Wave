"""
Запуск браузерного e2e-теста на одноразовом профиле. Реальные данные пользователя не затрагиваются.

    python tests/run_e2e.py [--shots ПАПКА] [--keep] [--timeout СЕКУНДЫ]

Что делает:
  1. создаёт временные каталоги данных и загрузок с файлом-меткой .wave-test-profile;
  2. запускает backend (uvicorn) на свободном порту 127.0.0.1 с WAVE_DATA_DIR / WAVE_DOWNLOADS_DIR;
  3. ждёт /api/health и запускает tests/e2e_smoke.py;
  4. останавливает backend и удаляет временный профиль (с --keep оставляет его для разбора).

Коды выхода: 0 — тест прошёл; 1 — тест нашёл дефект; 3 — окружение не готово (BLOCKED):
нет Playwright/Chromium или backend не запустился.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
E2E = ROOT / "tests" / "e2e_smoke.py"
MARKER = ".wave-test-profile"          # тот же файл проверяет tests/e2e_smoke.py
EXIT_BLOCKED = 3
# Секреты пользователя (cookies, прокси) тестовому backend не нужны: весь интернет в тесте подменён
SCRUB_ENV = ("YTDLP_COOKIES", "YTDLP_PROXY", "WAVE_DATA_DIR", "WAVE_DOWNLOADS_DIR")


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_health(url: str, proc: subprocess.Popen, timeout: float) -> bool:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return False
        try:
            with opener.open(f"{url}/api/health", timeout=2) as r:
                if r.status == 200:
                    return True
        except OSError:
            pass
        time.sleep(0.25)
    return False


def stop(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(5)


def tail(path: Path, n: int = 40) -> str:
    try:
        return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-n:])
    except OSError:
        return "(журнал backend недоступен)"


def main() -> int:
    ap = argparse.ArgumentParser(description="e2e-тест Wave на одноразовом профиле")
    ap.add_argument("--shots", type=Path, help="папка для скриншотов (по умолчанию новая временная)")
    ap.add_argument("--keep", action="store_true", help="не удалять временный профиль после теста")
    ap.add_argument("--timeout", type=float, default=60.0, help="сколько ждать запуска backend, с")
    args = ap.parse_args()

    if importlib.util.find_spec("playwright") is None:
        print("BLOCKED: не установлен Playwright (pip install -r requirements-dev.txt -c constraints.txt; "
              "python -m playwright install chromium)", file=sys.stderr)
        return EXIT_BLOCKED

    root = Path(tempfile.mkdtemp(prefix="wave-e2e-"))
    data, downloads = root / "data", root / "downloads"
    for d in (data, downloads):
        d.mkdir()
        (d / MARKER).write_text("Каталог создан tests/run_e2e.py для тестов. Можно удалить.\n", encoding="utf-8")
    shots = args.shots or Path(tempfile.mkdtemp(prefix="wave-e2e-shots-"))
    log_path = root / "backend.log"

    env = {k: v for k, v in os.environ.items() if k not in SCRUB_ENV}
    env.update(WAVE_DATA_DIR=str(data), WAVE_DOWNLOADS_DIR=str(downloads),
               PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")

    port = free_port()
    url = f"http://127.0.0.1:{port}"
    cmd = [sys.executable, "-m", "uvicorn", "main:app", "--app-dir", str(BACKEND),
           "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"]
    print(f"Профиль теста: {root}")
    print(f"Backend: {url}")
    with open(log_path, "w", encoding="utf-8") as log:
        backend = subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        if not wait_health(url, backend, args.timeout):
            print(f"BLOCKED: backend не ответил на /api/health за {args.timeout:.0f} с. Журнал:\n{tail(log_path)}",
                  file=sys.stderr)
            return EXIT_BLOCKED
        result = subprocess.run([sys.executable, str(E2E), url, str(shots)], cwd=ROOT, env=env)
        if result.returncode == 2:              # защита профиля сработала: так быть не должно
            print("BLOCKED: e2e отказался работать с профилем (см. сообщение выше).", file=sys.stderr)
            return EXIT_BLOCKED
        if result.returncode != 0:
            print(f"\nFAIL: e2e завершился с кодом {result.returncode}. Журнал backend:\n{tail(log_path)}",
                  file=sys.stderr)
            return 1
        print(f"\nPASS. Скриншоты: {shots}")
        return 0
    finally:
        stop(backend)
        if args.keep:
            print(f"Временный профиль сохранён: {root}")
        else:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
