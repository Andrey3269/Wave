"""
Wave Desktop: окно (pywebview / Microsoft Edge WebView2) + встроенный локальный backend.

Порядок запуска:
    окно с заставкой -> backend (FastAPI/uvicorn в потоке) на свободном порту 127.0.0.1
    -> проверка /api/health -> окно загружает интерфейс
Порядок закрытия:
    закрыли окно -> остановка backend -> завершение дочерних процессов -> выход.

Дочерние процессы (находка L8): yt-dlp запускает JS-runtime Deno (runtime/deno/deno.exe) для задач плеера
YouTube. Они учитываются в jsruntime.GUARD: без окна консоли, не дольше jsruntime.PROCESS_TIMEOUT, завершаются
при остановке backend и при выходе; на Windows ещё и состоят в Job Object с KILL_ON_JOB_CLOSE, поэтому
не переживают Wave даже при аварийном завершении.

Запуск в разработке: python backend/launcher.py (нужен pywebview).
"""
from __future__ import annotations

import argparse
import base64
import ctypes
import json
import logging
import os
import socket
import sys
import threading
import time
import urllib.request

# Оконное приложение (console=False) запускается без stdout/stderr, а библиотеки в них пишут.
_NO_STDOUT = sys.stdout is None          # для --diagnostics: вывод придётся искать отдельно (_cli_stream)
for _name in ("stdout", "stderr"):
    if getattr(sys, _name) is None:
        setattr(sys, _name, open(os.devnull, "w", encoding="utf-8"))

import applog  # noqa: E402
import jsruntime  # noqa: E402
import paths  # noqa: E402

APP_NAME = "Wave"
WEBVIEW2_GUID = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
WEBVIEW2_URL = "https://go.microsoft.com/fwlink/p/?LinkId=2124703"
HEALTH_TIMEOUT = 40.0       # сколько ждём первый ответ backend (импорт yt-dlp при холодном старте долгий)
WATCH_INTERVAL = 2.0        # как часто проверяем, что backend жив
MAX_RESTARTS = 2
IS_WIN = sys.platform == "win32"

log = logging.getLogger("wave.desktop")


# ---------------------------------------------------------------------------
# Страницы внутри окна (заставка и ошибка): без внешних файлов и запросов
# ---------------------------------------------------------------------------
def _page(body: str) -> str:
    return (
        "<!doctype html><html><head><meta charset='utf-8'><style>"
        "body{margin:0;height:100vh;display:grid;place-items:center;background:#141416;color:#F5F5F7;"
        "font:16px 'Segoe UI',system-ui,sans-serif;text-align:center}"
        ".bars{display:flex;gap:6px;height:48px;align-items:center;justify-content:center;margin-bottom:16px}"
        ".bars i{width:6px;height:12px;background:#FF6A4D;border-radius:3px;animation:w 1s ease-in-out infinite}"
        ".bars i:nth-child(2){animation-delay:.1s}.bars i:nth-child(3){animation-delay:.2s}"
        ".bars i:nth-child(4){animation-delay:.3s}.bars i:nth-child(5){animation-delay:.4s}"
        "@keyframes w{0%,100%{height:12px}50%{height:44px}}"
        "p{margin:6px 24px;color:#9A9AA3;max-width:520px;line-height:1.5}h1{margin:0 0 8px;font-size:22px}"
        "@media (prefers-color-scheme:light){body{background:#FFFFFF;color:#17171A}}"
        "@media (prefers-reduced-motion:reduce){.bars i{animation:none}}"
        f"</style></head><body><div>{body}</div></body></html>"
    )


def splash() -> str:
    """Заставка с логотипом (берётся из frontend/assets, внутрь страницы вшивается как data-URI)."""
    try:
        raw = (paths.resource_dir() / "frontend" / "assets" / "wave_brand.svg").read_bytes()
        logo = f"<img src='data:image/svg+xml;base64,{base64.b64encode(raw).decode()}' width='160' alt='' style='margin-bottom:12px'>"
    except OSError:
        logo = "<div class='bars'><i></i><i></i><i></i><i></i><i></i></div><h1>Wave</h1>"
    return _page(f"{logo}<p>Запускаем…</p>")


def error_page(title: str, text: str) -> str:
    return _page(f"<h1>{title}</h1><p>{text}</p><p>Подробности записаны в журнал: "
                 f"<br>{paths.data_dir() / 'logs' / 'wave.log'}</p>")


def _message(title: str, text: str) -> None:
    if IS_WIN:
        ctypes.windll.user32.MessageBoxW(0, text, title, 0x10)   # MB_ICONERROR
    else:
        print(f"{title}: {text}", file=sys.__stderr__)


# ---------------------------------------------------------------------------
# Проверки окружения Windows
# ---------------------------------------------------------------------------
def webview2_installed() -> bool:
    """Есть ли Microsoft Edge WebView2 Runtime. Без него окно с современным интерфейсом не откроется."""
    if not IS_WIN:
        return True
    import winreg

    locations = [
        (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_GUID}"),
        (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_GUID}"),
        (winreg.HKEY_CURRENT_USER, rf"Software\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_GUID}"),
    ]
    for hive, path in locations:
        try:
            with winreg.OpenKey(hive, path) as key:
                version, _ = winreg.QueryValueEx(key, "pv")
                if version and version != "0.0.0.0":
                    return True
        except OSError:
            continue
    return False


_mutex = None


def single_instance() -> bool:
    """Один запущенный Wave на пользователя (два backend с одной базой данных нам не нужны)."""
    global _mutex
    if not IS_WIN:
        return True
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _mutex = kernel32.CreateMutexW(None, False, "Local\\Wave.Desktop.SingleInstance")
    return ctypes.get_last_error() != 183          # ERROR_ALREADY_EXISTS


# ---------------------------------------------------------------------------
# Встроенный backend
# ---------------------------------------------------------------------------
def listen_socket(port: int = 0) -> socket.socket:
    """
    Сокет только на 127.0.0.1 (извне недоступен). Порт 0 = свободный порт, выбранный системой.
    Сокет создаём сами и отдаём серверу готовым: так нет гонки «порт заняли между проверкой и запуском».
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if IS_WIN:
        # На Windows SO_REUSEADDR позволил бы другому процессу занять тот же порт
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    else:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", port))
    return sock


class Backend:
    """FastAPI + uvicorn в отдельном потоке этого же процесса."""

    def __init__(self) -> None:
        self.server = None
        self.thread: threading.Thread | None = None
        self.port = 0

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self, prefer_port: int = 0) -> None:
        import uvicorn
        from main import app

        try:
            sock = listen_socket(prefer_port)
        except OSError:
            log.warning("Порт %s занят, выбираем другой", prefer_port)
            sock = listen_socket(0)
        self.port = sock.getsockname()[1]
        config = uvicorn.Config(app, log_config=None, loop="asyncio", ws="none", access_log=False)
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self._run, args=(sock,), name="wave-backend", daemon=True)
        self.thread.start()
        log.info("Backend запущен: %s", self.url)

    def _run(self, sock: socket.socket) -> None:
        try:
            self.server.run(sockets=[sock])
        except BaseException:                       # uvicorn при ошибке запуска вызывает sys.exit
            log.exception("Backend остановился с ошибкой")
        finally:
            sock.close()

    def wait_ready(self, timeout: float = HEALTH_TIMEOUT) -> bool:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # localhost без системного прокси
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.thread or not self.thread.is_alive():
                return False
            try:
                with opener.open(f"{self.url}/api/health", timeout=1.5) as r:
                    if r.status == 200:
                        return True
            except OSError:
                pass
            time.sleep(0.25)
        return False

    def stop(self, wait: float = 8.0) -> None:
        if self.server:
            self.server.should_exit = True
        if self.thread:
            self.thread.join(wait)


# ---------------------------------------------------------------------------
# Жизненный цикл окна
# ---------------------------------------------------------------------------
def watchdog(window, backend: Backend, closing: threading.Event) -> None:
    """Если backend неожиданно остановился, перезапускаем (не больше MAX_RESTARTS раз)."""
    restarts = 0
    while not closing.wait(WATCH_INTERVAL):
        if backend.thread and backend.thread.is_alive():
            continue
        log.error("Backend остановился неожиданно")
        if restarts >= MAX_RESTARTS:
            window.load_html(error_page("Служебная часть Wave остановилась",
                                        "Закройте приложение и запустите его снова."))
            return
        restarts += 1
        try:
            backend.start(prefer_port=backend.port)
            ok = backend.wait_ready()
        except Exception:
            log.exception("Перезапуск backend не удался")
            ok = False
        if ok:
            log.info("Backend перезапущен (попытка %d)", restarts)
            window.load_url(backend.url)


def boot(window, backend: Backend, closing: threading.Event) -> None:
    """Выполняется в потоке после показа окна: запускает backend и открывает интерфейс."""
    try:
        backend.start()
        if not backend.wait_ready():
            raise RuntimeError("backend не ответил на проверку здоровья")
    except Exception:
        log.exception("Не удалось запустить backend")
        window.load_html(error_page("Wave не смог запуститься", "Не удалось запустить служебную часть приложения."))
        return
    window.load_url(backend.url)
    threading.Thread(target=watchdog, args=(window, backend, closing), name="wave-watchdog", daemon=True).start()
    # Проверить JS-runtime заранее: версия попадает в журнал при каждом запуске, а первый трек YouTube
    # не ждёт проверки. Сбой не мешает интерфейсу: YouTube сообщит о компоненте при воспроизведении.
    threading.Thread(target=_check_runtime, name="wave-jsruntime", daemon=True).start()


def _check_runtime() -> None:
    try:
        jsruntime.status()
    except Exception:
        log.exception("Проверка JS-runtime не удалась")


# ---------------------------------------------------------------------------
# Командная строка: Wave.exe --diagnostics [--live <videoId>] [--json] [--output <файл.json>]
# ---------------------------------------------------------------------------
def parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Неизвестные аргументы игнорируются: обычный запуск (в т.ч. с чужими аргументами) не ломается."""
    ap = argparse.ArgumentParser(prog="Wave", description="Wave / «ВОЛНА»")
    ap.add_argument("--diagnostics", action="store_true",
                    help="проверить компоненты воспроизведения YouTube без окна и сети, код возврата 0 или 1")
    ap.add_argument("--live", metavar="VIDEO_ID",
                    help="вместе с диагностикой получить поток публичного трека YouTube (нужна сеть)")
    ap.add_argument("--json", action="store_true", help="вывести отчёт в JSON")
    ap.add_argument("--output", metavar="FILE", help="записать отчёт в JSON-файл")
    args, _unknown = ap.parse_known_args(argv)
    return args


def _cli_stream():
    """
    Куда писать отчёт. Wave.exe собран как оконное приложение (без консоли), поэтому:
      1) запуск из исходников или с уже открытым stdout — обычный stdout;
      2) Windows, вывод перенаправлен (`Wave.exe --diagnostics > out.txt`, конвейер) — этот дескриптор;
      3) Windows, запуск из консоли — подключаемся к консоли родителя (cmd: `start /wait Wave.exe --diagnostics`,
         иначе cmd не ждёт оконную программу).
    Надёжный способ для сценариев — `--output файл.json` и код возврата. None — писать некуда.
    """
    if not _NO_STDOUT:
        return sys.stdout
    if not IS_WIN:
        return None
    import io
    import msvcrt
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.GetStdHandle.argtypes = [wintypes.DWORD]
    k32.GetStdHandle.restype = wintypes.HANDLE
    k32.GetFileType.argtypes = [wintypes.HANDLE]
    k32.GetFileType.restype = wintypes.DWORD
    k32.AttachConsole.argtypes = [wintypes.DWORD]
    k32.AttachConsole.restype = wintypes.BOOL
    handle = k32.GetStdHandle(0xFFFFFFF5)                       # STD_OUTPUT_HANDLE (-11)
    invalid = ctypes.c_void_p(-1).value
    if handle and handle != invalid and k32.GetFileType(handle) in (1, 3):   # FILE_TYPE_DISK, FILE_TYPE_PIPE
        return open(msvcrt.open_osfhandle(int(handle), 0), "w", encoding="utf-8", closefd=False)
    if k32.AttachConsole(0xFFFFFFFF):                           # ATTACH_PARENT_PROCESS
        try:
            return io.TextIOWrapper(io._WindowsConsoleIO("CONOUT$", "w"), encoding="utf-8", line_buffering=True)
        except (AttributeError, OSError):
            return open("CONOUT$", "w", encoding="utf-8")
    return None


def run_diagnostics(args: argparse.Namespace) -> int:
    """Без окна, без WebView2 и без проверки единственного экземпляра: работает и рядом с запущенным Wave."""
    applog.setup()
    log.info("Диагностика (frozen=%s, live=%s)", getattr(sys, "frozen", False), bool(args.live))
    code = 1
    text = ""
    try:
        import diagnostics

        report = diagnostics.collect(refresh=True)
        if args.live:
            report["live"] = diagnostics.live_check(args.live)
        code = diagnostics.exit_code(report)
        if args.output:
            with open(args.output, "w", encoding="utf-8") as f:
                json.dump(report, f, ensure_ascii=False, indent=2)
        text = json.dumps(report, ensure_ascii=False, indent=2) if args.json else diagnostics.format_text(report)
        log.info("Диагностика: код %d, проблемы: %s", code, [i["code"] for i in report["issues"]] or "нет")
    except Exception as exc:                                      # отчёт об ошибке вместо молчаливого выхода
        log.exception("Диагностика не выполнена")
        text = f"Диагностика не выполнена: {type(exc).__name__}: {exc}"
    finally:
        jsruntime.terminate_children()
    out = _cli_stream()
    if out is not None:
        try:
            out.write(text + "\n")
            out.flush()
        except OSError:
            pass
    return code


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.diagnostics or args.live:
        return run_diagnostics(args)
    applog.setup()
    log.info("Wave Desktop запускается (frozen=%s)", getattr(sys, "frozen", False))
    if IS_WIN:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Wave.Desktop")   # значок на панели задач
    if not single_instance():
        _message(APP_NAME, "Wave уже запущен.")
        return 0
    if not webview2_installed():
        log.error("Не найден WebView2 Runtime")
        _message(APP_NAME, f"Не найден Microsoft Edge WebView2 Runtime. Установите его и запустите Wave снова:\n{WEBVIEW2_URL}")
        return 1

    import webview

    backend, closing = Backend(), threading.Event()
    window = webview.create_window(APP_NAME, html=splash(), width=1280, height=820, min_size=(980, 620),
                                   background_color="#141416")
    window.events.closed += closing.set
    storage = paths.data_dir() / "webview"          # localStorage и кэш окна живут между запусками
    storage.mkdir(parents=True, exist_ok=True)
    webview.start(boot, (window, backend, closing), gui="edgechromium" if IS_WIN else None,
                  storage_path=str(storage), private_mode=False, debug=bool(os.getenv("WAVE_DEBUG")))

    # Окно закрыто: останавливаем backend, завершаем дочерние процессы и выходим
    closing.set()
    backend.stop()
    jsruntime.terminate_children()
    log.info("Wave закрыт")
    logging.shutdown()
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except Exception:
        log.exception("Критическая ошибка запуска")
        _message(APP_NAME, f"Wave не смог запуститься. Журнал: {paths.data_dir() / 'logs' / 'wave.log'}")
        code = 1
    finally:
        jsruntime.terminate_children()   # потоки os._exit не ждёт, а дочерние процессы сам не завершает
    os._exit(code)     # не ждём фоновых потоков (загрузки, yt-dlp): после остановки backend они не нужны