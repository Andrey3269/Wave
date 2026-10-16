"""
Сохранение очереди при закрытии окна (S10) в настоящем браузере против настоящего backend (uvicorn).

Здесь нет перехвата запросов Playwright (ctx.route): с ним браузер не доставляет запросы, которые страница
отправляет при выгрузке (проверено: 0 из 4 доходили до backend, без перехвата — 10 из 10), поэтому основной
e2e-тест эту часть проверить не может. Внешние адреса не нужны: обложек у треков нет, поток SoundCloud отдаёт
FakeNet harness (cdn.test), рекомендации — FakeYTMusic.
"""
from __future__ import annotations

import os

import pytest

from wavetest.fakes import track_payload
from wavetest.hlsfix import mp3_frames

sync_api = pytest.importorskip("playwright.sync_api")

IDS = [str(2000000201 + i) for i in range(5)]


def _launch(p):
    try:
        return p.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])
    except Exception as exc:
        reason = f"Chromium для Playwright недоступен: {str(exc).strip().splitlines()[0][:200]}"
        if os.getenv("CI"):
            pytest.fail(reason, pytrace=False)
        pytest.skip(f"BLOCKED: {reason}")


def _seed(c, **over) -> None:
    tracks = [track_payload("sc", i, title=f"Сохранённый {k}", cover=None, duration=8) for k, i in enumerate(IDS)]
    body = {"tracks": tracks, "index": 2, "position": 3.0, "repeat": "off", "shuffle": False, **over}
    assert c.put("/api/player/state", json=body).status_code == 200


def _open(page, port: int) -> None:
    page.goto(f"http://127.0.0.1:{port}/")
    page.wait_for_function("document.body.dataset.track === '1'", timeout=15000)


def test_unsaved_mode_change_is_saved_when_window_closes(harness):
    """Режим поменяли и сразу закрыли окно (до плановой записи): изменение уходит через sendBeacon."""
    with harness.serve() as c, sync_api.sync_playwright() as p:
        _seed(c)
        browser = _launch(p)
        try:
            page = browser.new_page()
            _open(page, c.port)
            assert page.evaluate("document.querySelector('#audio').paused && !document.querySelector('#audio').getAttribute('src')")
            for want in ("all", "one", "off", "all", "one"):
                page.click(".nav__item[data-view=history]")
                page.wait_for_selector("#player:not([hidden])")
                page.click("#player [data-do=repeat]")
                assert page.get_attribute("#player [data-do=repeat]", "data-mode") == want
                page.reload()                                                     # сразу: плановая запись (0,8 с) не успела
                page.wait_for_function("document.body.dataset.track === '1'", timeout=15000)
                assert page.get_attribute("#player [data-do=repeat]", "data-mode") == want, want
                assert c.get("/api/player/state").json()["state"]["repeat"] == want
        finally:
            browser.close()


def test_position_is_saved_when_window_closes_during_playback(harness):
    """Трек играет, позиция ещё не записывалась (запись раз в 10 с): при закрытии окна она уходит через beacon."""
    for i in IDS:
        harness.net.add_file(f"/sc/{i}", mp3_frames(300), "audio/mpeg")          # ≈8 с, прогрессивный поток
    with harness.serve() as c, sync_api.sync_playwright() as p:
        _seed(c, position=1.0)
        browser = _launch(p)
        try:
            page = browser.new_page()
            _open(page, c.port)
            page.click("#hero-play")                                              # «Продолжить» с 1 с
            page.wait_for_function("(() => { const a = document.querySelector('#audio');"
                                   f" return a.src.endsWith('/api/stream/sc/{IDS[2]}') && !a.paused && a.currentTime > 3.5 }})()",
                                   timeout=20000)
            at = page.evaluate("document.querySelector('#audio').currentTime")
            page.reload()
            page.wait_for_function("document.body.dataset.track === '1'", timeout=15000)
            st = c.get("/api/player/state").json()["state"]
            assert st["index"] == 2 and at - 0.5 <= st["position"] <= at + 1.5, (at, st["position"])
            assert page.evaluate("document.querySelector('#audio').paused")                # после перезагрузки звук не включился
            assert page.text_content("#hero-cur") in ("0:03", "0:04", "0:05")
        finally:
            browser.close()


def test_frequent_changes_still_save_within_max_wait(harness):
    """
    Изменения идут чаще паузы перед записью (0,8 с): запись всё равно уходит не позже 3 с после первого из них,
    а не откладывается до конца серии (иначе при сбое во время быстрого листания терялось бы всё).
    """
    with harness.serve() as c, sync_api.sync_playwright() as p:
        _seed(c)
        before = c.get("/api/player/state").json()["state"]["saved_at"]
        browser = _launch(p)
        try:
            page = browser.new_page()
            _open(page, c.port)
            page.click(".nav__item[data-view=history]")
            page.wait_for_selector("#player:not([hidden])")
            for _ in range(14):                                                   # ≈4,2 с: каждое нажатие — раньше 0,8 с
                page.click("#player [data-do=repeat]")
                page.wait_for_timeout(300)
            during = c.get("/api/player/state").json()["state"]["saved_at"]
            assert during > before, "за серию изменений длиннее 3 с не было ни одной записи"
            page.wait_for_timeout(1200)                                           # серия закончилась — последняя запись
            assert c.get("/api/player/state").json()["state"]["repeat"] == page.get_attribute("#player [data-do=repeat]", "data-mode")
        finally:
            browser.close()
