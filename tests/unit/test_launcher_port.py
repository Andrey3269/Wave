"""
WAVE_PORT (S4, WIN-13) и второй запуск (WIN-4) в backend/launcher.py.

WAVE_PORT — только переменная окружения для тестов собранного приложения: фиксированный порт на 127.0.0.1.
Без неё лаунчер, как и раньше, берёт свободный порт. Неверное значение или занятый порт — ошибка запуска.
Сквозные проверки запускают лаунчер отдельным процессом с подменённым модулем webview (окно не создаётся,
но backend настоящий): так проверяется, что интерфейс открывается именно на заданном порту.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

import launcher

ROOT = Path(__file__).resolve().parents[2]

# Заглушка webview: start() выполняет boot() лаунчера, ждёт, пока окно получит адрес интерфейса,
# запрашивает /api/health по этому адресу и записывает результат; затем «окно закрывается».
FAKE_WEBVIEW = r'''
import json, os, threading, time, urllib.request
_log = os.environ["WAVE_FAKE_WEBVIEW_LOG"]
def _note(event):
    with open(_log, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")
_note({"event": "import"})
class _Events:
    def __init__(self):
        self.closed = self
    def __iadd__(self, fn):
        return self
class _Window:
    events = _Events()
    def __init__(self):
        self.url = None
        self.html = None
        self.loaded = threading.Event()
    def load_url(self, url):
        self.url = url
        self.loaded.set()
    def load_html(self, html):
        self.html = html
        self.loaded.set()
_win = _Window()
def create_window(*a, **kw):
    _note({"event": "create_window"})
    return _win
def start(func=None, args=None, **kw):
    _note({"event": "start"})
    threading.Thread(target=func, args=args, daemon=True).start()
    if not _win.loaded.wait(60):
        _note({"event": "timeout"})
        return
    if _win.url is None:
        _note({"event": "error_page", "html": _win.html})
        return
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(_win.url + "/api/health", timeout=10) as r:
        _note({"event": "health", "url": _win.url, "body": json.load(r)})
'''


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------------------------------------------------------------------------
# Разбор значения
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("raw", [None, "", "   "])
def test_unset_port_means_free_port(monkeypatch, raw):
    if raw is None:
        monkeypatch.delenv(launcher.ENV_PORT, raising=False)
    else:
        monkeypatch.setenv(launcher.ENV_PORT, raw)
    assert launcher.configured_port() == 0


@pytest.mark.parametrize("raw, port", [("8765", 8765), (" 8765 ", 8765), ("1024", 1024), ("65535", 65535)])
def test_valid_port(monkeypatch, raw, port):
    monkeypatch.setenv(launcher.ENV_PORT, raw)
    assert launcher.configured_port() == port


@pytest.mark.parametrize("raw", ["0", "80", "1023", "65536", "-1", "8765x", "abc", "8 765", "0x2000", "٨٧٦٥", "8765.0"])
def test_invalid_port_is_rejected(monkeypatch, raw):
    monkeypatch.setenv(launcher.ENV_PORT, raw)
    with pytest.raises(ValueError, match="WAVE_PORT"):
        launcher.configured_port()


def test_busy_fixed_port_is_an_error_not_a_silent_fallback():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]
        b = launcher.Backend()
        with pytest.raises(OSError):
            b.start(prefer_port=port, strict=True)
        assert b.thread is None                     # backend не запускался


def test_listen_socket_binds_loopback_only():
    sock = launcher.listen_socket(0)
    try:
        assert sock.getsockname()[0] == "127.0.0.1"
    finally:
        sock.close()


# ---------------------------------------------------------------------------
# Лаунчер целиком (отдельный процесс, настоящий backend, подменённое окно)
# ---------------------------------------------------------------------------
@pytest.fixture
def run_launcher(tmp_path, wave_profile):
    stub = tmp_path / "stub"
    stub.mkdir()
    (stub / "webview.py").write_text(FAKE_WEBVIEW, encoding="utf-8")
    log = tmp_path / "webview-events.jsonl"

    def run(port_env: str | None, single: bool = True, timeout: float = 90):
        env = {**os.environ, "PYTHONPATH": str(stub), "WAVE_FAKE_WEBVIEW_LOG": str(log),
               "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8"}
        env.pop(launcher.ENV_PORT, None)
        if port_env is not None:
            env[launcher.ENV_PORT] = port_env
        # _HEADLESS: сообщения пишутся в журнал, а не в MessageBox (на Windows он ждал бы нажатия бесконечно)
        code = ("import sys; sys.path.insert(0, %r); import launcher; launcher._HEADLESS = True; "
                "launcher.single_instance = lambda: %r; launcher.webview2_installed = lambda: True; "
                "sys.exit(launcher.main([]))" % (str(ROOT / "backend"), single))
        p = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, env=env, capture_output=True, text=True,
                           encoding="utf-8", timeout=timeout)
        events = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []
        wave_log = wave_profile.data / "logs" / "wave.log"
        text = wave_log.read_text(encoding="utf-8") if wave_log.exists() else ""
        if log.exists():
            log.unlink()
        return p, events, text

    return run


def test_fixed_port_opens_the_interface_on_that_port(run_launcher):
    port = _free_port()
    p, events, log_text = run_launcher(str(port))
    assert p.returncode == 0, p.stdout + p.stderr
    health = [e for e in events if e["event"] == "health"]
    assert health, events
    assert health[0]["url"] == f"http://127.0.0.1:{port}"
    from version import __version__
    assert health[0]["body"] == {"status": "ok", "version": __version__}
    assert f"WAVE_PORT: {port}" in log_text


def test_default_start_still_uses_a_free_port(run_launcher):
    p, events, log_text = run_launcher(None)
    assert p.returncode == 0, p.stdout + p.stderr
    health = [e for e in events if e["event"] == "health"]
    assert health and health[0]["url"].startswith("http://127.0.0.1:")
    assert "WAVE_PORT" not in log_text


def test_invalid_port_stops_before_the_window(run_launcher):
    p, events, log_text = run_launcher("80")
    assert p.returncode == 1
    assert events == []                             # webview даже не импортировался: окна нет
    assert "WAVE_PORT='80'" in log_text


def test_busy_port_shows_an_error_instead_of_another_port(run_launcher):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]
        p, events, log_text = run_launcher(str(port))
    assert p.returncode == 0                        # окно с ошибкой открыто и закрыто пользователем
    assert [e for e in events if e["event"] == "error_page"], events
    assert not [e for e in events if e["event"] == "health"]
    assert f"Порт {port} из WAVE_PORT занят" in log_text


def test_second_instance_does_not_start_a_backend(run_launcher):
    """WIN-4 (логика): второй экземпляр не создаёт окно и backend, о чём пишет в журнал."""
    p, events, log_text = run_launcher(str(_free_port()), single=False)
    assert p.returncode == 0
    assert events == []
    assert "второй экземпляр завершается, backend не запускается" in log_text
    assert "Backend запущен" not in log_text


# ---------------------------------------------------------------------------
# Ошибка базы данных при запуске (S6)
# ---------------------------------------------------------------------------
def test_database_error_is_shown_on_the_start_page(run_launcher, wave_profile):
    """База от более новой Wave: backend не стартует, окно показывает причину, а не общий текст; база не тронута."""
    import sqlite3
    con = sqlite3.connect(wave_profile.db_path)
    con.execute("CREATE TABLE favorites (key TEXT PRIMARY KEY, data TEXT NOT NULL, added_at REAL NOT NULL)")
    con.execute("PRAGMA user_version = 99")
    con.commit()
    con.close()
    raw = wave_profile.db_path.read_bytes()
    p, events, log_text = run_launcher(None)
    pages = [e for e in events if e["event"] == "error_page"]
    assert pages, (events, p.stdout, p.stderr)
    assert "Wave не смог открыть базу данных" in pages[0]["html"]
    assert "более новой версией Wave" in pages[0]["html"]
    assert "более новой версией Wave" in log_text
    assert wave_profile.db_path.read_bytes() == raw


def test_startup_error_page_escapes_reason_and_falls_back(monkeypatch):
    from persistence import db as store
    monkeypatch.setattr(store, "_startup_error", "Файл C:\\Users\\<script>\\wave.db повреждён")
    page = launcher.startup_error_page()
    assert "Wave не смог открыть базу данных" in page and "&lt;script&gt;" in page and "<script>" not in page
    monkeypatch.setattr(store, "_startup_error", None)
    assert "Не удалось запустить служебную часть приложения." in launcher.startup_error_page()


# ---------------------------------------------------------------------------
# L5 (S17): второй запуск активирует окно первого экземпляра
# ---------------------------------------------------------------------------
def test_pick_window_finds_the_first_instance():
    exe = os.path.join(os.sep, "apps", "Wave", "Wave.exe")
    windows = [
        (1, "Wave", 400, os.path.join(os.sep, "other", "Wave.exe")),      # чужая программа с тем же заголовком
        (2, "Wave", 999, exe),                                             # окно самого второго экземпляра
        (3, "Wave — заметки", 300, exe),                                   # другой заголовок
        (4, "Wave", 300, ""),                                              # путь процесса не прочитан
        (5, "Wave", 300, exe),
    ]
    assert launcher.pick_window(windows, own_pid=999, exe=exe) == 5
    assert launcher.pick_window(windows[:4], own_pid=999, exe=exe) is None


def test_activation_is_a_no_op_off_windows(monkeypatch):
    monkeypatch.setattr(launcher, "IS_WIN", False)
    assert launcher.activate_running_window() is False
