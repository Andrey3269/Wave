"""
backend/jsruntime.py: поиск и проверка Deno, настройка yt-dlp, окружение и дочерние процессы (S3).

Настоящий Deno здесь не запускается и не скачивается: используются поддельные исполняемые файлы
(tests/wavetest/fakedeno.py). Проверка с настоящим Deno — tests/unit/test_jsruntime_real.py (в CI
запускается после отдельного шага загрузки со сверкой SHA-256).
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from wavetest.fakedeno import DEFAULT_VERSION, calls, fake_name, make_broken_executable, make_fake_deno

IS_WIN = sys.platform == "win32"
SPACED = ("Мои программы", "Волна Тест")          # пробелы и кириллица в пути (требование S3)


@pytest.fixture
def js(backend, monkeypatch):
    """jsruntime без кэша и без поиска в PATH (системный Deno на машине не должен влиять на тесты)."""
    m = backend["jsruntime"]
    m.reset()
    monkeypatch.setattr(m, "_which", lambda name: None)
    monkeypatch.delenv(m.ENV_PATH, raising=False)
    monkeypatch.setenv("WAVE_FAKE_PY", sys.executable)
    if IS_WIN:                                       # поддельный deno на Windows — обёртка .cmd
        monkeypatch.setattr(m, "BUNDLED_NAME", fake_name())
    yield m
    m.GUARD.terminate_all()
    m.reset()


def _frozen(monkeypatch, root: Path) -> Path:
    """Имитация собранного приложения: sys.frozen и каталог ресурсов PyInstaller (_internal)."""
    internal = root / "_internal"
    internal.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(internal), raising=False)
    return internal


# ---------------------------------------------------------------------------
# Версия и проверка
# ---------------------------------------------------------------------------
def test_min_version_matches_installed_yt_dlp(js):
    from yt_dlp.utils._jsruntime import DenoJsRuntime

    assert js.MIN_VERSION == DenoJsRuntime.MIN_SUPPORTED_VERSION


def test_compatible_runtime_is_found(js, tmp_path, monkeypatch):
    exe = make_fake_deno(tmp_path / "deno")
    monkeypatch.setenv(js.ENV_PATH, str(exe))
    st = js.status()
    assert st.ok and st.source == "env" and st.version == "2.9.7" and st.path == exe and st.problem is None


def test_env_may_point_to_folder(js, tmp_path, monkeypatch):
    folder = tmp_path / "rt"
    make_fake_deno(folder, name=js.BUNDLED_NAME)
    monkeypatch.setenv(js.ENV_PATH, str(folder))
    assert js.status().path == folder / js.BUNDLED_NAME


def test_missing_runtime(js, tmp_path, monkeypatch):
    st = js.status()                                   # ни переменной, ни упакованного, ни в PATH
    assert not st.ok and st.problem == "not_found"
    assert js.ydl_options() == {"js_runtimes": {}}
    monkeypatch.setenv(js.ENV_PATH, str(tmp_path / "нет" / "deno.exe"))
    st = js.status(refresh=True)
    assert not st.ok and st.problem == "not_found" and st.source == "env"
    assert js.ENV_PATH in js.user_message(st)


def test_runtime_that_cannot_start(js, tmp_path, monkeypatch):
    exe = make_broken_executable(tmp_path / "rt" / ("deno.exe" if IS_WIN else "deno"))
    monkeypatch.setenv(js.ENV_PATH, str(exe))
    st = js.status()
    assert not st.ok and st.problem == "start_failed", st
    assert js.ydl_options() == {"js_runtimes": {}}


def test_runtime_exiting_with_error(js, tmp_path, monkeypatch):
    monkeypatch.setenv(js.ENV_PATH, str(make_fake_deno(tmp_path / "rt", version_output="boom", exit_code=3)))
    st = js.status()
    assert not st.ok and st.problem == "start_failed" and "3" in st.detail


@pytest.mark.parametrize("output, problem, version", [
    ("Python 3.12.1", "bad_output", None),                         # чужая программа под именем deno
    ("", "bad_output", None),
    ("deno version unknown", "bad_output", None),
    ("deno 2.2.9 (stable, release, x86_64)", "unsupported_version", "2.2.9"),
    ("deno 1.46.3 (stable)", "unsupported_version", "1.46.3"),
])
def test_unexpected_version_output(js, tmp_path, monkeypatch, output, problem, version):
    monkeypatch.setenv(js.ENV_PATH, str(make_fake_deno(tmp_path / "rt", version_output=output)))
    st = js.status()
    assert not st.ok and st.problem == problem and st.version == version
    assert js.ydl_options() == {"js_runtimes": {}}


@pytest.mark.parametrize("output, version", [
    ("deno 2.3.0 (stable, release, x86_64)", "2.3.0"),             # граница
    ("deno 2.10.1-rc.2 (canary)", "2.10.1-rc.2"),
    (DEFAULT_VERSION, "2.9.7"),
])
def test_supported_versions(js, tmp_path, monkeypatch, output, version):
    monkeypatch.setenv(js.ENV_PATH, str(make_fake_deno(tmp_path / "rt", version_output=output)))
    st = js.status()
    assert st.ok and st.version == version


def test_hanging_runtime_times_out(js, tmp_path, monkeypatch):
    monkeypatch.setattr(js, "PROBE_TIMEOUT", 1.5)
    monkeypatch.setenv(js.ENV_PATH, str(make_fake_deno(tmp_path / "rt", hang_version=True, hang_seconds=8)))
    t0 = time.monotonic()
    st = js.status()
    assert not st.ok and st.problem == "timeout"
    assert time.monotonic() - t0 < 7


def test_failed_check_is_retried_later_and_success_is_cached(js, tmp_path, monkeypatch):
    target = tmp_path / "rt" / js.BUNDLED_NAME
    monkeypatch.setenv(js.ENV_PATH, str(target))
    assert not js.status().ok
    make_fake_deno(target.parent, name=js.BUNDLED_NAME)
    assert not js.status().ok                          # неудача ещё в кэше
    monkeypatch.setattr(js, "RETRY_FAILED_AFTER", 0.0)
    first = js.status()
    assert first.ok
    target.unlink()
    assert js.status() is first                        # удача кэшируется до конца работы


# ---------------------------------------------------------------------------
# Порядок поиска: разработка и собранное приложение
# ---------------------------------------------------------------------------
def test_frozen_build_uses_bundled_runtime_only(js, tmp_path, monkeypatch):
    app = tmp_path.joinpath("Program Files", *SPACED)
    internal = _frozen(monkeypatch, app)
    bundled = make_fake_deno(internal / "runtime" / "deno", name=js.BUNDLED_NAME)
    system = make_fake_deno(tmp_path / "system", version_output="deno 2.5.0 (stable)")
    cwd = make_fake_deno(tmp_path / "cwd", name=js.BUNDLED_NAME, version_output="deno 2.6.0 (stable)")
    monkeypatch.setattr(js, "_which", lambda name: str(system))
    monkeypatch.chdir(cwd.parent)
    st = js.status()
    assert st.ok and st.source == "bundled" and st.path == bundled and st.version == "2.9.7"
    assert js.bundled_path() == internal / "runtime" / "deno" / js.BUNDLED_NAME


def test_frozen_build_does_not_fall_back_to_system_deno(js, tmp_path, monkeypatch):
    """Сборка не зависит от случайно установленного Deno: без упакованного runtime — понятная ошибка."""
    _frozen(monkeypatch, tmp_path / "app")
    system = make_fake_deno(tmp_path / "system")
    monkeypatch.setattr(js, "_which", lambda name: str(system))
    st = js.status()
    assert not st.ok and st.source == "bundled" and st.problem == "not_found"
    assert js.ydl_options() == {"js_runtimes": {}}
    msg = js.user_message(st)
    assert "Переустановите приложение" in msg and str(tmp_path) not in msg


def test_frozen_build_with_corrupted_runtime(js, tmp_path, monkeypatch):
    internal = _frozen(monkeypatch, tmp_path / "app")
    make_broken_executable(internal / "runtime" / "deno" / js.BUNDLED_NAME)
    monkeypatch.setattr(js, "_which", lambda name: str(make_fake_deno(tmp_path / "system")))
    st = js.status()
    assert not st.ok and st.source == "bundled" and st.problem in ("start_failed", "bad_output")


def test_development_uses_system_deno(js, tmp_path, monkeypatch):
    system = make_fake_deno(tmp_path / "system")
    monkeypatch.setattr(js, "_which", lambda name: str(system))
    st = js.status()
    assert st.ok and st.source == "system" and st.path == system


def test_env_override_is_not_silently_replaced(js, tmp_path, monkeypatch):
    monkeypatch.setattr(js, "_which", lambda name: str(make_fake_deno(tmp_path / "system")))
    monkeypatch.setenv(js.ENV_PATH, str(tmp_path / "missing" / "deno"))
    st = js.status()
    assert not st.ok and st.source == "env"


# ---------------------------------------------------------------------------
# Настройка yt-dlp и окружение
# ---------------------------------------------------------------------------
def test_yt_dlp_receives_explicit_runtime_path(js, backend, tmp_path, monkeypatch):
    """Настоящий YoutubeDL с опциями Wave видит именно наш runtime (путь с пробелами и кириллицей)."""
    from yt_dlp import YoutubeDL

    exe = make_fake_deno(tmp_path.joinpath(*SPACED, "runtime"))
    monkeypatch.setenv(js.ENV_PATH, str(exe))
    opts = backend["stream"].ydl_opts()
    assert opts["js_runtimes"] == {"deno": {"path": str(exe)}}
    with YoutubeDL(opts) as ydl:
        info = ydl._js_runtimes["deno"].info
    assert info is not None and info.supported and info.path == str(exe) and info.version == "2.9.7"


def test_yt_dlp_gets_no_runtime_when_unusable(js, backend, tmp_path, monkeypatch):
    """Без пригодного runtime yt-dlp не включает deno и не ищет его сам (в frozen — в cwd и PATH)."""
    from yt_dlp import YoutubeDL

    monkeypatch.chdir(make_fake_deno(tmp_path / "cwd", name=js.BUNDLED_NAME).parent)
    opts = backend["stream"].ydl_opts()
    assert opts["js_runtimes"] == {}
    with YoutubeDL(opts) as ydl:
        assert ydl._js_runtimes == {}


def test_runtime_environment(js, tmp_path, monkeypatch, wave_profile):
    monkeypatch.setenv("DENO_DIR", "/somewhere/else")
    monkeypatch.delenv("DENO_NO_UPDATE_CHECK", raising=False)
    monkeypatch.setenv(js.ENV_PATH, str(make_fake_deno(tmp_path / "rt")))
    js.status()
    assert os.environ["DENO_DIR"] == str(wave_profile.data / "deno")       # записываемый каталог данных
    assert os.environ["DENO_NO_UPDATE_CHECK"] == "1"
    assert (wave_profile.data / "deno").is_dir()
    js.reset()
    assert os.environ["DENO_DIR"] == "/somewhere/else" and "DENO_NO_UPDATE_CHECK" not in os.environ


def test_runtime_process_gets_environment_like_yt_dlp(js, tmp_path, monkeypatch, wave_profile):
    """Запуск как в yt-dlp (DenoJCP._run_deno): тот же Popen, копия os.environ, скрипт через stdin."""
    from yt_dlp.extractor.youtube.jsc._builtin.deno import DenoJCP
    from yt_dlp.utils import Popen

    folder = tmp_path.joinpath(*SPACED)
    exe = make_fake_deno(folder, run_output='{"type": "result"}')
    monkeypatch.setenv(js.ENV_PATH, str(exe))
    st = js.status()
    cmd = [str(st.path), "run", *DenoJCP._DENO_BASE_OPTIONS, "--no-npm", "--cached-only", "-"]
    with Popen(cmd, text=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
               env=os.environ.copy()) as proc:
        out, err = proc.communicate_or_kill("console.log(1)")
    assert proc.returncode == 0 and out == '{"type": "result"}', err
    rec = calls(folder)[-1]
    assert rec["args"][-1] == "-" and "--no-remote" in rec["args"] and rec["stdin_len"] == len("console.log(1)")
    assert rec["env"]["DENO_DIR"] == str(wave_profile.data / "deno") and rec["env"]["DENO_NO_UPDATE_CHECK"] == "1"


def test_ydl_instance_is_recreated_when_runtime_changes(js, backend, tmp_path, monkeypatch):
    stream = backend["stream"]
    first = stream._get_ydl("yt")
    assert stream._get_ydl("yt") is first and first.params["js_runtimes"] == {}
    monkeypatch.setenv(js.ENV_PATH, str(make_fake_deno(tmp_path / "rt")))
    js.status(refresh=True)
    second = stream._get_ydl("yt")
    assert second is not first and "deno" in second.params["js_runtimes"]


def test_display_path_hides_user_directories(js, backend, wave_profile):
    paths = backend["paths"]
    assert js.display_path(wave_profile.data / "deno") == f"<data>{os.sep}deno"
    assert js.display_path(paths.resource_dir() / "runtime" / "deno" / "deno.exe").startswith("<app>")
    assert js.display_path(Path.home() / ".deno" / "bin" / "deno").startswith("~")
    assert js.display_path(None) is None


# ---------------------------------------------------------------------------
# Понятные сообщения
# ---------------------------------------------------------------------------
def test_user_messages(js, monkeypatch, tmp_path):
    missing = js.RuntimeStatus(False, problem="not_found")
    assert "Установите Deno" in js.user_message(missing) and "2.3.0" in js.user_message(missing)
    old = js.RuntimeStatus(False, source="system", version="2.1.0", problem="unsupported_version")
    assert "2.1.0" in js.user_message(old) and "Обновите Deno" in js.user_message(old)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    for st in (missing, old, js.RuntimeStatus(False, source="bundled", problem="start_failed")):
        assert js.user_message(st) == ("Компонент воспроизведения YouTube (Deno) не найден или повреждён. "
                                       "Переустановите приложение.")


# ---------------------------------------------------------------------------
# Дочерние процессы
# ---------------------------------------------------------------------------
def _sleeper(seconds: float = 30) -> list[str]:
    return [sys.executable, "-c", f"import time; time.sleep({seconds})"]


def test_guard_is_installed_on_the_popen_yt_dlp_uses_for_deno(js):
    import yt_dlp.extractor.youtube.jsc._builtin.deno as deno_provider
    from yt_dlp.utils import Popen

    js.install_process_guard()
    js.install_process_guard()                         # повторный вызов не оборачивает дважды
    assert deno_provider.Popen is Popen
    assert getattr(Popen.__init__, "_wave_guard", False)
    assert not getattr(Popen.__init__.__wrapped__, "_wave_guard", False)


def test_overdue_child_process_is_killed(js, monkeypatch):
    """yt-dlp ждёт deno без таймаута; сторож завершает процесс, и вызов возвращается."""
    from yt_dlp.utils import Popen

    monkeypatch.setattr(js.GUARD, "timeout", 1.0)
    before = js.GUARD.killed_on_timeout
    t0 = time.monotonic()
    with Popen(_sleeper(), text=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as proc:
        proc.communicate_or_kill("")
    assert time.monotonic() - t0 < 10
    assert proc.returncode != 0 and js.GUARD.killed_on_timeout == before + 1


def test_terminate_all_stops_children(js):
    from yt_dlp.utils import Popen

    procs = [Popen(_sleeper(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL) for _ in range(2)]
    assert js.terminate_children() >= 2
    assert all(p.poll() is not None for p in procs)
    assert js.GUARD.live() == []


def test_finished_processes_are_forgotten(js):
    from yt_dlp.utils import Popen

    Popen.run([sys.executable, "-c", "print(1)"], text=True, stdout=subprocess.PIPE)
    deadline = time.monotonic() + 5
    while js.GUARD._procs and time.monotonic() < deadline:
        time.sleep(0.05)
    assert js.GUARD._procs == {}


@pytest.mark.skipif(not IS_WIN, reason="окно консоли и Job Object есть только в Windows (проверяется в CI windows-latest)")
def test_child_has_no_console_window(js):
    from yt_dlp.utils import Popen

    out, _, code = Popen.run([sys.executable, "-c", "import ctypes; print(ctypes.windll.kernel32.GetConsoleWindow())"],
                             text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert code == 0 and out.strip() == "0"


@pytest.mark.skipif(not IS_WIN, reason="Job Object есть только в Windows (проверяется в CI windows-latest)")
def test_job_object_kills_children_when_closed(js):
    import ctypes
    from ctypes import wintypes

    guard = js.ProcessGuard(use_job=True)
    proc = subprocess.Popen(_sleeper(60), creationflags=js.CREATE_NO_WINDOW)
    try:
        guard.track(proc)
        assert guard._job is not None
        assert ctypes.sizeof(guard._job.EXTENDED) == (144 if ctypes.sizeof(ctypes.c_void_p) == 8 else 112)
        k32 = ctypes.WinDLL("kernel32")
        in_job = wintypes.BOOL()
        assert k32.IsProcessInJob(wintypes.HANDLE(int(proc._handle)), wintypes.HANDLE(guard._job.handle),
                                  ctypes.byref(in_job))
        assert in_job.value
        guard._job.close()                             # как при завершении процесса Wave
        proc.wait(timeout=10)
        assert proc.returncode is not None
    finally:
        if proc.poll() is None:
            proc.kill()


# ---------------------------------------------------------------------------
# Сообщения yt-dlp
# ---------------------------------------------------------------------------
def test_yt_dlp_logger_remembers_js_issue_per_thread(js, caplog):
    import threading

    logger = js.YtDlpLogger("wave.test.ytdlp")
    logger.begin()
    logger.warning("[youtube] x: Some web client https formats have been skipped")
    assert logger.take_js_issue() is None
    logger.warning("[youtube] [jsc:deno] Error running deno process (returncode: 1)")
    seen = []
    t = threading.Thread(target=lambda: seen.append(logger.take_js_issue()))
    t.start()
    t.join()
    assert seen == [None]                              # у другого потока своё состояние
    assert "deno" in logger.take_js_issue()
    assert logger.take_js_issue() is None


def test_yt_dlp_logger_writes_warnings_once(js, caplog):
    logger = js.YtDlpLogger("wave.test.ytdlp2")
    with caplog.at_level("WARNING", logger="wave.test.ytdlp2"):
        for _ in range(3):
            logger.warning("Signature solving failed: Some formats may be missing")
        logger.debug("[debug] noisy")
        logger.error("ERROR: handled by caller")
    assert [r.getMessage() for r in caplog.records] == ["Signature solving failed: Some formats may be missing"]
