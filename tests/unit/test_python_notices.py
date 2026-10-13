"""packaging/python_notices.py (S4): лицензии Python-пакетов, попавших в сборку, без PyInstaller и Windows."""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "packaging"))
import python_notices  # noqa: E402


def test_modules_and_files_are_mapped_to_distributions():
    dists = python_notices.distributions_for(
        ["fastapi.routing", "yt_dlp.extractor.youtube", "asyncio.events", "main", "launcher"],
        ["certifi/cacert.pem", "frontend/index.html", "licenses/THIRD_PARTY_NOTICES.txt", "yt_dlp_ejs/yt/solver/lib.min.js"],
    )
    assert {"fastapi", "yt-dlp", "certifi", "yt-dlp-ejs"} <= set(dists)
    assert "pytest" not in dists                       # не упомянут — не попадает, даже если установлен


def test_collect_copies_license_files_and_writes_summary(tmp_path):
    toc = python_notices.collect(["fastapi", "httpx", "yt_dlp"], ["certifi/cacert.pem"], tmp_path,
                                 extra=[("Пример SDK", "1.2.3", "проверочная запись")],
                                 embedded=[("pytest", "встроенная часть (проверка)")])
    dests = [d for d, _s, kind in toc]
    assert all(kind == "DATA" for _d, _s, kind in toc)
    assert all(d.startswith("licenses/python/") for d in dests)
    assert any(d.startswith("licenses/python/fastapi-") and "LICENSE" in d.upper() for d in dests)
    assert any(d.startswith("licenses/python/yt-dlp-") for d in dests)
    assert all(Path(s).is_file() for _d, s, _k in toc)
    summary = (tmp_path / "PACKAGES.txt").read_text(encoding="utf-8")
    assert "fastapi " in summary and "httpx " in summary and "certifi " in summary
    assert "Пример SDK 1.2.3 — проверочная запись" in summary
    assert re.search(r"^pytest \S+ — .*в сборке: встроенная часть \(проверка\)$", summary, re.M)
    assert f"Python {'.'.join(map(str, sys.version_info[:3]))}" in summary
    assert "licenses/python/PACKAGES.txt" in dests


def test_license_expression_falls_back_to_classifiers():
    from importlib import metadata

    expr = python_notices.license_expression(metadata.distribution("httpx"))
    assert "BSD" in expr


def test_name_normalization():
    assert python_notices.norm("yt_dlp") == python_notices.norm("yt-dlp") == "yt-dlp"
    assert python_notices.top_level("pydantic_core/_pydantic_core.pyd") == "pydantic_core"
    assert python_notices.top_level("webview\\lib\\WebView2Loader.dll") == "webview"
