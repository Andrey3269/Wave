"""Раздача интерфейса (S9): ES-модули должны приходить как JavaScript при любом системном реестре типов файлов.

На Windows модуль `mimetypes` берёт типы из реестра; там `.js` бывает записан как `text/plain`. Модульный
<script type="module"> с таким типом браузер (и WebView2) не исполняет — интерфейс не запустился бы вовсе.
"""
from __future__ import annotations

import mimetypes

import pytest


@pytest.fixture()
def registry_says_text_plain(monkeypatch):
    """Как на Windows с испорченной записью реестра: mimetypes считает .js обычным текстом."""
    mimetypes.init()
    db = mimetypes._db                                            # тот же объект, что использует guess_type
    monkeypatch.setitem(db.types_map[True], ".js", "text/plain")
    assert mimetypes.guess_type("x.js")[0] == "text/plain"


@pytest.mark.parametrize("path", ["/app.js", "/vendor/hls.light.min.js"])
def test_js_is_served_as_javascript_despite_registry(api, registry_says_text_plain, path):
    r = api.get(path)
    assert r.status_code == 200
    assert r.headers["content-type"] == "text/javascript; charset=utf-8"


@pytest.mark.parametrize("path, ctype", [("/", "text/html"), ("/index.html", "text/html"), ("/style.css", "text/css"),
                                         ("/app.js", "text/javascript")])
def test_frontend_files_are_revalidated(api, path, ctype):
    r = api.get(path)
    assert r.status_code == 200 and r.headers["content-type"].startswith(ctype)
    assert r.headers["cache-control"] == "no-cache"           # после обновления — не смешивать версии модулей
    assert r.headers.get("etag")


def test_unchanged_file_is_not_sent_again(api):
    etag = api.get("/app.js").headers["etag"]
    r = api.get("/app.js", headers={"If-None-Match": etag})
    assert r.status_code == 304 and r.content == b"" and r.headers["cache-control"] == "no-cache"


def test_api_responses_are_not_affected(api):
    r = api.get("/api/health")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/json")
    assert "no-cache" not in r.headers.get("cache-control", "")
