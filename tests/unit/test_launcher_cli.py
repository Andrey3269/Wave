"""
Командная строка лаунчера (S3): `Wave.exe --diagnostics` и совместимость обычного запуска.

Лаунчер запускается отдельным процессом, как настоящий exe. Модуль webview подменяется заглушкой,
которая записывает, был ли он импортирован и вызван: диагностика не должна открывать окно.
Окружение подпроцесса наследует временный профиль сессии (HOME, LOCALAPPDATA, WAVE_DATA_DIR — во temp).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from wavetest.fakedeno import fake_name, make_fake_deno, pid_alive, pids

ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = ROOT / "backend" / "launcher.py"

FAKE_WEBVIEW = '''
import json, os
_log = os.environ["WAVE_FAKE_WEBVIEW_LOG"]
def _note(event):
    with open(_log, "a", encoding="utf-8") as f:
        f.write(json.dumps(event) + "\\n")
_note({"event": "import"})
class _Events:
    def __init__(self):
        self.closed = self
    def __iadd__(self, fn):
        return self
class _Window:
    events = _Events()
def create_window(*a, **kw):
    _note({"event": "create_window", "title": a[0] if a else None})
    return _Window()
def start(func=None, args=None, **kw):
    _note({"event": "start", "gui": kw.get("gui"), "storage": bool(kw.get("storage_path"))})
'''


@pytest.fixture
def cli(tmp_path, wave_profile):
    stub = tmp_path / "stub"
    stub.mkdir()
    (stub / "webview.py").write_text(FAKE_WEBVIEW, encoding="utf-8")
    log = tmp_path / "webview-events.jsonl"
    env = {**os.environ, "PYTHONPATH": str(stub), "WAVE_FAKE_WEBVIEW_LOG": str(log), "WAVE_FAKE_PY": sys.executable,
           "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8"}
    env.pop("WAVE_JS_RUNTIME", None)

    def run(*args, runtime: Path | None = None, code: str | None = None, timeout: float = 60):
        e = dict(env)
        if runtime is not None:
            e["WAVE_JS_RUNTIME"] = str(runtime)
        cmd = [sys.executable, "-c", code] if code else [sys.executable, str(LAUNCHER), *args]
        p = subprocess.run(cmd, cwd=tmp_path, env=e, capture_output=True, text=True, encoding="utf-8", timeout=timeout)
        events = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []
        return p, events

    run.tmp = tmp_path
    run.profile = wave_profile
    return run


def test_diagnostics_without_window(cli):
    rt = make_fake_deno(cli.tmp.joinpath("Мои программы", "Волна Тест"), name=fake_name())
    out = cli.tmp / "отчёт.json"
    p, events = cli("--diagnostics", "--output", str(out), runtime=rt)
    assert p.returncode == 0, p.stdout + p.stderr
    assert "deno 2.9.7 supported=true" in p.stdout and "не проверялось" in p.stdout
    assert events == [], "диагностика не должна импортировать webview"
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["js_runtime"]["ok"] and report["issues"] == []
    assert (cli.profile.data / "logs" / "wave.log").exists()                # журнал — в каталоге данных


def test_diagnostics_exit_code_on_missing_runtime(cli):
    p, events = cli("--diagnostics", runtime=cli.tmp / "нет" / "deno")
    assert p.returncode == 1
    assert "НЕ ГОТОВ" in p.stdout and "критично" in p.stdout and events == []


def test_diagnostics_json_output(cli):
    rt = make_fake_deno(cli.tmp / "rt", name=fake_name())
    p, _ = cli("--diagnostics", "--json", runtime=rt)
    assert p.returncode == 0
    assert json.loads(p.stdout)["js_runtime"]["version"] == "2.9.7"


def test_diagnostics_with_hanging_runtime_finishes_and_kills_it(cli):
    """Runtime «висит» на --version: диагностика укладывается в таймаут, сообщает о нём и завершает процесс."""
    rt = make_fake_deno(cli.tmp / "rt", name=fake_name(), hang_version=True, hang_seconds=30)
    code = ("import sys, runpy; sys.argv=['launcher.py','--diagnostics']; "
            f"sys.path.insert(0, {str(ROOT / 'backend')!r}); import jsruntime; jsruntime.PROBE_TIMEOUT = 2; "
            f"runpy.run_path({str(LAUNCHER)!r}, run_name='__main__')")
    p, _ = cli(code=code, runtime=rt, timeout=25)
    assert p.returncode == 1 and "timeout" in p.stdout
    if os.name != "nt":                                 # на Windows подделка — cmd + python (см. описание теста)
        started = pids(rt.parent)
        assert started and not [pid for pid in started if pid_alive(pid)]


def test_normal_start_is_unchanged(cli):
    """Без аргументов (и с посторонними аргументами) лаунчер идёт обычным путём: окно WebView создаётся."""
    for extra in ([], ["--some-unknown-flag", "C:\\Музыка\\трек.mp3"]):
        code = ("import sys; sys.path.insert(0, %r); import launcher; "
                "launcher.single_instance = lambda: True; launcher.webview2_installed = lambda: True; "
                "sys.exit(launcher.main(%r))" % (str(ROOT / "backend"), extra))
        p, events = cli(code=code)
        assert p.returncode == 0, p.stdout + p.stderr
        assert [e["event"] for e in events][-3:] == ["import", "create_window", "start"], events
        assert events[-1]["storage"] is True
        (cli.tmp / "webview-events.jsonl").unlink()


def test_parse_args():
    import launcher

    assert not launcher.parse_args([]).diagnostics
    a = launcher.parse_args(["--diagnostics", "--live", "dQw4w9WgXcQ", "--output", "r.json", "--json"])
    assert a.diagnostics and a.live == "dQw4w9WgXcQ" and a.output == "r.json" and a.json
    assert not launcher.parse_args(["--unknown", "file.mp3"]).diagnostics
