"""Шаг jstest в tests/run_checks.py (S9): упавший JS-тест, пустой набор и отсутствие Node не выдаются за PASS."""
from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def rc(monkeypatch):
    spec = importlib.util.spec_from_file_location("wave_run_checks", ROOT / "tests" / "run_checks.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _suite(tmp_path: Path, body: str) -> Path:
    d = tmp_path / "js"
    d.mkdir()
    (d / "x.test.mjs").write_text('import { test } from "node:test";\n' + body, encoding="utf-8")
    return d


needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node не установлен")


@needs_node
def test_passing_suite_is_pass(rc, tmp_path, monkeypatch):
    monkeypatch.setattr(rc, "ROOT", tmp_path)
    monkeypatch.setattr(rc, "JS_TESTS", _suite(tmp_path, 'test("ok", () => {});\n'))
    status, note = rc.step_jstest()
    assert status == "PASS" and "тестов: 1" in note


@needs_node
def test_failing_test_is_fail(rc, tmp_path, monkeypatch):
    monkeypatch.setattr(rc, "ROOT", tmp_path)
    monkeypatch.setattr(rc, "JS_TESTS", _suite(tmp_path, 'test("bad", () => { throw new Error("сломано"); });\n'))
    status, note = rc.step_jstest()
    assert status == "FAIL" and "сломано" in note


@needs_node
def test_file_without_tests_is_fail(rc, tmp_path, monkeypatch):
    monkeypatch.setattr(rc, "ROOT", tmp_path)
    monkeypatch.setattr(rc, "JS_TESTS", _suite(tmp_path, "// ни одного теста\n"))
    assert rc.step_jstest()[0] == "FAIL"


@needs_node
def test_one_empty_file_among_others_is_fail(rc, tmp_path, monkeypatch):
    d = _suite(tmp_path, 'test("ok", () => {});\n')
    (d / "y.test.mjs").write_text("// забыли написать тесты\n", encoding="utf-8")
    monkeypatch.setattr(rc, "ROOT", tmp_path)
    monkeypatch.setattr(rc, "JS_TESTS", d)
    status, note = rc.step_jstest()
    assert status == "FAIL" and "y.test.mjs" in note


def test_empty_suite_is_fail(rc, tmp_path, monkeypatch):
    (tmp_path / "js").mkdir()
    monkeypatch.setattr(rc, "ROOT", tmp_path)
    monkeypatch.setattr(rc, "JS_TESTS", tmp_path / "js")
    monkeypatch.setattr(rc.shutil, "which", lambda name: "/usr/bin/node")
    assert rc.step_jstest()[0] == "FAIL"


def test_missing_node_is_blocked(rc, monkeypatch):
    monkeypatch.setattr(rc.shutil, "which", lambda name: None)
    assert rc.step_jstest() == ("BLOCKED", "node не найден")


def test_jstest_is_part_of_standard_checks():
    src = (ROOT / "tests" / "run_checks.py").read_text(encoding="utf-8")
    assert '("jstest", step_jstest)' in src
