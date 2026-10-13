"""
JS-runtime с НАСТОЯЩИМ Deno (S3). Нужна переменная WAVE_TEST_REAL_DENO — путь к deno(.exe),
подготовленному `python packaging/fetch_runtime.py` (официальный архив, сверка SHA-256).

В CI её выставляет отдельный шаг перед тестами (Linux и Windows). Сами тесты ничего не скачивают
и в сеть не ходят: Deno запускается с теми же флагами, что у yt-dlp (--no-remote --no-npm --cached-only).
Без переменной тесты пропускаются с причиной — локально их можно запустить так же, как в CI.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

REAL = os.getenv("WAVE_TEST_REAL_DENO")
pytestmark = pytest.mark.skipif(not REAL, reason="WAVE_TEST_REAL_DENO не задана (в CI задаётся после fetch_runtime.py)")
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def real(backend, monkeypatch, tmp_path):
    js = backend["jsruntime"]
    js.reset()
    monkeypatch.setattr(js, "_which", lambda name: None)
    exe = tmp_path.joinpath("Мои программы", "Волна Тест", Path(REAL).name)    # пробелы и кириллица
    exe.parent.mkdir(parents=True)
    shutil.copy2(REAL, exe)
    monkeypatch.setenv(js.ENV_PATH, str(exe))
    yield js, exe
    js.GUARD.terminate_all()
    js.reset()


def _deno_options():
    from yt_dlp.extractor.youtube.jsc._builtin.deno import DenoJCP

    # те же флаги, что DenoJCP._run_js_runtime для скриптов yt-dlp-ejs (вариант не-npm)
    return [*DenoJCP._DENO_BASE_OPTIONS, "--no-npm", "--cached-only"]


def _run(js, exe, script: str, timeout: float = 60):
    from yt_dlp.utils import Popen

    with Popen([str(exe), "run", *_deno_options(), "-"], text=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
               stderr=subprocess.PIPE, env=os.environ.copy()) as proc:
        out, err = proc.communicate_or_kill(script, timeout=timeout)
    return proc.returncode, out, err


def test_real_runtime_is_detected_with_pinned_version(real):
    js, exe = real
    sys.path.insert(0, str(ROOT / "packaging"))
    import fetch_runtime

    st = js.status()
    assert st.ok and st.path == exe and st.version == fetch_runtime.load_lock()["version"], st


def test_yt_dlp_accepts_real_runtime(real, backend):
    from yt_dlp import YoutubeDL

    js, exe = real
    with YoutubeDL(backend["stream"].ydl_opts()) as ydl:
        info = ydl._js_runtimes["deno"].info
    assert info.supported and info.path == str(exe)


def test_ejs_solver_scripts_run_offline(real, wave_profile):
    """Скрипты yt-dlp-ejs (core + lib со встроенными meriyah/astring) исполняются без сети и npm."""
    import yt_dlp_ejs.yt.solver as solver

    js, exe = real
    js.status()
    script = (f"{solver.lib()}\nObject.assign(globalThis, lib);\n{solver.core()}\n"
              "console.log(JSON.stringify({core: typeof jsc, lib: Object.keys(lib).length > 0}));\n")
    code, out, err = _run(js, exe, script)
    assert code == 0 and out.strip() == '{"core":"function","lib":true}', err
    assert (wave_profile.data / "deno").is_dir()                       # кэш Deno — в каталоге данных


def test_runtime_writes_only_to_deno_dir(real, wave_profile):
    js, exe = real
    js.status()
    home = Path.home()
    before = {p for p in home.rglob("*")} if home.exists() else set()
    code, out, err = _run(js, exe, "console.log(1+1)")
    assert code == 0 and out.strip() == "2", err
    after = {p for p in home.rglob("*")} if home.exists() else set()
    assert not [p for p in after - before if "deno" in str(p).lower()], "Deno писал вне DENO_DIR"
    assert any((wave_profile.data / "deno").iterdir())


def test_hanging_real_runtime_is_killed(real, monkeypatch):
    js, exe = real
    js.status()
    monkeypatch.setattr(js.GUARD, "timeout", 2.0)
    t0 = time.monotonic()
    code, _, _ = _run(js, exe, "while (true) {}")
    assert code != 0 and time.monotonic() - t0 < 20
