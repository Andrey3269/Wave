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

sys.path.insert(0, str(Path(__file__).resolve().parent))
from wavetest.hlsfix import MP3_FRAME_SEC, media_playlist, mp3_frames  # noqa: E402  (тот же каталог tests/)

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


# Поля Track, которые до S6 терялись при сохранении в избранное и плейлисты (находка M6)
FULL_FIELDS = {"explicit": True, "plays": 12345, "released": "2025-03-01T10:00:00Z"}


def artist_tracks(n: int) -> list[dict]:
    """Треки страницы артиста. Второй (trk00000061) лайкается из строки списка — у него все поля Track."""
    tracks = [track(60 + i) for i in range(n)]
    tracks[1].update(FULL_FIELDS)
    return tracks


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


# Названия фирменных функций других сервисов: в интерфейсе их быть не должно (находка M13)
FOREIGN_NAMES = ("Моя волна", "Моя Волна", "Открытия недели", "Встряхнуть мою волну", "Под твою волну", "Слушать волну")


def foreign_names(page) -> list[str]:
    """Чужие фирменные названия в видимом тексте, title и aria-label (всё, что читает пользователь или скринридер)."""
    return page.evaluate("""(names) => {
        const texts = [document.body.innerText, document.title,
            ...[...document.querySelectorAll('[title],[aria-label]')].flatMap(n => [n.title || '', n.getAttribute('aria-label') || ''])];
        return names.filter(x => texts.some(t => t.includes(x)));
    }""", list(FOREIGN_NAMES))


# ---------------------------------------------------------------- подмена сетевых эндпоинтов
# Режим ответа /api/recommendations для ленты главной (раздел «Источник рекомендаций недоступен»):
# normal | failed (источник не ответил) | empty (ответил, но предложить нечего) | partial (часть запросов упала)
RECS = {"mode": "normal"}             # ещё personal: лента собрана по истории (personalized=true)
# Режим /api/stream/: normal | js_runtime (нет JS-runtime Deno — ответ backend из S3, stream._runtime_error)
STREAM = {"mode": "normal"}
JS_RUNTIME_DETAIL = "Компонент воспроизведения YouTube (Deno) не найден или повреждён. Переустановите приложение."
# Загрузки (S5). Скачать по-настоящему в e2e нечего (источников нет), поэтому на время раздела «Загрузки»
# /api/downloads* отвечает этот скрипт: этапы задаёт тест (DL["stage"]), исходы — DL_OUTCOME. Вне раздела
# запросы уходят в настоящий backend. Сама загрузка, какие треки скачиваются и воспроизведение с диска
# проверяются API-тестами (tests/api/test_downloads.py, test_download_policy.py).
DL = {"mock": False, "stage": {}, "posts": [], "done": {}}
DL_OUTCOME = {"trk00000041": ("error", "unavailable", "Трек недоступен"),
              "trk00000042": ("error", "network", "Нет связи с YouTube Music. Проверьте подключение к интернету.")}


# HLS SoundCloud (S8). Сеть подменена: /playback отвечает kind=hls, плейлист и сегменты — настоящие mp3-кадры
# (их воспроизводит hls.js в Chromium). Сам hls.js приходит с backend (frontend/vendor), как в приложении.
HLS_TOKEN = "e2eHlsToken0123456789AB"
HLS_SEG_FRAMES = 383                                                  # ≈10 с
HLS_SEG = mp3_frames(HLS_SEG_FRAMES)
HLS_PLAYLIST = media_playlist([f"/api/stream/hls/{HLS_TOKEN}/main/s{i}" for i in range(3)],
                              [round(HLS_SEG_FRAMES * MP3_FRAME_SEC, 6)] * 3)
HLS = {"released": [], "segments": 0}
# Поиск «m11» (раздел M11): вторая страница отдаётся только после того, как тест её разрешит. До этого
# запрос обрывается (как пропавшая сеть) — иначе автоподгрузка пришла бы раньше, чем тест проверит очередь.
M11 = {"page2": False}


def sc_track(n: int) -> dict:
    return {"id": str(3000000000 + n), "source": "sc", "title": f"SoundCloud {n}", "artist": "Uploader", "artists": ["Uploader"],
            "album": None, "duration": 30, "cover": None, "explicit": False, "stream_url": f"/api/stream/sc/{3000000000 + n}",
            "artist_id": None, "artist_ids": [], "plays": None, "released": None, "url": None}


def dl_job(tid: str) -> dict:
    stage = DL["stage"][tid]
    if isinstance(stage, tuple):                                       # готовый исход: ошибка с кодом
        _, code, text = stage
        return {"state": "error", "progress": 0, "error": text, "code": code, "received": 0, "total": None,
                "finished_at": 1.0}
    job = {"state": stage, "progress": 0, "error": None, "code": None, "received": 0, "total": None}
    if stage == "downloading":
        job.update(progress=40, received=400_000, total=1_000_000)
    elif stage == "processing":
        job.update(progress=99, received=1_000_000, total=1_000_000)
    elif stage == "done":
        job.update(progress=100, received=1_000_000, total=1_000_000, finished_at=2.0)
    return job


def dl_mock(route: Route, path: str) -> bool:
    req = route.request
    if path == "/api/downloads" and req.method == "POST":
        body = req.post_data_json
        DL["posts"].append(body["id"])
        if body["id"] in DL["done"]:
            js(route, {"state": "done", "progress": 100})
            return True
        outcome = DL_OUTCOME.get(body["id"])
        DL["stage"][body["id"]] = outcome if outcome else "preparing"
        js(route, {"state": "preparing", "progress": 0, "error": None, "code": None}, 202)
        return True
    if path == "/api/downloads/jobs":
        jobs = {f"yt:{tid}": dl_job(tid) for tid in DL["stage"]}
        for tid, stage in list(DL["stage"].items()):
            if stage == "done":
                DL["done"][tid] = {**track(int(tid[3:])), "downloaded": True, "size": 1_000_000, "has_lyrics": False}
        js(route, jobs)
        return True
    if path == "/api/downloads" and req.method == "GET":
        js(route, {"items": list(DL["done"].values()), "dir": "e2e"})
        return True
    m = re.match(r"^/api/downloads/yt/(\w+)$", path)
    if m and req.method == "DELETE":
        DL["done"].pop(m.group(1), None)
        DL["stage"].pop(m.group(1), None)
        js(route, {"ok": True})
        return True
    return False


def handle(route: Route) -> None:
    req = route.request
    u = urlparse(req.url)
    path, q = u.path, {k: v[0] for k, v in parse_qs(u.query).items()}

    if "googleusercontent.com" in u.netloc:
        return route.fulfill(status=200, content_type="image/svg+xml", body=cover_svg(req.url))

    if DL["mock"] and path.startswith("/api/downloads") and dl_mock(route, path):
        return None

    if path.startswith("/api/stream/hls/"):
        if req.method == "DELETE":
            HLS["released"].append(path.rsplit("/", 1)[-1])
            return route.fulfill(status=204)
        if path.endswith("/main.m3u8"):
            return route.fulfill(status=200, body=HLS_PLAYLIST, headers={"Content-Type": "application/vnd.apple.mpegurl"})
        if re.search(r"/main/s\d+$", path):
            HLS["segments"] += 1
            return route.fulfill(status=200, body=HLS_SEG, headers={"Content-Type": "audio/mpeg"})
        return route.fulfill(status=404)
    m = re.match(r"^/api/stream/sc/(\d+)/playback$", path)
    if m:
        if m.group(1) == str(3000000000):                              # первый трек — только HLS
            return js(route, {"kind": "hls", "url": f"/api/stream/hls/{HLS_TOKEN}/main.m3u8", "preview": False, "codec": "mp3"})
        return js(route, {"kind": "progressive", "url": f"/api/stream/sc/{m.group(1)}", "preview": False})

    if path.startswith("/api/stream/"):
        if q.get("warm"):
            return route.fulfill(status=204)
        if STREAM["mode"] == "js_runtime":
            return route.fulfill(status=503, content_type="application/json", headers={"X-Wave-Error": "js_runtime"},
                                 body=json.dumps({"detail": JS_RUNTIME_DETAIL}, ensure_ascii=False))
        rng = re.match(r"bytes=(\d+)-(\d*)", req.headers.get("range", ""))
        if rng:
            a = int(rng.group(1)); b = min(int(rng.group(2) or len(AUDIO) - 1), len(AUDIO) - 1)
            return route.fulfill(status=206, body=AUDIO[a:b + 1], headers={
                "Content-Type": "audio/wav", "Accept-Ranges": "bytes", "Content-Range": f"bytes {a}-{b}/{len(AUDIO)}"})
        return route.fulfill(status=200, body=AUDIO, headers={"Content-Type": "audio/wav", "Accept-Ranges": "bytes"})

    if path == "/api/recommendations":
        if q.get("id"):                                               # «радио» трека
            title = q.get("title", "")
            return js(route, {"tracks": [track(20 + i, f"Похоже на «{title}»") for i in range(14)], "personalized": False,
                              "radio": True, "page": 0, "failed_sources": [], "source_status": {"yt": "ok"}})
        page = int(q.get("page", 0))
        mode = RECS["mode"]
        if mode == "failed":
            return js(route, {"tracks": [], "personalized": False, "radio": False, "page": page,
                              "failed_sources": ["yt"], "source_status": {"yt": "failed"}})
        if mode == "empty":
            return js(route, {"tracks": [], "personalized": False, "radio": False, "page": page,
                              "failed_sources": [], "source_status": {"yt": "ok"}})
        base = 1 + page * 24
        reasons = ["Подборка"] if not page else ["Любимый исполнитель: GSPD", "Из твоей истории: Vtornik"]
        bad = ["yt"] if mode == "partial" else []
        return js(route, {"tracks": [track(base + i, reasons[i % len(reasons)]) for i in range(24 if page else 30)],
                          "personalized": bool(page) or mode == "personal", "radio": False, "page": page,
                          "failed_sources": bad, "source_status": {"yt": "partial" if bad else "ok"}})

    if path == "/api/search" and q.get("q") == "m11":                  # две страницы для очереди (M11)
        off = int(q.get("offset", 0))
        if off and not M11["page2"]:
            return route.abort("failed")
        return js(route, {"query": "m11", "count": 20, "tracks": [track(100 + off + i) for i in range(20)] if off < 40 else [],
                          "failed_sources": [], "has_more": off + 20 < 40})
    if path == "/api/search" and q.get("q") == "s10":                 # очередь из 10 треков (повтор и перемешивание, S10)
        return js(route, {"query": "s10", "count": 10, "tracks": [track(200 + i) for i in range(10)], "failed_sources": [],
                          "has_more": False})
    if path == "/api/search" and q.get("sources") == "sc":
        return js(route, {"query": q.get("q"), "count": 3, "tracks": [sc_track(i) for i in range(3)], "failed_sources": [],
                          "has_more": False, "source_status": {"sc": "ok"}})
    if path == "/api/search":
        return js(route, {"query": q.get("q"), "count": 20, "tracks": [track(40 + i) for i in range(20)], "failed_sources": [], "has_more": False})

    if path.startswith("/api/search/artists"):
        return js(route, {"items": [{"id": f"UCart{i:04d}", "source": "yt", "kind": "artist", "name": ARTISTS[i], "cover": track(i)["cover"], "subtitle": "1,2 млн подписчиков"} for i in range(6)], "has_more": False})
    if path.startswith("/api/search/albums"):
        return js(route, {"items": [{"id": f"MPRE{i}", "source": "yt", "kind": "album", "title": f"Альбом {i + 1}", "artist": ARTISTS[i], "year": "2025", "cover": track(i)["cover"], "track_count": 10, "playlist_id": f"OLAK{i}"} for i in range(6)], "has_more": False})

    m = re.match(r"^/api/artist/(\w+)/([\w-]+)(/tracks)?$", path)
    if m:
        if m.group(3):
            return js(route, {"tracks": artist_tracks(10), "has_more": False})
        albums = [{"id": f"MPRE{i}", "source": "yt", "kind": "album", "title": f"Альбом {i + 1}", "artist": "GSPD", "year": "202" + str(i), "cover": track(i + 2)["cover"], "track_count": 9, "playlist_id": f"OLAK{i}"} for i in range(5)]
        related = [{"id": f"UCart{i:04d}", "source": "yt", "kind": "artist", "name": ARTISTS[i + 3], "cover": track(i + 3)["cover"], "subtitle": "300 тыс. подписчиков"} for i in range(4)]
        return js(route, {"artist": {"name": "GSPD", "cover": track(1)["cover"], "subtitle": "2,5 млн подписчиков"}, "albums": albums, "related": related, "tracks": artist_tracks(20), "has_more": True})

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


# Media Session (WIN-11, автоматическая часть): обработчики, которые приложение регистрирует через
# navigator.mediaSession.setActionHandler, запоминаются, чтобы тест мог вызвать их как системные медиаклавиши.
# Настоящие медиаклавиши и оверлей Windows этим не проверяются (ручной чек-лист S10_WINDOWS_CHECK.md).
MEDIA_SESSION_SPY = """
(() => {
  window.__ms = {};
  const ms = navigator.mediaSession;
  if (!ms) return;
  const real = ms.setActionHandler.bind(ms);
  ms.setActionHandler = (name, fn) => { window.__ms[name] = fn; return real(name, fn); };
})();
"""


# Тост показывает одно сообщение: следующее (например «Очередь закончилась…», когда трек доиграл) затирает
# прежнее. Проверки «сообщение было показано» смотрят журнал всех показанных тостов, а не только последний.
TOAST_LOG = """
document.addEventListener("DOMContentLoaded", () => {
  window.__toasts = [];
  const t = document.querySelector("#toast");
  new MutationObserver(() => { if (!t.hidden && t.textContent) window.__toasts.push(t.textContent); })
    .observe(t, {childList: true, characterData: true, subtree: true, attributes: true, attributeFilter: ["hidden"]});
});
"""


def shown_toast(page, text: str) -> bool:
    return page.evaluate("(x) => (window.__toasts || []).some((t) => t.includes(x))", text)


def wait_toast(page, text: str, timeout: int = 10000) -> bool:
    """Тост с этим текстом был показан (дожидается его, даже если потом его затёр другой)."""
    try:
        page.wait_for_function("(x) => (window.__toasts || []).some((t) => t.includes(x))", arg=text, timeout=timeout)
        return True
    except Exception:
        return False


def media_session(page) -> dict:
    return page.evaluate("""(() => { const ms = navigator.mediaSession, m = ms.metadata;
        return {state: ms.playbackState, title: m && m.title, artist: m && m.artist, album: m && m.album,
                art: m ? m.artwork.length : 0, handlers: Object.keys(window.__ms || {}).sort()}; })()""")


def ms_action(page, name: str, details: dict | None = None) -> None:
    page.evaluate("([n, d]) => window.__ms[n]({action: n, ...(d || {})})", [name, details])


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
        ctx.add_init_script(MEDIA_SESSION_SPY)
        ctx.add_init_script(TOAST_LOG)
        page = ctx.new_page()
        page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
        page.on("console", lambda m: errors.append(f"console.{m.type}: {m.text}") if m.type == "error" and "Failed to load resource" not in m.text else None)

        print("Главная")
        page.goto(BASE)
        # Явный признак готовности ленты (раньше ждали, пока заголовок перестанет быть «Моя волна»)
        page.wait_for_function("document.querySelector('#hero').dataset.feed === 'ready'", timeout=15000)
        page.wait_for_timeout(500)
        page.screenshot(path=str(SHOTS / "01-home.png"))
        check(foreign_names(page) == [], "в интерфейсе нет фирменных названий других сервисов")
        check(page.text_content("#hero-eyebrow-text") == "С чего начать" and page.inner_text("#hero-play-label") == "Слушать подборку",
              "без истории лента названа «С чего начать», кнопка «Слушать подборку»")
        check(page.text_content("#h-mixes") == "Тематические миксы", "без истории ряд миксов не называется «по вашим трекам»")
        year = page.evaluate("new Date().getFullYear()")
        check(page.locator("#mixes .mix[data-mix='theme:discover'] .mix__title").text_content() == f"Новинки {year}"
              and page.locator("#mixes .mix[data-mix='theme:shake'] .mix__title").text_content() == "Другая подборка",
              "тематические миксы названы по содержанию")
        check(page.locator(".side").count() == 1 and page.locator("aside.side + aside, .side ~ .side").count() == 0, "одна боковая панель, второй постоянной колонки нет")
        check(page.locator(".nav__item[aria-current='page']").count() == 1, "ровно один активный пункт меню")
        check(page.locator(".nav__item[aria-current='page']").first.get_attribute("data-view") == "home", "активна «Главная»")
        check(page.locator("#mixes .mix").count() == 5, "в «Твоей музыке» 5 миксов")
        check(not visible(page, "#player"), "на главной нижнего плеера нет (ничего не играет)")
        check(page.locator("#mix-row .mixcard").count() >= 5, "ряд миксов заполнен")
        check(page.locator("#new-grid .tcard").count() >= 20, "«Новое для тебя» заполнено реальными треками")
        check("Химия" in page.inner_text("#hero-title") or page.inner_text("#hero-title").strip() != "", "заголовок баннера из данных рекомендаций")

        print("«Слушать подборку»")
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
        check(page.locator("#queue .q").count() > 5, "очередь показывает ту же ленту главной")
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
        check(page.locator("#mix-row .mixcard").first.get_attribute("data-mix").startswith("radio:"), "ряд миксов начинается с радио по недавним трекам")
        check(page.text_content("#h-mixes") == "Миксы по вашим трекам", "с историей ряд называется «Миксы по вашим трекам»")
        page.screenshot(path=str(SHOTS / "03c-home-with-history.png"))

        print("Media Session: медиаклавиши и системный оверлей (WIN-11, автоматическая часть)")
        ms = media_session(page)
        check({"play", "pause", "previoustrack", "nexttrack", "seekbackward", "seekforward", "seekto", "stop"} <= set(ms["handlers"]),
              "Media Session: зарегистрированы Play, Pause, Previous, Next, Seek Backward/Forward, Seek To, Stop")
        check(ms["title"] == page.text_content("#hero-title") and ms["artist"] and ms["art"] == 1,
              "Media Session: название, исполнитель и обложка текущего трека")
        check(ms["state"] == "playing", "Media Session: состояние «играет»")
        ms_action(page, "pause")
        page.wait_for_function("document.querySelector('#audio').paused && navigator.mediaSession.playbackState === 'paused'")
        check(True, "Media Session: Pause ставит паузу, состояние «пауза»")
        ms_action(page, "play")
        page.wait_for_function("!document.querySelector('#audio').paused && navigator.mediaSession.playbackState === 'playing'")
        check(True, "Media Session: Play продолжает воспроизведение")
        ms_action(page, "seekto", {"seekTime": 12})
        page.wait_for_function("Math.abs(document.querySelector('#audio').currentTime - 12) < 1.5")
        ms_action(page, "seekbackward", {"seekOffset": 5})
        page.wait_for_function("Math.abs(document.querySelector('#audio').currentTime - 7) < 1.5")
        ms_action(page, "seekforward", {})
        page.wait_for_function("Math.abs(document.querySelector('#audio').currentTime - 17) < 1.5")
        check(True, "Media Session: Seek To, Seek Backward и Seek Forward (по умолчанию 10 с) перематывают")
        ms_title = ms["title"]
        ms_action(page, "nexttrack")
        page.wait_for_function("(t) => navigator.mediaSession.metadata && navigator.mediaSession.metadata.title !== t", arg=ms_title)
        check(media_session(page)["title"] == page.text_content("#hero-title"), "Media Session: Next переключает трек и обновляет название")
        page.wait_for_function("!document.querySelector('#audio').paused")
        ms_action(page, "previoustrack")
        page.wait_for_function("(t) => navigator.mediaSession.metadata && navigator.mediaSession.metadata.title === t", arg=ms_title)
        check(page.text_content("#hero-title") == ms_title, "Media Session: Previous возвращает предыдущий трек")
        page.wait_for_function("!document.querySelector('#audio').paused")

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
        saved = page.evaluate("fetch('/api/favorites').then(r => r.json())"
                              ".then(d => d.items.find(t => t.id === 'trk00000061') || null)")
        check(saved is not None and {k: saved.get(k) for k in FULL_FIELDS} == FULL_FIELDS,
              "избранное сохраняет explicit, прослушивания и дату выхода трека (S6, M6)")
        page.click("#tracks .row:nth-child(1) .track__more")
        page.click("#menu [data-act=playlist]")
        page.wait_for_selector("#dlg-pick[open] .pick__item")
        page.click("#pick-list [data-pl=new]")
        page.wait_for_selector("#dlg[open]")
        page.fill("#dlg-input", "Тестовый плейлист")
        page.click("#dlg-ok")
        # Ждём именно это сообщение: другой тост (например «Очередь закончилась…») мог быть уже виден
        check(wait_toast(page, "Добавлено в плейлист"), "создание плейлиста и добавление трека через диалоги")
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
        check("без интернета" in page.inner_text("#empty") and "Creative Commons" not in page.inner_text("#empty"),
              "«Скачанные»: скачанное играет без интернета, без лишних условий источников")
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

        print("Загрузки (S5)")
        DL["mock"] = True
        page.fill("#search-input", "gspd")
        page.keyboard.press("Enter")
        page.click("#tabs [data-tab=tracks]")                       # вкладка «Артисты» осталась от раздела поиска
        page.wait_for_selector("#tracks .row:not(.row--skel)")
        row_dl = "#tracks .row:nth-child({}) .track__dl".format
        label_starts = "([s, t]) => (document.querySelector(s)?.getAttribute('aria-label') || '').startsWith(t)"
        has_class = "([s, c]) => !!document.querySelector(s)?.classList.contains(c)"
        toast_has = "(t) => (document.querySelector('#toast')?.innerText || '').includes(t)"
        ok_id = "trk00000040"
        page.click(row_dl(1))
        page.wait_for_function(label_starts, arg=[row_dl(1), "Подготовка"])
        check(page.is_disabled(row_dl(1)) and DL["posts"] == [ok_id], "«Скачать»: запрос ушёл, этап «Подготовка», кнопка занята")
        DL["stage"][ok_id] = "downloading"
        page.wait_for_function(label_starts, arg=[row_dl(1), "Скачивание: 40%"])
        check(page.inner_text(row_dl(1) + " .track__pct") == "40%", "этап «Скачивание» с реальным процентом от backend")
        page.screenshot(path=str(SHOTS / "16b-download-progress.png"))
        DL["stage"][ok_id] = "processing"
        page.wait_for_function(label_starts, arg=[row_dl(1), "Обработка"])
        check(True, "этап «Обработка» (обложка, текст, metadata.json)")
        DL["stage"][ok_id] = "done"
        page.wait_for_function(has_class, arg=[row_dl(1), "is-done"])
        page.wait_for_function(toast_has, arg="Готово")
        check(page.get_attribute(row_dl(1), "aria-label").startswith("Скачано") and not page.is_disabled(row_dl(1)),
              "готово: тост «Готово», кнопка «Скачано» (нажатие — удалить)")

        page.click(row_dl(2))
        page.wait_for_function(has_class, arg=[row_dl(2), "is-denied"])
        page.wait_for_function(toast_has, arg="Трек недоступен")
        check("Трек недоступен" in page.get_attribute(row_dl(2), "title"),
              "поток недоступен: причина в подсказке кнопки и в сообщении, вместо бесконечного прогресса")
        posts = len(DL["posts"])
        page.click(row_dl(2))
        page.wait_for_function(toast_has, arg="Скачивание недоступно")
        check(len(DL["posts"]) == posts, "повторное нажатие на недоступный трек объясняет причину и не шлёт запрос")
        page.click("#tracks .row:nth-child(2) .track__more")
        check(page.inner_text("#menu [data-act=download]") == "Скачивание недоступно", "меню трека не обещает недоступное скачивание")
        page.keyboard.press("Escape")
        page.screenshot(path=str(SHOTS / "16c-download-denied.png"))

        page.click(row_dl(3))
        page.wait_for_function(has_class, arg=[row_dl(3), "is-error"])
        check("повторить" in page.get_attribute(row_dl(3), "aria-label"), "временная ошибка: причина и предложение повторить")
        DL_OUTCOME.pop("trk00000042")
        posts = len(DL["posts"])
        page.click(row_dl(3))
        page.wait_for_function(label_starts, arg=[row_dl(3), "Подготовка"])
        check(len(DL["posts"]) == posts + 1, "после временной ошибки загрузку можно запустить снова")
        DL["stage"]["trk00000042"] = "done"
        page.wait_for_function(has_class, arg=[row_dl(3), "is-done"])

        page.click(".nav__item[data-view=downloads]")
        page.wait_for_function("document.querySelectorAll('#tracks .row:not(.row--skel)').length === 2")
        check(page.locator("#tracks .row .track__dl.is-done").count() == 2 and "Скачано: 2" in page.inner_text("body"),
              "«Скачанные»: оба готовых трека в списке")
        page.screenshot(path=str(SHOTS / "16d-downloads-list.png"))
        page.locator("#tracks .row").first.click(position={"x": 260, "y": 20})
        page.wait_for_function("(id) => document.querySelector('#audio').src.includes(id)", arg=ok_id)
        check(audio_state(page)["src"].endswith(f"/api/stream/yt/{ok_id}"),
              "скачанный трек играет через тот же /api/stream (backend отдаёт файл с диска)")
        page.click(row_dl(1))
        page.wait_for_selector("#dlg-confirm[open]")
        page.click("#dc-ok")
        page.wait_for_function("document.querySelectorAll('#tracks .row:not(.row--skel)').length === 1")
        check(ok_id not in DL["done"] and page.locator("#tracks .row").count() == 1, "удаление локальной копии с подтверждением")
        DL["mock"] = False
        DL["stage"].clear()
        DL["done"].clear()

        print("Пропуск трека при смене списка")
        skips: list[dict] = []
        on_skip = lambda r: skips.append(r.post_data_json) if r.url.endswith("/api/history/skip") else None   # noqa: E731
        page.on("request", on_skip)
        page.fill("#search-input", "gspd")
        page.keyboard.press("Enter")
        page.click("#tabs [data-tab=tracks]")
        page.wait_for_selector("#tracks .row:not(.row--skel)")
        page.locator("#tracks .row").first.click(position={"x": 260, "y": 20})
        page.wait_for_function("(() => { const a = document.querySelector('#audio'); return a.src.endsWith('/api/stream/yt/trk00000040') && !a.paused && a.currentTime > 0.3 })()")
        skips.clear()
        page.click("#tabs [data-tab=albums]")
        page.wait_for_selector("#cards .card--album")
        page.locator("#cards .card--album").first.click()
        page.wait_for_function("document.querySelector('#d-kind').textContent === 'Альбом' && document.querySelectorAll('#tracks .row:not(.row--skel)').length === 8")
        page.locator("#tracks .row:nth-child(2)").click(position={"x": 260, "y": 20})
        page.wait_for_function("document.querySelector('#audio').src.endsWith('/api/stream/yt/trk00000081')")
        page.wait_for_timeout(400)
        page.remove_listener("request", on_skip)
        check(skips == [{"source": "yt", "id": "trk00000040"}],
              f"сигнал «пропуск» — для прерванного трека, а не для трека нового списка с тем же номером ({skips})")

        print("Очередь не зависит от списка на экране (M11)")
        q_titles = "[...document.querySelectorAll('#queue-list .q .q__title')].map(n => n.textContent)"
        row_titles = "[...document.querySelectorAll('#tracks .row .track__title')].map(n => n.textContent)"
        playing = "(id) => { const a = document.querySelector('#audio'); return a.src.endsWith('/api/stream/yt/' + id) && !a.paused }"

        def reopen_queue() -> list[str]:                            # очередь рисуется при открытии панели
            if visible(page, "#queue"):
                page.click("#queue-close")
            page.click("#btn-queue")
            page.wait_for_selector("#queue:not([hidden]) .q")
            return page.evaluate(q_titles)

        page.fill("#search-input", "m11")
        page.keyboard.press("Enter")
        page.click("#tabs [data-tab=tracks]")
        page.wait_for_function("document.querySelectorAll('#tracks .row:not(.row--skel)').length === 20")
        listed = page.evaluate(row_titles)
        page.locator("#tracks .row").first.click(position={"x": 260, "y": 20})
        page.wait_for_function(playing, arg="trk00000100")
        queued = reopen_queue()
        check(queued == listed, "очередь запущена из списка в его порядке")
        page.select_option("#sort", "artist")
        page.wait_for_function(f"(t) => JSON.stringify({row_titles}) !== t", arg=json.dumps(listed, ensure_ascii=False))
        resorted = page.evaluate(row_titles)
        check(resorted != listed and sorted(resorted) == sorted(listed) and resorted[0] != listed[0],
              "список на экране отсортирован по исполнителю")
        check(reopen_queue() == queued, "сортировка списка не изменила порядок очереди")
        check(page.locator("#queue-list .q.is-current .q__title").text_content() == listed[0], "текущий трек очереди не сменился")
        page.click("#player [data-do=next]")
        page.wait_for_function(playing, arg="trk00000101")
        check(True, "«Следующий» играет следующий трек очереди, а не отсортированного списка")
        page.click("#tracks .row[data-key='yt:trk00000101'] .track__title")
        page.wait_for_function("document.querySelector('#audio').paused")
        check(audio_state(page)["src"].endswith("/api/stream/yt/trk00000101"), "клик по играющей строке после сортировки — пауза, а не перезапуск")
        page.click("#player [data-do=play]")
        page.click("#queue-close")
        M11["page2"] = True
        page.click("#more")                                          # следующая страница источника («Повторить»)
        page.wait_for_function("document.querySelectorAll('#tracks .row:not(.row--skel)').length === 40")
        page.wait_for_timeout(200)
        queued2 = reopen_queue()
        check(len(queued2) == 40 and queued2[:20] == queued
              and sorted(queued2[20:]) == sorted(t for t in page.evaluate(row_titles) if t not in queued),
              "новая страница списка продолжила очередь в конце, прежний порядок очереди сохранён")
        page.click("#queue-close")

        print("Повтор off / all / one (S10)")
        cur_id = "document.querySelector('#audio').src.split('/').pop()"
        rep_btn = "#player [data-do=repeat]"

        def repeat_ui() -> list[dict]:
            return page.evaluate("""[...document.querySelectorAll('[data-do=repeat]')].map(b => ({mode: b.dataset.mode,
                pressed: b.getAttribute('aria-pressed'), label: b.getAttribute('aria-label'), title: b.title,
                icon: b.querySelector('use').getAttribute('href')}))""")

        def to_end() -> None:                                       # конец трека без ожидания 30 с
            page.wait_for_function("(() => { const a = document.querySelector('#audio'); return Number.isFinite(a.duration) && a.currentTime > 0 })()")
            page.evaluate("(() => { const a = document.querySelector('#audio'); a.currentTime = a.duration - 0.4 })()")

        def wait_track(tid: str, timeout: int = 10000) -> bool:
            try:
                page.wait_for_function("(id) => { const a = document.querySelector('#audio'); return a.src.endsWith('/' + id) && !a.paused }",
                                       arg=tid, timeout=timeout)
                return True
            except Exception:
                return False

        def set_autoplay(on: bool) -> None:                          # как пользователь: экран настроек
            page.click(".nav__item[data-view=settings]")
            page.wait_for_selector("#view-settings:not([hidden])")
            page.set_checked("#set-autoplay", on)
            page.wait_for_function("(v) => fetch('/api/settings').then(r => r.json()).then(d => d.values.autoplay === v)", arg=on)

        set_autoplay(False)                                          # конец очереди — без автопродолжения похожими
        page.fill("#search-input", "s10")
        page.keyboard.press("Enter")
        page.click("#tabs [data-tab=tracks]")
        page.wait_for_function("document.querySelectorAll('#tracks .row:not(.row--skel)').length === 10")
        page.locator("#tracks .row").first.click(position={"x": 260, "y": 20})
        check(wait_track("trk00000200"), "очередь из 10 треков запущена с первого")
        ui = repeat_ui()
        check(len(ui) == 3 and all(u == {"mode": "off", "pressed": "false", "label": "Повтор: выключен", "title": "Повтор: выключен",
                                         "icon": "#i-repeat"} for u in ui), "повтор выключен: подпись, title, aria-pressed на всех кнопках")
        page.click(rep_btn)
        ui = repeat_ui()
        check(all(u["mode"] == "all" and u["pressed"] == "true" and u["label"] == "Повтор: вся очередь" and u["icon"] == "#i-repeat" for u in ui),
              "первое нажатие — повтор очереди (на всех экранах)")
        page.click(rep_btn)
        ui = repeat_ui()
        check(all(u["mode"] == "one" and u["pressed"] == "true" and u["label"] == "Повтор: текущий трек" and u["icon"] == "#i-repeat-one" for u in ui),
              "второе нажатие — повтор трека, значок с «1»")
        to_end()
        page.wait_for_function("(() => { const a = document.querySelector('#audio'); return a.src.endsWith('/trk00000200') && !a.paused && a.currentTime < 2 })()",
                               timeout=10000)
        check(page.evaluate("document.querySelector('#audio').loop") is False,
              "repeat one: доигравший трек начинается сначала (через ended, без audio.loop)")
        page.click("#player [data-do=next]")
        check(wait_track("trk00000201"), "repeat one: кнопка «Следующий» переключает на следующий трек")
        page.click(rep_btn)
        check(all(u["mode"] == "off" and u["pressed"] == "false" for u in repeat_ui()), "третье нажатие — повтор выключен")
        page.locator("#tracks .row").nth(9).click(position={"x": 260, "y": 20})
        check(wait_track("trk00000209"), "последний трек очереди играет")
        to_end()
        page.wait_for_function("document.querySelector('#audio').paused && document.body.dataset.audio === 'paused'", timeout=10000)
        check(page.evaluate(cur_id) == "trk00000209", "repeat off: после последнего трека очередь закончилась (без автопродолжения)")
        page.click(rep_btn)                                          # all
        page.click("#player [data-do=play]")
        page.wait_for_function("!document.querySelector('#audio').paused")
        to_end()
        check(wait_track("trk00000200"), "repeat all: после последнего трека — снова первый")
        page.click(rep_btn); page.click(rep_btn)                     # one → off
        check(all(u["mode"] == "off" for u in repeat_ui()), "повтор снова выключен")

        print("Перемешивание без повторов на 10 треках (S10)")
        page.click("#player [data-do=shuffle]")
        check(page.get_attribute("#player [data-do=shuffle]", "aria-pressed") == "true", "перемешивание включено")
        order = [page.evaluate(cur_id)]
        for _ in range(9):
            before = order[-1]
            page.click("#player [data-do=next]")
            page.wait_for_function(f"(b) => {cur_id} !== b && !document.querySelector('#audio').paused", arg=before)
            order.append(page.evaluate(cur_id))
        check(len(set(order)) == 10 and set(order) == {f"trk{200 + i:08d}" for i in range(10)},
              f"shuffle: 10 треков без повторов до конца круга ({order})")
        page.click("#player [data-do=next]")
        page.wait_for_timeout(500)
        check(page.evaluate(cur_id) == order[-1], "shuffle, repeat off: круг сыгран — «Следующий» ничего не повторяет")
        back = []
        for _ in range(3):
            before = page.evaluate(cur_id)
            # «Назад» в первые 3 с трека — предыдущий трек (позже — перемотка в начало): условие задаём явно,
            # чтобы проверка не зависела от скорости машины
            page.evaluate("document.querySelector('#audio').currentTime = 0.5")
            page.click("#player [data-do=prev]")
            page.wait_for_function(f"(b) => {cur_id} !== b", arg=before)
            back.append(page.evaluate(cur_id))
        check(back == order[-2:-5:-1], f"«Назад» идёт по фактическому порядку перемешивания ({back} vs {order[-2:-5:-1]})")
        page.click("#player [data-do=next]")
        page.wait_for_function(f"(b) => {cur_id} !== b", arg=back[-1])
        check(page.evaluate(cur_id) == order[-3], "после «Назад» «Вперёд» идёт по уже сыгранному порядку, без повтора трека")
        page.click("#player [data-do=shuffle]")
        set_autoplay(True)

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

        print("Источник рекомендаций недоступен (M4)")
        page.set_viewport_size({"width": 1440, "height": 900})
        home_page = ctx.new_page()                           # новая вкладка: ничего не играет, лента грузится заново
        home_page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
        home_page.on("console", lambda m: errors.append(f"console.{m.type}: {m.text}") if m.type == "error" and "Failed to load resource" not in m.text else None)
        RECS["mode"] = "failed"
        page.close()
        home_page.goto(BASE)
        home_page.wait_for_selector("#home-empty:not([hidden])", timeout=15000)
        empty_text = home_page.inner_text("#home-empty")
        check("Источник сейчас недоступен: YouTube Music" in empty_text and "Пока нечего предложить" not in empty_text,
              "сбой источника не выдан за «нечего предложить»")
        check(visible(home_page, "#home-empty .empty__retry"), "рядом с сообщением есть «Повторить»")
        check("Источник сейчас недоступен" in home_page.inner_text("#hero-sub"), "баннер тоже говорит о недоступности источника")
        home_page.screenshot(path=str(SHOTS / "23-home-source-down.png"))
        RECS["mode"] = "normal"
        home_page.click("#home-empty .empty__retry")
        home_page.wait_for_selector("#new-grid .tcard", timeout=15000)
        check(home_page.locator("#new-grid .tcard").count() >= 20 and not visible(home_page, "#home-empty"),
              "«Повторить» загружает ленту, сообщение исчезает")
        RECS["mode"] = "partial"
        home_page.reload()
        home_page.wait_for_selector("#new-grid .tcard", timeout=15000)
        home_page.wait_for_selector("#home-empty:not([hidden])")
        check("Подборка неполная" in home_page.inner_text("#home-empty") and home_page.locator("#new-grid .tcard").count() >= 20,
              "частичный сбой: треки показаны, неполнота подборки названа")
        RECS["mode"] = "empty"
        home_page.reload()
        home_page.wait_for_selector("#home-empty:not([hidden])", timeout=15000)
        check("Пока нечего предложить" in home_page.inner_text("#home-empty") and not visible(home_page, "#home-empty .empty__retry"),
              "источник ответил, но предложить нечего: обычное пустое состояние без «Повторить»")
        RECS["mode"] = "personal"
        home_page.reload()
        home_page.wait_for_function("document.querySelector('#hero').dataset.feed === 'ready'", timeout=15000)
        check(home_page.text_content("#hero-eyebrow-text") == "Подобрано по вашей истории",
              "«Подобрано по вашей истории» — только когда backend сообщил personalized=true")
        check(foreign_names(home_page) == [], "после всех сценариев фирменных названий других сервисов нет")
        RECS["mode"] = "normal"

        print("SoundCloud: трек только по HLS (S8)")
        hls_page = ctx.new_page()
        hls_page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
        hls_page.on("console", lambda m: errors.append(f"console.{m.type}: {m.text}") if m.type == "error" and "Failed to load resource" not in m.text else None)
        hls_requests: list[str] = []
        hls_page.on("request", lambda r: hls_requests.append(r.url))
        hls_page.goto(BASE)
        hls_page.wait_for_function("document.querySelector('#hero').dataset.feed === 'ready'", timeout=15000)
        hls_page.click("[data-source=sc]")
        hls_page.fill("#search-input", "hls")
        hls_page.keyboard.press("Enter")
        hls_page.wait_for_selector("#tracks .row:not(.row--skel)")
        hls_page.locator("#tracks .row .track__num").first.click()
        started = True
        try:
            hls_page.wait_for_function("(() => { const a = document.querySelector('#audio'); return !a.paused && a.currentTime > 1 })()", timeout=20000)
        except Exception:
            started = False
        st = audio_state(hls_page)
        check(started and st["src"].startswith("blob:"), "HLS-трек SoundCloud играет через hls.js в том же <audio> (MSE)")
        check(any(u.endswith("/vendor/hls.light.min.js") for u in hls_requests)
              and not [u for u in hls_requests if not u.startswith(BASE) and not u.startswith("blob:") and "googleusercontent" not in u],
              "hls.js загружен с backend приложения, без CDN и внешних адресов")
        dur = hls_page.evaluate("document.querySelector('#audio').duration")
        check(abs(dur - 3 * HLS_SEG_FRAMES * MP3_FRAME_SEC) < 1.0, "длительность HLS-трека — из плейлиста")
        hls_page.evaluate("document.querySelector('#audio').currentTime = 21")
        try:
            hls_page.wait_for_function("(() => { const a = document.querySelector('#audio'); return !a.paused && a.currentTime > 21.5 })()", timeout=15000)
            seek_ok = True
        except Exception:
            seek_ok = False
        check(seek_ok and HLS["segments"] >= 3, "перемотка HLS-трека вперёд загружает нужный сегмент и продолжает играть")
        hls_page.click("#btn-play")
        hls_page.wait_for_function("document.querySelector('#audio').paused && document.body.dataset.audio === 'paused'")
        t_paused = audio_state(hls_page)["t"]
        hls_page.click("#btn-play")
        try:
            hls_page.wait_for_function(f"document.body.dataset.audio === 'playing' && document.querySelector('#audio').currentTime > {t_paused + 0.3}",
                                       timeout=10000)
            resumed = True
        except Exception:
            resumed = False
        check(resumed, "пауза и продолжение HLS-трека с той же позиции")
        hls_page.screenshot(path=str(SHOTS / "23-hls-playing.png"))
        hls_page.click("#player [data-do=next]")
        try:
            hls_page.wait_for_function("(() => { const a = document.querySelector('#audio'); return a.src.endsWith('/api/stream/sc/3000000001') && !a.paused })()", timeout=15000)
            switched = True
        except Exception:
            switched = False
        check(switched and HLS["released"] == [HLS_TOKEN], "следующий трек (обычный поток) играет, HLS-сеанс прошлого трека освобождён")
        segs_after = HLS["segments"]
        hls_page.wait_for_timeout(1500)
        check(HLS["segments"] == segs_after, "после переключения прошлый HLS-поток больше ничего не загружает")
        hls_page.close()

        print("Компонент воспроизведения YouTube недоступен (S3)")
        STREAM["mode"] = "js_runtime"
        rt_page = ctx.new_page()
        rt_page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
        rt_page.on("console", lambda m: errors.append(f"console.{m.type}: {m.text}") if m.type == "error" and "Failed to load resource" not in m.text else None)
        home_page.close()
        rt_page.goto(BASE)
        rt_page.wait_for_function("document.querySelector('#hero').dataset.feed === 'ready'", timeout=15000)
        rt_page.click("#hero-play")
        shown = True
        try:
            rt_page.wait_for_function("(t) => (document.querySelector('#toast')?.innerText || '').includes(t)",
                                      arg="Компонент воспроизведения YouTube", timeout=15000)
        except Exception:
            shown = False
        toast_text = rt_page.inner_text("#toast") if rt_page.locator("#toast").count() else ""
        check(shown and "Трек недоступен" not in toast_text,
              "нет компонента воспроизведения YouTube: пользователь видит отдельное сообщение, а не «Трек недоступен»")
        rt_page.screenshot(path=str(SHOTS / "24-js-runtime-missing.png"))
        STREAM["mode"] = "normal"

        browser.close()

    print(f"\nПроверок: {checks}, провалено: {len(failures)}")
    real = [e for e in errors]
    print("Ошибки консоли/страницы:", real if real else "нет")
    return 1 if failures or real else 0


if __name__ == "__main__":
    sys.exit(main())