"""
OriginGuard (S13, находка M7): изменяющие запросы с чужого Origin получают 403 и ничего не меняют; штатный интерфейс
(same-origin) и клиенты без браузерных заголовков (тесты, лаунчер) работают как раньше.
"""
from __future__ import annotations

import os
import re

import pytest

import main
from wavetest.fakes import track_payload

A = "AAAAAAAAAA1"
FOREIGN = ["https://evil.example", "http://evil.example", "null", "http://127.0.0.1:1", "http://localhost",
           "https://127.0.0.1", "http://127.0.0.1.evil.example", "http://127.0.0.1/", " http://evil.example"]
SAMPLE = {"source": "yt", "track_id": A, "pid": "1", "token": "t" * 16, "path": "main.m3u8"}


def _routes(routes):
    """Маршруты приложения вместе с маршрутами подключённых роутеров (FastAPI ≥ 0.140 хранит их обёрткой)."""
    for r in routes:
        if hasattr(r, "original_router"):
            yield from _routes(r.original_router.routes)
        else:
            yield r


def _mutating_routes() -> list[tuple[str, str]]:
    """Все изменяющие маршруты API приложения (метод, путь с подставленными параметрами)."""
    out = []
    for r in _routes(main.app.routes):
        methods = getattr(r, "methods", None) or set()
        path = getattr(r, "path", "")
        for m in sorted(methods - {"GET", "HEAD", "OPTIONS"}):
            concrete = re.sub(r"\{(\w+)(?::\w+)?\}", lambda g: SAMPLE.get(g.group(1), "x"), path)
            out.append((m, concrete))
    return out


ROUTES = _mutating_routes()


def test_route_list_covers_known_mutations():
    """Защиту проверяем на всех изменяющих маршрутах, а не на выборке: список строится по приложению."""
    paths = {f"{m} {p}" for m, p in ROUTES}
    for want in ("POST /api/favorites", "DELETE /api/favorites/yt/" + A, "PUT /api/settings",
                 "POST /api/settings/clear-history", "POST /api/downloads", "DELETE /api/downloads/yt/" + A,
                 "POST /api/playlists", "PUT /api/player/state", "POST /api/player/state/position"):
        assert want in paths
    assert len(ROUTES) >= 20


@pytest.mark.parametrize("method, path", ROUTES, ids=[f"{m} {p}" for m, p in ROUTES])
def test_every_mutation_from_foreign_origin_is_refused(api, method, path):
    r = api.http.request(method, path, headers={"Origin": "https://evil.example"}, json={})
    assert r.status_code == 403
    assert r.json() == {"detail": "Запрос с другого сайта отклонён.", "code": "forbidden_origin"}


@pytest.mark.parametrize("origin", FOREIGN)
def test_foreign_origin_variants_are_refused(api, origin):
    r = api.post("/api/favorites", json=track_payload(id_=A), headers={"Origin": origin})
    assert r.status_code == 403
    assert api.profile.sql("SELECT COUNT(*) FROM favorites")[0][0] == 0


def test_cross_origin_mutations_change_nothing(api):
    api.post("/api/favorites", json=track_payload(id_=A))
    api.post("/api/history", json=track_payload(id_=A))
    evil = {"Origin": "https://evil.example"}
    assert api.delete(f"/api/favorites/yt/{A}", headers=evil).status_code == 403
    assert api.delete("/api/history", headers=evil).status_code == 403
    assert api.put("/api/settings", json={"theme": "light"}, headers=evil).status_code == 403
    # «простой» запрос формы (text/plain, без preflight) — тот, от которого CORS не защищает
    assert api.post("/api/playlists", content="name=x", headers={**evil, "Content-Type": "text/plain"}).status_code == 403
    assert api.profile.sql("SELECT COUNT(*) FROM favorites")[0][0] == 1
    assert api.profile.sql("SELECT COUNT(*) FROM history")[0][0] == 1
    assert api.profile.sql("SELECT COUNT(*) FROM playlists")[0][0] == 0
    assert api.get("/api/settings").json()["values"]["theme"] == "auto"


@pytest.mark.parametrize("site", ["cross-site", "same-site", "CROSS-SITE"])
def test_cross_site_fetch_without_origin_is_refused(api, site):
    r = api.post("/api/settings/clear-history", headers={"Sec-Fetch-Site": site})
    assert r.status_code == 403


@pytest.mark.parametrize("headers", [{}, {"Sec-Fetch-Site": "same-origin"}, {"Sec-Fetch-Site": "none"},
                                     {"Origin": "http://127.0.0.1"}, {"Origin": "HTTP://127.0.0.1"},
                                     {"Origin": "http://127.0.0.1", "Sec-Fetch-Site": "same-origin"}])
def test_same_origin_and_non_browser_clients_are_allowed(api, headers):
    assert api.post("/api/favorites", json=track_payload(id_=A), headers=headers).status_code == 200
    assert api.delete(f"/api/favorites/yt/{A}", headers=headers).status_code == 200


def test_conflicting_duplicate_origin_headers_are_refused(api):
    r = api.http.post("/api/settings/clear-history", headers=[("Origin", "http://127.0.0.1"), ("Origin", "https://evil.example")])
    assert r.status_code == 403


def test_reads_from_foreign_origin_are_not_blocked_by_the_guard(api):
    """Чтение не меняет данные: его ограничивает CORS (ответ без Access-Control-Allow-Origin), а не 403."""
    r = api.get("/api/settings", headers={"Origin": "https://evil.example"})
    assert r.status_code == 200 and "access-control-allow-origin" not in r.headers


def test_guard_compares_with_the_real_port(harness):
    """Настоящий uvicorn на случайном порту: свой origin с портом проходит, другой порт и другое имя хоста — нет."""
    with harness.serve() as c:
        port = c.port
        assert c.post("/api/favorites", json=track_payload(id_=A), headers={"Origin": f"http://127.0.0.1:{port}"}).status_code == 200
        for origin in (f"http://127.0.0.1:{port + 1}", f"http://localhost:{port}", "http://127.0.0.1"):
            assert c.post("/api/settings/clear-history", headers={"Origin": origin}).status_code == 403, origin
        assert c.get("/api/favorites").json()["items"][0]["id"] == A


sync_api = pytest.importorskip("playwright.sync_api")


def _launch(p):
    try:
        return p.chromium.launch()
    except Exception as exc:
        reason = f"Chromium для Playwright недоступен: {str(exc).strip().splitlines()[0][:200]}"
        if os.getenv("CI"):
            pytest.fail(reason, pytrace=False)
        pytest.skip(f"BLOCKED: {reason}")


def test_browser_same_origin_passes_and_foreign_page_is_refused(harness):
    """
    Настоящий Chromium: страница интерфейса (same-origin fetch и sendBeacon) меняет данные; страница с другого origin
    (тот же сервер под именем localhost) шлёт «простые» запросы no-cors на 127.0.0.1 — они отклоняются, данные целы.
    """
    with harness.serve() as c, sync_api.sync_playwright() as p:
        port = c.port
        assert c.post("/api/history", json=track_payload(id_=A)).status_code == 200
        browser = _launch(p)
        try:
            page = browser.new_page()
            page.goto(f"http://localhost:{port}/api/health")
            page.evaluate("""async (base) => {
                const opts = {method: 'POST', mode: 'no-cors', body: 'x', headers: {'Content-Type': 'text/plain'}};
                await fetch(base + '/api/settings/clear-history', opts).catch(() => null);
                await fetch(base + '/api/playlists', opts).catch(() => null);
                navigator.sendBeacon(base + '/api/settings/clear-history', 'x');
            }""", f"http://127.0.0.1:{port}")
            page.wait_for_timeout(300)
            assert harness.profile.sql("SELECT COUNT(*) FROM history")[0][0] == 1
            assert harness.profile.sql("SELECT COUNT(*) FROM playlists")[0][0] == 0

            page.goto(f"http://127.0.0.1:{port}/api/health")
            status = page.evaluate("""async () => (await fetch('/api/playlists', {method: 'POST',
                headers: {'Content-Type': 'application/json'}, body: JSON.stringify({name: 'Свой'})})).status""")
            assert status == 201
            assert page.evaluate("navigator.sendBeacon('/api/settings/clear-history', 'x')") is True
            page.wait_for_timeout(300)
            assert harness.profile.sql("SELECT COUNT(*) FROM history")[0][0] == 0
        finally:
            browser.close()
