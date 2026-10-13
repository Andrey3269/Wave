"""
Поддельный Deno для тестов (настоящий Deno тесты не скачивают и не запускают).

make_fake_deno(папка, **поведение) создаёт исполняемый файл, который ведёт себя как `deno`:
  * `--version`  — печатает version_output (по умолчанию как Deno 2.9.7) и выходит с exit_code;
  * `run ... -`  — читает скрипт из stdin, записывает аргументы и переменные DENO_* в calls.jsonl рядом
                   и печатает run_output.
Режимы hang_version / hang_run: процесс закрывает stdout/stderr и «зависает» на hang_seconds
(закрытые потоки нужны, чтобы после завершения обёртки .cmd на Windows не держались открытые каналы).

POSIX: файл `deno` с shebang на текущий интерпретатор. Windows: обёртка `deno.cmd` только из ASCII:
путь к скрипту берётся из %~dp0 (работает в папках с пробелами и кириллицей), интерпретатор — из
переменной WAVE_FAKE_PY (её выставляет фикстура).
"""
from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

DEFAULT_VERSION = "deno 2.9.7 (stable, release, x86_64-pc-windows-msvc)\nv8 15.0.245.2-rusty\ntypescript 6.0.3"

_SCRIPT = r'''
import json, os, sys, time
here = os.path.dirname(os.path.abspath(__file__))
cfg = json.load(open(os.path.join(here, "fake_deno.json"), encoding="utf-8"))
args = sys.argv[1:]

def hang():
    sys.stdout.flush()
    os.close(1); os.close(2)
    time.sleep(cfg.get("hang_seconds", 20))
    sys.exit(0)

if args[:1] == ["--version"]:
    if cfg.get("hang_version"):
        hang()
    sys.stdout.write(cfg.get("version_output", ""))
    sys.stdout.flush()
    sys.exit(cfg.get("exit_code", 0))

if args[:1] == ["run"]:
    stdin = sys.stdin.read()
    rec = {"args": args, "stdin_len": len(stdin),
           "env": {k: v for k, v in os.environ.items() if k.startswith("DENO_")}}
    with open(os.path.join(here, "calls.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    if cfg.get("hang_run"):
        hang()
    sys.stdout.write(cfg.get("run_output", ""))
    sys.exit(cfg.get("run_exit_code", 0))

sys.stderr.write("unknown command\n")
sys.exit(2)
'''


def fake_name() -> str:
    return "deno.cmd" if sys.platform == "win32" else "deno"


def make_fake_deno(folder: Path, name: str | None = None, *, version_output: str = DEFAULT_VERSION,
                   exit_code: int = 0, run_output: str = '{"ok": true}', run_exit_code: int = 0,
                   hang_version: bool = False, hang_run: bool = False, hang_seconds: float = 20) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "fake_deno.py").write_text(_SCRIPT, encoding="utf-8")
    (folder / "fake_deno.json").write_text(json.dumps({
        "version_output": version_output, "exit_code": exit_code, "run_output": run_output,
        "run_exit_code": run_exit_code, "hang_version": hang_version, "hang_run": hang_run,
        "hang_seconds": hang_seconds}), encoding="utf-8")
    exe = folder / (name or fake_name())
    if sys.platform == "win32":
        exe.write_text('@"%WAVE_FAKE_PY%" "%~dp0fake_deno.py" %*\r\n', encoding="ascii")
    else:
        exe.write_text(f"#!{sys.executable}\n" + _SCRIPT, encoding="utf-8")
        exe.chmod(exe.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        # shebang-вариант читает fake_deno.json из своей папки так же, как скрипт
    return exe


def calls(folder: Path) -> list[dict]:
    f = folder / "calls.jsonl"
    if not f.exists():
        return []
    return [json.loads(line) for line in f.read_text(encoding="utf-8").splitlines() if line.strip()]


def make_broken_executable(path: Path) -> Path:
    """Файл с исполняемым битом, но не программа (повреждённая загрузка): запуск даёт OSError."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x00\x01not an executable\x02" * 8)
    if os.name != "nt":
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path
