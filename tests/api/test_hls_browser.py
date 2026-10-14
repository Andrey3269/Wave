"""
HLS в настоящем браузере (S8): интерфейс Wave в Chromium (Playwright) против настоящего backend (uvicorn) —
резолвер и CDN подменены (FakeResolver, FakeNet), всё остальное работает как в приложении: /playback,
HLS-прокси, hls.js из frontend/vendor, тот же <audio>, переключение на прогрессивный трек.

Аудио — настоящие mp3-кадры (wavetest.hlsfix). AAC/fMP4 этот Chromium не декодирует (сборка Playwright без
проприетарных кодеков) — такой поток проверяется на уровне backend и вручную в WebView2 (чек-лист S8).
Браузер не ходит в интернет: всё, кроме 127.0.0.1, обрывается.
"""
from __future__ import annotations

import os
import re

import pytest

from streaming import hls_proxy
from wavetest.hlsfix import MP3_FRAME_SEC, media_playlist, mp3_frames

sync_api = pytest.importorskip("playwright.sync_api")

HLS_ID, PROG_ID = 2000000001, 2000000002
SEG_FRAMES = 383                                   # ≈10 с на сегмент
SEGS = [mp3_frames(SEG_FRAMES) for _ in range(3)]  # ≈30 с


def _launch(p):
    try:
        return p.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])
    except Exception as exc:                        # нет установленного Chromium: проверка невозможна
        reason = f"Chromium для Playwright недоступен: {str(exc).strip().splitlines()[0][:200]}"
        if os.getenv("CI"):                         # в CI Chromium ставится всегда: пропуск = непроверенный HLS
            pytest.fail(reason, pytrace=False)
        pytest.skip(f"BLOCKED: {reason}")


def _audio(page) -> dict:
    return page.evaluate("(() => { const a = document.querySelector('#audio');"
                         " return {t: a.currentTime, d: a.duration, paused: a.paused, src: a.src, err: a.error && a.error.code} })()")


def _row(page, title: str):
    return page.locator("#tracks .row", has_text=title).first


def test_hls_track_plays_seeks_switches_and_releases(harness):
    harness.sc.tracks["*"] = [
        {"kind": "track", "id": HLS_ID, "title": "Only HLS", "duration": 30000, "user": {"id": 1, "username": "Uploader"}},
        {"kind": "track", "id": PROG_ID, "title": "Progressive", "duration": 8000, "user": {"id": 1, "username": "Uploader"}},
    ]
    dur = [round(SEG_FRAMES * MP3_FRAME_SEC, 6)] * 3
    for i, data in enumerate(SEGS):
        harness.net.add_file(f"/sc/{HLS_ID}/seg{i}.mp3", data, "audio/mpeg")
    harness.net.add_file(f"/sc/{HLS_ID}/main.m3u8",
                         media_playlist([f"seg{i}.mp3?Signature=S{i}" for i in range(3)], dur).encode(),
                         "application/vnd.apple.mpegurl")
    harness.resolver.hls[f"sc:{HLS_ID}"] = "mp3"
    harness.net.add_file(f"/sc/{PROG_ID}", mp3_frames(300), "audio/mpeg")          # обычный файл ≈8 с

    errors: list[str] = []
    with harness.serve() as c, sync_api.sync_playwright() as p:
        browser = _launch(p)
        ctx = browser.new_context(viewport={"width": 1366, "height": 860}, color_scheme="dark")
        ctx.route(re.compile(r"^(?!http://127\.0\.0\.1:%d/).*" % c.port), lambda route: route.abort())
        page = ctx.new_page()
        page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
        page.on("console", lambda m: errors.append(f"console: {m.text}")
                if m.type == "error" and "Failed to load resource" not in m.text else None)
        requests: list[str] = []
        page.on("request", lambda r: requests.append(r.url))
        try:
            page.goto(f"http://127.0.0.1:{c.port}/")
            page.click("[data-source=sc]")
            page.fill("#search-input", "test")
            page.keyboard.press("Enter")
            page.wait_for_selector("#tracks .row:not(.row--skel)", timeout=15000)

            # 1. HLS-only трек играет через hls.js в тот же <audio>
            _row(page, "Only HLS").click(position={"x": 260, "y": 20})
            page.wait_for_function("(() => { const a = document.querySelector('#audio'); return !a.paused && a.currentTime > 1 })()",
                                   timeout=20000)
            st = _audio(page)
            assert st["src"].startswith("blob:"), st                       # Media Source Extensions (hls.js)
            assert abs(st["d"] - sum(dur)) < 1.0, st                         # длительность из плейлиста
            assert page.evaluate("typeof window.Hls") == "function"
            assert any(u.endswith("/vendor/hls.light.min.js") for u in requests)
            assert not [u for u in requests if "cdn" in u.lower() and "127.0.0.1" not in u]
            hls_urls = [u for u in requests if "/api/stream/hls/" in u]
            assert hls_urls and all("Signature" not in u and "cdn.test" not in u for u in hls_urls)
            assert not [u for u in requests if re.search(rf"/api/stream/sc/{HLS_ID}$", u)]   # progressive-адрес не трогали
            assert page.evaluate("document.body.dataset.audio") == "playing"

            # 2. перемотка вперёд (в третий сегмент) и назад
            page.evaluate("document.querySelector('#audio').currentTime = 22")
            page.wait_for_function("(() => { const a = document.querySelector('#audio'); return !a.paused && a.currentTime > 22.5 })()",
                                   timeout=15000)
            assert any(u.endswith("/main/s2") for u in requests)
            page.evaluate("document.querySelector('#audio').currentTime = 2")
            page.wait_for_function("(() => { const a = document.querySelector('#audio'); return !a.paused && a.currentTime > 2.3 && a.currentTime < 8 })()",
                                   timeout=15000)

            # 3. пауза и продолжение кнопкой мини-плеера
            page.click("#btn-play")
            page.wait_for_function("document.querySelector('#audio').paused", timeout=5000)
            t_paused = _audio(page)["t"]
            page.wait_for_timeout(500)
            assert abs(_audio(page)["t"] - t_paused) < 0.05                  # на паузе позиция стоит
            page.click("#btn-play")
            page.wait_for_function(f"document.querySelector('#audio').currentTime > {t_paused + 0.5}", timeout=10000)

            # 4. переключение на прогрессивный трек: HLS освобождается (hls.js уничтожен, сеанс удалён)
            assert hls_proxy.stats()["sessions"] == 1
            _row(page, "Progressive").click(position={"x": 260, "y": 20})
            page.wait_for_function(f"document.querySelector('#audio').src.endsWith('/api/stream/sc/{PROG_ID}')", timeout=15000)
            page.wait_for_function("(() => { const a = document.querySelector('#audio'); return !a.paused && a.currentTime > 0.5 })()",
                                   timeout=15000)
            assert hls_proxy.stats()["sessions"] == 0                        # DELETE /api/stream/hls/{token}
            before = len([u for u in requests if "/api/stream/hls/" in u])
            page.wait_for_timeout(1500)
            after = [u for u in requests if "/api/stream/hls/" in u][before:]
            assert not [u for u in after if "/main/s" in u], after           # старый поток больше ничего не грузит

            # 5. обратно на HLS-трек — новый сеанс, снова играет
            _row(page, "Only HLS").click(position={"x": 260, "y": 20})
            page.wait_for_function("(() => { const a = document.querySelector('#audio'); return a.src.startsWith('blob:') && !a.paused && a.currentTime > 0.5 })()",
                                   timeout=20000)
            assert hls_proxy.stats()["sessions"] == 1
        finally:
            browser.close()
    assert errors == [], errors


def test_hls_session_loss_recovers_from_same_position(harness):
    """
    Сеанс HLS пропал (перезапуск backend, «Очистить кэш», TTL): hls.js получает 404, плеер сам, ограниченно,
    перезапускает трек через /playback с той же позиции.
    """
    harness.sc.tracks["*"] = [{"kind": "track", "id": HLS_ID, "title": "Only HLS", "duration": 30000,
                               "user": {"id": 1, "username": "Uploader"}}]
    n = 12                                                       # ≈2 мин: hls.js держит в буфере не больше 30 с
    dur = [round(SEG_FRAMES * MP3_FRAME_SEC, 6)] * n
    for i in range(n):
        harness.net.add_file(f"/sc/{HLS_ID}/seg{i}.mp3", SEGS[0], "audio/mpeg")
    harness.net.add_file(f"/sc/{HLS_ID}/main.m3u8", media_playlist([f"seg{i}.mp3" for i in range(n)], dur).encode(),
                         "application/vnd.apple.mpegurl")
    harness.resolver.hls[f"sc:{HLS_ID}"] = "mp3"
    errors: list[str] = []
    with harness.serve() as c, sync_api.sync_playwright() as p:
        browser = _launch(p)
        ctx = browser.new_context()
        ctx.route(re.compile(r"^(?!http://127\.0\.0\.1:%d/).*" % c.port), lambda route: route.abort())
        page = ctx.new_page()
        page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
        playbacks: list[str] = []
        page.on("request", lambda r: playbacks.append(r.url) if r.url.endswith("/playback") else None)
        try:
            page.goto(f"http://127.0.0.1:{c.port}/")
            page.click("[data-source=sc]")
            page.fill("#search-input", "test")
            page.keyboard.press("Enter")
            page.wait_for_selector("#tracks .row:not(.row--skel)", timeout=15000)
            _row(page, "Only HLS").click(position={"x": 260, "y": 20})
            page.wait_for_function("(() => { const a = document.querySelector('#audio'); return !a.paused && a.currentTime > 1 })()",
                                   timeout=20000)
            hls_proxy.clear()                                                # сеанс исчез
            page.evaluate("document.querySelector('#audio').currentTime = 91")   # нужен новый сегмент → 404
            page.wait_for_function("(() => { const a = document.querySelector('#audio'); return !a.paused && a.currentTime > 91.3 })()",
                                   timeout=20000)
            assert len(playbacks) == 2                                       # один перезапуск через /playback
            assert hls_proxy.stats()["sessions"] == 1
        finally:
            browser.close()
    assert errors == [], errors


def _open_single_hls_track(harness, c, p, segments: list[bytes]):
    harness.sc.tracks["*"] = [{"kind": "track", "id": HLS_ID, "title": "Only HLS", "duration": 30000,
                               "user": {"id": 1, "username": "Uploader"}}]
    for i, data in enumerate(segments):
        if data is not None:
            harness.net.add_file(f"/sc/{HLS_ID}/seg{i}.mp3", data, "audio/mpeg")
    harness.net.add_file(f"/sc/{HLS_ID}/main.m3u8",
                         media_playlist([f"seg{i}.mp3" for i in range(len(segments))], [10.0] * len(segments)).encode(),
                         "application/vnd.apple.mpegurl")
    harness.resolver.hls[f"sc:{HLS_ID}"] = "mp3"
    browser = _launch(p)
    ctx = browser.new_context()
    ctx.route(re.compile(r"^(?!http://127\.0\.0\.1:%d/).*" % c.port), lambda route: route.abort())
    page = ctx.new_page()
    page.goto(f"http://127.0.0.1:{c.port}/")
    page.click("[data-source=sc]")
    page.fill("#search-input", "test")
    page.keyboard.press("Enter")
    page.wait_for_selector("#tracks .row:not(.row--skel)", timeout=15000)
    return browser, page


def test_unrecoverable_hls_stream_stops_after_bounded_retries(harness):
    """Сегменты недоступны навсегда: плеер перезапускает трек не больше 2 раз и показывает ошибку, без цикла."""
    with harness.serve() as c, sync_api.sync_playwright() as p:
        browser, page = _open_single_hls_track(harness, c, p, [None, None])        # файлов сегментов нет: 404
        playbacks: list[str] = []
        page.on("request", lambda r: playbacks.append(r.url) if r.url.endswith("/playback") else None)
        try:
            _row(page, "Only HLS").click(position={"x": 260, "y": 20})
            page.wait_for_function("(document.querySelector('#toast')?.innerText || '').includes('Не удалось воспроизвести')",
                                   timeout=60000)
            page.wait_for_timeout(3000)                                       # и дальше ничего не перезапускается
            assert len(playbacks) == 3, playbacks                             # первый запуск + 2 восстановления
            assert hls_proxy.stats()["sessions"] == 0                         # сеанс освобождён
            assert page.evaluate("document.body.dataset.audio") == "paused"
            toast = page.inner_text("#toast")
            assert "Only HLS" in toast and "yt-dlp" not in toast
            assert "не отдаёт аудио по ссылке даже после её обновления (HTTP 404)" in toast, toast   # текст backend
        finally:
            browser.close()


def test_undecodable_hls_stream_gives_decoder_message(harness):
    """Поток пришёл, но это не аудио: одна попытка восстановления декодера, затем честный текст про декодирование."""
    junk = b"<html>" + bytes(range(256)) * 200
    with harness.serve() as c, sync_api.sync_playwright() as p:
        browser, page = _open_single_hls_track(harness, c, p, [junk, junk])
        try:
            _row(page, "Only HLS").click(position={"x": 260, "y": 20})
            page.wait_for_function("(document.querySelector('#toast')?.innerText || '').includes('Не удалось воспроизвести')",
                                   timeout=60000)
            toast = page.inner_text("#toast")
            assert "декодировать" in toast, toast
            assert "yt-dlp" not in toast and "изменил формат" not in toast
        finally:
            browser.close()
