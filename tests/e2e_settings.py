"""
Браузерный тест настроек интерфейса между запусками backend на разных портах (этап S2a, находка H3).

В приложении backend при каждом запуске получает новый порт, а localStorage окна привязан к origin
http://127.0.0.1:<порт>. Значит, после перезапуска окно видит пустой localStorage, и всё, что хранилось
только там, терялось. Тест воспроизводит это честно: каждый режим открывает новый контекст браузера
(пустой localStorage), а запускает тест tests/run_e2e.py, поднимая backend на разных портах с одним профилем.

    python tests/e2e_settings.py set     URL [ФАЙЛ]  — выставить настройки через интерфейс (порт A)
    python tests/e2e_settings.py verify  URL [ФАЙЛ]  — после перезапуска на порту B: всё на месте
    python tests/e2e_settings.py migrate URL         — новый профиль + старые значения в localStorage: перенос один раз

Профиль проверяется так же, как в tests/e2e_smoke.py (временный каталог или файл-метка), иначе выход с кодом 2.
Внешние источники подменены (рекомендации, звук, обложки, тексты); настройки — настоящий backend и SQLite.
"""
from __future__ import annotations

import json
import re
import struct
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import Route, sync_playwright

MODE = sys.argv[1] if len(sys.argv) > 1 else ""
BASE = sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:8765"
# Файл, через который режим set передаёт ожидания режиму verify (run_e2e.py кладёт его во временный профиль)
STATE = Path(sys.argv[3]) if len(sys.argv) > 3 else Path(tempfile.gettempdir()) / "wave-e2e-settings-expect.json"
TEST_PROFILE_MARKER = ".wave-test-profile"

# Что выставляем в режиме set и ожидаем в verify
NAME = "Порт-тест"
VOLUME = 0.4
# Старые значения в localStorage для режима migrate (ключи — как в версиях до S2a)
LEGACY = {"volume": "0.3", "wave-last-volume": "0.45", "wave-lyrics": "0", "wave-profile-name": "Из старой версии",
          "wave-mix-covers": json.dumps({"indie": "https://lh3.googleusercontent.com/c7=w544-h544"})}


def _is_test_dir(raw: str) -> bool:
    p = Path(raw).resolve()
    return p.is_relative_to(Path(tempfile.gettempdir()).resolve()) or (p / TEST_PROFILE_MARKER).is_file()


def ensure_test_profile(base: str) -> None:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"{base}/api/settings", timeout=10) as r:
            info = json.load(r)["paths"]
    except Exception as exc:
        sys.exit(f"Не удалось проверить профиль backend ({base}/api/settings): {exc!r}. Тест не запущен.")
    bad = [f"{k}: {info[k]}" for k in ("data", "downloads") if not _is_test_dir(info[k])]
    if bad:
        print("ОСТАНОВКА: backend работает не на тестовом профиле:", *bad, sep="\n  ", file=sys.stderr)
        sys.exit(2)


# ---------------------------------------------------------------- подмена внешнего мира
def _wav(seconds: int = 30, rate: int = 8000) -> bytes:
    data = b"\x00\x00" * rate * seconds
    return (b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
            + b"data" + struct.pack("<I", len(data)) + data)


AUDIO = _wav()


def _track(n: int) -> dict:
    return {"id": f"set{n:08d}", "source": "yt", "title": f"Трек {n}", "artist": "Исполнитель", "artists": ["Исполнитель"],
            "album": None, "duration": 30, "cover": f"https://lh3.googleusercontent.com/c{n}=w544-h544", "explicit": False,
            "stream_url": f"/api/stream/yt/set{n:08d}", "artist_id": None, "artist_ids": [], "plays": None,
            "released": None, "url": None}


def _js(route: Route, body) -> None:
    route.fulfill(status=200, content_type="application/json", body=json.dumps(body, ensure_ascii=False))


def handle(route: Route) -> None:
    u = urlparse(route.request.url)
    path, q = u.path, {k: v[0] for k, v in parse_qs(u.query).items()}
    if "googleusercontent.com" in u.netloc:
        return route.fulfill(status=200, content_type="image/svg+xml",
                             body="<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 4 4'><rect width='4' height='4' fill='#246'/></svg>")
    if path.startswith("/api/stream/"):
        if q.get("warm"):
            return route.fulfill(status=204)
        m = re.match(r"bytes=(\d+)-(\d*)", route.request.headers.get("range", ""))
        if m:
            a = int(m.group(1)); b = min(int(m.group(2) or len(AUDIO) - 1), len(AUDIO) - 1)
            return route.fulfill(status=206, body=AUDIO[a:b + 1], headers={
                "Content-Type": "audio/wav", "Accept-Ranges": "bytes", "Content-Range": f"bytes {a}-{b}/{len(AUDIO)}"})
        return route.fulfill(status=200, body=AUDIO, headers={"Content-Type": "audio/wav", "Accept-Ranges": "bytes"})
    if path == "/api/recommendations":
        return _js(route, {"tracks": [_track(i) for i in range(1, 13)], "personalized": False, "page": int(q.get("page", 0)),
                           "failed_sources": [], "source_status": {"yt": "ok"}})
    if path == "/api/search":
        return _js(route, {"query": q.get("q"), "count": 12, "tracks": [_track(i) for i in range(20, 32)],
                           "failed_sources": [], "has_more": False})
    if path == "/api/lyrics":
        return _js(route, {"synced": [{"t": 0.0, "text": "строка"}], "plain": None, "provider": "lrclib"})
    route.continue_()


# ---------------------------------------------------------------- проверки
failures: list[str] = []
checks = 0


def check(cond: bool, msg: str) -> None:
    global checks
    checks += 1
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        failures.append(msg)


# Снимок до запуска скриптов страницы: тема в момент DOMContentLoaded и пустой ли localStorage этого origin
PROBE = """
window.__lsAtStart = (() => { try { return localStorage.length; } catch (e) { return -1; } })();
document.addEventListener('DOMContentLoaded', () => { window.__themeAtDcl = document.documentElement.dataset.theme; });
"""


def backend_values(page) -> dict:
    return page.evaluate("fetch('/api/settings').then(r => r.json())")


SAVE_TIMEOUT_MS = 10_000          # верхняя граница ожидания записи (пауза записи громкости в app.js — 400 мс)


def wait_backend(page, expect: dict, timeout_ms: int = SAVE_TIMEOUT_MS) -> tuple[dict, bool]:
    """
    Ждёт, пока backend отдаст ожидаемые значения (числа — с точностью 0,002), не дольше timeout_ms.
    Возвращает (последние значения, дождались ли). Проверки делаются после этого и не ослабляются:
    если не дождались, они упадут, а фактические значения будут напечатаны.
    """
    def ok(v: dict) -> bool:
        return all(abs(v[k] - x) < 0.002 if isinstance(x, float) else v[k] == x for k, x in expect.items())

    deadline = time.monotonic() + timeout_ms / 1000
    while True:
        v = backend_values(page)["values"]
        if ok(v):
            return v, True
        if time.monotonic() >= deadline:
            return v, False
        page.wait_for_timeout(100)


def diag(v: dict, keys: tuple[str, ...], waited: bool) -> str:
    """Фактические значения для сообщения о провале (только при провале)."""
    got = ", ".join(f"{k}={v.get(k)!r}" for k in keys)
    return f"фактически: {got}" + ("" if waited else f"; ожидание {SAVE_TIMEOUT_MS} мс истекло")


def check_eq(v: dict, expect: dict, msg: str, waited: bool) -> None:
    ok = all(abs(v[k] - x) < 0.002 if isinstance(x, float) else v[k] == x for k, x in expect.items())
    check(ok, msg if ok else f"{msg} — {diag(v, tuple(expect), waited)}")


def open_page(page) -> None:
    page.goto(BASE)
    page.wait_for_function("document.body.dataset.settings === 'ready'", timeout=15000)


def start_playback(page) -> None:
    page.wait_for_selector("#hero-play:not([disabled])")
    page.click("#hero-play")
    page.wait_for_function("(() => { const a = document.querySelector('#audio'); return !a.paused && a.currentTime > 0.1 })()", timeout=15000)


def mode_set(page) -> None:
    print("Порт A: выставляем настройки через интерфейс")
    open_page(page)
    check(page.evaluate("window.__lsAtStart") == 0, "новый контекст: localStorage этого origin пуст")
    start_playback(page)
    page.click("#hero-title")
    page.wait_for_selector("#view-now:not([hidden])")
    box = page.locator("#now-volume").bounding_box()
    page.mouse.click(box["x"] + box["width"] * VOLUME, box["y"] + box["height"] / 2)
    vol = page.evaluate("document.querySelector('#audio').volume")
    check(abs(vol - VOLUME) < 0.06, f"громкость выставлена кликом по шкале ({vol:.2f})")
    page.click("#now-lyrics-btn")
    check(page.locator("#now-stage.no-lyrics").count() == 1, "панель текста скрыта")
    page.click(".nav__item[data-view=settings]")
    page.wait_for_selector("#view-settings:not([hidden])")
    page.fill("#set-name", NAME)
    page.dispatch_event("#set-name", "change")
    page.select_option("#set-theme", "light")
    page.wait_for_function("document.documentElement.dataset.theme === 'light'")
    # Вместо фиксированной паузы — ограниченное ожидание фактической записи всех значений в backend
    expect = {"volume": round(vol, 3), "last_volume": round(vol, 3), "lyrics_visible": False,
              "profile_name": NAME, "theme": "light"}
    v, waited = wait_backend(page, expect)
    check_eq(v, {"volume": expect["volume"]}, "громкость записана в backend", waited)
    check_eq(v, {"last_volume": expect["last_volume"]}, "последняя слышимая громкость записана", waited)
    check_eq(v, {"lyrics_visible": False}, "видимость текста записана", waited)
    check_eq(v, {"profile_name": NAME, "theme": "light"}, "имя профиля и тема записаны", waited)
    check(any("/c" in c for c in v["mix_covers"].values()), "обложка открытого ранее микса хранится в backend")
    STATE.write_text(json.dumps(v), encoding="utf-8")


def mode_verify(page) -> None:
    print("Порт B (тот же профиль): настройки на месте")
    expect = json.loads(STATE.read_text(encoding="utf-8"))
    open_page(page)
    check(page.evaluate("window.__lsAtStart") == 0, "другой порт = другой origin: localStorage пуст")
    check(page.evaluate("window.__themeAtDcl") == "light", "светлая тема применена до запуска интерфейса (без вспышки)")
    check(page.inner_text("#profile-name") == NAME, "имя профиля в боковой панели")
    vol = page.evaluate("document.querySelector('#audio').volume")
    check(abs(vol - expect["volume"]) < 0.002, f"громкость восстановлена ({vol:.2f})")
    check(page.evaluate("getComputedStyle(document.querySelector('#now-volume')).getPropertyValue('--p').trim()")
          == f"{round(expect['volume'] * 100)}%", "шкала громкости показывает восстановленное значение")
    mood, cover = next(iter(expect["mix_covers"].items()))
    src = page.get_attribute(f"#mixes .mix[data-mix='theme:{mood}'] img", "src") or ""
    check(re.search(r"/c\d+=", cover).group(0) in src, "обложка микса в боковой панели взята из настроек")
    start_playback(page)
    page.click("#hero-title")
    page.wait_for_selector("#view-now:not([hidden])")
    check(page.locator("#now-stage.no-lyrics").count() == 1 and page.get_attribute("#now-lyrics-btn", "aria-pressed") == "false",
          "панель текста по-прежнему скрыта")
    page.click(".nav__item[data-view=settings]")
    page.wait_for_selector("#view-settings:not([hidden])")
    page.wait_for_function("document.querySelector('#set-name').value !== ''")
    check(page.input_value("#set-name") == NAME and page.input_value("#set-theme") == "light", "страница настроек показывает сохранённое")


def mode_migrate(ctx, page) -> None:
    print("Новый профиль: перенос старых значений из localStorage")
    seed = ("if (!localStorage.getItem('__seeded')) { const v = %s; for (const k in v) localStorage.setItem(k, v[k]);"
            " localStorage.setItem('__seeded', '1'); }" % json.dumps(LEGACY, ensure_ascii=False))
    ctx.add_init_script(seed)
    open_page(page)
    d = backend_values(page)
    v = d["values"]
    check(d["meta"]["local_ui_migrated"] is True, "backend отметил перенос")
    check(v["profile_name"] == "Из старой версии" and abs(v["volume"] - 0.3) < 1e-9 and abs(v["last_volume"] - 0.45) < 1e-9
          and v["lyrics_visible"] is False, "имя, громкость, последняя громкость и тексты перенесены в backend")
    check(v["mix_covers"] == {"indie": "https://lh3.googleusercontent.com/c7=w544-h544"}, "обложки миксов перенесены")
    check(page.inner_text("#profile-name") == "Из старой версии" and abs(page.evaluate("document.querySelector('#audio').volume") - 0.3) < 0.002,
          "интерфейс показывает перенесённые значения")
    # устаревшие данные в localStorage (например, другое окно/порт) больше не применяются
    page.evaluate("localStorage.setItem('wave-profile-name', 'Чужое старое'); localStorage.setItem('volume', '0.9')")
    open_page(page)
    v2 = backend_values(page)["values"]
    check(v2["profile_name"] == "Из старой версии" and abs(v2["volume"] - 0.3) < 1e-9, "перенос однократный: повторный запуск не затирает backend")
    check(page.inner_text("#profile-name") == "Из старой версии", "после перезагрузки имя из backend, а не из localStorage")
    page.click(".nav__item[data-view=settings]")
    page.wait_for_selector("#view-settings:not([hidden])")
    page.fill("#set-name", "Новое имя")
    page.dispatch_event("#set-name", "change")
    page.wait_for_function("fetch('/api/settings').then(r => r.json()).then(d => d.values.profile_name === 'Новое имя')")
    open_page(page)
    check(page.inner_text("#profile-name") == "Новое имя", "изменение через интерфейс переживает перезагрузку")
    check(page.evaluate("localStorage.getItem('wave-profile-name')") == "Чужое старое", "старые ключи localStorage не удаляются")


def main() -> int:
    if MODE not in ("set", "verify", "migrate"):
        sys.exit(__doc__)
    ensure_test_profile(BASE)
    errors: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])
        ctx = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark")
        ctx.route(re.compile(r".*"), handle)
        ctx.add_init_script(PROBE)
        page = ctx.new_page()
        page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
        page.on("console", lambda m: errors.append(f"console.{m.type}: {m.text}")
                if m.type == "error" and "Failed to load resource" not in m.text else None)
        if MODE == "set":
            mode_set(page)
        elif MODE == "verify":
            mode_verify(page)
        else:
            mode_migrate(ctx, page)
        browser.close()
    print(f"\nПроверок ({MODE}): {checks}, провалено: {len(failures)}")
    print("Ошибки консоли/страницы:", errors if errors else "нет")
    return 1 if failures or errors else 0


if __name__ == "__main__":
    sys.exit(main())
