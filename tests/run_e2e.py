"""
Запуск браузерного e2e-теста на одноразовом профиле. Реальные данные пользователя не затрагиваются.

    python tests/run_e2e.py [--shots ПАПКА] [--keep] [--timeout СЕКУНДЫ]

Что делает:
  1. создаёт временные каталоги данных и загрузок с файлом-меткой .wave-test-profile;
  2. запускает backend (uvicorn) на свободном порту 127.0.0.1 с WAVE_DATA_DIR / WAVE_DOWNLOADS_DIR;
  3. ждёт /api/health и запускает tests/e2e_smoke.py;
  4. на том же порту — tests/e2e_settings.py set (настройки интерфейса через UI);
  5. перезапускает backend на ДРУГОМ порту с тем же профилем — tests/e2e_settings.py verify (находка H3:
     в WebView другой порт = другой localStorage, настройки должны прийти из базы);
  6. отдельный чистый профиль — tests/e2e_settings.py migrate (однократный перенос из localStorage);
  7. останавливает backend и удаляет временные профили (с --keep оставляет их для разбора).

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
E2E_SETTINGS = ROOT / "tests" / "e2e_settings.py"
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


def make_profile(root: Path) -> tuple[Path, Path]:
    data, downloads = root / "data", root / "downloads"
    for d in (data, downloads):
        d.mkdir(parents=True)
        (d / MARKER).write_text("Каталог создан tests/run_e2e.py для тестов. Можно удалить.\n", encoding="utf-8")
    return data, downloads


class Backend:
    """uvicorn в отдельном процессе на свободном порту, с указанным профилем."""

    def __init__(self, data: Path, downloads: Path, log_path: Path, avoid: set[int] = frozenset()):
        self.env = {k: v for k, v in os.environ.items() if k not in SCRUB_ENV}
        self.env.update(WAVE_DATA_DIR=str(data), WAVE_DOWNLOADS_DIR=str(downloads),
                        PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
        self.port = free_port()
        while self.port in avoid:                 # перезапуск должен прийти на другой порт (другой origin)
            self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.log_path = log_path
        cmd = [sys.executable, "-m", "uvicorn", "main:app", "--app-dir", str(BACKEND),
               "--host", "127.0.0.1", "--port", str(self.port), "--log-level", "warning"]
        with open(log_path, "a", encoding="utf-8") as log:
            self.proc = subprocess.Popen(cmd, cwd=ROOT, env=self.env, stdout=log, stderr=subprocess.STDOUT)

    def ready(self, timeout: float) -> bool:
        return wait_health(self.url, self.proc, timeout)

    def run(self, script: Path, *args: str) -> int:
        return subprocess.run([sys.executable, str(script), *args], cwd=ROOT, env=self.env).returncode

    def stop(self) -> None:
        stop(self.proc)


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
    data, downloads = make_profile(root)
    shots = args.shots or Path(tempfile.mkdtemp(prefix="wave-e2e-shots-"))
    log_path = root / "backend.log"
    expect = root / "settings-expect.json"
    print(f"Профиль теста: {root}")
    backend: Backend | None = None

    def phase(title: str, b: Backend, script: Path, *script_args: str) -> int:
        """0 — прошло; иначе код выхода main()."""
        print(f"\n=== {title}: {b.url}")
        code = b.run(script, *script_args)
        if code == 2:                              # защита профиля сработала: так быть не должно
            print("BLOCKED: e2e отказался работать с профилем (см. сообщение выше).", file=sys.stderr)
            return EXIT_BLOCKED
        if code != 0:
            print(f"\nFAIL: {script.name} {' '.join(script_args[:1])} завершился с кодом {code}. "
                  f"Журнал backend:\n{tail(b.log_path)}", file=sys.stderr)
            return 1
        return 0

    def boot(d: Path, dl: Path, log: Path, avoid: set[int] = frozenset()) -> Backend | None:
        b = Backend(d, dl, log, avoid)
        if not b.ready(args.timeout):
            print(f"BLOCKED: backend не ответил на /api/health за {args.timeout:.0f} с. Журнал:\n{tail(log)}",
                  file=sys.stderr)
            b.stop()
            return None
        return b

    try:
        backend = boot(data, downloads, log_path)
        if backend is None:
            return EXIT_BLOCKED
        port_a = backend.port
        for title, script, extra in (("Smoke", E2E, (str(shots),)),
                                     ("Настройки: порт A", E2E_SETTINGS, ("set",))):
            a = (backend.url, *extra) if script is E2E else (extra[0], backend.url, str(expect))
            if (code := phase(title, backend, script, *a)):
                return code
        backend.stop()

        backend = boot(data, downloads, log_path, avoid={port_a})
        if backend is None:
            return EXIT_BLOCKED
        if backend.port == port_a:
            print("FAIL: перезапуск пришёлся на тот же порт — проверка смены origin невозможна.", file=sys.stderr)
            return 1
        if (code := phase(f"Настройки: перезапуск на порту B (A был {port_a})", backend, E2E_SETTINGS,
                          "verify", backend.url, str(expect))):
            return code
        backend.stop()

        d2, dl2 = make_profile(root / "fresh")
        backend = boot(d2, dl2, root / "backend-fresh.log")
        if backend is None:
            return EXIT_BLOCKED
        if (code := phase("Настройки: перенос из localStorage", backend, E2E_SETTINGS, "migrate", backend.url)):
            return code
        print(f"\nPASS. Скриншоты: {shots}")
        return 0
    finally:
        if backend is not None:
            backend.stop()
        if args.keep:
            print(f"Временный профиль сохранён: {root}")
        else:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
