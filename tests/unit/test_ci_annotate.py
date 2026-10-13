"""tests/ci_annotate.py: упавшие тесты и строки FAIL превращаются в аннотации GitHub Actions."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "ci_annotate.py"

JUNIT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" tests="3" failures="1" errors="1">
<testcase classname="tests.unit.test_x" name="test_ok" time="0.1"/>
<testcase classname="tests.unit.test_x" name="test_bad[a:b]" time="0.1">
<failure message="AssertionError: 100% неверно">def test_bad():
&gt;   assert 1 == 2
E   assert 1 == 2
tests/unit/test_x.py:3: AssertionError</failure></testcase>
<testcase classname="tests.api.test_y" name="test_err" time="0.1"><error message="fixture boom">trace</error></testcase>
</testsuite></testsuites>"""


SUMMARY_JUNIT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest">
<testcase classname="a" name="test_ok"/>
<testcase classname="a" name="test_win_only"><skipped type="pytest.skip" message="только Windows"/></testcase>
<testcase classname="a" name="test_known_bug"><skipped type="pytest.xfail" message="M6"/></testcase>
</testsuite></testsuites>"""


def test_summary_lists_skipped_tests(tmp_path):
    (tmp_path / "s.xml").write_text(SUMMARY_JUNIT, encoding="utf-8")
    p = subprocess.run([sys.executable, str(SCRIPT), "--summary", "--junit", str(tmp_path / "s.xml")],
                       capture_output=True, text=True, encoding="utf-8")
    assert p.returncode == 0
    assert p.stdout.strip() == ("::notice title=pytest::1 passed, 0 failed, 0 error, 1 skipped; "
                                "skipped: test_win_only (только Windows)")


def test_annotations(tmp_path):
    (tmp_path / "p.xml").write_text(JUNIT, encoding="utf-8")
    (tmp_path / "r.log").write_text("[PASS] hygiene\n  ok   проверка\n  FAIL настройки не сохранились\n[FAIL] e2e: код 1\n",
                                    encoding="utf-8")
    p = subprocess.run([sys.executable, str(SCRIPT), "--junit", str(tmp_path / "p.xml"), "--log", str(tmp_path / "r.log"),
                        "--junit", str(tmp_path / "missing.xml")], capture_output=True, text=True, encoding="utf-8")
    assert p.returncode == 0
    lines = p.stdout.splitlines()
    assert len(lines) == 4 and all(x.startswith("::error title=") for x in lines)
    assert "test_bad[a%3Ab]" in lines[0] and "100%25 неверно" in lines[0] and "E   assert 1 == 2" in lines[0]
    assert "%0A" in lines[0] and "\n" not in lines[0]
    assert "test_err" in lines[1] and "fixture boom" in lines[1]
    assert lines[2].endswith("::FAIL настройки не сохранились") and lines[3].endswith("::[FAIL] e2e: код 1")
