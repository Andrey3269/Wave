"""
Аннотации GitHub Actions для упавших проверок (используется только в .github/workflows/ci.yml).

    python tests/ci_annotate.py --junit pytest.xml --log run_checks.log

Печатает строки `::error title=...::...` для упавших тестов pytest (из JUnit XML) и для строк FAIL
из вывода tests/run_checks.py и e2e. Аннотации видны на странице запуска и через API check-runs —
даже там, где полный журнал запуска недоступен. Ничего не меняет, всегда завершается с кодом 0.
"""
from __future__ import annotations

import argparse
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

LIMIT = 40          # GitHub показывает не больше 50 аннотаций на job


def _escape(text: str) -> str:
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _title(text: str) -> str:
    return _escape(text).replace(":", "%3A").replace(",", "%2C")


def junit_failures(path: Path) -> list[tuple[str, str]]:
    if not path.is_file():
        return []
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        return [("pytest junit", f"не удалось разобрать {path.name}: {exc}")]
    out = []
    for case in root.iter("testcase"):
        for tag in ("failure", "error"):
            node = case.find(tag)
            if node is not None:
                name = f"{case.get('classname', '')}::{case.get('name', '')}"
                body = (node.get("message") or "") + "\n" + (node.text or "")
                lines = [ln for ln in body.strip().splitlines() if ln.strip()]
                # начало сообщения и строки с ошибкой/assert дают суть без всего traceback
                keep = lines[:3] + [ln for ln in lines[3:] if ln.lstrip().startswith(("E ", "assert", "Error"))][:12]
                out.append((f"{tag}: {name}", "\n".join(keep)[:1500]))
    return out


def junit_summary(path: Path) -> str | None:
    """Итог pytest одной строкой: сколько прошло/упало/пропущено и какие тесты пропущены (с причиной)."""
    if not path.is_file():
        return None
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError:
        return None
    counts = {"passed": 0, "failed": 0, "error": 0, "skipped": 0}
    skipped = []
    for case in root.iter("testcase"):
        if case.find("failure") is not None:
            counts["failed"] += 1
        elif case.find("error") is not None:
            counts["error"] += 1
        elif (s := case.find("skipped")) is not None:
            if (s.get("type") or "") == "pytest.xfail":          # ожидаемо падающие (xfail) — не пропуск
                continue
            counts["skipped"] += 1
            skipped.append(f"{case.get('name')} ({(s.get('message') or '').strip()[:80]})")
        else:
            counts["passed"] += 1
    text = ", ".join(f"{v} {k}" for k, v in counts.items())
    return text + (("; skipped: " + "; ".join(skipped)) if skipped else "; skipped: none")


def log_failures(path: Path) -> list[tuple[str, str]]:
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        s = line.strip()
        if re.match(r"^\[FAIL\]|^\[BLOCKED\]|^FAIL ", s):
            out.append(("run_checks / e2e", s[:1000]))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--junit", action="append", default=[])
    ap.add_argument("--log", action="append", default=[])
    ap.add_argument("--summary", action="store_true", help="также напечатать ::notice с итогом pytest")
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")       # кириллица в сообщениях и при кодировке консоли cp1252
    if args.summary:
        for p in args.junit:
            if (s := junit_summary(Path(p))) is not None:
                print(f"::notice title={_title('pytest')}::{_escape(s)[:3000]}")
    items = [f for p in args.junit for f in junit_failures(Path(p))]
    items += [f for p in args.log for f in log_failures(Path(p))]
    for title, message in items[:LIMIT]:
        print(f"::error title={_title(title)}::{_escape(message)}")
    if len(items) > LIMIT:
        print(f"::error title=ещё {len(items) - LIMIT}::см. журнал запуска")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
