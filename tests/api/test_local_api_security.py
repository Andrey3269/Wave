"""Базовая защита локального API (backend/main.py: TrustedHostMiddleware, CORS) и известная находка M7."""
from __future__ import annotations

import pytest


def test_foreign_host_header_rejected(api):
    """Защита от DNS rebinding: запрос с чужим Host отклоняется."""
    r = api.get("/api/health", headers={"Host": "evil.example"})
    assert r.status_code == 400


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "127.0.0.1:8765"])
def test_local_host_header_allowed(api, host):
    r = api.get("/api/health", headers={"Host": host})
    assert r.status_code == 200 and r.json() == {"status": "ok"}


def test_cors_does_not_allow_foreign_origin(api):
    r = api.get("/api/settings", headers={"Origin": "https://evil.example"})
    assert r.status_code == 200
    assert "access-control-allow-origin" not in r.headers


def test_cors_preflight_for_mutation_from_foreign_origin_fails(api):
    r = api.http.options("/api/favorites", headers={"Origin": "https://evil.example",
                                                    "Access-Control-Request-Method": "POST"})
    assert r.status_code == 400
    assert "access-control-allow-origin" not in r.headers


@pytest.mark.xfail(strict=True, reason="M7: POST /api/settings/clear-history без тела выполняется с чужого Origin (CSRF), план S13")
def test_cross_origin_mutation_is_refused(api):
    api.post("/api/history", json={"id": "AAAAAAAAAA1", "source": "yt", "title": "t", "artist": "a",
                                   "stream_url": "/api/stream/yt/AAAAAAAAAA1"})
    r = api.post("/api/settings/clear-history", headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    assert api.profile.sql("SELECT COUNT(*) FROM history")[0][0] == 1


def test_frontend_is_served_after_api_routes(api):
    """Фронтенд смонтирован на / после всех /api-маршрутов: API не перехватывается статикой."""
    assert api.get("/").status_code == 200
    assert "text/html" in api.get("/").headers["content-type"]
    assert api.get("/api/health").json() == {"status": "ok"}
    assert api.get("/api/does-not-exist").status_code == 404
