"""
Запуск интерфейса в настоящем браузере (Chromium через Playwright) против настоящего backend (uvicorn).

Гонка старта (найдена при разборе красного CI 38850541134 и нестабильных тестов test_hls_browser.py):
boot() ждёт /api/favorites и /api/settings, а затем безусловно открывал главную и выставлял источник поиска
по умолчанию. Если пользователь (или тест) в эти секунды уже выполнил поиск, его уводило на главную, а выбранный
источник сбрасывался на YouTube Music. Здесь ответы задерживаются детерминированно (обёртка fetch в странице),
поэтому проверка не зависит от скорости машины.
"""
from __future__ import annotations

import os
import re

import pytest

sync_api = pytest.importorskip("playwright.sync_api")

# Ответы настроек и избранного приходят через 1,5 с — как у медленного backend на холодном старте
SLOW_START = """
(() => {
  const real = window.fetch.bind(window);
  window.fetch = (input, init) => {
    const url = typeof input === "string" ? input : input.url;
    const method = ((init && init.method) || "GET").toUpperCase();
    if (method === "GET" && /\\/api\\/(settings|favorites)$/.test(new URL(url, location.href).pathname)) {
      return new Promise((resolve) => setTimeout(resolve, 1500)).then(() => real(input, init));
    }
    return real(input, init);
  };
})();
"""


def _launch(p):
    try:
        return p.chromium.launch()
    except Exception as exc:                        # нет установленного Chromium: проверка невозможна
        reason = f"Chromium для Playwright недоступен: {str(exc).strip().splitlines()[0][:200]}"
        if os.getenv("CI"):
            pytest.fail(reason, pytrace=False)
        pytest.skip(f"BLOCKED: {reason}")


def test_search_started_during_startup_is_not_replaced_by_home(harness):
    harness.sc.tracks["*"] = [{"kind": "track", "id": 2000000101, "title": "Early Search", "duration": 30000,
                               "user": {"id": 1, "username": "Uploader"}}]
    with harness.serve() as c, sync_api.sync_playwright() as p:
        browser = _launch(p)
        try:
            ctx = browser.new_context()
            ctx.route(re.compile(r"^(?!http://127\.0\.0\.1:%d/).*" % c.port), lambda route: route.abort())
            ctx.add_init_script(SLOW_START)
            page = ctx.new_page()
            searches: list[str] = []
            page.on("request", lambda r: searches.append(r.url) if "/api/search?" in r.url else None)
            page.goto(f"http://127.0.0.1:{c.port}/", wait_until="domcontentloaded")
            # До ответа настроек: пользователь выбирает SoundCloud и ищет
            page.click("[data-source=sc]")
            page.fill("#search-input", "early")
            page.keyboard.press("Enter")
            page.wait_for_selector("#tracks .row:not(.row--skel)", timeout=10000)
            # Настройки и избранное пришли (boot() завершился) — экран и источник остались выбранными пользователем
            page.wait_for_timeout(2500)
            assert page.evaluate("document.body.dataset.view") == "search"
            assert page.locator("#tracks .row", has_text="Early Search").first.is_visible()
            assert page.get_attribute("[data-source=sc]", "aria-selected") == "true"
            assert searches and all("sources=sc" in u for u in searches), searches
        finally:
            browser.close()


def test_without_user_action_startup_opens_home(harness):
    """Без действий пользователя запуск по-прежнему открывает главную (поведение до исправления)."""
    with harness.serve() as c, sync_api.sync_playwright() as p:
        browser = _launch(p)
        try:
            ctx = browser.new_context()
            ctx.route(re.compile(r"^(?!http://127\.0\.0\.1:%d/).*" % c.port), lambda route: route.abort())
            ctx.add_init_script(SLOW_START)
            page = ctx.new_page()
            page.goto(f"http://127.0.0.1:{c.port}/", wait_until="domcontentloaded")
            page.wait_for_function("document.querySelector('#hero').dataset.feed !== undefined", timeout=10000)
            page.wait_for_timeout(2500)
            assert page.evaluate("document.body.dataset.view") == "home"
            assert page.get_attribute("[data-source=yt]", "aria-selected") == "true"   # источник по умолчанию
        finally:
            browser.close()
