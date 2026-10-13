"""
JavaScript-runtime для yt-dlp (этап S3, находки H1 и L8).

С yt-dlp 2025.11 полная поддержка YouTube требует решать JS-задачи плеера во внешнем
JS-runtime. Wave использует Deno: в распространяемой сборке он лежит в папке приложения
(runtime/deno/deno.exe), в разработке берётся из переменной WAVE_JS_RUNTIME или из PATH.

Здесь собрано всё, что касается runtime, в одном месте:
  * поиск и проверка исполняемого файла (запуск `deno --version` с таймаутом, сравнение версии);
  * настройка для yt-dlp (`js_runtimes`) и окружение дочерних процессов (DENO_DIR, DENO_NO_UPDATE_CHECK);
  * контроль дочерних процессов yt-dlp: без окна консоли, с таймаутом, с завершением при выходе;
  * перехват предупреждений yt-dlp о JS-задачах (для понятного текста ошибки и журнала);
  * понятные сообщения, если runtime недоступен.

Порядок поиска:
  1. WAVE_JS_RUNTIME — явный путь к файлу (или папке с ним). Если задан, используется только он:
     молча подставлять другой runtime вместо указанного нельзя.
  2. Упакованный runtime: paths.resource_dir()/runtime/deno/deno(.exe).
  3. Только в разработке: `deno` из PATH. В собранном приложении системный Deno, текущий каталог
     и PATH не используются: сборка не должна зависеть от того, что установлено у пользователя.

Если подходящего runtime нет, yt-dlp получает пустой `js_runtimes`: так он не ищет `deno`
сам (в собранном exe он заглянул бы в текущий каталог и PATH) и работает в урезанном режиме
без JS. Ошибка воспроизведения YouTube тогда сообщает именно о компоненте, а не «трек недоступен».
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import paths

log = logging.getLogger("wave.jsruntime")

RUNTIME_NAME = "deno"
ENV_PATH = "WAVE_JS_RUNTIME"
# Минимальная версия Deno для yt-dlp 2026.8.19 (yt_dlp.utils._jsruntime.DenoJsRuntime.MIN_SUPPORTED_VERSION).
# Тест tests/unit/test_jsruntime.py сверяет её с установленным yt-dlp.
MIN_VERSION: tuple[int, int, int] = (2, 3, 0)
# Первый запуск 97-мегабайтного deno.exe антивирус может проверять несколько секунд.
PROBE_TIMEOUT = 10.0
# Сколько может работать один дочерний процесс yt-dlp (решение JS-задачи обычно < 2 с).
# yt-dlp ждёт процесс без таймаута: без этой границы зависший deno навсегда занял бы поток резолвера.
PROCESS_TIMEOUT = 30.0
# Неудачную проверку повторяем не чаще раза в минуту (вдруг файл был временно заблокирован).
RETRY_FAILED_AFTER = 60.0
IS_WIN = sys.platform == "win32"
BUNDLED_NAME = "deno.exe" if IS_WIN else "deno"

_VERSION_RE = re.compile(r"deno (\d+)\.(\d+)\.(\d+)(\S*)")


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def bundled_path() -> Path:
    """Где runtime лежит в собранном приложении (packaging/wave.spec кладёт его туда же)."""
    return paths.resource_dir() / "runtime" / "deno" / BUNDLED_NAME


def lock_path() -> Path:
    """packaging/runtime/deno.lock: закреплённая версия, URL и SHA-256 дистрибутива Deno."""
    if is_frozen():
        return paths.resource_dir() / "runtime" / "deno.lock"
    return paths.resource_dir() / "packaging" / "runtime" / "deno.lock"


def expected_version() -> str | None:
    """Версия Deno, закреплённая для сборки (None, если lock-файл не найден или повреждён)."""
    try:
        data = json.loads(lock_path().read_text(encoding="utf-8"))
        version = data.get("version")
        return version if isinstance(version, str) else None
    except (OSError, ValueError):
        return None


def deno_dir() -> Path:
    """Кэш Deno в записываемом каталоге данных пользователя, а не в папке установки и не в общем ~/.cache."""
    return paths.data_dir() / "deno"


def display_path(p: Path | str | None) -> str | None:
    """
    Путь для журнала и диагностики без лишних подробностей о системе: папка приложения -> <app>,
    каталог данных -> <data>, домашний каталог -> ~ (в нём обычно имя пользователя).
    """
    if p is None:
        return None
    s = str(p)
    for base, label in ((paths.resource_dir(), "<app>"), (paths.data_dir(), "<data>"), (Path.home(), "~")):
        try:
            rel = Path(s).resolve().relative_to(Path(base).resolve())
        except (ValueError, OSError):
            continue
        return label if str(rel) == "." else f"{label}{os.sep}{rel}"
    return s


# ---------------------------------------------------------------------------
# Окружение дочерних процессов
# ---------------------------------------------------------------------------
_saved_env: dict[str, str | None] = {}
_env_lock = threading.Lock()


def runtime_env() -> dict[str, str]:
    """
    Переменные для Deno (проверено по исходникам Deno 2.9.7):
      DENO_DIR             — каталог кэша (анализ модулей); иначе Deno пишет в общий %LOCALAPPDATA%\\deno;
      DENO_NO_UPDATE_CHECK — любое значение выключает фоновую проверку обновлений по сети
                             (cli/tools/upgrade.rs: upgrade_check_enabled).
    """
    return {"DENO_DIR": str(deno_dir()), "DENO_NO_UPDATE_CHECK": "1"}


def configure_environment() -> None:
    """
    Записывает переменные в окружение процесса. yt-dlp запускает Deno с копией os.environ
    (DenoJCP._get_env_options), другого способа передать их нет. Затрагивает только Wave и его потомков.
    """
    with _env_lock:
        for key, value in runtime_env().items():
            if key not in _saved_env:
                _saved_env[key] = os.environ.get(key)
            os.environ[key] = value
        try:
            deno_dir().mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.warning("Не удалось создать каталог кэша Deno %s: %s", display_path(deno_dir()), exc)


def _restore_environment() -> None:
    with _env_lock:
        for key, value in _saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        _saved_env.clear()


# ---------------------------------------------------------------------------
# Поиск и проверка
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Probe:
    """Результат проверки одного кандидата."""
    source: str                    # env | bundled | system
    path: Path
    ok: bool
    version: str | None = None
    problem: str | None = None     # not_found | start_failed | timeout | bad_output | unsupported_version
    detail: str | None = None      # для журнала: без секретов, коротко


@dataclass(frozen=True)
class RuntimeStatus:
    ok: bool                       # найден, запускается, версия поддерживается
    name: str = RUNTIME_NAME
    source: str | None = None
    path: Path | None = None
    version: str | None = None
    problem: str | None = None     # код проблемы выбранного кандидата или not_found
    detail: str | None = None
    probes: tuple[Probe, ...] = ()
    checked_at: float = 0.0

    @property
    def version_tuple(self) -> tuple[int, ...] | None:
        m = _VERSION_RE.fullmatch(f"deno {self.version}") if self.version else None
        return tuple(int(m.group(i)) for i in (1, 2, 3)) if m else None


def _version_text(v: tuple[int, ...]) -> str:
    return ".".join(map(str, v))


def probe(path: Path, source: str) -> Probe:
    """Запускает `<path> --version` (без окна консоли, с таймаутом) и проверяет ответ и версию."""
    if not path.is_file():
        return Probe(source, path, False, problem="not_found", detail="файл не найден")
    configure_environment()
    from yt_dlp.utils import Popen                 # тот же запуск, что у yt-dlp (скрытое окно, окружение PyInstaller)

    install_process_guard()
    try:
        proc = Popen([str(path), "--version"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                     stderr=subprocess.STDOUT, text=True)
    except OSError as exc:                         # нет прав, повреждённый или чужой формат файла
        return Probe(source, path, False, problem="start_failed", detail=f"{type(exc).__name__}: {exc.strerror or exc}")
    try:
        out, _ = proc.communicate(timeout=PROBE_TIMEOUT)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        return Probe(source, path, False, problem="timeout", detail=f"нет ответа за {PROBE_TIMEOUT:.0f} с")
    first = (out or "").strip().splitlines()[0][:120] if (out or "").strip() else ""
    if proc.returncode != 0:
        return Probe(source, path, False, problem="start_failed", detail=f"код выхода {proc.returncode}: {first}")
    m = _VERSION_RE.match(first)
    if not m:
        return Probe(source, path, False, problem="bad_output", detail=f"неожиданный ответ: {first!r}")
    vt = tuple(int(m.group(i)) for i in (1, 2, 3))
    version = f"{_version_text(vt)}{m.group(4)}"
    if vt < MIN_VERSION:
        return Probe(source, path, False, version=version, problem="unsupported_version",
                     detail=f"нужна версия {_version_text(MIN_VERSION)} или новее")
    return Probe(source, path, True, version=version)


def _which(name: str) -> str | None:                # отдельная функция: тесты подменяют поиск в PATH
    return shutil.which(name)


def _env_candidate() -> Path | None:
    raw = os.getenv(ENV_PATH, "").strip().strip('"')
    if not raw:
        return None
    p = Path(raw)
    return p / BUNDLED_NAME if p.is_dir() else p


def detect() -> RuntimeStatus:
    """Полная проверка без кэша (см. порядок поиска в описании модуля)."""
    configure_environment()
    probes: list[Probe] = []

    def done(p: Probe | None) -> RuntimeStatus:
        if p is not None and p.ok:
            return RuntimeStatus(True, source=p.source, path=p.path, version=p.version, probes=tuple(probes),
                                 checked_at=time.time())
        last = p or (probes[-1] if probes else None)
        return RuntimeStatus(False, source=last.source if last else None, path=last.path if last else None,
                             version=last.version if last else None,
                             problem=last.problem if last else "not_found",
                             detail=last.detail if last else "runtime не найден", probes=tuple(probes),
                             checked_at=time.time())

    env = _env_candidate()
    if env is not None:                              # явно указанный путь: без молчаливой подмены
        probes.append(probe(env, "env"))
        return done(probes[-1])

    bundled = bundled_path()
    if bundled.exists() or is_frozen():
        probes.append(probe(bundled, "bundled"))
        if probes[-1].ok or is_frozen():
            return done(probes[-1])

    if not is_frozen():
        found = _which(RUNTIME_NAME)
        if found:
            probes.append(probe(Path(found), "system"))
            return done(probes[-1])
    return done(None)


_status: RuntimeStatus | None = None
_status_lock = threading.Lock()


def status(refresh: bool = False) -> RuntimeStatus:
    """
    Результат проверки с кэшем на процесс. Удачный результат держится до конца работы,
    неудачный перепроверяется не чаще раза в RETRY_FAILED_AFTER секунд.
    """
    global _status
    with _status_lock:
        cached = _status
        if (not refresh and cached is not None
                and (cached.ok or time.time() - cached.checked_at < RETRY_FAILED_AFTER)):
            return cached
        new = detect()
        if cached is None or (new.ok, new.path, new.version) != (cached.ok, cached.path, cached.version):
            if new.ok:
                log.info("JS-runtime для yt-dlp: deno %s (%s, %s)", new.version, new.source, display_path(new.path))
            else:
                log.warning("JS-runtime для yt-dlp недоступен: %s — %s (%s, %s). YouTube работает в урезанном режиме",
                            new.problem, new.detail, new.source or "—", display_path(new.path) or "—")
        _status = new
        return new


def ydl_options() -> dict:
    """
    Опции yt-dlp для JS-runtime. Путь всегда явный. Если runtime непригоден — пустой словарь:
    иначе yt-dlp по умолчанию включил бы deno и стал искать его сам (в frozen-сборке — в текущем каталоге и PATH).
    """
    st = status()
    if st.ok and st.path is not None:
        return {"js_runtimes": {RUNTIME_NAME: {"path": str(st.path)}}}
    return {"js_runtimes": {}}


def config_key() -> str:
    """Меняется, когда меняется выбранный runtime: экземпляры YoutubeDL с прежней настройкой пересоздаются."""
    st = status()
    return f"{st.path}|{st.version}" if st.ok else ""


def user_message(st: RuntimeStatus | None = None) -> str:
    """Короткий текст для пользователя: без путей, версий окружения и технических подробностей."""
    st = st or status()
    need = _version_text(MIN_VERSION)
    if is_frozen() and st.source != "env":
        return "Компонент воспроизведения YouTube (Deno) не найден или повреждён. Переустановите приложение."
    if st.source == "env":
        return (f"Deno из переменной {ENV_PATH} не запускается или не подходит (нужна версия {need} или новее). "
                "Проверьте путь.")
    if st.problem == "unsupported_version":
        return f"Для воспроизведения YouTube нужен Deno {need} или новее, установлен {st.version}. Обновите Deno."
    return (f"Для воспроизведения YouTube нужен Deno {need} или новее. Установите Deno или укажите путь к нему "
            f"в переменной {ENV_PATH}.")


# ---------------------------------------------------------------------------
# Дочерние процессы yt-dlp (находка L8)
# ---------------------------------------------------------------------------
CREATE_NO_WINDOW = 0x08000000
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9


class _WinJob:
    """
    Windows Job Object с KILL_ON_JOB_CLOSE. Дескриптор задания живёт, пока жив процесс Wave;
    когда процесс завершается любым способом (закрытие окна, os._exit, падение), Windows закрывает
    дескриптор и завершает все процессы задания. Так после Wave не остаётся deno.exe.
    """

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [(n, ctypes.c_ulonglong) for n in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class BASIC(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class EXTENDED(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BASIC), ("IoInfo", IO_COUNTERS),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

        self.EXTENDED = EXTENDED
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        k32.SetInformationJobObject.restype = wintypes.BOOL
        k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        k32.AssignProcessToJobObject.restype = wintypes.BOOL
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        self.k32, self.ctypes = k32, ctypes

        handle = k32.CreateJobObjectW(None, None)
        if not handle:
            raise OSError(ctypes.get_last_error(), "CreateJobObjectW")
        info = EXTENDED()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k32.SetInformationJobObject(handle, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
                                           ctypes.byref(info), ctypes.sizeof(info)):
            err = ctypes.get_last_error()
            k32.CloseHandle(handle)
            raise OSError(err, "SetInformationJobObject")
        self.handle = handle

    def assign(self, proc: subprocess.Popen) -> None:
        if not self.k32.AssignProcessToJobObject(self.handle, int(proc._handle)):
            raise OSError(self.ctypes.get_last_error(), "AssignProcessToJobObject")

    def close(self) -> None:
        """Закрыть дескриптор (= завершить процессы задания). В работе приложения не вызывается."""
        if self.handle:
            self.k32.CloseHandle(self.handle)
            self.handle = None


class ProcessGuard:
    """
    Учёт дочерних процессов, которые запускает yt-dlp (в Wave это JS-runtime):
      * сторож завершает процесс, работающий дольше timeout (yt-dlp ждал бы его бесконечно);
      * на Windows процесс помещается в Job Object с KILL_ON_JOB_CLOSE;
      * terminate_all() завершает оставшиеся процессы при остановке backend и выходе из приложения.
    """

    def __init__(self, timeout: float = PROCESS_TIMEOUT, use_job: bool = IS_WIN):
        self.timeout = timeout
        self.use_job = use_job
        self.spawned = 0                 # всего запущено (для диагностики --live)
        self.killed_on_timeout = 0
        self._procs: dict[int, tuple[subprocess.Popen, float]] = {}
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._job: _WinJob | None = None
        self._job_failed = False
        self._job_warned = False

    def track(self, proc: subprocess.Popen) -> None:
        with self._lock:
            self.spawned += 1
            self._procs[id(proc)] = (proc, time.monotonic())
            if self._thread is None:
                self._thread = threading.Thread(target=self._watch, name="wave-procguard", daemon=True)
                self._thread.start()
        if self.use_job:
            self._assign_job(proc)

    def _assign_job(self, proc: subprocess.Popen) -> None:
        try:
            with self._lock:
                if self._job is None and not self._job_failed:
                    self._job = _WinJob()
                job = self._job
            if job is not None:
                job.assign(proc)
        except OSError as exc:
            with self._lock:
                self._job_failed = self._job is None      # задание не создалось: больше не пытаемся
                first, self._job_warned = not self._job_warned, True
            if first:
                log.warning("Не удалось привязать дочерний процесс к заданию Windows (%s). "
                            "Процессы завершаются при штатном выходе", exc)

    def live(self) -> list[subprocess.Popen]:
        with self._lock:
            return [p for p, _ in self._procs.values() if p.poll() is None]

    def _watch(self) -> None:
        while True:
            now = time.monotonic()
            with self._lock:
                for key in [k for k, (p, _) in self._procs.items() if p.poll() is not None]:
                    del self._procs[key]
                if not self._procs:
                    self._thread = None
                    return
                overdue = [p for p, started in self._procs.values() if now - started > self.timeout]
            for p in overdue:
                if p.poll() is None:
                    self.killed_on_timeout += 1
                    log.warning("Дочерний процесс yt-dlp (pid %s) работает дольше %.0f с — завершён", p.pid, self.timeout)
                    try:
                        p.kill()
                    except OSError:
                        pass
            time.sleep(0.2)

    def terminate_all(self, wait: float = 2.0) -> int:
        """Завершает все живые дочерние процессы. Возвращает их число."""
        procs = self.live()
        for p in procs:
            try:
                p.kill()
            except OSError:
                pass
        deadline = time.monotonic() + wait
        for p in procs:
            try:
                p.wait(timeout=max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                log.warning("Дочерний процесс (pid %s) не завершился за %.0f с", p.pid, wait)
        if procs:
            log.info("Завершено дочерних процессов yt-dlp: %d", len(procs))
        return len(procs)


GUARD = ProcessGuard()
_install_lock = threading.Lock()


def install_process_guard() -> None:
    """
    Подключает контроль к классу yt_dlp.utils.Popen, через который yt-dlp запускает JS-runtime
    (yt_dlp/extractor/youtube/jsc/_builtin/deno.py). Повторный вызов ничего не делает.
    На Windows добавляется CREATE_NO_WINDOW: консольный deno.exe не создаёт окно (даже скрытое).
    """
    from yt_dlp.utils import Popen as YtPopen

    with _install_lock:
        if getattr(YtPopen.__init__, "_wave_guard", False):
            return
        original = YtPopen.__init__

        def __init__(self, *args, **kwargs):
            if IS_WIN:
                kwargs["creationflags"] = kwargs.get("creationflags", 0) | CREATE_NO_WINDOW
            original(self, *args, **kwargs)
            GUARD.track(self)

        __init__._wave_guard = True
        __init__.__wrapped__ = original
        YtPopen.__init__ = __init__


def terminate_children() -> int:
    return GUARD.terminate_all()


# ---------------------------------------------------------------------------
# Сообщения yt-dlp
# ---------------------------------------------------------------------------
_JS_MARKERS = ("jsc", "challenge", "javascript runtime", "js runtime", "deno", "solver", "ejs")


class YtDlpLogger:
    """
    Логгер для опции yt-dlp `logger`: предупреждения уходят в журнал Wave (повторы реже раза
    в 10 минут не пишутся), отладочный вывод отбрасывается. Последнее предупреждение о JS-задачах
    запоминается для текущего потока: по нему ошибка резолва отличается от «трек недоступен».
    """

    DEDUP_SECONDS = 600

    def __init__(self, name: str = "wave.yt_dlp"):
        self._log = logging.getLogger(name)
        self._tls = threading.local()
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()

    def debug(self, msg: str) -> None:
        pass

    def info(self, msg: str) -> None:
        pass

    def warning(self, msg: str) -> None:
        text = str(msg)
        if any(m in text.lower() for m in _JS_MARKERS):
            self._tls.js_issue = text
        now = time.monotonic()
        with self._lock:
            if now - self._seen.get(text, -self.DEDUP_SECONDS) < self.DEDUP_SECONDS:
                return
            if len(self._seen) > 256:
                self._seen.clear()
            self._seen[text] = now
        self._log.warning("%s", text)

    def error(self, msg: str) -> None:
        # Ошибки yt-dlp приходят и исключением DownloadError: их журналирует вызывающий код
        pass

    def begin(self) -> None:
        """Начало извлечения в этом потоке: забыть прошлые предупреждения."""
        self._tls.js_issue = None

    def take_js_issue(self) -> str | None:
        issue = getattr(self._tls, "js_issue", None)
        self._tls.js_issue = None
        return issue


YDL_LOGGER = YtDlpLogger()


def reset() -> None:
    """Для тестов: забыть результат проверки и вернуть окружение процесса."""
    global _status
    with _status_lock:
        _status = None
    _restore_environment()
