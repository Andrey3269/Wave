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
AUDIO6 = wav(6)                        # короткие треки раздела «История» (S10): порог прослушивания 3 с
E2E_ALBUM_ID = "MPREb_e2eAlbum0001"    # id альбома у треков страницы альбома (S10: «Открыть альбом» по id)


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
# S11: queued — id, для которых POST отвечает «в очереди»; cancels — запросы отмены; cancel_done — id, для которых отмена
# опоздала (трек успел сохраниться, ответ done)
# hold — id, для которых ответ на POST придерживается до явного held_release() (двойной клик до ответа backend)
DL = {"mock": False, "stage": {}, "posts": [], "done": {}, "queued": set(), "cancels": [], "cancel_done": set(),
      "hold": set(), "held": []}
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
# «Загрузить ещё» (S12). Поиск «more12»: вторая страница обрывается (fail), придерживается до more_release() (hold)
# или отдаётся (ok). Лента главной: RECS["paged"] — следующие страницы отдают новые треки (а не радио последнего),
# RECS["fail_more"] — следующие страницы отвечают 502.
MORE = {"mode": "fail", "held": []}
# Следующие страницы ленты главной придерживаются, пока RECS["hold"] (S12, аудит 2): (route, page) до recs_release()
RECS_HELD: list = []


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
    if stage == "cancelled":
        job.update(code="cancelled", finished_at=3.0)
    if stage == "downloading":
        job.update(progress=40, received=400_000, total=1_000_000)
    elif stage == "processing":
        job.update(progress=99, received=1_000_000, total=1_000_000)
    elif stage == "done":
        job.update(progress=100, received=1_000_000, total=1_000_000, finished_at=2.0)
    return job


def more12_page(off: int) -> dict:
    return {"query": "more12", "count": 20, "tracks": [track(500 + off + i) for i in range(20)], "failed_sources": [],
            "has_more": off + 20 < 40}


def more_release() -> None:
    """Отвечает на придержанные запросы второй страницы «more12» (S12)."""
    while MORE["held"]:
        js(MORE["held"].pop(0), more12_page(20))


def recs_release() -> None:
    """Отвечает на придержанные запросы следующих страниц ленты главной (S12, аудит 2)."""
    while RECS_HELD:
        route, page = RECS_HELD.pop(0)
        try:
            js(route, {"tracks": [track(1 + page * 24 + i, "Подборка") for i in range(24)], "personalized": False, "radio": False,
                       "page": page, "failed_sources": [], "source_status": {"yt": "ok"}})
        except Exception:                                              # запрос уже отменён страницей
            pass


def held_release() -> None:
    """Отвечает на придержанные POST /api/downloads (S11: проверка двойного клика)."""
    while DL["held"]:
        route, tid, first = DL["held"].pop(0)
        DL["stage"][tid] = first
        js(route, {"state": first, "progress": 0, "error": None, "code": None}, 202)


def dl_mock(route: Route, path: str) -> bool:
    req = route.request
    if path == "/api/downloads" and req.method == "POST":
        body = req.post_data_json
        DL["posts"].append(body["id"])
        if body["id"] in DL["done"]:
            js(route, {"state": "done", "progress": 100})
            return True
        outcome = DL_OUTCOME.get(body["id"])
        first = "queued" if body["id"] in DL["queued"] else "preparing"
        if body["id"] in DL["hold"]:                                   # ответ отдаст тест (held_release)
            DL["held"].append((route, body["id"], first))
            return True
        DL["stage"][body["id"]] = outcome if outcome else first
        js(route, {"state": first, "progress": 0, "error": None, "code": None}, 202)
        return True
    m = re.match(r"^/api/downloads/jobs/yt/(\w+)$", path)
    if m and req.method == "DELETE":                                   # S11: отмена
        tid = m.group(1)
        DL["cancels"].append(tid)
        if tid not in DL["stage"]:
            js(route, {"detail": "Этой загрузки уже нет."}, 404)
            return True
        DL["stage"][tid] = "done" if tid in DL["cancel_done"] else "cancelled"
        js(route, dl_job(tid))
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
        audio = AUDIO6 if re.search(r"/trk000003\d\d$", path) else AUDIO
        rng = re.match(r"bytes=(\d+)-(\d*)", req.headers.get("range", ""))
        if rng:
            a = int(rng.group(1)); b = min(int(rng.group(2) or len(audio) - 1), len(audio) - 1)
            return route.fulfill(status=206, body=audio[a:b + 1], headers={
                "Content-Type": "audio/wav", "Accept-Ranges": "bytes", "Content-Range": f"bytes {a}-{b}/{len(audio)}"})
        return route.fulfill(status=200, body=audio, headers={"Content-Type": "audio/wav", "Accept-Ranges": "bytes"})

    if path == "/api/recommendations":
        if q.get("id") and not (RECS.get("paged") or RECS.get("fail_more")):   # «радио» трека
            title = q.get("title", "")
            return js(route, {"tracks": [track(20 + i, f"Похоже на «{title}»") for i in range(14)], "personalized": False,
                              "radio": True, "page": 0, "failed_sources": [], "source_status": {"yt": "ok"}})
        page = int(q.get("page", 0))
        mode = RECS["mode"]
        if page and RECS.get("hold"):
            RECS_HELD.append((route, page))
            return None
        if page and RECS.get("fail_more"):
            return route.fulfill(status=502, content_type="application/json", body=json.dumps({"detail": "Источник не ответил"}))
        if page and RECS.get("paged"):
            return js(route, {"tracks": [track(1 + page * 24 + i, "Подборка") for i in range(24)], "personalized": False, "radio": False,
                              "page": page, "failed_sources": [], "source_status": {"yt": "ok"}})
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
    if path == "/api/search" and q.get("q") == "long3":               # три длинных имени исполнителей (S12, аудит)
        names = ["Номер Один Очень Длинное Имя Исполнителя", "Номер Два Ещё Более Длинное Имя", "Номер Три С Длинным Именем"]
        t = {**track(450), "artist": ", ".join(names), "artists": names, "artist_ids": ["UCart0001", "UCart0002", "UCart0003"]}
        return js(route, {"query": "long3", "count": 2, "tracks": [t, track(451)], "failed_sources": [], "has_more": False})
    if path == "/api/search" and q.get("q") == "dup12":               # вторая страница — одни повторы (S12, аудит)
        return js(route, {"query": "dup12", "count": 20, "tracks": [track(600 + i) for i in range(20)], "failed_sources": [],
                          "has_more": not int(q.get("offset", 0))})
    if path == "/api/search" and q.get("q") == "more12":              # «Загрузить ещё» (S12)
        off = int(q.get("offset", 0))
        if off and MORE["mode"] == "fail":
            return route.abort("failed")
        if off and MORE["mode"] == "hold":
            MORE["held"].append(route)
            return None
        return js(route, more12_page(off))
    if path == "/api/search" and q.get("q") == "s10":                 # очередь из 10 треков (повтор и перемешивание, S10)
        return js(route, {"query": "s10", "count": 10, "tracks": [track(200 + i) for i in range(10)], "failed_sources": [],
                          "has_more": False})
    if path == "/api/search" and q.get("q") == "s10h":                # короткие треки по 6 с (история, S10)
        return js(route, {"query": "s10h", "count": 4, "tracks": [{**track(300 + i), "duration": 6} for i in range(4)],
                          "failed_sources": [], "has_more": False})
    if path == "/api/search" and q.get("q") == "many6":               # шесть исполнителей у одного трека (S12, аудит 2)
        names = ["Первый Исполнитель", "Второй Исполнитель", "Третий Исполнитель", "Четвёртый", "Пятый Исполнитель", "Шестой"]
        many = {**track(410), "artist": ", ".join(names), "artists": names, "artist_ids": [f"UCart{9100 + i}" for i in range(6)]}
        return js(route, {"query": "many6", "count": 1, "tracks": [many], "failed_sources": [], "has_more": False})
    if path == "/api/search" and q.get("q") == "feat":                # несколько исполнителей, у одного нет id (S12)
        duo = {**track(400), "artist": "GSPD, Vtornik", "artists": ["GSPD", "Vtornik"], "artist_ids": ["UCart0001", "UCart0002"]}
        no_id = {**track(401), "artist": "Кино, Безымянный", "artists": ["Кино", "Безымянный"], "artist_id": "UCart0008",
                 "artist_ids": ["UCart0008"]}
        return js(route, {"query": "feat", "count": 2, "tracks": [duo, no_id], "failed_sources": [], "has_more": False})
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
                          "tracks": [{**track(80 + i), "album_id": E2E_ALBUM_ID} for i in range(8)], "unavailable": 0})

    if path == "/api/lyrics":
        lines = [{"t": i * 3.0, "text": f"Строка текста номер {i + 1}"} for i in range(9)]
        return js(route, {"synced": lines, "plain": None, "provider": "lrclib"})

    route.continue_()


def clear_playback() -> None:
    """
    Забыть сохранённую очередь (S10) перед сценарием «приложение запущено, ничего не играет». Закрытая вкладка
    могла ещё дописать состояние запросом keepalive, поэтому удаление повторяется, пока состояние не исчезнет.
    """
    import time
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for _ in range(20):
        opener.open(urllib.request.Request(f"{BASE}/api/player/state", method="DELETE"), timeout=10).read()
        time.sleep(0.15)
        with opener.open(f"{BASE}/api/player/state", timeout=10) as r:
            if json.load(r)["state"] is None:
                return
    raise AssertionError("сохранённое состояние воспроизведения не удалось очистить")


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


def poll(page, expr: str, arg=None, timeout: float = 10.0) -> bool:
    """
    Ждёт, пока выражение (может вернуть Promise, например fetch) станет истинным. page.wait_for_function для этого
    не годится: значение Promise он не ждёт и считает любой Promise выполненным условием.
    """
    import time
    deadline = time.monotonic() + timeout
    while True:
        if page.evaluate(expr, arg):
            return True
        if time.monotonic() > deadline:
            return False
        page.wait_for_timeout(100)


def media_session(page) -> dict:
    return page.evaluate("""(() => { const ms = navigator.mediaSession, m = ms.metadata;
        return {state: ms.playbackState, title: m && m.title, artist: m && m.artist, album: m && m.album,
                art: m ? m.artwork.length : 0, handlers: Object.keys(window.__ms || {}).sort()}; })()""")


def ms_action(page, name: str, details: dict | None = None) -> None:
    page.evaluate("([n, d]) => window.__ms[n]({action: n, ...(d || {})})", [name, details])


def audio_state(page) -> dict:
    return page.evaluate("(() => { const a = document.querySelector('#audio'); return {paused: a.paused, t: a.currentTime, vol: a.volume, muted: a.muted, src: a.src} })()")


# Рамка фокуса (S12): у элемента в фокусе есть рамка не тоньше 2px, она не обрезана ни одним предком с overflow, сам
# элемент виден и ничем не перекрыт (например, закреплённой верхней панелью). У кнопки трека рамка — на ::after (вся
# строка), у текста песни на странице трека — у панели; поле поиска выделяется рамкой всей строки поиска (.search).
FOCUS_RING = """() => {
  const a = document.activeElement;
  const who = (n) => n === document.body ? "body" : `${n.tagName.toLowerCase()}${n.id ? "#" + n.id : ""}${n.classList.length ? "." + [...n.classList].join(".") : ""}${n.dataset.do ? "[do=" + n.dataset.do + "]" : ""}`;
  if (!a || a === document.body) return { ok: false, why: "фокус на body", who: "body" };
  if (a.id === "search-input") { const f = getComputedStyle(a.closest(".search")); return { ok: f.borderTopColor !== getComputedStyle(document.querySelector(".seg")).borderTopColor, why: "рамка поля поиска не выделена", who: who(a) }; }
  let box = a, cs = getComputedStyle(a);
  if (a.classList.contains("track__play")) { box = a.closest(".track"); cs = getComputedStyle(a, "::after"); }
  if (a.id === "np-lyrics") { box = a.parentElement; cs = getComputedStyle(box); }
  if (a.classList.contains("mixcard__open")) {          // двухцветная рамка: акцент снаружи и тёмная линия внутри
    box = a.closest(".mixcard"); cs = getComputedStyle(box);
    const inner = getComputedStyle(box, "::after"), rgb = (inner.borderTopColor.match(/\\d+/g) || []).slice(0, 3).map(Number);
    if (parseFloat(inner.borderTopWidth) < 2 || rgb.length < 3 || Math.max(...rgb) > 40)
      return { ok: false, why: `нет тёмной линии внутри рамки карточки (${inner.borderTopWidth} ${inner.borderTopColor})`, who: who(a) };
  }
  if (cs.outlineStyle === "none" || parseFloat(cs.outlineWidth) < 2) return { ok: false, why: "нет рамки", who: who(a) };
  if (!a.checkVisibility()) return { ok: false, why: "элемент не виден", who: who(a) };
  const r = box.getBoundingClientRect(), g = parseFloat(cs.outlineOffset) + parseFloat(cs.outlineWidth);
  const ring = { l: r.left - g, t: r.top - g, r: r.right + g, b: r.bottom + g };
  for (let p = box.parentElement; p && p !== document.documentElement; p = p.parentElement) {
    const s = getComputedStyle(p);
    if (s.overflowX === "visible" && s.overflowY === "visible") continue;
    if ((s.overflowX === "hidden" || s.overflowY === "hidden") && (p.scrollLeft || p.scrollTop))
      return { ok: false, why: `фокус прокрутил необрезаемую область (${who(p)})`, who: who(a) };
    const pr = p.getBoundingClientRect();
    const c = { l: pr.left + parseFloat(s.borderLeftWidth), t: pr.top + parseFloat(s.borderTopWidth),
      r: pr.right - parseFloat(s.borderRightWidth), b: pr.bottom - parseFloat(s.borderBottomWidth) };
    const out = [];
    if (s.overflowX !== "visible" && ring.l < c.l - 0.5) out.push("слева");
    if (s.overflowX !== "visible" && ring.r > c.r + 0.5) out.push("справа");
    if (s.overflowY !== "visible" && ring.t < c.t - 0.5) out.push("сверху");
    if (s.overflowY !== "visible" && ring.b > c.b + 0.5) out.push("снизу");
    if (out.length) return { ok: false, why: `рамка обрезана ${out.join(",")} (${who(p)})`, who: who(a) };
  }
  const cx = (r.left + r.right) / 2;
  for (const y of [(r.top + r.bottom) / 2, r.top + 3, r.bottom - 3]) {     // центр, верх и низ: закреплённые панели
    const hit = document.elementFromPoint(cx, y);
    if (!hit || !(box.contains(hit) || hit.contains(box))) return { ok: false, why: `перекрыт (${hit ? who(hit) : "вне окна"})`, who: who(a) };
  }
  return { ok: true, who: who(a) };
}"""


def tab_walk(pg, steps: int, key: str = "Tab") -> tuple[list[str], int]:
    """Нажимает Tab (или Shift+Tab) steps раз; возвращает проблемы с рамкой фокуса и число разных элементов."""
    bad, seen = [], set()
    for _ in range(steps):
        pg.keyboard.press(key)
        if pg.evaluate("!!document.activeElement.closest('.scroller')"):
            pg.wait_for_timeout(350)                                  # прокрутка ряда к карточке (scroll-snap)
        r = pg.evaluate(FOCUS_RING)
        if r["who"] == "body":                                       # конец документа: фокус уходит в окно браузера
            continue
        seen.add(r["who"])
        if not r["ok"] and f"{r['why']} :: {r['who']}" not in bad:
            bad.append(f"{r['why']} :: {r['who']}")
    return bad, len(seen)


# Контраст отрисованного элемента (S12): цвет текста (или значка — currentColor) против фактического фона — первые
# непрозрачные фоны предков с наложением полупрозрачных. Порог: текст 4.5:1, крупный (24px или 18.66px жирным) 3:1,
# значок без текста 3:1. Возвращает [{sel, ratio, need}] для первого видимого элемента каждого селектора.
CONTRAST_JS = """(sels) => {
  const parse = (c) => { const m = c.match(/rgba?\\(([^)]+)\\)/); if (!m) return [0, 0, 0, 0];
    const p = m[1].split(/[ ,\\/]+/).filter(Boolean).map(Number); return [p[0], p[1], p[2], p.length > 3 ? p[3] : 1]; };
  const over = (f, b) => [0, 1, 2].map((i) => f[3] * f[i] + (1 - f[3]) * b[i]).concat(1);
  const lum = (c) => { const ch = (x) => { x /= 255; return x <= 0.04045 ? x / 12.92 : ((x + 0.055) / 1.055) ** 2.4; };
    return 0.2126 * ch(c[0]) + 0.7152 * ch(c[1]) + 0.0722 * ch(c[2]); };
  const ratio = (a, b) => { const [x, y] = [lum(a), lum(b)].sort((p, q) => q - p); return (x + 0.05) / (y + 0.05); };
  const background = (n) => { const layers = [];
    for (let e = n; e; e = e.parentElement) { const c = parse(getComputedStyle(e).backgroundColor); if (c[3] > 0) layers.push(c); if (c[3] >= 1) break; }
    let bg = [255, 255, 255, 1];
    for (let i = layers.length - 1; i >= 0; i--) bg = over(layers[i], bg);
    return bg; };
  return sels.map((sel) => {
    const n = [...document.querySelectorAll(sel)].find((x) => x.checkVisibility() && x.getClientRects().length);
    if (!n) return { sel, ratio: 0, need: 0, missing: true };
    const cs = getComputedStyle(n), bg = background(n), fg = over(parse(cs.color), bg);
    const size = parseFloat(cs.fontSize), bold = Number(cs.fontWeight) >= 700;
    const icon = n.tagName.toLowerCase() === "svg" || n.closest("svg");
    const need = icon ? 3 : (size >= 24 || (size >= 18.66 && bold)) ? 3 : 4.5;
    return { sel, ratio: Math.round(ratio(fg, bg) * 100) / 100, need };
  });
}"""


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
        page.wait_for_function("(s) => (document.querySelector(s)?.getAttribute('aria-label') || '').includes('отменить')",
                               arg=row_dl(1))
        # S11: кнопка идущей загрузки не отключается — она отменяет загрузку (раньше: disabled, «кнопка занята»)
        check(not page.is_disabled(row_dl(1)) and "Нажми, чтобы отменить загрузку" in page.get_attribute(row_dl(1), "aria-label")
              and page.get_attribute(row_dl(1) + " use", "href") == "#i-x" and DL["posts"] == [ok_id],
              "«Скачать»: запрос ушёл, этап «Подготовка», кнопка предлагает отмену и доступна")
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

        print("Загрузки: очередь и отмена (S11)")
        page.fill("#search-input", "gspd")
        page.keyboard.press("Enter")
        page.click("#tabs [data-tab=tracks]")
        page.wait_for_selector("#tracks .row:not(.row--skel)")
        q_id, late_id, wait_id, run_id = "trk00000043", "trk00000044", "trk00000045", "trk00000046"
        DL["queued"].update({q_id, wait_id})
        page.click(row_dl(4))
        page.wait_for_function(label_starts, arg=[row_dl(4), "В очереди"])
        page.wait_for_function(toast_has, arg="в очереди")
        check(page.evaluate(has_class, [row_dl(4), "is-queued"]) and page.inner_text(row_dl(4) + " .track__pct") == ""
              and not page.is_disabled(row_dl(4)) and "отменить" in page.get_attribute(row_dl(4), "aria-label"),
              "лимит загрузок: «В очереди» без процента, отдельно от передачи данных; кнопка доступна для отмены")
        DL["stage"][q_id] = "downloading"
        page.wait_for_function(label_starts, arg=[row_dl(4), "Скачивание: 40%"])
        check(not page.evaluate(has_class, [row_dl(4), "is-queued"]), "из очереди — в передачу данных с реальным процентом")
        page.click("#tracks .row:nth-child(4) .track__more")
        check(page.inner_text("#menu [data-act=download]") == "Отменить загрузку", "меню трека во время загрузки предлагает отмену")
        page.keyboard.press("Escape")
        page.focus(row_dl(4))
        page.keyboard.press("Enter")
        page.wait_for_function(toast_has, arg="отменена")
        page.wait_for_function(label_starts, arg=[row_dl(4), "Скачать"])
        check(DL["cancels"] == [q_id] and not page.is_disabled(row_dl(4))
              and not page.evaluate(has_class, [row_dl(4), "is-busy"])
              and page.evaluate("(s) => document.activeElement === document.querySelector(s)", row_dl(4)),
              "отмена с клавиатуры: запрос отмены, сообщение, кнопка снова «Скачать», фокус на ней")
        page.screenshot(path=str(SHOTS / "16e-download-cancelled.png"))
        DL["queued"].discard(q_id)
        posts = len(DL["posts"])
        page.click(row_dl(4))
        page.wait_for_function(label_starts, arg=[row_dl(4), "Подготовка"])
        DL["stage"][q_id] = "done"
        page.wait_for_function(has_class, arg=[row_dl(4), "is-done"])
        check(len(DL["posts"]) == posts + 1, "после отмены тот же трек скачивается снова")

        DL["cancel_done"].add(late_id)
        page.click(row_dl(5))
        page.wait_for_function(label_starts, arg=[row_dl(5), "Подготовка"])
        page.click(row_dl(5))
        page.wait_for_function(toast_has, arg="отменить не успели")
        page.wait_for_function(has_class, arg=[row_dl(5), "is-done"])
        check(DL["cancels"][-1] == late_id, "отмена опоздала (трек уже сохраняется): честное «уже сохранён», кнопка «Скачано»")

        dbl_id = "trk00000047"                                          # строка 8
        DL["hold"].add(dbl_id)
        posts, cancels = len(DL["posts"]), len(DL["cancels"])
        page.dblclick(row_dl(8))
        page.wait_for_function(label_starts, arg=[row_dl(8), "Подготовка"])
        check(page.get_attribute(row_dl(8), "aria-disabled") == "true" and len(DL["posts"]) == posts + 1
              and len(DL["cancels"]) == cancels, "двойной клик «Скачать» до ответа backend: один запуск, без отмены")
        page.click("#tracks .row:nth-child(8) .track__more")
        check(page.inner_text("#menu [data-act=download]") == "Загрузка запускается…"
              and page.get_attribute("#menu [data-act=download]", "aria-disabled") == "true",
              "меню, пока запуск не подтверждён: «Загрузка запускается…», пункт неактивен")
        page.keyboard.press("Escape")
        held_release()
        page.wait_for_function("(s) => (document.querySelector(s)?.getAttribute('aria-label') || '').includes('отменить')",
                               arg=row_dl(8))
        check(page.get_attribute(row_dl(8), "aria-disabled") is None and len(DL["cancels"]) == cancels,
              "после ответа backend кнопка снова доступна и предлагает отмену")
        DL["hold"].discard(dbl_id)
        DL["stage"][dbl_id] = "done"
        page.wait_for_function(has_class, arg=[row_dl(8), "is-done"])

        page.click(row_dl(6))
        page.click(row_dl(7))
        page.wait_for_function(label_starts, arg=[row_dl(6), "В очереди"])
        DL["stage"][run_id] = "downloading"
        page.wait_for_function(label_starts, arg=[row_dl(7), "Скачивание"])
        page.screenshot(path=str(SHOTS / "16f-download-queue.png"))
        page.click(".nav__item[data-view=downloads]")
        page.wait_for_function("document.body.dataset.view === 'downloads'")
        page.wait_for_function("(t) => document.querySelector('#status')?.innerText.includes(t)", arg="в очереди: 1")
        check("загружается: 1 · в очереди: 1" in page.inner_text("#status"),
              "«Скачанные»: идущие и ждущие места загрузки посчитаны отдельно")
        DL["stage"][wait_id] = "cancelled"
        DL["stage"][run_id] = "cancelled"
        page.wait_for_function("(t) => !document.querySelector('#status')?.innerText.includes(t)", arg="в очереди")
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
        # S10: в теле ещё и снимок трека (пропуск до порога — отдельная запись истории)
        check([(x["source"], x["id"], (x.get("track") or {}).get("id")) for x in skips] == [("yt", "trk00000040", "trk00000040")],
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
            assert poll(page, "(v) => fetch('/api/settings').then(r => r.json()).then(d => d.values.autoplay === v)", on), "autoplay не записан"

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
        check(media_session(page)["state"] == "paused", "Media Session: очередь закончилась — состояние «пауза»")
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

        print("Восстановление очереди, трека, позиции и режимов после перезагрузки (S10)")
        saved_state = "fetch('/api/player/state').then(r => r.json()).then(d => d.state)"
        page.fill("#search-input", "s10")
        page.keyboard.press("Enter")
        page.wait_for_function("document.querySelectorAll('#tracks .row:not(.row--skel)').length === 10")
        page.locator("#tracks .row").nth(3).click(position={"x": 260, "y": 20})
        check(wait_track("trk00000203"), "играет 4-й трек очереди из 10")
        page.click(rep_btn); page.click(rep_btn)                     # повтор трека
        page.wait_for_function("(() => { const a = document.querySelector('#audio'); return Number.isFinite(a.duration) && a.currentTime > 0 })()")
        page.evaluate("document.querySelector('#audio').currentTime = 12")
        page.wait_for_function("document.querySelector('#audio').currentTime >= 12")
        page.click("#player [data-do=play]")                         # пауза: позиция записывается сразу
        page.wait_for_function("document.querySelector('#audio').paused")
        check(poll(page, f"{saved_state}.then(s => !!s && s.index === 3 && s.repeat === 'one' && !s.shuffle"
                         " && s.position >= 11.9 && s.position < 14 && s.tracks.length === 10)"),
              "очередь, текущий трек, позиция и режимы записаны в backend (без записи на каждый timeupdate)")
        title203 = track(203)["title"]
        page.reload()
        page.wait_for_function("document.querySelector('#hero').dataset.feed === 'ready' && document.body.dataset.track === '1'", timeout=15000)
        st = audio_state(page)
        check(st["paused"] and not page.evaluate("document.querySelector('#audio').getAttribute('src')"),
              "после перезапуска звук сам не включается (поток даже не запрашивается)")
        check(page.text_content("#hero-title") == title203 and page.inner_text("#hero-play-label") == "Продолжить",
              "баннер показывает восстановленный трек на паузе: «Продолжить»")
        check(page.text_content("#hero-cur") in ("0:12", "0:13"), f"позиция восстановлена ({page.text_content('#hero-cur')})")
        check(all(u["mode"] == "one" for u in repeat_ui()) and page.get_attribute("#player [data-do=shuffle]", "aria-pressed") == "false",
              "режимы повтора и перемешивания восстановлены")
        ms = media_session(page)
        check(ms["title"] == title203 and ms["state"] == "paused", "системный оверлей (Media Session): восстановленный трек, пауза")
        page.click(".nav__item[data-view=history]")
        page.wait_for_selector("#player:not([hidden])")
        check(page.text_content("#player-title") == title203, "мини-плеер на другом экране — тот же восстановленный трек")
        page.click("#btn-queue")
        page.wait_for_selector("#queue:not([hidden]) .q")
        q_after = page.evaluate(q_titles)
        check(len(q_after) == 10 and q_after == [track(200 + i)["title"] for i in range(10)]
              and page.locator("#queue-list .q.is-current .q__title").text_content() == title203,
              "очередь восстановлена целиком, в прежнем порядке, текущий трек отмечен")
        page.click("#queue-close")
        ms_action(page, "play")                                      # медиаклавиша Play (кнопка — в e2e_settings verify)
        try:
            page.wait_for_function("(() => { const a = document.querySelector('#audio'); return a.src.endsWith('/trk00000203')"
                                   " && !a.paused && a.currentTime >= 11.5 })()", timeout=10000)
            resumed = True
        except Exception:
            resumed = False
        check(resumed and media_session(page)["state"] == "playing",
              "медиаклавиша Play продолжает восстановленный трек с сохранённой позиции")
        page.click("#player [data-do=next]")
        check(wait_track("trk00000204"), "«Следующий» — следующий трек восстановленной очереди")
        page.click(rep_btn)                                          # one → off

        print("История: запись после порога, сигнал пропуска (S10)")
        played: list[str] = []
        skipped: list[dict] = []

        def on_history(r) -> None:
            if r.method == "POST" and r.url.endswith("/api/history"):
                played.append(r.post_data_json["id"])
            elif r.url.endswith("/api/history/skip"):
                skipped.append(r.post_data_json)

        def wait_played(tid: str, n: int, timeout: float = 10.0) -> bool:
            import time
            deadline = time.monotonic() + timeout
            while played.count(tid) < n and time.monotonic() < deadline:
                page.wait_for_timeout(100)
            return played.count(tid) >= n

        page.on("request", on_history)
        page.fill("#search-input", "s10h")
        page.keyboard.press("Enter")
        page.wait_for_function("document.querySelectorAll('#tracks .row:not(.row--skel)').length === 4")
        page.locator("#tracks .row").first.click(position={"x": 260, "y": 20})
        page.wait_for_function("(() => { const a = document.querySelector('#audio'); return a.src.endsWith('/trk00000300') && a.currentTime > 0.6 })()")
        page.click("#player [data-do=next]")
        check(wait_track("trk00000301"), "короткие треки (6 с, порог прослушивания 3 с) играют")
        page.wait_for_timeout(300)
        s300 = [(x["source"], x["id"], (x.get("track") or {}).get("id")) for x in skipped if x["id"] == "trk00000300"]
        check("trk00000300" not in played and s300 == [("yt", "trk00000300", "trk00000300")],
              f"запуск короче порога — не прослушивание, а один сигнал пропуска со снимком трека ({played}, {s300})")
        check(wait_played("trk00000301", 1) and played.count("trk00000301") == 1, "после 3 с прослушивания (50 % от 6 с) — одна запись в историю")
        page.evaluate("document.querySelector('#audio').currentTime = 0.2")       # назад и снова через порог
        page.click("#player [data-do=play]"); page.wait_for_timeout(400); page.click("#player [data-do=play]")   # пауза и продолжение
        page.wait_for_function("(() => { const a = document.querySelector('#audio'); return !a.paused && a.currentTime > 3.6 })()", timeout=10000)
        check(played.count("trk00000301") == 1, "перемотка назад и пауза не дают второй записи того же запуска")
        page.click("#player [data-do=next]")
        check(wait_track("trk00000302"), "следующий трек")
        page.wait_for_timeout(200)
        check(not [x for x in skipped if x["id"] == "trk00000301"], "переключение после порога — не пропуск")
        page.wait_for_function("Number.isFinite(document.querySelector('#audio').duration)")
        page.evaluate("document.querySelector('#audio').currentTime = 5")         # перемотка почти в конец
        check(wait_track("trk00000303"), "трек с перемоткой доиграл, очередь продолжилась")
        check("trk00000302" not in played and not [x for x in skipped if x["id"] == "trk00000302"],
              "перемотка вперёд не обходит порог; естественный конец трека — не пропуск")
        page.click(rep_btn); page.click(rep_btn)                      # повтор трека
        check(wait_played("trk00000303", 1), "первый проигрыш трека с повтором записан после порога")
        page.wait_for_function("(() => { const a = document.querySelector('#audio'); return a.src.endsWith('/trk00000303') && a.currentTime < 1.5 && !a.paused })()",
                               timeout=10000)
        check(played.count("trk00000303") == 1, "повтор трека: новый проигрыш до порога ещё не записан")
        check(wait_played("trk00000303", 2) and played.count("trk00000303") == 2 and not [x for x in skipped if x["id"] == "trk00000303"],
              "повтор трека: каждый проигрыш — одна запись после своего порога, пропусков нет")
        page.click(rep_btn)                                          # one → off
        page.remove_listener("request", on_history)

        print("«Открыть альбом»: по ID и резервный поиск по названию (S10)")
        album_reqs: list[str] = []
        on_album = lambda r: album_reqs.append(urlparse(r.url).path) if "/api/album/" in r.url or "/api/search/albums" in r.url else None   # noqa: E731
        page.on("request", on_album)
        page.fill("#search-input", "gspd")
        page.keyboard.press("Enter")
        page.click("#tabs [data-tab=tracks]")
        page.wait_for_function("document.querySelectorAll('#tracks .row:not(.row--skel)').length === 20")
        page.click("#tracks .row:nth-child(1) .track__more")
        page.click("#menu [data-act=album]")
        page.wait_for_function("document.querySelector('#d-kind').textContent === 'Альбом' && document.querySelectorAll('#tracks .row:not(.row--skel)').length === 8")
        check(album_reqs[:2] == ["/api/search/albums", "/api/album/yt/MPRE0"],
              f"трек без id альбома: альбом найден поиском по исполнителю и названию, как раньше ({album_reqs})")
        album_reqs.clear()
        page.click("#tracks .row:nth-child(2) .track__more")
        page.click("#menu [data-act=album]")
        page.wait_for_function("(p) => performance.getEntriesByType('resource').some((e) => e.name.endsWith(p))",
                               arg=f"/api/album/yt/{E2E_ALBUM_ID}")
        page.wait_for_function("document.querySelectorAll('#tracks .row:not(.row--skel)').length === 8")
        page.wait_for_timeout(300)
        check(album_reqs == [f"/api/album/yt/{E2E_ALBUM_ID}"], f"трек с id альбома: альбом открыт по id, без поиска ({album_reqs})")
        page.click("#back")
        page.wait_for_function("document.querySelector('#d-kind').textContent === 'Альбом'")
        check(True, "«Назад» после альбома по id работает")
        page.remove_listener("request", on_album)

        print("Нет соединения: сообщение вместо перехода на «Скачанные» (S10)")
        page.fill("#search-input", "gspd")
        page.keyboard.press("Enter")
        page.click("#tabs [data-tab=tracks]")
        page.wait_for_function("document.querySelectorAll('#tracks .row:not(.row--skel)').length === 20")
        page.locator("#tracks .row").first.click(position={"x": 260, "y": 20})
        check(wait_track("trk00000040"), "перед потерей сети играет трек из поиска")
        q_before = page.evaluate("[...document.querySelectorAll('#queue-list .q')].length")
        ctx.set_offline(True)
        page.wait_for_selector("#offline:not([hidden])")
        check(page.get_attribute("body", "data-view") == "search" and page.locator("#tracks .row").count() == 20,
              "без сети пользователь остаётся на открытом экране (поиск), список на месте")
        check(page.get_attribute("#offline", "role") == "status" and "Скачанные треки доступны" in page.inner_text("#offline")
              and visible(page, "#offline-downloads"), "видно сообщение (role=status) с кнопкой «Скачанные»")
        check(audio_state(page)["src"].endswith("/trk00000040") and not page.is_disabled("#player [data-do=next]")
              and page.evaluate("[...document.querySelectorAll('#queue-list .q')].length") == q_before,
              "плеер и очередь не сброшены, управление доступно")
        page.screenshot(path=str(SHOTS / "25-offline-banner.png"))
        ctx.set_offline(False)
        page.wait_for_selector("#offline", state="hidden")
        check(True, "событие online скрывает сообщение")
        for _ in range(3):                                           # повторные offline/online
            ctx.set_offline(True)
            page.wait_for_selector("#offline:not([hidden])")
            ctx.set_offline(False)
            page.wait_for_selector("#offline", state="hidden")
        ctx.set_offline(True)
        page.wait_for_selector("#offline:not([hidden])")
        check(page.locator("#offline").count() == 1 and page.locator("#offline-downloads").count() == 1,
              "повторные события не создают дубликатов сообщения")
        page.click("#offline-close")
        check(page.is_hidden("#offline") and page.evaluate("navigator.onLine") is False, "сообщение можно закрыть (сеть ещё не вернулась)")
        ctx.set_offline(False); ctx.set_offline(True)
        page.wait_for_selector("#offline:not([hidden])")
        page.click("#offline-downloads")
        page.wait_for_function("document.body.dataset.view === 'downloads'")
        check(True, "переход в «Скачанные» — только по кнопке сообщения")
        check(visible(page, "#offline"), "в «Скачанных» сообщение остаётся, пока браузер не сообщит о сети")
        ctx.set_offline(False)
        page.wait_for_selector("#offline", state="hidden")
        # Запись несохранённого при закрытии окна (sendBeacon) проверяет tests/api/test_playback_browser.py: здесь
        # все запросы идут через перехват Playwright (ctx.route), а он не доставляет запросы выгружаемой страницы.

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
        clear_playback()                                     # S10: иначе вкладка восстановила бы прошлую очередь
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
        rt_page.close()

        # ------------------------------------------------------------ S12: доступность
        # Отдельная вкладка (общий профиль и подмена сети). Сначала — поведение мышью, которое правки доступности
        # не должны изменить; затем клавиатура, роли и имена, фокус, контраст.
        ax = ctx.new_page()
        ax.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
        ax.on("console", lambda m: errors.append(f"console.{m.type}: {m.text}") if m.type == "error" and "Failed to load resource" not in m.text else None)
        clear_playback()                                             # вкладка без восстановленной очереди (S10)
        ax.goto(BASE)
        ax.wait_for_function("document.querySelector('#hero').dataset.feed === 'ready'", timeout=15000)
        rows_ready = "(n) => document.querySelectorAll('#tracks .row:not(.row--skel)').length === n"
        is_artist_page = "document.querySelector('#d-kind').textContent === 'Артист' && document.querySelector('#d-title').textContent === 'GSPD'"

        def ax_search(q: str = "gspd", n: int = 20) -> None:
            ax.fill("#search-input", q)
            ax.keyboard.press("Enter")
            ax.wait_for_selector("#tabs:not([hidden])")
            if ax.get_attribute("#tabs [data-tab=tracks]", "aria-selected") != "true":   # вкладка «Артисты» с прошлого шага
                ax.click("#tabs [data-tab=tracks]")
            ax.wait_for_function(rows_ready, arg=n)

        def ax_playing(tid: str) -> bool:
            try:
                ax.wait_for_function("(id) => { const a = document.querySelector('#audio'); return a.src.endsWith('/' + id) && !a.paused }",
                                     arg=tid, timeout=10000)
                return True
            except Exception:
                return False

        print("Доступность (S12): поведение мышью сохраняется")
        RECS["paged"] = True                                         # следующие страницы ленты — новые треки
        ax.wait_for_function("document.querySelectorAll('#new-grid .tcard').length >= 20")
        feed_n = ax.locator("#new-grid .tcard").count()
        ax.evaluate("document.querySelector('#main').scrollTo({top: 1e6})")
        try:
            ax.wait_for_function("(n) => document.querySelectorAll('#new-grid .tcard').length > n", arg=feed_n, timeout=10000)
            grown = True
        except Exception:
            grown = False
        check(grown, "прокрутка до конца ленты главной подгружает следующую страницу (IntersectionObserver)")

        print("Доступность (S12): «Загрузить ещё» на главной")
        more_btn = """(s) => { const b = document.querySelector(s); return {tag: b.tagName, type: b.type, role: b.getAttribute('role'),
            tabindex: b.getAttribute('tabindex'), disabled: b.disabled, ad: b.getAttribute('aria-disabled'), busy: b.getAttribute('aria-busy'),
            state: b.dataset.state, text: b.textContent, hidden: b.hidden} }"""
        to_bottom = "document.querySelector('#main').scrollTo({top: 1e6})"
        RECS["fail_more"] = True
        ax.evaluate(to_bottom)
        ax.wait_for_function("document.querySelector('#home-more').dataset.state === 'error'")
        hb = ax.evaluate(more_btn, "#home-more")
        check(hb["tag"] == "BUTTON" and hb["type"] == "button" and hb["role"] is None and hb["tabindex"] is None
              and not hb["disabled"] and hb["ad"] is None and hb["text"] == "Не удалось загрузить. Нажми, чтобы повторить"
              and ax.get_by_role("button", name="Не удалось загрузить. Нажми, чтобы повторить").count() == 1,
              f"#home-more — настоящая кнопка; ошибка подгрузки: текст на русском, кнопка доступна для повтора ({hb})")
        RECS["fail_more"] = False
        n = ax.locator("#new-grid .tcard").count()
        ax.focus("#home-more")
        ax.keyboard.press("Enter")
        ax.wait_for_function("(n) => document.querySelectorAll('#new-grid .tcard').length >= n + 24", arg=n)
        ax.wait_for_function("document.querySelector('#home-more').dataset.state === 'idle'")
        hb = ax.evaluate(more_btn, "#home-more")
        check(hb["text"] == "Загрузить ещё" and hb["busy"] is None and ax.evaluate("document.activeElement.id") == "home-more",
              "Enter повторяет подгрузку ленты; после неё кнопка «Загрузить ещё», фокус на ней")
        RECS["fail_more"] = True
        ax.evaluate(to_bottom)
        ax.wait_for_function("document.querySelector('#home-more').dataset.state === 'error'")
        RECS["fail_more"] = False
        n = ax.locator("#new-grid .tcard").count()
        ax.focus("#home-more")
        ax.keyboard.press("Space")
        try:
            ax.wait_for_function("(n) => document.querySelectorAll('#new-grid .tcard').length >= n + 24", arg=n, timeout=10000)
            spaced = True
        except Exception:
            spaced = False
        check(spaced, "пробел на «Загрузить ещё» тоже повторяет подгрузку ленты")
        RECS.update(paged=False, fail_more=False)
        ax.evaluate("document.querySelector('#main').scrollTo({top: 0})")
        ax_search()
        before = audio_state(ax)
        ax.click("#tracks .row:nth-child(3) .who.is-link")
        ax.wait_for_function(is_artist_page)
        after = audio_state(ax)
        check(after["src"] == before["src"] and after["paused"] == before["paused"],
              "мышь: имя исполнителя в строке открывает его страницу и не запускает трек")
        ax.click("#back")
        ax.wait_for_function(rows_ready, arg=20)
        ax.locator("#tracks .row").nth(4).click(position={"x": 260, "y": 20})
        check(ax_playing("trk00000044"), "мышь: клик по строке трека запускает этот трек")
        ax.click("#player-artist .who.is-link")
        ax.wait_for_function(is_artist_page)
        check(ax_playing("trk00000044"), "мышь: имя исполнителя в мини-плеере открывает его страницу, трек продолжает играть")
        ax.click("#back")
        ax.wait_for_function(rows_ready, arg=20)
        q_count = "document.querySelectorAll('#queue-list .q').length"
        ax.click("#btn-queue")
        ax.wait_for_selector("#queue:not([hidden]) .q")
        q_before = ax.evaluate(q_count)
        ax.click("#queue-close")
        ax.click("#tracks .row:nth-child(7) .track__more")
        ax.click("#menu [data-act=add]")
        ax.click("#btn-queue")
        ax.wait_for_selector("#queue:not([hidden]) .q")
        check(ax.evaluate(q_count) == q_before + 1 and ax.is_hidden("#menu"),
              "мышь: пункт меню «Добавить в очередь» выполняется один раз, меню закрывается")
        ax.click("#queue-close")
        ax.click("#tracks .row:nth-child(7) .track__more")
        ax.click("#search-input")
        check(ax.is_hidden("#menu") and ax.evaluate("document.activeElement === document.querySelector('#search-input')"),
              "мышь: клик вне меню закрывает его, фокус остаётся там, куда нажал пользователь")

        print("Доступность (S12): ссылки на исполнителей")
        active = "() => { const a = document.activeElement; return {cls: a.className, label: a.getAttribute('aria-label'), tag: a.tagName," \
                 " row: a.closest('.row') ? [...a.closest('#tracks').children].indexOf(a.closest('.row')) + 1 : 0} }"
        # Интерактивные элементы внутри других: кнопка в кнопке, ссылка в кнопке, что угодно в role=button и т. п.
        nested = """() => [...document.querySelectorAll('button, a[href], input, select, textarea, [tabindex], [role=button], [role=link], [role=menuitem], [role=tab], [role=slider]')]
            .filter(n => n.parentElement?.closest('button, a[href], [role=button], [role=link], [role=menuitem], [role=tab]'))
            .map(n => n.outerHTML.slice(0, 90))"""
        check(ax.evaluate(nested) == [] and ax.locator("button.track").count() == 0
              and ax.locator("#tracks .row .track__play").count() == 20
              and ax.locator("#tracks .row .track__play button, #tracks .row .track__play .who").count() == 0,
              "нет интерактивных элементов внутри кнопок: кнопка трека (.track__play) отдельно от ссылок исполнителей")
        a2 = ARTISTS[41 % len(ARTISTS)]
        ax.focus("#tracks .row:nth-child(2) .track__play")
        ax.keyboard.press("Tab")
        f1 = ax.evaluate(active)
        ax.keyboard.press("Tab")
        f2 = ax.evaluate(active)
        ax.keyboard.press("Shift+Tab")
        ax.keyboard.press("Shift+Tab")
        f0 = ax.evaluate(active)
        check(f1 == {"cls": "who is-link", "label": f"Исполнитель: {a2}", "tag": "BUTTON", "row": 2}
              and "track__fav" in f2["cls"] and f2["row"] == 2 and f0["cls"] == "track__play" and f0["row"] == 2,
              f"Tab: кнопка трека → «Исполнитель: {a2}» → «В избранное»; Shift+Tab обратно ({f1}, {f2}, {f0})")
        check(ax.get_by_role("button", name=f"Исполнитель: {a2}").count() >= 1
              and ax.get_by_role("button", name=re.compile(r"^Играть: ")).count() == 20,
              "роли и имена: ссылка исполнителя — кнопка «Исполнитель: …», у каждой строки кнопка «Играть: …»")
        before = audio_state(ax)
        ax.focus("#tracks .row:nth-child(3) .who.is-link")
        ax.keyboard.press("Enter")
        ax.wait_for_function(is_artist_page)
        after = audio_state(ax)
        check(after["src"] == before["src"] and after["paused"] == before["paused"] and not after["paused"],
              "Enter на ссылке исполнителя открывает его страницу, трек продолжает играть (не запускается и не ставится на паузу)")
        ax.click("#back")
        ax.wait_for_function(rows_ready, arg=20)
        ax.focus("#tracks .row:nth-child(4) .who.is-link")
        ax.keyboard.press("Space")
        ax.wait_for_function(is_artist_page)
        after = audio_state(ax)
        check(after["src"] == before["src"] and not after["paused"], "пробел на ссылке исполнителя — тоже переход, пауза не срабатывает")
        ax.click("#back")
        ax.wait_for_function(rows_ready, arg=20)
        ax.focus("#tracks .row:nth-child(6) .track__play")
        ax.keyboard.press("Enter")
        check(ax_playing("trk00000045"), "Enter на кнопке трека запускает этот трек")
        ax.focus("#tracks .row:nth-child(6) .track__play")
        ax.keyboard.press("Space")
        ax.wait_for_function("document.querySelector('#audio').paused")
        ax.keyboard.press("Enter")
        check(ax_playing("trk00000045"), "пробел на играющей строке — пауза (один раз), Enter — продолжить")

        ax_search("feat", 2)
        names = "(s) => [...document.querySelectorAll(s)].map(n => [n.tagName, n.textContent, n.getAttribute('aria-label')])"
        check(ax.evaluate(names, "#tracks .row:nth-child(1) .track__artist .who") == [["BUTTON", "GSPD", "Исполнитель: GSPD"], ["BUTTON", "Vtornik", "Исполнитель: Vtornik"]]
              and ax.text_content("#tracks .row:nth-child(1) .track__artist") == "GSPD, Vtornik"
              # строка имён — flex (аудит S12): innerText разбивает её по элементам, поэтому видимую «одну строку» проверяем по раскладке
              and ax.evaluate("new Set([...document.querySelectorAll('#tracks .row:nth-child(1) .track__artist > *')].map(n => Math.round(n.getBoundingClientRect().top))).size") == 1,
              "несколько исполнителей — раздельные ссылки, видимый текст «GSPD, Vtornik»")
        check(ax.evaluate(names, "#tracks .row:nth-child(2) .track__artist .who") == [["BUTTON", "Кино", "Исполнитель: Кино"], ["SPAN", "Безымянный", None]],
              "исполнитель без id — обычный текст, ложной ссылки нет")

        print("  мини-плеер, страница трека, полноэкранный плеер, очередь")
        cur_title = track(45)["title"]
        check(ax.get_attribute("#player-now", "role") is None and ax.get_attribute("#player-now", "tabindex") is None
              and ax.get_by_role("button", name=f"Открыть страницу трека: {cur_title}").count() == 1,
              "мини-плеер: блок не кнопка; страницу трека открывает кнопка «Открыть страницу трека: …»")
        ax.focus("#player-open")
        ax.keyboard.press("Tab")
        f = ax.evaluate(active)
        check(f["cls"] == "who is-link" and f["label"] == f"Исполнитель: {ARTISTS[5]}", f"мини-плеер: после названия Tab ведёт к ссылке исполнителя ({f})")
        ax.keyboard.press("Enter")
        ax.wait_for_function("document.querySelector('#d-kind').textContent === 'Артист'")
        check(ax_playing("trk00000045"), "мини-плеер: Enter на исполнителе открывает его страницу, трек играет")
        ax.focus("#player-open")
        ax.keyboard.press("Enter")
        ax.wait_for_selector("#view-now:not([hidden])")
        check(ax.get_attribute("body", "data-view") == "now", "мини-плеер: Enter на названии открывает страницу трека")
        check(ax.evaluate(names, "#now-artist .who") == [["BUTTON", ARTISTS[5], f"Исполнитель: {ARTISTS[5]}"]],
              "страница трека: исполнитель — кнопка-ссылка")
        ax.click("[data-do=expand]")
        ax.wait_for_selector("#fs:not([hidden])")
        ax.focus("#fs-artist .who.is-link")
        ax.keyboard.press("Enter")
        ax.wait_for_function("document.querySelector('#d-kind').textContent === 'Артист'")
        check(ax.is_hidden("#fs") and ax.get_attribute("body", "data-focus") == "0" and ax_playing("trk00000045"),
              "полноэкранный плеер: Enter на исполнителе закрывает режим и открывает страницу, трек играет")
        ax.click("#btn-queue")
        ax.wait_for_selector("#queue:not([hidden]) .q")
        check(ax.locator("#queue-list .q__artist button, #queue-list .q__artist [tabindex]").count() == 0 and ax.evaluate(nested) == [],
              "очередь: имя исполнителя — текст внутри кнопки строки, без вложенных ссылок")
        ax.click("#queue-close")

        ax_search()
        ring_room = """() => { const b = document.querySelector('#tracks .row:nth-child(2) .who.is-link'), c = b.parentElement;
            const r = b.getBoundingClientRect(), p = c.getBoundingClientRect();
            return r.top - p.top >= 1.9 && p.bottom - r.bottom >= 1.9 && r.left - p.left >= 1.9 }"""
        for (w, h) in [(980, 640), (1920, 1080)]:
            ax.set_viewport_size({"width": w, "height": h})
            ax.wait_for_timeout(150)
            meta = rect(ax, "#tracks .row:nth-child(2) .track__meta")
            link = rect(ax, "#tracks .row:nth-child(2) .who.is-link")
            check(visible(ax, "#tracks .row:nth-child(2) .who.is-link") and link["l"] >= meta["l"] - 4 and link["r"] <= meta["r"] + 4
                  and ax.evaluate(ring_room) and ax.evaluate("document.querySelector('#main').scrollWidth <= document.querySelector('#main').clientWidth + 1"),
                  f"{w}px: ссылка исполнителя под названием, рамке фокуса хватает места, переполнения нет")
        ax.set_viewport_size({"width": 1440, "height": 900})

        print("Доступность (S12): меню трека с клавиатуры")
        act = "document.activeElement.dataset.act || ''"

        def menu_to(pg, name: str) -> None:                          # стрелкой вниз до пункта name (не больше круга)
            for _ in range(12):
                if pg.evaluate(act) == name:
                    return
                pg.keyboard.press("ArrowDown")
            raise AssertionError(f"в меню нет пункта {name}")
        more3 = "#tracks .row:nth-child(3) .track__more"
        is_focused = "(s) => document.activeElement === document.querySelector(s)"
        ax.focus(more3)
        ax.keyboard.press("ArrowDown")
        check(ax.is_hidden("#menu") and ax.evaluate(is_focused, more3), "закрытое меню: стрелки ничего не открывают и фокус не двигают")
        ax.keyboard.press("Enter")
        ax.wait_for_selector("#menu:not([hidden])")
        acts = ax.evaluate("[...document.querySelectorAll('#menu [role=menuitem]')].map(b => b.dataset.act)")
        check(ax.evaluate(act) == acts[0] == "next" and ax.get_attribute(more3, "aria-expanded") == "true"
              and ax.get_attribute("#menu", "role") == "menu" and ax.get_attribute("#menu", "aria-label").startswith("Действия с треком: ")
              and ax.evaluate("[...document.querySelectorAll('#menu > *')].every(b => b.getAttribute('role') === 'menuitem' && b.tabIndex === -1)"),
              "Enter на «⋯»: меню с ролью и именем, фокус на первом пункте, у «⋯» aria-expanded=true")
        seq = []
        for key in ["ArrowDown", "ArrowDown", "ArrowUp", "ArrowUp", "ArrowUp", "ArrowDown", "End", "Home"]:
            ax.keyboard.press(key)
            seq.append(ax.evaluate(act))
        check(seq == [acts[1], acts[2], acts[1], acts[0], acts[-1], acts[0], acts[-1], acts[0]],
              f"стрелки вниз/вверх по кругу, Home и End — крайние пункты ({seq})")
        ax.keyboard.press("Escape")
        check(ax.is_hidden("#menu") and ax.evaluate(is_focused, more3) and ax.get_attribute(more3, "aria-expanded") is None,
              "Escape закрывает меню и возвращает фокус на «⋯», aria-expanded снят")
        ax.click("#btn-queue")
        ax.wait_for_selector("#queue:not([hidden]) .q")
        q_before = ax.evaluate(q_count)
        ax.click("#queue-close")
        ax.focus(more3)
        ax.keyboard.press("Enter")
        ax.keyboard.press("ArrowDown")
        ax.keyboard.press("Enter")
        check(ax.is_hidden("#menu") and ax.evaluate(is_focused, more3), "Enter на пункте закрывает меню, фокус снова на «⋯»")
        ax.click("#btn-queue")
        ax.wait_for_selector("#queue:not([hidden]) .q")
        check(ax.evaluate(q_count) == q_before + 1, "действие пункта с клавиатуры выполнено ровно один раз")
        ax.click("#queue-close")
        ax.focus(more3)
        ax.keyboard.press("Enter")
        ax.keyboard.press("Tab")
        tab_next = ax.evaluate(active)
        ax.focus(more3)
        ax.keyboard.press("Enter")
        ax.keyboard.press("Shift+Tab")
        tab_prev = ax.evaluate(active)
        check(ax.is_hidden("#menu") and tab_next["cls"] == "track__play" and tab_next["row"] == 4
              and "track__dl" in tab_prev["cls"] and tab_prev["row"] == 3,
              f"Tab в меню закрывает его и продолжает обход от «⋯» (вперёд и назад: {tab_next['cls']}, {tab_prev['cls']})")
        ax.click(more3)
        ax.click(more3)
        check(ax.is_hidden("#menu") and ax.get_attribute(more3, "aria-expanded") is None, "повторное нажатие на ту же «⋯» закрывает меню")
        ax.focus(more3)
        ax.keyboard.press("Enter")
        ax.click("#search-input")
        check(ax.is_hidden("#menu") and ax.evaluate(is_focused, "#search-input") and ax.get_attribute(more3, "aria-expanded") is None,
              "меню, открытое с клавиатуры: клик вне закрывает его, фокус остаётся на поле поиска")

        ax.focus(more3)
        ax.keyboard.press("Enter")
        ax.keyboard.press("ArrowDown")
        ax.keyboard.press("ArrowDown")
        ax.keyboard.press("Enter")                                   # «Добавить в плейлист…»
        ax.wait_for_selector("#dlg-pick[open] .pick__item")
        ax.keyboard.press("Escape")
        ax.wait_for_selector("#dlg-pick", state="hidden")
        check(ax.evaluate(is_focused, more3), "диалог выбора плейлиста из меню: Escape закрывает его, фокус возвращается на «⋯»")

        artist_reqs: list[str] = []
        on_artist = lambda r: artist_reqs.append(r.url) if re.search(r"/api/artist/yt/[\w-]+$", urlparse(r.url).path) else None   # noqa: E731
        ax.on("request", on_artist)
        ax.focus("#tracks .row:nth-child(2) .track__more")
        ax.keyboard.press("Enter")
        menu_to(ax, "artist")
        ax.keyboard.press("Enter")                                   # «Перейти к исполнителю»
        ax.wait_for_function(is_artist_page)
        ax.wait_for_timeout(300)
        ax.remove_listener("request", on_artist)
        focus_ok = ax.evaluate("""() => { const a = document.activeElement;
            return a.id === 'd-title' && a.checkVisibility() && !a.closest('[hidden]') }""")
        check(len(artist_reqs) == 1 and focus_ok,
              f"«Перейти к исполнителю» с клавиатуры: переход один раз, фокус на заголовке новой страницы, не в скрытом DOM ({len(artist_reqs)})")
        ax.click("#back")
        ax.wait_for_function(rows_ready, arg=20)

        dis_row, dis_id = 9, "trk00000048"
        more9 = f"#tracks .row:nth-child({dis_row}) .track__more"
        DL["mock"] = True
        DL["hold"].add(dis_id)
        posts, cancels = len(DL["posts"]), len(DL["cancels"])
        ax.click(f"#tracks .row:nth-child({dis_row}) .track__dl")
        ax.wait_for_function("(s) => document.querySelector(s)?.getAttribute('aria-disabled') === 'true'", arg=f"#tracks .row:nth-child({dis_row}) .track__dl")
        ax.focus(more9)
        ax.keyboard.press("Enter")
        ax.keyboard.press("End")
        end_act = ax.evaluate(act)
        ax.keyboard.press("ArrowUp")
        on_disabled = ax.evaluate("[document.activeElement.dataset.act, document.activeElement.getAttribute('aria-disabled')]")
        ax.keyboard.press("Enter")
        ax.wait_for_timeout(300)
        check(end_act == "share" and on_disabled == ["download", "true"] and not ax.is_hidden("#menu")
              and ax.evaluate(act) == "download" and len(DL["posts"]) == posts + 1 and len(DL["cancels"]) == cancels,
              "недоступный пункт: стрелки его фокусируют, Enter ничего не делает (меню открыто, запросов нет); End — последний доступный")
        ax.keyboard.press("Escape")
        check(ax.evaluate(is_focused, more9), "Escape с недоступного пункта — фокус на «⋯»")
        held_release()
        DL["hold"].discard(dis_id)
        DL["stage"][dis_id] = "cancelled"
        ax.wait_for_function("(s) => (document.querySelector(s)?.getAttribute('aria-label') || '') === 'Скачать'", arg=f"#tracks .row:nth-child({dis_row}) .track__dl")
        DL["mock"] = False
        DL["stage"].clear()

        print("  очередь полноэкранного плеера, панель очереди, карточки")
        ax.click("#player-open")
        ax.wait_for_selector("#view-now:not([hidden])")
        ax.click("[data-do=expand]")
        ax.wait_for_selector("#fs:not([hidden])")
        ax.click("#fs-queue-btn")
        ax.wait_for_selector("#fs-q-list .fq__row")
        rows = queue_rows(ax)
        mover = [r for r in rows if not r["cur"]][1]
        ax.focus(f"#fs-q-list .fq__more[data-index='{mover['i']}']")
        ax.keyboard.press("Enter")
        menu_to(ax, "down")
        ax.keyboard.press("Enter")                                   # «Ниже»
        ax.wait_for_function("([i, t]) => document.querySelector(`#fs-q-list .fq__row[data-index='${i}'] .fq__title`)?.textContent === t",
                             arg=[mover["i"] + 1, mover["t"]])
        moved = ax.evaluate("""() => { const a = document.activeElement, r = a.closest('.fq__row');
            return a.classList.contains('fq__more') && r ? {i: +a.dataset.index, t: r.querySelector('.fq__title').textContent} : null }""")
        check(moved == {"i": mover["i"] + 1, "t": mover["t"]}, f"очередь: «Ниже» с клавиатуры — фокус на «⋯» того же трека на новом месте ({moved})")
        ax.keyboard.press("Enter")
        menu_to(ax, "rm")
        ax.keyboard.press("Enter")                                   # «Убрать из очереди»
        ax.wait_for_function("(t) => ![...document.querySelectorAll('#fs-q-list .fq__title')].some(n => n.textContent === t)", arg=mover["t"])
        after_rm = ax.evaluate("[document.activeElement.classList.contains('fq__more'), +document.activeElement.dataset.index]")
        check(after_rm == [True, mover["i"] + 1], f"очередь: после «Убрать» фокус на «⋯» строки, вставшей на это место ({after_rm})")
        ax.keyboard.press("Escape")                                  # панель очереди
        ax.keyboard.press("Escape")                                  # полноэкранный режим
        ax.wait_for_selector("#fs", state="hidden")
        ax.click("[data-do=back]")
        ax.wait_for_function("document.body.dataset.view !== 'now'")
        ax.wait_for_selector("#player:not([hidden])")
        ax.focus("#btn-queue")
        ax.keyboard.press("Enter")
        ax.wait_for_selector("#queue:not([hidden]) .q")
        ax.focus("#queue-list .q__main")
        ax.keyboard.press("Escape")
        check(ax.is_hidden("#queue") and ax.evaluate(is_focused, "#btn-queue"), "панель очереди: Escape из панели — фокус на кнопке «Очередь»")
        ax.keyboard.press("Enter")
        ax.wait_for_selector("#queue:not([hidden]) .q")
        ax.focus("#queue-close")
        ax.keyboard.press("Enter")
        check(ax.is_hidden("#queue") and ax.evaluate(is_focused, "#btn-queue"), "панель очереди: «Закрыть» с клавиатуры — фокус на кнопке «Очередь»")
        ax.focus(".nav__item[data-view=home]")
        ax.keyboard.press("Enter")
        ax.wait_for_selector("#view-home:not([hidden]) #new-grid .tcard")
        check(ax.evaluate(is_focused, ".nav__item[data-view=home]"), "переход по пункту меню: фокус остаётся на видимом пункте навигации")
        card_more = "#new-grid .tcard:nth-child(2) .tcard__more"
        ax.focus(card_more)
        ax.wait_for_timeout(250)                                     # переход opacity .12s
        check(ax.evaluate("(s) => getComputedStyle(document.querySelector(s)).opacity", card_more) == "1", "«⋯» карточки видна при фокусе с клавиатуры")
        ax.keyboard.press("Enter")
        opened = ax.evaluate(act) == "next"
        ax.keyboard.press("Escape")
        hero_more = "#hero [data-do=more]"
        ax.focus(hero_more)
        ax.keyboard.press("Enter")
        opened_hero = ax.evaluate(act) == "next" and ax.get_attribute(hero_more, "aria-expanded") == "true"
        ax.keyboard.press("Escape")
        check(opened and opened_hero and ax.evaluate(is_focused, hero_more),
              "меню карточки главной и баннера открываются с клавиатуры, Escape возвращает фокус")

        print("Доступность (S12): «Загрузить ещё» в списке")
        more_reqs: list[str] = []
        on_more = lambda r: more_reqs.append(r.url) if "q=more12" in r.url and "offset=20" in r.url else None   # noqa: E731
        ax.on("request", on_more)
        MORE["mode"] = "fail"
        ax_search("more12", 20)
        ax.evaluate(to_bottom)
        ax.wait_for_function("document.querySelector('#more').dataset.state === 'error'")
        mb = ax.evaluate(more_btn, "#more")
        check(mb["tag"] == "BUTTON" and mb["type"] == "button" and mb["role"] is None and mb["tabindex"] is None
              and not mb["disabled"] and mb["text"] == "Не удалось загрузить. Нажми, чтобы повторить"
              and ax.get_by_role("button", name="Не удалось загрузить. Нажми, чтобы повторить").count() == 1,
              f"#more — настоящая кнопка с понятным именем; ошибка подгрузки: текст на русском, кнопка доступна для повтора ({mb})")
        n = len(more_reqs)
        ax.focus("#more")
        ax.keyboard.press("Space")
        for _ in range(50):                                          # запрос ушёл и снова оборвался
            if len(more_reqs) > n and ax.evaluate("document.querySelector('#more').dataset.state") == "error":
                break
            ax.wait_for_timeout(100)
        check(len(more_reqs) == n + 1 and ax.evaluate("document.activeElement.id") == "more",
              f"пробел на «Загрузить ещё» повторяет запрос, фокус остаётся на кнопке ({len(more_reqs) - n})")
        MORE["mode"] = "hold"
        ax.keyboard.press("Enter")
        ax.wait_for_function("document.querySelector('#more').dataset.state === 'loading'")
        mb = ax.evaluate(more_btn, "#more")
        ax.click("#more", force=True)                                # aria-disabled: Playwright без force не нажимает
        ax.keyboard.press("Enter")                                   # и с клавиатуры (последнее действие — клавиша)
        ax.wait_for_timeout(300)
        check(mb["busy"] == "true" and mb["ad"] == "true" and not mb["disabled"] and mb["text"] == "Загружаем…"
              and ax.evaluate("document.activeElement.id") == "more" and len(more_reqs) == n + 2,
              f"запрос идёт: aria-busy, aria-disabled, «Загружаем…», фокус на кнопке; повторные Enter и клик второго запроса не шлют ({len(more_reqs) - n})")
        more_release()
        ax.wait_for_function(rows_ready, arg=40)
        ax.wait_for_function("document.querySelector('#more').hidden")
        f = ax.evaluate(active)
        check(f["cls"] == "track__play" and f["row"] == 21, f"страниц больше нет: кнопка скрылась, фокус — на первой подгруженной строке ({f})")
        ax.remove_listener("request", on_more)
        MORE["mode"] = "fail"

        print("Доступность (S12): вкладки и источник с клавиатуры")
        ax_search()
        tabs_state = "[...document.querySelectorAll('#tabs .tab')].map(b => [b.dataset.tab, b.getAttribute('aria-selected'), b.tabIndex])"
        check(ax.evaluate(tabs_state) == [["tracks", "true", 0], ["artists", "false", -1], ["albums", "false", -1]]
              and ax.get_attribute("#results", "role") == "tabpanel" and ax.get_attribute("#results", "aria-labelledby") == "tab-tracks",
              "вкладки: в порядке Tab только выбранная, результаты — панель выбранной вкладки")
        ax.focus("#search-input")
        for _ in range(4):
            ax.keyboard.press("Tab")
            if ax.evaluate("document.activeElement.classList.contains('tab')"):
                break
        on_tab = ax.evaluate("document.activeElement.dataset.tab || ''")
        moves = []
        for key in ["ArrowRight", "ArrowRight", "ArrowRight", "ArrowLeft", "End", "Home"]:
            ax.keyboard.press(key)
            moves.append(ax.evaluate("document.activeElement.dataset.tab"))
        check(on_tab == "tracks" and moves == ["artists", "albums", "tracks", "albums", "albums", "tracks"]
              and ax.evaluate(tabs_state)[0][1] == "true",
              f"Tab приходит на выбранную вкладку; стрелки (по кругу), Home и End двигают фокус, не меняя выбор ({moves})")
        ax.keyboard.press("ArrowRight")
        ax.keyboard.press("Enter")
        ax.wait_for_selector("#cards .card--artist")
        check(ax.evaluate(tabs_state)[1] == ["artists", "true", 0] and ax.get_attribute("#results", "aria-labelledby") == "tab-artists"
              and ax.evaluate("document.activeElement.dataset.tab") == "artists",
              "Enter выбирает вкладку «Артисты»: карточки, aria-selected, панель подписана вкладкой, фокус на ней")
        ax.keyboard.press("ArrowLeft")
        ax.keyboard.press("Space")
        ax.wait_for_function(rows_ready, arg=20)
        check(ax.evaluate(tabs_state)[0] == ["tracks", "true", 0], "пробел выбирает вкладку «Треки»")
        src_state = "[...document.querySelectorAll('#source-switch .seg__btn')].map(b => [b.dataset.source, b.getAttribute('aria-selected'), b.tabIndex])"
        ax.focus("#source-switch [data-source=yt]")
        ax.keyboard.press("ArrowRight")
        ax.keyboard.press("Enter")
        ax.wait_for_function("document.querySelector('#tracks .row:not(.row--skel)') && document.querySelector('#tracks .row').dataset.key.startsWith('sc:')")
        sc_ok = ax.evaluate(src_state) == [["yt", "false", -1], ["sc", "true", 0]]
        ax.keyboard.press("ArrowLeft")
        ax.keyboard.press("Enter")
        ax.wait_for_function("document.querySelector('#tracks .row:not(.row--skel)') && document.querySelector('#tracks .row').dataset.key.startsWith('yt:')")
        check(sc_ok and ax.evaluate(src_state) == [["yt", "true", 0], ["sc", "false", -1]],
              "источник: стрелки и Enter переключают YouTube ↔ SoundCloud, поиск перезапускается, в Tab — только выбранный")
        ax.set_viewport_size({"width": 1440, "height": 900})

        print("Доступность (S12): рамка фокуса видна и не обрезана (обход Tab)")

        def walk_check(label: str, steps: int, key: str = "Tab", start: str | None = None, kinds: int = 3) -> None:
            # конечные анимации появления (панель очереди въезжает справа 0.18 с) должны закончиться: иначе рамка первого
            # элемента ещё за краем окна (сбой CI Linux 39019158518); бесконечные (эквалайзер, скелетоны) не ждём
            ax.wait_for_function("document.getAnimations().every((a) => a.playState !== 'running' || a.effect.getTiming().iterations === Infinity)")
            if start:
                ax.focus(start)
            bad, n = tab_walk(ax, steps, key)
            check(not bad and n >= kinds, f"{label}: {n} видов элементов, рамка фокуса видна и не обрезана {bad}")

        for (w, h) in [(1440, 900), (980, 640)]:
            ax.set_viewport_size({"width": w, "height": h})
            ax.click(".nav__item[data-view=home]")
            ax.wait_for_selector("#view-home:not([hidden]) #new-grid .tcard")
            ax.wait_for_timeout(400)
            walk_check(f"{w}px главная (боковая панель, баннер, ряды карточек)", 90, start=".brand")
            ax_search()
            walk_check(f"{w}px поиск: вкладки, строки, ссылки исполнителей", 60, start="#search-input")
            walk_check(f"{w}px поиск: обратный обход Shift+Tab (верхняя панель не перекрывает фокус)", 50, "Shift+Tab")
            ax.click("#tabs [data-tab=artists]")
            ax.wait_for_selector("#cards .card--artist")
            ax.locator("#cards .card--artist").first.click()
            ax.wait_for_selector("#albums-row .card")
            walk_check(f"{w}px страница артиста: альбомы, похожие, треки", 50, start="#back")
            ax.click("#player-open")
            ax.wait_for_selector("#similar-row .tcard:not(.tcard--skel)")
            walk_check(f"{w}px страница трека: управление, тексты, похожие", 60, start="#view-now [data-do=back]")
            ax.click("[data-do=queue].rbtn")
            ax.wait_for_selector("#queue:not([hidden]) .q")
            walk_check(f"{w}px панель очереди", 16, start="#queue-clear")
            ax.click("#queue-close")
            ax.click("[data-do=expand]")
            ax.wait_for_selector("#fs:not([hidden])")
            walk_check(f"{w}px полноэкранный плеер", 16)
            ax.click("#fs-queue-btn")
            ax.wait_for_selector("#fs-q-list .fq__row")
            walk_check(f"{w}px полноэкранная очередь", 24, kinds=2)            # строки: кнопка трека и «⋯»
            ax.keyboard.press("Escape")
            ax.keyboard.press("Escape")
            ax.wait_for_selector("#fs", state="hidden")
            ax.click(".nav__item[data-view=settings]")
            ax.wait_for_selector("#view-settings:not([hidden])")
            walk_check(f"{w}px настройки", 16, start="#search-input")
            ax.click(".nav__item[data-view=collection]")
            ax.wait_for_selector("#col-tiles .tile")
            walk_check(f"{w}px коллекция", 16, start="#search-input")
        ax.set_viewport_size({"width": 900, "height": 640})            # компактная боковая панель (окно уже 960px)
        ax.click(".nav__item[data-view=home]")
        ax.wait_for_selector("#view-home:not([hidden]) #new-grid .tcard")
        compact = ax.evaluate("getComputedStyle(document.querySelector('.nav__label')).display") == "none"
        walk_check(f"900px компактная боковая панель ({'свёрнута' if compact else 'НЕ свёрнута'})", 18, start=".brand")
        ax.set_viewport_size({"width": 1440, "height": 900})
        ax_search()
        ax.focus(more3)
        ax.keyboard.press("Enter")
        menu_bad = []
        for _ in range(8):
            r = ax.evaluate(FOCUS_RING)
            if not r["ok"]:
                menu_bad.append(f"{r['why']} :: {r['who']}")
            ax.keyboard.press("ArrowDown")
        ax.keyboard.press("Escape")
        check(not menu_bad, f"меню трека: рамка фокуса каждого пункта видна {menu_bad}")
        ax.focus(more3)
        ax.keyboard.press("Enter")
        menu_to(ax, "playlist")
        ax.keyboard.press("Enter")
        ax.wait_for_selector("#dlg-pick[open] .pick__item")
        dlg_bad, dlg_out = [], []
        for _ in range(4):
            r = ax.evaluate(FOCUS_RING)
            if r["who"] != "body":                                   # после последней кнопки — окно браузера
                if not ax.evaluate("!!document.activeElement.closest('#dlg-pick')"):
                    dlg_out.append(r["who"])
                if not r["ok"]:
                    dlg_bad.append(f"{r['why']} :: {r['who']}")
            ax.keyboard.press("Tab")
        ax.keyboard.press("Escape")
        check(not dlg_bad and not dlg_out, f"диалог выбора плейлиста: фокус не выходит в приложение, рамка видна {dlg_bad} {dlg_out}")
        ax.evaluate("document.documentElement.dataset.theme = 'light'")
        ax.click(".nav__item[data-view=home]")
        ax.wait_for_selector("#view-home:not([hidden]) #new-grid .tcard")
        walk_check("светлая тема: главная", 40, start=".brand")
        ax_search()
        walk_check("светлая тема: поиск", 30, start="#search-input")
        ax.evaluate("document.documentElement.dataset.theme = 'dark'")
        check(ax.locator("[tabindex]:not([tabindex='-1']):not([tabindex='0'])").count() == 0, "положительных tabindex нет: порядок обхода — порядок разметки")

        print("Доступность (S12): основной сценарий только с клавиатуры")
        kb = ctx.new_page()
        kb.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
        kb.on("console", lambda m: errors.append(f"console.{m.type}: {m.text}") if m.type == "error" and "Failed to load resource" not in m.text else None)
        ax.close()
        clear_playback()
        kb.goto(BASE)
        kb.wait_for_function("document.querySelector('#hero').dataset.feed === 'ready'", timeout=15000)

        def tab_to(js_pred: str, key: str = "Tab", limit: int = 200) -> bool:
            for _ in range(limit):
                kb.keyboard.press(key)
                if kb.evaluate(js_pred):
                    return True
            return False

        kb.keyboard.press("/")
        kb.keyboard.type("gspd")
        kb.keyboard.press("Enter")
        kb.wait_for_function(rows_ready, arg=20)
        reached = tab_to("document.activeElement.matches('#tracks .row:nth-child(1) .track__play')")
        kb.keyboard.press("Enter")
        try:
            kb.wait_for_function("(() => { const a = document.querySelector('#audio'); return a.src.endsWith('/trk00000040') && !a.paused })()", timeout=10000)
            played = True
        except Exception:
            played = False
        check(reached and played, "клавиатура: «/», запрос, Enter, Tab до трека, Enter — трек играет")
        kb.keyboard.press("Tab")
        kb.keyboard.press("Enter")                                   # исполнитель первого трека
        kb.wait_for_function("document.querySelector('#d-kind').textContent === 'Артист'")
        on_title = kb.evaluate("document.activeElement.id")
        kb.keyboard.press("Shift+Tab")
        back_ok = kb.evaluate("document.activeElement.id") == "back"
        kb.keyboard.press("Enter")
        kb.wait_for_function(rows_ready, arg=20)
        check(on_title == "d-title" and back_ok and kb.evaluate("document.activeElement.id") == "results-title",
              "клавиатура: исполнитель → фокус на заголовке, Shift+Tab — «Назад», Enter — снова результаты, фокус на их заголовке")
        src_before = audio_state(kb)["src"]
        kb.keyboard.press("Space")
        paused_ok = poll(kb, "document.querySelector('#audio').paused", timeout=5)
        kb.keyboard.press("Space")
        resumed_ok = poll(kb, "!document.querySelector('#audio').paused", timeout=5)
        check(paused_ok and resumed_ok and audio_state(kb)["src"] == src_before,
              "клавиатура: пробел вне кнопок — пауза и продолжение того же трека")
        reached = tab_to("document.activeElement.id === 'player-open'")
        kb.keyboard.press("Enter")
        kb.wait_for_selector("#view-now:not([hidden])")
        reached = reached and tab_to("document.activeElement.dataset.do === 'expand'", "Shift+Tab", 30)
        kb.keyboard.press("Enter")
        kb.wait_for_selector("#fs:not([hidden])")
        kb.keyboard.press("Escape")
        kb.wait_for_selector("#fs", state="hidden")
        check(reached and kb.evaluate("document.activeElement.dataset.do") == "expand" and kb.get_attribute("body", "data-view") == "now",
              "клавиатура: мини-плеер → страница трека → полноэкранный режим → Esc, фокус вернулся")
        reached = tab_to("document.activeElement.matches('.nav__item[data-view=liked]')", "Shift+Tab")
        kb.keyboard.press("Enter")
        kb.wait_for_function("document.body.dataset.view === 'liked'")
        check(reached and kb.evaluate("document.activeElement.dataset.view") == "liked", "клавиатура: боковое меню — «Нравится»")

        print("Доступность (S12): контраст отрисованных элементов в обеих темах")
        # Тема переключается через настройки, как у пользователя; фон-картинки (баннер, страница трека) не измеряются
        list_sels = [".nav__item:not([aria-current])", ".nav__item[aria-current]", ".side__h", ".mix__title", ".mix__sub",
                     ".seg__btn[aria-selected=true]", ".seg__btn[aria-selected=false]", ".tab[aria-selected=true]", ".tab[aria-selected=false]",
                     ".results__title", "#status", ".row.is-playing .track__title", ".row:not(.is-playing) .track__title",
                     ".track__artist .who", ".track__album", ".track__time", ".track__num", ".track__tools .icon", ".sort",
                     ".mp__title", ".mp__artist .who", "#time-current", "#player [data-do=shuffle] .icon", ".profile__name", ".profile__sub",
                     ".more"]
        detail_sels = [".detail__kind", ".detail__title", ".detail__sub", "#d-play", "#d-shuffle", ".back", ".card__title", ".card__sub"]
        other_sels = {"settings": [".page-h", ".set h3", ".set__row", ".set__hint", "#set-dir"],
                      "collection": [".tile__title", ".tile__sub", ".tile__ico .icon"]}
        menu_sels = ["#menu [role=menuitem] span", "#menu [role=menuitem] .icon"]
        queue_sels = [".queue__titles h2", ".queue__label", ".q.is-current .q__title", ".q:not(.is-current) .q__title", ".q__artist", ".queue__clear"]
        for theme in ("dark", "light"):
            cx = ctx.new_page()
            cx.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
            kb.close() if theme == "dark" else None
            clear_playback()
            cx.goto(BASE)
            cx.wait_for_function("document.querySelector('#hero').dataset.feed === 'ready'", timeout=15000)
            cx.click(".nav__item[data-view=settings]")
            cx.select_option("#set-theme", theme)
            cx.wait_for_function("(t) => document.documentElement.dataset.theme === t", arg=theme)
            res = cx.evaluate(CONTRAST_JS, other_sels["settings"])
            cx.click(".nav__item[data-view=collection]")
            cx.wait_for_selector("#col-tiles .tile")
            res += cx.evaluate(CONTRAST_JS, other_sels["collection"])
            cx.fill("#search-input", "more12")
            cx.keyboard.press("Enter")
            cx.wait_for_function(rows_ready, arg=20)
            cx.locator("#tracks .row").first.click(position={"x": 260, "y": 20})
            cx.wait_for_function("!document.querySelector('#audio').paused")
            cx.evaluate("document.querySelector('#main').scrollTo({top: 1e6})")
            cx.wait_for_function("document.querySelector('#more').dataset.state === 'error'")
            cx.evaluate("document.querySelector('#main').scrollTo({top: 0})")
            res += cx.evaluate(CONTRAST_JS, list_sels)
            cx.click("#tracks .row:nth-child(2) .track__more")
            res += cx.evaluate(CONTRAST_JS, menu_sels)
            cx.keyboard.press("Escape")
            cx.click("#btn-queue")
            cx.wait_for_selector("#queue:not([hidden]) .q")
            res += cx.evaluate(CONTRAST_JS, queue_sels)
            cx.click("#queue-close")
            cx.click("#tracks .row:nth-child(3) .who.is-link")
            cx.wait_for_selector("#albums-row .card")
            res += cx.evaluate(CONTRAST_JS, detail_sels)
            low = [f"{r['sel']} {r['ratio']}<{r['need']}" for r in res if r.get("missing") or r["ratio"] < r["need"]]
            worst = min(res, key=lambda r: r["ratio"] - r["need"])
            check(not low, f"{theme}: контраст {len(res)} видов отрисованных текстов и значков не ниже WCAG AA; "
                           f"наименьший запас — {worst['sel']} {worst['ratio']}:1 {low}")
            cx.click(".nav__item[data-view=settings]")
            cx.select_option("#set-theme", "dark")
            cx.wait_for_function("document.documentElement.dataset.theme === 'dark'")
            cx.close()

        print("Доступность (S12): исправления по независимому аудиту")
        au = ctx.new_page()
        au.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
        au.on("console", lambda m: errors.append(f"console.{m.type}: {m.text}") if m.type == "error" and "Failed to load resource" not in m.text else None)
        clear_playback()
        au.goto(BASE)
        au.wait_for_function("document.querySelector('#hero').dataset.feed === 'ready'", timeout=15000)
        au_active = "() => { const a = document.activeElement; return {cls: a.className, idx: a.dataset.index ?? null, q: a.dataset.q ?? null," \
                    " row: a.closest('.q') ? a.closest('.q').querySelector('.q__title').textContent : null} }"

        def au_search(q: str, n: int) -> None:
            au.fill("#search-input", q)
            au.keyboard.press("Enter")
            au.wait_for_selector("#tabs:not([hidden])")
            if au.get_attribute("#tabs [data-tab=tracks]", "aria-selected") != "true":
                au.click("#tabs [data-tab=tracks]")
            au.wait_for_function(rows_ready, arg=n)

        # 1 (HIGH): уход с экрана во время подгрузки не оставляет «Загрузить ещё» нового экрана мёртвой
        RECS.update(paged=False, fail_more=False)
        au.focus("#home-more")
        au.keyboard.press("Enter")                                    # радио последнего: 3 новых трека
        au.wait_for_function("document.querySelector('#home-more').dataset.state === 'idle' || document.querySelector('#home-more').hidden")
        for _ in range(3):
            if au.is_hidden("#home-more"):
                break
            au.focus("#home-more")
            au.keyboard.press("Enter")                                # дальше новых нет — кнопка скрывается
            au.wait_for_function("document.querySelector('#home-more').dataset.state !== 'loading'")
        f = au.evaluate("() => { const a = document.activeElement, c = document.querySelectorAll('#new-grid .tcard');"
                        " return [a.className, a.closest('.tcard') === c[c.length - 1]] }")
        check(au.is_hidden("#home-more") and f == ["tcard__open", True],
              f"главная: кнопка скрылась без новых карточек — фокус на последней карточке ленты, не на body ({f})")
        au_search("dup12", 20)
        au.focus("#more")
        au.keyboard.press("Enter")
        au.wait_for_function("document.querySelector('#more').hidden")
        f = au.evaluate(active)
        check(f["cls"] == "track__play" and f["row"] == 20, f"список: вторая страница из одних повторов — фокус на последней строке ({f})")
        MORE["mode"] = "hold"
        au_search("more12", 20)
        au.evaluate(to_bottom)
        au.wait_for_function("document.querySelector('#more').dataset.state === 'loading'")
        au.evaluate("document.querySelector('#main').scrollTo({top: 0})")
        au.click("#tracks .row:nth-child(3) .who.is-link")              # уход посреди подгрузки
        au.wait_for_selector("#albums-row .card")
        art_more: list[str] = []
        on_art = lambda r: art_more.append(r.url) if "/tracks?" in r.url and "offset=20" in r.url else None   # noqa: E731
        au.on("request", on_art)
        au.evaluate(to_bottom)
        for _ in range(50):
            if art_more:
                break
            au.wait_for_timeout(100)
        au.remove_listener("request", on_art)
        for r in MORE["held"]:                                        # придержанный запрос уже отменён страницей
            try:
                r.abort()
            except Exception:
                pass
        MORE["held"].clear()
        MORE["mode"] = "fail"
        check(len(art_more) == 1, f"уход с экрана посреди подгрузки: на странице артиста следующая страница грузится ({len(art_more)})")

        # 3: длинные имена исполнителей — фокус не прокручивает строку имён, каждое имя видно
        au.set_viewport_size({"width": 1100, "height": 760})
        au_search("long3", 2)
        au.focus("#tracks .row:nth-child(1) .track__play")
        seen_w = []
        for _ in range(3):
            au.keyboard.press("Tab")
            seen_w.append(au.evaluate("""() => { const a = document.activeElement, c = a.parentElement, r = a.getBoundingClientRect(),
                q = c.getBoundingClientRect(); return [a.textContent.slice(0, 9), Math.round(Math.min(r.right, q.right) - Math.max(r.left, q.left)),
                c.scrollLeft, a.getAttribute('aria-label').startsWith('Исполнитель: ')] }"""))
        ring = au.evaluate(FOCUS_RING)
        au.keyboard.press("Tab")
        check([w[0] for w in seen_w] == ["Номер Оди", "Номер Два", "Номер Три"] and all(w[1] >= 8 and w[2] == 0 and w[3] for w in seen_w)
              and ring["ok"] and au.evaluate("document.querySelector('#tracks .row .track__artist').scrollLeft") == 0,
              f"три длинных имени: каждое — видимая ссылка со своим многоточием, фокус не сдвигает строку ({seen_w})")
        au.set_viewport_size({"width": 1440, "height": 900})

        # 4: сообщение «Нет подключения» — фокус при обходе вверх не уходит под выросшую верхнюю панель
        au_search("gspd", 20)
        au.locator("#tracks .row").first.click(position={"x": 260, "y": 20})
        au.wait_for_function("!document.querySelector('#audio').paused")
        au.set_viewport_size({"width": 1000, "height": 640})
        ctx.set_offline(True)
        au.wait_for_selector("#offline:not([hidden])")
        au.focus("#tracks .row:nth-child(20) .track__more")
        bad, n = tab_walk(au, 40, "Shift+Tab")
        top_h = au.evaluate("document.querySelector('.topbar').offsetHeight")
        ctx.set_offline(False)
        au.wait_for_selector("#offline", state="hidden")
        au.set_viewport_size({"width": 1440, "height": 900})
        check(not bad and top_h > 100, f"«Нет подключения» (верхняя панель {top_h}px): обход Shift+Tab, фокус не перекрыт {bad}")

        # 5: панель очереди — «Ниже», «Выше», «Убрать» с клавиатуры не теряют фокус
        au.focus("#btn-queue")
        au.keyboard.press("Enter")
        au.wait_for_selector("#queue:not([hidden]) .q")
        title_at = "(i) => document.querySelector(`#queue-list .q__main[data-index='${i}'] .q__title`).textContent"
        mover_i = 3
        mover_t = au.evaluate(title_at, mover_i)
        au.focus(f"#queue-list [data-q=down][data-index='{mover_i}']")
        au.keyboard.press("Enter")
        f_down = au.evaluate(au_active)
        au.keyboard.press("Tab")                                      # с «Ниже» на «Убрать» той же строки
        au.keyboard.press("Enter")
        f_rm = au.evaluate(au_active)
        next_t = au.evaluate(title_at, mover_i + 1)
        check(f_down == {"cls": "q__btn", "idx": str(mover_i + 1), "q": "down", "row": mover_t}
              and f_rm["idx"] == str(mover_i + 1) and f_rm["row"] == next_t and mover_t not in au.evaluate(q_titles),
              f"панель очереди: «Ниже» — фокус на «Ниже» того же трека, «Убрать» — на строке, вставшей на место ({f_down}, {f_rm})")
        au.keyboard.press("Escape")

        # 6: полноэкранный режим — пробел на исполнителе открывает его, а не ставит паузу
        au.click("#player-open")
        au.wait_for_selector("#view-now:not([hidden])")
        au.click("[data-do=expand]")
        au.wait_for_selector("#fs:not([hidden])")
        au.focus("#fs-artist .who.is-link")
        au.keyboard.press("Space")
        au.wait_for_function("document.querySelector('#d-kind').textContent === 'Артист'")
        check(au.is_hidden("#fs") and not audio_state(au)["paused"], "полноэкранный режим: пробел на исполнителе — его страница, музыка играет")

        # n1: Esc в меню полноэкранной очереди закрывает только меню
        au.click("#player-open")
        au.wait_for_selector("#view-now:not([hidden])")
        au.click("[data-do=expand]")
        au.wait_for_selector("#fs:not([hidden])")
        au.click("#fs-queue-btn")
        au.wait_for_selector("#fs-q-list .fq__more")
        au.focus("#fs-q-list .fq__more")
        au.keyboard.press("Enter")
        au.keyboard.press("Escape")
        check(au.is_hidden("#menu") and au.get_attribute("#fs", "data-mode") == "queue" and au.is_visible("#fs")
              and au.evaluate("document.activeElement.classList.contains('fq__more')"),
              "меню полноэкранной очереди: Esc закрывает только меню (панель и режим на месте), фокус на «⋯»")
        au.keyboard.press("Escape")
        au.keyboard.press("Escape")
        au.wait_for_selector("#fs", state="hidden")

        # 8: поле ввода в диалоге — обычная рамка фокуса
        au.click("[data-do=back]")
        au.wait_for_selector("#player:not([hidden])")
        au.wait_for_function(rows_ready, arg=20)                      # снова страница артиста
        au.focus("#tracks .row:nth-child(2) .track__more")
        au.keyboard.press("Enter")
        menu_to(au, "playlist")
        au.keyboard.press("Enter")
        au.wait_for_selector("#dlg-pick[open] [data-pl=new]")
        au.focus("#dlg-pick [data-pl=new]")
        au.keyboard.press("Enter")
        au.wait_for_selector("#dlg[open]")
        ring = au.evaluate(FOCUS_RING)
        au.keyboard.press("Escape")
        check(ring["ok"] and "dlg-input" in ring["who"], f"диалог: поле ввода в фокусе с видимой рамкой ({ring})")

        print("Доступность (S12): исправления по независимому аудиту, раунд 2")
        # A: подгрузка ленты идёт, пользователь открыл страницу трека и вернулся — второй запрос не уходит, ответ первого
        # дописывает карточки один раз
        au.click(".brand")
        au.wait_for_selector("#view-home:not([hidden]) #new-grid .tcard")
        au.wait_for_function("document.querySelector('#hero').dataset.feed === 'ready'")
        RECS_HELD.clear()
        RECS.update(paged=True, hold=True)
        au.evaluate(to_bottom)
        au.wait_for_function("document.querySelector('#home-more').dataset.state === 'loading'")
        for _ in range(50):
            if RECS_HELD:
                break
            au.wait_for_timeout(100)
        held_first = len(RECS_HELD)
        au.evaluate("document.querySelector('#main').scrollTo({top: 0})")
        au.click(".hero [data-do=now]")                               # на главной мини-плеера нет: страница трека — из баннера
        au.wait_for_selector("#view-now:not([hidden])")
        au.click("[data-do=back]")
        au.wait_for_selector("#view-home:not([hidden]) #new-grid .tcard")
        au.evaluate(to_bottom)
        au.wait_for_timeout(800)
        held_after, st = len(RECS_HELD), au.evaluate("document.querySelector('#home-more').dataset.state")
        n_cards = au.locator("#new-grid .tcard").count()
        recs_release()
        try:                                                          # из 24 треков страницы новые — те, которых в ленте нет
            au.wait_for_function("(n) => document.querySelectorAll('#new-grid .tcard').length > n", arg=n_cards, timeout=10000)
            grown = True
        except Exception:
            grown = False
        au.wait_for_timeout(600)
        held_end = len(RECS_HELD)
        titles = au.evaluate("[...document.querySelectorAll('#new-grid .tcard .tcard__title')].map((n) => n.textContent)")
        fresh = 24 - len({1 + 24 + i for i in range(24)} & {20 + i for i in range(14)})   # страница 1 против радио трека
        check(held_first == 1 and held_after == 1 and held_end == 0 and st == "loading" and grown
              and len(titles) == len(set(titles)) == n_cards + fresh,
              f"главная: возврат на главную во время подгрузки — без второго запроса, карточки дописаны один раз "
              f"(запросов {held_first}→{held_after}, состояние {st}, карточек {n_cards}→{len(titles)}, ожидалось +{fresh}, разных {len(set(titles))})")
        # подгрузка главной идёт, пользователь ушёл в поиск — «Загрузить ещё» поиска работает сразу
        au.evaluate(to_bottom)
        for _ in range(50):
            if RECS_HELD:
                break
            au.wait_for_timeout(100)
        held_home = len(RECS_HELD)
        MORE["mode"] = "hold"
        au_search("more12", 20)
        srch_more: list[str] = []
        on_srch = lambda r: srch_more.append(r.url) if "q=more12" in r.url and "offset=20" in r.url else None   # noqa: E731
        au.on("request", on_srch)
        au.evaluate(to_bottom)
        for _ in range(50):
            if srch_more:
                break
            au.wait_for_timeout(100)
        au.remove_listener("request", on_srch)
        more_release()
        MORE["mode"] = "fail"
        recs_release()                                                # ответ ленты, которую уже не показывают
        RECS.update(paged=False, hold=False)
        check(held_home == 1 and len(srch_more) == 1,
              f"подгрузка ленты главной не мешает подгрузке поиска, открытого посреди неё ({held_home}, {len(srch_more)})")

        # B: мышью — фокус после действия не переносится, пробел по-прежнему ставит паузу
        au.evaluate("document.querySelector('#main').scrollTo({top: 0})")
        au_search("gspd", 20)
        au.locator("#tracks .row").nth(1).click(position={"x": 260, "y": 20})
        playing = "(id) => { const a = document.querySelector('#audio'); return a.src.endsWith('/' + id) && !a.paused }"
        au.wait_for_function(playing, arg="trk00000041")
        au.click("#btn-queue")
        au.wait_for_selector("#queue:not([hidden]) .q")
        q_n = au.evaluate(q_count)
        au.click("#queue-list [data-q=rm][data-index='3']")
        au.wait_for_timeout(150)                                      # «исправление фокуса» в следующем кадре
        fb = au.evaluate(au_active)
        au.keyboard.press("Space")
        au.wait_for_timeout(200)
        paused, q_n2 = audio_state(au)["paused"], au.evaluate(q_count)
        check(paused and q_n2 == q_n - 1 and fb["q"] is None,
              f"мышь: «Убрать» в панели очереди убирает один трек, фокус не переходит на соседнюю кнопку, пробел — пауза "
              f"({q_n}→{q_n2}, фокус {fb}, пауза {paused})")
        au.keyboard.press("Space")
        au.wait_for_function(playing, arg="trk00000041")
        au.click("#queue-close")
        fc = au.evaluate("document.activeElement === document.body")
        au.keyboard.press("Space")
        au.wait_for_timeout(200)
        paused = audio_state(au)["paused"]
        check(fc and paused and au.is_hidden("#queue"), f"мышь: «Закрыть» панели очереди — фокус не остаётся в скрытой панели, пробел — пауза ({fc}, {paused})")
        au.keyboard.press("Space")
        au.wait_for_function(playing, arg="trk00000041")
        au.click("#tracks .row:nth-child(6) .track__more")
        au.click("#menu [data-act=add]")
        # фокус не на «⋯» и не в скрытом меню (Chromium на Windows снимает его со скрытого пункта не сразу — CI S12)
        fm = au.evaluate("(() => { const a = document.activeElement; return a === document.body ? 'body' : `${a.tagName}.${a.className}${a.closest('#menu') ? ' в меню' : ''}` })()")
        au.keyboard.press("Space")
        au.wait_for_timeout(200)
        paused = audio_state(au)["paused"]
        check(paused and au.is_hidden("#menu") and fm == "body",
              f"мышь: пункт меню «Добавить в очередь» — фокус не возвращается на «⋯», пробел ставит паузу, меню не открывается ({fm})")
        au.keyboard.press("Space")
        au.wait_for_function(playing, arg="trk00000041")
        # «Загрузить ещё» мышью: страниц больше нет, кнопка скрылась — фокус не переносится на строку, пробел — пауза
        MORE["mode"] = "fail"                                         # автоподгрузка обрывается: остаётся кнопка повтора
        au_search("more12", 20)
        au.evaluate(to_bottom)
        au.wait_for_function("document.querySelector('#more').dataset.state === 'error'")
        MORE["mode"] = "ok"
        au.click("#more")
        au.wait_for_function(rows_ready, arg=40)
        au.wait_for_function("document.querySelector('#more').hidden")
        fl = au.evaluate("document.activeElement.className")
        au.keyboard.press("Space")
        au.wait_for_timeout(200)
        paused = audio_state(au)["paused"]
        MORE["mode"] = "fail"
        check(paused and fl != "track__play", f"мышь: «Загрузить ещё» скрылась — фокус не на строке трека, пробел ставит паузу ({fl!r})")
        au.keyboard.press("Space")
        au.wait_for_function(playing, arg="trk00000041")
        au_search("gspd", 20)
        # то же синхронно, до пересчёта стилей: Chromium на Linux снимает фокус со скрытого элемента при ближайшем
        # пересчёте, на Windows (CI) — позже; проверка в том же задании показывает, снимает ли его само приложение
        mouse_click_sync = """(sel) => { const b = document.querySelector(sel); b.focus();
            b.dispatchEvent(new PointerEvent('pointerdown', {bubbles: true})); b.click();
            const a = document.activeElement; return a === document.body ? 'body' : (a.dataset.act || a.id || a.className) }"""
        au.click("#tracks .row:nth-child(6) .track__more")
        s_menu = au.evaluate(mouse_click_sync, "#menu [data-act=add]")
        au.click("#btn-queue")
        au.wait_for_selector("#queue:not([hidden]) .q")
        s_queue = au.evaluate(mouse_click_sync, "#queue-close")
        check(s_menu == "body" and s_queue == "body" and au.is_hidden("#menu") and au.is_hidden("#queue"),
              f"мышь: фокус снимается со скрытого пункта меню и закрытой панели сразу, в том же задании ({s_menu}, {s_queue})")
        # с клавиатуры фокус по-прежнему возвращается на «⋯»
        au.focus("#tracks .row:nth-child(6) .track__more")
        au.keyboard.press("Enter")
        menu_to(au, "add")
        au.keyboard.press("Enter")
        check(au.evaluate("document.activeElement === document.querySelector('#tracks .row:nth-child(6) .track__more')"),
              "клавиатура: после пункта меню фокус снова на «⋯» той же строки")

        # c: Enter на треке в панели очереди — после перерисовки (асинхронно, по событию плеера) фокус на этом треке
        au.focus("#btn-queue")
        au.keyboard.press("Enter")
        au.wait_for_selector("#queue:not([hidden]) .q")
        target = au.evaluate("document.querySelector(`#queue-list .q__main[data-index='4'] .q__title`).textContent")
        au.focus("#queue-list .q__main[data-index='4']")
        au.keyboard.press("Enter")
        au.wait_for_function("(t) => document.querySelector('#queue-list .q.is-current .q__title')?.textContent === t", arg=target)
        au.wait_for_timeout(300)
        fq = au.evaluate("() => { const a = document.activeElement; return [a.className, a.closest('.q')?.classList.contains('is-current'),"
                         " a.closest('.q')?.querySelector('.q__title').textContent] }")
        check(fq == ["q__main", True, target], f"панель очереди: Enter на треке — он играет, фокус на его строке после перерисовки ({fq})")
        au.keyboard.press("Escape")

        # C: на странице трека разделитель имён — обычный текст (переносится с именем, не в начало строки)
        au_search("feat", 2)
        au.locator("#tracks .row").first.click(position={"x": 260, "y": 20})
        au.click("#player-open")
        au.wait_for_selector("#view-now:not([hidden]) #now-artist .who__sep")
        ws = au.evaluate("[getComputedStyle(document.querySelector('#now-artist .who__sep')).whiteSpace,"
                         " getComputedStyle(document.querySelector('#player-artist .who__sep')).whiteSpace]")
        check(ws[0] != "pre" and ws[1] == "pre", f"разделитель имён: на странице трека — обычный текст, в мини-плеере — flex-элемент ({ws})")
        au.click("[data-do=back]")

        # D: шесть исполнителей — ни одно видимое имя не сжимается до нескольких пикселей (строка списка, мини-плеер)
        au.set_viewport_size({"width": 980, "height": 760})
        au_search("many6", 1)
        au.locator("#tracks .row").first.click(position={"x": 200, "y": 20})
        au.wait_for_selector("#player-artist .who.is-link")
        widths = au.evaluate("""() => [...document.querySelectorAll('#tracks .row .track__artist, #player-artist')].map((c) => {
            const q = c.getBoundingClientRect();
            return [...c.querySelectorAll('.who')].map((w) => { const r = w.getBoundingClientRect();
              return Math.round(Math.max(0, Math.min(r.right, q.right) - Math.max(r.left, q.left))) }) })""")
        more6 = au.evaluate("[document.querySelector('#player-artist .who__more')?.textContent, document.querySelector('#player-artist .who__more')?.title]")
        check(len(widths) == 2 and len(widths[0]) == 6 and len(widths[1]) == 2 and all(w >= 16 for line in widths for w in line)
              and more6[0] == "+4" and more6[1].startswith("Ещё: Третий Исполнитель"),
              f"шесть исполнителей при ширине 980: в строке списка все имена шире 16px, в мини-плеере два имени и «+4» ({widths}, {more6})")
        au.set_viewport_size({"width": 1440, "height": 900})

        # G: двухцветная рамка карточки микса — тёмная линия внутри и в светлой теме
        au.click(".brand")
        au.wait_for_selector("#view-home:not([hidden]) #mix-row .mixcard__open")
        theme0 = au.evaluate("document.documentElement.dataset.theme || ''")
        au.evaluate("document.documentElement.dataset.theme = 'light'")
        au.focus("#mix-row .mixcard__open")
        au.keyboard.press("Shift+Tab")
        au.keyboard.press("Tab")
        ring = au.evaluate(FOCUS_RING)
        au.evaluate("(t) => { document.documentElement.dataset.theme = t }", theme0)
        check(ring["ok"] and "mixcard__open" in ring["who"], f"светлая тема: рамка карточки микса с тёмной линией внутри ({ring})")
        au.close()

        browser.close()

    print(f"\nПроверок: {checks}, провалено: {len(failures)}")
    real = [e for e in errors]
    print("Ошибки консоли/страницы:", real if real else "нет")
    return 1 if failures or real else 0


if __name__ == "__main__":
    sys.exit(main())