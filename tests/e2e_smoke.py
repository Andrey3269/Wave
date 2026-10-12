"""
Smoke-тест интерфейса Wave в настоящем браузере (Chromium через Playwright).

Что настоящее: backend (FastAPI, SQLite: избранное, история, плейлисты, настройки, загрузки).
Что подменено: только то, что требует интернета (поиск, рекомендации, страницы артиста/альбома,
тексты, звук, картинки): их отдаёт этот скрипт, чтобы тест не зависел от YouTube/SoundCloud.

Рекомендуемый запуск (сам поднимает backend на временном профиле и убирает его после теста):
    python tests/run_e2e.py

Ручной запуск против уже запущенного backend:
    python tests/e2e_smoke.py [http://127.0.0.1:8765] [папка_скриншотов]

Тест меняет данные (избранное, история, плейлисты, настройки), поэтому до первого действия он
проверяет, что backend работает на тестовом профиле: каталоги данных и загрузок должны лежать
во временной папке системы или содержать файл-метку .wave-test-profile. Иначе тест завершается
с кодом 2 и ничего не меняет. Скриншоты по умолчанию пишутся во временную папку.
"""
from __future__ import annotations

import json
import re
import struct
import sys
import tempfile
import urllib.request
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import Route, sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765"
SHOTS = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(tempfile.mkdtemp(prefix="wave-e2e-shots-"))
SHOTS.mkdir(parents=True, exist_ok=True)
TEST_PROFILE_MARKER = ".wave-test-profile"   # тот же файл создаёт tests/run_e2e.py


# ---------------------------------------------------------------- защита реального профиля
def _is_test_dir(raw: str) -> bool:
    """Каталог тестовый: лежит во временной папке системы или помечен файлом-меткой."""
    p = Path(raw).resolve()
    tmp = Path(tempfile.gettempdir()).resolve()
    return p.is_relative_to(tmp) or (p / TEST_PROFILE_MARKER).is_file()


def ensure_test_profile(base: str) -> None:
    """До любых действий: backend должен работать на тестовых каталогах, а не на данных пользователя."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # localhost без системного прокси
    try:
        with opener.open(f"{base}/api/settings", timeout=10) as r:      # чтение настроек ничего не меняет
            info = json.load(r)["paths"]
    except Exception as exc:                                    # любой сбой здесь = тест не запускаем
        sys.exit(f"Не удалось проверить профиль backend ({base}/api/settings): {exc!r}. Тест не запущен.")
    bad = [f"{name}: {info[name]}" for name in ("data", "downloads") if not _is_test_dir(info[name])]
    if bad:
        print("ОСТАНОВКА: backend работает не на тестовом профиле, тест изменил бы данные пользователя.", file=sys.stderr)
        for line in bad:
            print(f"  {line}", file=sys.stderr)
        print("Запустите тест через `python tests/run_e2e.py` или укажите WAVE_DATA_DIR и WAVE_DOWNLOADS_DIR "
              f"во временной папке (либо с файлом {TEST_PROFILE_MARKER}).", file=sys.stderr)
        sys.exit(2)

# ---------------------------------------------------------------- тестовые данные
ARTISTS = ["Мэйби Бэйби", "GSPD", "Vtornik", "PHARAOH", "SCIRENA", "Монеточка", "Скриптонит", "Zivert", "Кино", "Lumen"]
TITLES = ["Химия", "Город засыпает", "Ночной ритм", "Летние воспоминания", "Вечерний вайб", "Давай останемся", "Фантом",
          "Под кожей", "Неоновый дождь", "Тихий свет", "Горизонт", "Эхо", "Последний рейс", "Волна", "Пульс", "Сигнал"]
COLORS = ["#8F251D", "#FF654A", "#2E4A8F", "#3A1E5C", "#1F6F6A", "#5E2A25", "#7A3B8F", "#B34A2A"]


def track(n: int, reason: str | None = None) -> dict:
    artist = ARTISTS[n % len(ARTISTS)]
    t = {
        "id": f"trk{n:08d}", "source": "yt", "title": f"{TITLES[n % len(TITLES)]}" + (f" {n // len(TITLES) + 1}" if n >= len(TITLES) else ""),
        "artist": artist, "artists": [artist], "album": f"Альбом {n % 4 + 1}", "duration": 30,
        "cover": f"https://lh3.googleusercontent.com/c{n}=w544-h544", "explicit": False,
        "stream_url": f"/api/stream/yt/trk{n:08d}", "artist_id": f"UCart{n % len(ARTISTS):04d}",
        "artist_ids": [f"UCart{n % len(ARTISTS):04d}"], "plays": None, "released": None,
        "url": f"https://music.youtube.com/watch?v=trk{n:08d}",
    }
    if reason:
        t["reason"], t["score"] = reason, 40
    return t


def wav(seconds: int = 30, rate: int = 8000) -> bytes:
    data = b"\x00\x00" * rate * seconds
    return (b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
            + b"data" + struct.pack("<I", len(data)) + data)


AUDIO = wav()


def cover_svg(url: str) -> str:
    n = int(re.search(r"/c(\d+)=", url).group(1))
    c1, c2 = COLORS[n % len(COLORS)], COLORS[(n + 3) % len(COLORS)]
    return (f"<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><defs><linearGradient id='g' x1='0' y1='0' x2='1' y2='1'>"
            f"<stop offset='0' stop-color='{c1}'/><stop offset='1' stop-color='{c2}'/></linearGradient></defs>"
            f"<rect width='100' height='100' fill='url(#g)'/><circle cx='50' cy='40' r='18' fill='#00000055'/>"
            f"<rect x='22' y='64' width='56' height='30' rx='14' fill='#00000055'/></svg>")


def js(route: Route, body, status: int = 200) -> None:
    route.fulfill(status=status, content_type="application/json", body=json.dumps(body, ensure_ascii=False))


# ---------------------------------------------------------------- подмена сетевых эндпоинтов
def handle(route: Route) -> None:
    req = route.request
    u = urlparse(req.url)
    path, q = u.path, {k: v[0] for k, v in parse_qs(u.query).items()}

    if "googleusercontent.com" in u.netloc:
        return route.fulfill(status=200, content_type="image/svg+xml", body=cover_svg(req.url))

    if path.startswith("/api/stream/"):
        if q.get("warm"):
            return route.fulfill(status=204)
        rng = re.match(r"bytes=(\d+)-(\d*)", req.headers.get("range", ""))
        if rng:
            a = int(rng.group(1)); b = min(int(rng.group(2) or len(AUDIO) - 1), len(AUDIO) - 1)
            return route.fulfill(status=206, body=AUDIO[a:b + 1], headers={
                "Content-Type": "audio/wav", "Accept-Ranges": "bytes", "Content-Range": f"bytes {a}-{b}/{len(AUDIO)}"})
        return route.fulfill(status=200, body=AUDIO, headers={"Content-Type": "audio/wav", "Accept-Ranges": "bytes"})

    if path == "/api/recommendations":
        if q.get("id"):                                               # «радио» трека
            title = q.get("title", "")
            return js(route, {"tracks": [track(20 + i, f"Похоже на «{title}»") for i in range(14)], "personalized": True, "page": 0})
        page = int(q.get("page", 0))
        base = 1 + page * 24
        reasons = ["Подборка"] if not page else ["Любимый исполнитель: GSPD", "Из твоей истории: Vtornik"]
        return js(route, {"tracks": [track(base + i, reasons[i % len(reasons)]) for i in range(24 if page else 30)], "personalized": False, "page": page})

    if path == "/api/search":
        return js(route, {"query": q.get("q"), "count": 20, "tracks": [track(40 + i) for i in range(20)], "failed_sources": [], "has_more": False})

    if path.startswith("/api/search/artists"):
        return js(route, {"items": [{"id": f"UCart{i:04d}", "source": "yt", "kind": "artist", "name": ARTISTS[i], "cover": track(i)["cover"], "subtitle": "1,2 млн подписчиков"} for i in range(6)], "has_more": False})
    if path.startswith("/api/search/albums"):
        return js(route, {"items": [{"id": f"MPRE{i}", "source": "yt", "kind": "album", "title": f"Альбом {i + 1}", "artist": ARTISTS[i], "year": "2025", "cover": track(i)["cover"], "track_count": 10, "playlist_id": f"OLAK{i}"} for i in range(6)], "has_more": False})

    m = re.match(r"^/api/artist/(\w+)/([\w-]+)(/tracks)?$", path)
    if m:
        if m.group(3):
            return js(route, {"tracks": [track(60 + i) for i in range(10)], "has_more": False})
        albums = [{"id": f"MPRE{i}", "source": "yt", "kind": "album", "title": f"Альбом {i + 1}", "artist": "GSPD", "year": "202" + str(i), "cover": track(i + 2)["cover"], "track_count": 9, "playlist_id": f"OLAK{i}"} for i in range(5)]
        related = [{"id": f"UCart{i:04d}", "source": "yt", "kind": "artist", "name": ARTISTS[i + 3], "cover": track(i + 3)["cover"], "subtitle": "300 тыс. подписчиков"} for i in range(4)]
        return js(route, {"artist": {"name": "GSPD", "cover": track(1)["cover"], "subtitle": "2,5 млн подписчиков"}, "albums": albums, "related": related, "tracks": [track(60 + i) for i in range(20)], "has_more": True})

    m = re.match(r"^/api/album/(\w+)/([\w-]+)$", path)
    if m:
        return js(route, {"album": {"id": m.group(2), "source": "yt", "kind": "album", "title": "Альбом 1", "artist": "GSPD", "year": "2025", "cover": track(2)["cover"], "track_count": 8},
                          "tracks": [track(80 + i) for i in range(8)], "unavailable": 0})

    if path == "/api/lyrics":
        lines = [{"t": i * 3.0, "text": f"Строка текста номер {i + 1}"} for i in range(9)]
        return js(route, {"synced": lines, "plain": None, "provider": "lrclib"})

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


def visible(page, sel: str) -> bool:
    return page.evaluate("(s) => { const n = document.querySelector(s); return !!n && getComputedStyle(n).display !== 'none' && n.getClientRects().length > 0 }", sel)


def rect(page, sel: str) -> dict:
    return page.evaluate("(s) => { const r = document.querySelector(s).getBoundingClientRect(); return {l: r.left, t: r.top, r: r.right, b: r.bottom, w: r.width, h: r.height} }", sel)


def fits(page, sel: str) -> bool:
    r = rect(page, sel)
    vw, vh = page.evaluate("[window.innerWidth, window.innerHeight]")
    return r["l"] >= -1 and r["t"] >= -1 and r["r"] <= vw + 1 and r["b"] <= vh + 1


def queue_rows(page) -> list:
    return page.evaluate("[...document.querySelectorAll('#fs-q-list .fq__row')].map(r => ({i: +r.dataset.index, t: r.querySelector('.fq__title').textContent, cur: r.classList.contains('is-current')}))")


def audio_state(page) -> dict:
    return page.evaluate("(() => { const a = document.querySelector('#audio'); return {paused: a.paused, t: a.currentTime, vol: a.volume, muted: a.muted, src: a.src} })()")


def main() -> int:
    ensure_test_profile(BASE)
    print(f"Скриншоты: {SHOTS}")
    errors: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])
        ctx = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark")
        ctx.route(re.compile(r".*"), handle)
        page = ctx.new_page()
        page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
        page.on("console", lambda m: errors.append(f"console.{m.type}: {m.text}") if m.type == "error" and "Failed to load resource" not in m.text else None)

        print("Главная")
        page.goto(BASE)
        page.wait_for_function("document.querySelector('#hero-title').textContent !== 'Моя волна'", timeout=15000)
        page.wait_for_timeout(500)
        page.screenshot(path=str(SHOTS / "01-home.png"))
        check(page.locator(".side").count() == 1 and page.locator("aside.side + aside, .side ~ .side").count() == 0, "одна боковая панель, второй постоянной колонки нет")
        check(page.locator(".nav__item[aria-current='page']").count() == 1, "ровно один активный пункт меню")
        check(page.locator(".nav__item[aria-current='page']").first.get_attribute("data-view") == "home", "активна «Главная»")
        check(page.locator("#mixes .mix").count() == 5, "в «Твоей музыке» 5 миксов")
        check(not visible(page, "#player"), "на главной нижнего плеера нет (ничего не играет)")
        check(page.locator("#mix-row .mixcard").count() >= 5, "ряд «Под твою волну» заполнен")
        check(page.locator("#new-grid .tcard").count() >= 20, "«Новое для тебя» заполнено реальными треками")
        check("Химия" in page.inner_text("#hero-title") or page.inner_text("#hero-title").strip() != "", "заголовок баннера из данных рекомендаций")

        print("«Слушать волну»")
        page.click("#hero-play")
        page.wait_for_function("(() => { const a = document.querySelector('#audio'); return !a.paused && a.currentTime > 0.2 })()", timeout=15000)
        check(True, "аудио реально играет (currentTime растёт)")
        check(page.inner_text("#hero-play-label") == "Пауза", "кнопка баннера показывает «Пауза»")
        check(page.get_attribute("body", "data-audio") == "playing", "состояние body = playing")
        check(not visible(page, "#player"), "на главной плеера нет и во время игры")
        check(page.text_content("#hero-eyebrow-text") == "Сейчас играет", "баннер показывает текущий трек")
        page.wait_for_timeout(600)
        page.screenshot(path=str(SHOTS / "02-home-playing.png"))
        page.click("#hero-play")
        page.wait_for_function("document.querySelector('#audio').paused")
        page.wait_for_function("document.querySelector('#hero-play-label').textContent === 'Продолжить'")
        check(True, "пауза с баннера работает, подпись кнопки обновилась")
        page.click("#hero-play")
        page.wait_for_function("!document.querySelector('#audio').paused")
        before = page.text_content("#hero-title")
        page.click("[data-do=next].hero__ctl")
        page.wait_for_function("(b) => document.querySelector('#hero-title').textContent !== b", arg=before)
        check(True, "кнопка «следующий» на баннере переключает трек")

        print("Страница трека")
        title = page.text_content("#hero-title")
        page.click("#hero-title")
        page.wait_for_selector("#view-now:not([hidden])")
        check(page.get_attribute("body", "data-view") == "now", "открыта страница текущего трека")
        check(not visible(page, "#player"), "на странице трека нижнего плеера нет")
        check(page.text_content("#now-title") == title, "заголовок страницы = текущий трек")
        page.wait_for_selector("#np-lyrics .ly")
        check(page.locator("#np-lyrics .ly").count() == 9, "синхронный текст отрисован из API")
        page.wait_for_selector("#similar-row .tcard")
        check(page.locator("#similar-row .tcard").count() >= 3, "похожие треки загружены")
        page.wait_for_timeout(400)
        page.screenshot(path=str(SHOTS / "03-now.png"), full_page=False)
        page.wait_for_function("document.querySelector('#np-lyrics .ly.cur')", timeout=8000)
        check(True, "текущая строка текста подсвечивается по таймкодам")
        page.click("#now-like")
        page.wait_for_function("document.querySelector('#now-like').classList.contains('is-fav')")
        check(True, "лайк на странице трека")
        # перемотка кликом по полосе
        box = page.locator("#now-bar").bounding_box()
        page.mouse.click(box["x"] + box["width"] * 0.8, box["y"] + 2)
        page.wait_for_function("document.querySelector('#audio').currentTime > 20", timeout=8000)
        check(True, "перемотка по полосе прогресса меняет позицию звука")
        page.click("#now-lyrics-btn")
        check(page.locator("#now-stage.no-lyrics").count() == 1, "панель текста скрывается")
        page.click("#now-lyrics-btn")
        page.click("[data-do=queue].rbtn")
        page.wait_for_selector("#queue:not([hidden]) .q")
        page.screenshot(path=str(SHOTS / "04-queue.png"))
        check(page.locator("#queue .q").count() > 5, "очередь показывает ту же ленту «Моя волна»")
        check(page.locator("#queue .q__fav.is-fav").count() == 1, "лайк в очереди синхронизирован со страницей трека")
        page.click("#queue-close")

        print("Полноэкранный плеер")
        page.click("[data-do=expand]")
        page.wait_for_selector("#fs:not([hidden])")
        check(page.get_attribute("body", "data-focus") == "1" and page.get_attribute("#fs", "data-mode") == "default", "открыт режим default")
        check(page.locator("audio").count() == 1, "в приложении один <audio>")
        check(not visible(page, ".side") and not visible(page, ".topbar") and not visible(page, "#player"), "нет навигации, поиска и мини-плеера")
        check(page.text_content("#fs-title") == page.text_content("#now-title"), "показан текущий трек")
        check(page.evaluate("document.querySelector('#fs-play').dataset.playing") == "true", "кнопка Play/Pause отражает, что музыка играет")
        check(page.evaluate("!!document.querySelector('#fs') && ['side','main','player','queue'].every(id => document.getElementById(id).hasAttribute('inert'))"), "остальное приложение недоступно (inert)")
        page.wait_for_timeout(500)
        page.screenshot(path=str(SHOTS / "19-fs-default.png"))

        page.click("#fs-play")
        page.wait_for_function("document.querySelector('#audio').paused")
        page.wait_for_function("document.querySelector('#fs-play').dataset.playing === 'false'")
        check(True, "Pause реально ставит звук на паузу, кнопка показывает Play")
        page.click("#fs-play")
        page.wait_for_function("!document.querySelector('#audio').paused")
        check(True, "Play возобновляет звук")

        box = page.locator("#fs-bar").bounding_box()
        page.mouse.click(box["x"] + box["width"] * 0.6, box["y"] + 2)
        page.wait_for_function("document.querySelector('#audio').currentTime > 15")
        check(True, "клик по шкале перематывает звук")
        page.mouse.move(box["x"] + box["width"] * 0.6, box["y"] + 2)
        page.mouse.down()
        page.mouse.move(box["x"] + box["width"] * 0.2, box["y"] + 2, steps=6)
        page.mouse.up()
        page.wait_for_function("document.querySelector('#audio').currentTime < 10")
        check(True, "перетаскивание маркера перематывает звук")
        check(abs(page.evaluate("parseFloat(document.querySelector('#fs-bar').getAttribute('aria-valuenow'))") - 100 * audio_state(page)["t"] / 30) < 12, "шкала отражает реальный currentTime")

        check(page.evaluate("getComputedStyle(document.querySelector('#fs-volume')).getPropertyValue('--p').trim()") == "100%"
              and page.evaluate("getComputedStyle(document.querySelector('#now-volume')).getPropertyValue('--p').trim()") == "100%", "шкалы громкости показывают реальную громкость после смены трека")
        vb = page.locator("#fs-volume").bounding_box()
        page.mouse.click(vb["x"] + vb["width"] * 0.3, vb["y"] + 2)
        check(abs(audio_state(page)["vol"] - 0.3) < 0.06, "клик по шкале громкости меняет audio.volume")
        page.click("#fs .vol-btn")
        st = audio_state(page)
        check(st["muted"] and abs(st["vol"] - 0.3) < 0.06, "mute выключает звук, не обнуляя громкость")
        check(page.evaluate("getComputedStyle(document.querySelector('#fs-volume')).getPropertyValue('--p').trim()") == "0%", "при mute шкала пуста")
        page.click("#fs .vol-btn")
        st = audio_state(page)
        check((not st["muted"]) and abs(st["vol"] - 0.3) < 0.06, "после unmute громкость та же, что была")
        page.focus("#fs-volume")
        page.keyboard.press("ArrowRight")
        check(abs(audio_state(page)["vol"] - 0.35) < 0.02, "стрелка на шкале громкости меняет громкость на 5%")
        page.mouse.move(vb["x"] + vb["width"] * 0.35, vb["y"] + 2)
        page.mouse.down()
        page.mouse.move(vb["x"] - 6, vb["y"] + 2, steps=6)
        page.mouse.up()
        check(audio_state(page)["vol"] == 0, "перетаскивание в ноль даёт нулевую громкость")
        page.click("#fs .vol-btn")
        check(0.25 < audio_state(page)["vol"] < 0.4, "из нуля mute возвращает последнюю слышимую громкость")
        check(page.locator("#fs-volume").count() == 1 and page.locator("#fs [role=slider][aria-label=Громкость]").count() == 1, "в плеере одна шкала громкости")

        print("  режим «Текст песни»")
        before = audio_state(page)
        page.click("#fs-lyrics-btn")
        page.wait_for_selector("#fs-lyrics .ly")
        after = audio_state(page)
        check(page.get_attribute("#fs", "data-mode") == "lyrics" and page.get_attribute("#fs-lyrics-btn", "aria-checked") == "true", "режим lyrics включён, переключатель активен")
        check((not after["paused"]) and after["src"] == before["src"] and after["t"] >= before["t"] - 0.1, "музыка не перезапустилась и не сбросила время")
        check(page.locator("#fs-lyrics .ly").count() == 9, "текст взят из API (LRCLIB), а не из шаблона")
        pr, lr, vw = rect(page, ".fs__player"), rect(page, "#fs-lyrics-panel"), page.evaluate("window.innerWidth")
        check(pr["r"] < lr["l"] and pr["l"] + pr["w"] / 2 < vw * 0.5 and 0.34 < lr["w"] / vw < 0.45, "двухколоночная компоновка: плеер слева, текст справа (~40%)")
        page.wait_for_function("document.querySelector('#fs-lyrics .ly.cur')", timeout=8000)
        check(True, "текущая строка выделяется по реальным таймкодам")
        page.screenshot(path=str(SHOTS / "20-fs-lyrics.png"))
        page.click("#fs-lyrics .ly[data-i='7']")
        page.wait_for_function("document.querySelector('#audio').currentTime > 20")
        check(True, "клик по строке перематывает к ней")
        page.click("#fs-lyrics-btn")
        check(page.get_attribute("#fs", "data-mode") == "default" and not visible(page, "#fs-lyrics-panel") and not audio_state(page)["paused"], "повторный клик возвращает к обложке, музыка играет")

        print("  режим очереди")
        page.click("#fs-queue-btn")
        page.wait_for_selector("#fs-q-list .fq__row")
        check(page.get_attribute("#fs", "data-mode") == "queue" and not visible(page, "#fs-lyrics-panel"), "режим queue включён, текст закрыт")
        qr = rect(page, "#fs-queue")
        check(318 <= qr["w"] <= 382, f"ширина панели очереди {qr['w']:.0f}px")
        rows = queue_rows(page)
        cur = [r for r in rows if r["cur"]]
        check(len(cur) == 1 and cur[0]["t"] == page.text_content("#fs-title"), "«Сейчас играет» = текущий трек")
        total = int(page.text_content("#fs-q-count").split()[0])
        check(len(rows) <= total and len(rows) == total - cur[0]["i"], "список «Далее» соответствует настоящей очереди")
        check(page.locator("#fs-q-list .fq__row .fq__dur").first.text_content() == "0:30", "у строк есть длительность")
        pr, qr = rect(page, ".fs__player"), rect(page, "#fs-queue")
        check(pr["r"] < qr["l"] - 20, "между плеером и панелью есть промежуток")
        page.screenshot(path=str(SHOTS / "21-fs-queue.png"))

        up = [r for r in rows if not r["cur"]]
        a_, b_, c_ = up[0], up[1], up[2]
        page.drag_and_drop(f"#fs-q-list .fq__row[data-index='{c_['i']}'] .fq__grip", f"#fs-q-list .fq__row[data-index='{a_['i']}']", target_position={"x": 120, "y": 4})
        page.wait_for_function("(t) => document.querySelectorAll('#fs-q-list .fq__row')[1].querySelector('.fq__title').textContent === t", arg=c_["t"])
        order = [r["t"] for r in queue_rows(page)[1:4]]
        check(order == [c_["t"], a_["t"], b_["t"]], "перетаскивание за «ручку» меняет порядок")
        check(not audio_state(page)["paused"], "перестановка не прерывает воспроизведение")
        page.click("#fs [data-do=next]")
        page.wait_for_function("(t) => document.querySelector('#fs-title').textContent === t", arg=c_["t"])
        check(True, "«Следующий» играет трек, поставленный перетаскиванием (очередь общая)")

        rows = queue_rows(page)
        victim = rows[2]
        page.click(f"#fs-q-list .fq__row[data-index='{victim['i']}'] .fq__more")
        page.click("#menu [data-act=rm]")
        page.wait_for_function("(n) => document.querySelectorAll('#fs-q-list .fq__row').length === n", arg=len(rows) - 1)
        check(victim["t"] not in [r["t"] for r in queue_rows(page)] and not audio_state(page)["paused"], "удаление через «⋯» работает, звук не прервался")
        rows = queue_rows(page)
        page.click(f"#fs-q-list .fq__row[data-index='{rows[3]['i']}'] .fq__more")
        page.click("#menu [data-act=playnow]")
        page.wait_for_function("(t) => document.querySelector('#fs-title').textContent === t", arg=rows[3]["t"])
        check(page.get_attribute("#fs", "data-mode") == "queue", "«Играть сейчас» переключает трек, панель остаётся")
        rows = queue_rows(page)
        page.click(f"#fs-q-list .fq__row[data-index='{rows[1]['i']}'] .fq__main")
        page.wait_for_function("(t) => document.querySelector('#fs-title').textContent === t", arg=rows[1]["t"])
        check(True, "клик по строке очереди запускает трек")

        page.click("#fs-q-clear")
        page.wait_for_selector("#dlg-confirm[open]")
        page.click("#dc-ok")
        page.wait_for_function("document.querySelectorAll('#fs-q-list .fq__row').length === 1")
        check(page.text_content("#fs-q-count").strip() == "1 трек" and not audio_state(page)["paused"], "«Очистить» с подтверждением оставляет играющий трек")

        print("  закрытие и клавиатура")
        page.keyboard.press("Escape")
        check(page.get_attribute("#fs", "data-mode") == "default" and not visible(page, "#fs-queue") and page.get_attribute("body", "data-focus") == "1", "Esc сначала закрывает панель очереди")
        page.focus("#fs-lyrics-btn")
        paused_before = audio_state(page)["paused"]
        page.keyboard.press("Space")
        page.wait_for_timeout(200)
        check(audio_state(page)["paused"] != paused_before and page.get_attribute("#fs", "data-mode") == "default", "пробел = Play/Pause даже при фокусе на переключателе")
        page.keyboard.press("Space")
        page.wait_for_function("!document.querySelector('#audio').paused")
        page.keyboard.press("Tab")
        check(page.evaluate("document.activeElement.closest('#fs') !== null"), "Tab остаётся внутри полноэкранного плеера")
        page.keyboard.press("Escape")
        check(page.get_attribute("body", "data-focus") == "0" and not visible(page, "#fs"), "второй Esc закрывает полноэкранный режим")
        check(page.get_attribute("body", "data-view") == "now" and page.evaluate("document.activeElement.dataset.do") == "expand", "возврат на страницу трека, фокус вернулся на кнопку")
        check(not audio_state(page)["paused"], "после закрытия музыка играет")
        check(page.evaluate("['side','main','player','queue'].every(id => !document.getElementById(id).hasAttribute('inert'))"), "приложение снова доступно")

        print("  размеры окна")
        for (w, h) in [(1920, 1080), (1366, 768), (980, 620)]:
            page.set_viewport_size({"width": w, "height": h})
            page.click("[data-do=expand]")
            page.wait_for_selector("#fs:not([hidden])")
            for mode in ("default", "lyrics", "queue"):
                if mode == "lyrics":
                    page.click("#fs-lyrics-btn")
                elif mode == "queue":
                    page.click("#fs-lyrics-btn") if page.get_attribute("#fs", "data-mode") == "lyrics" else None
                    page.click("#fs-queue-btn")
                page.wait_for_timeout(260)
                ok = all(fits(page, sel) for sel in ("#fs-title", "#fs-play", "#fs-bar", "#fs-volume", ".fs__cover"))
                if mode == "queue":
                    ok = ok and fits(page, "#fs-queue") and rect(page, ".fs__player")["r"] <= rect(page, "#fs-queue")["l"] + 1
                if mode == "lyrics":
                    ok = ok and fits(page, "#fs-lyrics-panel") and rect(page, ".fs__player")["r"] <= rect(page, "#fs-lyrics-panel")["l"] + 1
                cover = rect(page, ".fs__cover")["w"]
                check(ok and not page.evaluate("document.querySelector('#fs').scrollWidth > window.innerWidth + 1"), f"{w}x{h}, {mode}: всё помещается, обложка {cover:.0f}px")
                page.screenshot(path=str(SHOTS / f"22-fs-{w}-{mode}.png"))
            page.keyboard.press("Escape")
            page.keyboard.press("Escape")
            page.wait_for_selector("#fs", state="hidden")
        page.set_viewport_size({"width": 1440, "height": 900})

        print("Назад на главную: состояние сохранено")
        page.click("[data-do=back]")
        page.wait_for_selector("#view-home:not([hidden])")
        page.click("#hero-like")
        page.wait_for_function("document.querySelector('#hero-like').classList.contains('is-fav')")
        favs = page.evaluate("fetch('/api/favorites').then(r => r.json()).then(d => d.items.map(t => t.title))")
        check(page.text_content("#hero-title") in favs, "лайк на баннере главной сохраняется в избранное")
        page.click("#hero-like")
        page.wait_for_function("!document.querySelector('#hero-like').classList.contains('is-fav')")
        check(not page.evaluate("document.querySelector('#audio').paused"), "воспроизведение не сбросилось при навигации")
        check(page.locator("#continue-row .minicard").count() >= 1, "«Продолжить слушать» из реальной истории")
        page.wait_for_timeout(600)
        check(page.locator("#artist-mixes .mix").count() >= 1, "«Миксы по артистам» собраны из истории и избранного")
        check(page.locator("#mix-row .mixcard").first.get_attribute("data-mix").startswith("radio:"), "ряд «Под твою волну» начинается с радио по недавним трекам")
        page.screenshot(path=str(SHOTS / "03c-home-with-history.png"))

        print("Поиск и мини-плеер")
        page.fill("#search-input", "gspd")
        page.keyboard.press("Enter")
        page.wait_for_selector("#tracks .row:not(.row--skel)")
        check(visible(page, "#player"), "вне главной мини-плеер виден")
        check(page.get_attribute("body", "data-view") == "search", "открыт поиск")
        check(page.locator("#tracks .row").count() == 20, "результаты поиска отрисованы")
        page.screenshot(path=str(SHOTS / "05-search.png"))
        page.click("#tabs [data-tab=artists]")
        page.wait_for_selector("#cards .card--artist")
        check(page.locator("#cards .card--artist").count() == 6, "вкладка «Артисты»")
        page.screenshot(path=str(SHOTS / "06-search-artists.png"))
        page.locator("#cards .card--artist").first.click()
        page.wait_for_selector("#detail:not([hidden])")
        page.wait_for_selector("#albums-row .card")
        check(page.inner_text("#d-title") == "GSPD", "страница артиста")
        check(page.locator("#albums-row .card").count() == 5 and page.locator("#related-row .card").count() == 4, "альбомы и похожие исполнители")
        page.screenshot(path=str(SHOTS / "07-artist.png"))
        page.locator("#albums-row .card").first.click()
        page.wait_for_selector("#tracks .row:not(.row--skel)")
        check(page.locator("#tracks .row").count() == 8 and page.text_content("#d-kind") == "Альбом", "страница альбома")
        page.screenshot(path=str(SHOTS / "08-album.png"))
        page.click("#back")
        page.wait_for_selector("#albums-row .card")
        check(page.text_content("#d-kind") == "Артист", "«Назад» возвращает на артиста")

        print("Избранное, плейлисты, меню")
        page.click("#tracks .row:nth-child(2) .track__fav")
        page.wait_for_function("document.querySelectorAll('#tracks .row:nth-child(2) .track__fav.is-fav').length === 1")
        check(True, "лайк в строке списка")
        page.click("#tracks .row:nth-child(1) .track__more")
        page.click("#menu [data-act=playlist]")
        page.wait_for_selector("#dlg-pick[open] .pick__item")
        page.click("#pick-list [data-pl=new]")
        page.wait_for_selector("#dlg[open]")
        page.fill("#dlg-input", "Тестовый плейлист")
        page.click("#dlg-ok")
        page.wait_for_selector("#toast:not([hidden])")
        check("Добавлено в плейлист" in page.inner_text("#toast"), "создание плейлиста и добавление трека через диалоги")
        page.click(".nav__item[data-view=playlists]")
        page.wait_for_selector("#cards .card--playlist")
        page.screenshot(path=str(SHOTS / "09-playlists.png"))
        page.locator("#cards .card--playlist").first.click()
        page.wait_for_selector("#d-rename:not([hidden])")
        page.wait_for_selector("#tracks .row:not(.row--skel)")
        check(page.locator("#tracks .row").count() == 1 and page.locator("#d-delete:not([hidden])").count() == 1, "страница плейлиста с переименованием и удалением")
        page.screenshot(path=str(SHOTS / "10-playlist.png"))
        page.click("#d-delete")
        page.wait_for_selector("#dlg-confirm[open]")
        page.click("#dc-ok")
        page.wait_for_selector("#cards .card--new")
        check(page.locator("#cards .card--playlist").count() == 0, "удаление плейлиста с подтверждением")

        print("Остальные разделы")
        page.click(".nav__item[data-view=collection]")
        page.wait_for_selector("#col-tiles .tile")
        page.wait_for_timeout(300)
        check(page.locator("#col-tiles .tile").count() == 3 and page.locator("#col-artists .card").count() >= 1, "коллекция: плитки и исполнители из избранного")
        page.screenshot(path=str(SHOTS / "11-collection.png"))
        page.click(".nav__item[data-view=liked]")
        page.wait_for_selector("#tracks .row:not(.row--skel)")
        check(page.locator("#tracks .row").count() == 2, "«Нравится» = два трека, отмеченных выше")
        page.screenshot(path=str(SHOTS / "12-liked.png"))
        page.click(".nav__item[data-view=history]")
        page.wait_for_selector("#tracks .row:not(.row--skel)")
        check(page.locator("#tracks .row").count() >= 1 and visible(page, "#clear-history"), "история с кнопкой очистки")
        page.screenshot(path=str(SHOTS / "13-history.png"))
        page.click(".nav__item[data-view=downloads]")
        page.wait_for_selector("#empty:not([hidden])")
        check("скачал" in page.inner_text("#empty"), "пустое состояние «Скачанные»")
        page.screenshot(path=str(SHOTS / "14-downloads.png"))
        page.click(".nav__item[data-view=settings]")
        page.wait_for_selector("#view-settings:not([hidden])")
        page.fill("#set-name", "Mercury")
        page.dispatch_event("#set-name", "change")
        check(page.inner_text("#profile-name") == "Mercury", "имя профиля сохраняется и показывается в панели")
        page.select_option("#set-theme", "light")
        page.wait_for_function("document.documentElement.dataset.theme === 'light'")
        page.screenshot(path=str(SHOTS / "15-settings-light.png"))
        page.select_option("#set-theme", "dark")
        page.wait_for_function("document.documentElement.dataset.theme === 'dark'")
        check(page.evaluate("fetch('/api/settings').then(r => r.json()).then(d => d.values.theme)") == "dark", "настройки реально пишутся в backend")

        print("Тематический микс и боковая панель")
        page.click("#mixes .mix >> nth=0")
        page.wait_for_selector("#tracks .row:not(.row--skel)")
        check(page.text_content("#d-kind") == "Микс" and page.locator(".nav__item[aria-current='page']").count() == 0, "микс открывается страницей, пункт меню не подсвечен")
        page.click("#d-play")
        page.wait_for_function("document.querySelector('#audio').currentTime > 0.2 || !document.querySelector('#audio').paused")
        check(page.locator("#mixes .mix.is-active").count() == 1, "играющий микс отмечен в панели")
        page.screenshot(path=str(SHOTS / "16-mix.png"))

        print("Узкое окно (980x640) и светлая тема")
        page.set_viewport_size({"width": 980, "height": 640})
        page.click(".nav__item[data-view=home]")
        page.wait_for_timeout(700)
        page.screenshot(path=str(SHOTS / "17-home-980.png"))
        check(page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1 && document.querySelector('#main').scrollWidth <= document.querySelector('#main').clientWidth + 1"), "горизонтального переполнения на 980px нет")
        page.click("#hero-title")
        page.wait_for_selector("#view-now:not([hidden])")
        page.wait_for_timeout(500)
        page.screenshot(path=str(SHOTS / "18-now-980.png"))
        check(page.evaluate("document.querySelector('#main').scrollWidth <= document.querySelector('#main').clientWidth + 1"), "страница трека на 980px без горизонтальной прокрутки")

        browser.close()

    print(f"\nПроверок: {checks}, провалено: {len(failures)}")
    real = [e for e in errors]
    print("Ошибки консоли/страницы:", real if real else "нет")
    return 1 if failures or real else 0


if __name__ == "__main__":
    sys.exit(main())