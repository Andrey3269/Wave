"""
S16: воспроизводимые замеры производительности Wave на одноразовом профиле. Это НЕ тест CI: абсолютные тайминги
зависят от машины, поэтому скрипт печатает и сохраняет числа, а сравнивать их можно только между прогонами на
одной машине в одних условиях (например, базовая линия и ветка подряд).

    python tests/perf/measure.py [--root ПАПКА_РЕПОЗИТОРИЯ] [--repeat 5] [--favorites 5000] [--playlist 2000]
                                 [--history 10000] [--json ИТОГ.json] [--no-browser]

--root позволяет измерить другую копию репозитория (например, `git worktree` базовой линии) тем же скриптом.

Что измеряется (каждый замер — медиана по --repeat повторам, если не сказано иное):
  backend.cold_empty   — запуск uvicorn с ПУСТЫМ профилем до первого ответа 200 на /api/health, мс; RSS процесса, МБ;
  backend.cold_large   — то же с БОЛЬШИМ профилем (избранное, плейлист, история);
  api.*                — время ответа /api/favorites, /api/playlists/{id}, /api/history (offset 0 и 1000), мс;
  ui.boot              — Chromium (Playwright): от начала навигации до готовой главной (#hero[data-feed=ready]),
                         мс; размер кучи JS после сборки мусора, МБ; число DOM-узлов;
  ui.liked / ui.playlist / ui.history / ui.search
                       — от нажатия до показа списка: в #tracks есть min(N, 200) строк и прошли два кадра, мс;
                         куча JS и DOM-узлы после открытия;
  ui.liked_scroll      — прокрутка «Нравится» до конца: мс до отрисовки всех N строк, DOM-узлы в конце;
  backend.rss_after    — RSS backend после всех сценариев, МБ.

Интернет не нужен: внешние ответы (рекомендации, поиск, обложки) подменяются в браузере, как в e2e; локальные API
(избранное, плейлисты, история) работают на настоящем backend и SQLite временного профиля. Реальный профиль
пользователя не затрагивается: каталоги создаются во временной папке с меткой .wave-test-profile и удаляются.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import socket
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve()
MARKER = ".wave-test-profile"
SCRUB_ENV = ("YTDLP_COOKIES", "YTDLP_PROXY", "WAVE_DATA_DIR", "WAVE_DOWNLOADS_DIR")
PNG_1PX = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000"
                        "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082")
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


# ---------------------------------------------------------------------------
# Процесс backend
# ---------------------------------------------------------------------------
def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def rss_mb(pid: int) -> float | None:
    """Resident set size процесса, МБ: /proc на Linux, GetProcessMemoryInfo на Windows. Иначе None."""
    if sys.platform.startswith("linux"):
        try:
            for line in Path(f"/proc/{pid}/status").read_text().splitlines():
                if line.startswith("VmRSS:"):
                    return round(int(line.split()[1]) / 1024, 1)
        except OSError:
            return None
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class PMC(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)    # явные типы: HANDLE на x64 — 64 бита
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        k32.K32GetProcessMemoryInfo.restype = wintypes.BOOL
        k32.K32GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
        h = k32.OpenProcess(0x0410, False, pid)
        if not h:
            return None
        try:
            pmc = PMC(cb=ctypes.sizeof(PMC))
            if k32.K32GetProcessMemoryInfo(h, ctypes.byref(pmc), pmc.cb):
                return round(pmc.WorkingSetSize / 1048576, 1)
        finally:
            k32.CloseHandle(h)
    return None


class Backend:
    def __init__(self, root: Path, data: Path, downloads: Path, log: Path):
        env = {k: v for k, v in os.environ.items() if k not in SCRUB_ENV}
        env.update(WAVE_DATA_DIR=str(data), WAVE_DOWNLOADS_DIR=str(downloads), PYTHONDONTWRITEBYTECODE="1",
                   PYTHONIOENCODING="utf-8")
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        cmd = [sys.executable, "-m", "uvicorn", "main:app", "--app-dir", str(root / "backend"),
               "--host", "127.0.0.1", "--port", str(self.port), "--log-level", "warning"]
        self.t0 = time.perf_counter()
        with open(log, "a", encoding="utf-8") as fh:
            self.proc = subprocess.Popen(cmd, cwd=root, env=env, stdout=fh, stderr=subprocess.STDOUT)

    def wait_ready(self, timeout: float = 60.0) -> float:
        """Мс от запуска процесса до первого 200 на /api/health (опрос каждые 20 мс)."""
        deadline = time.perf_counter() + timeout
        while time.perf_counter() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError("backend завершился при запуске")
            try:
                with OPENER.open(f"{self.url}/api/health", timeout=2) as r:
                    if r.status == 200:
                        return (time.perf_counter() - self.t0) * 1000
            except OSError:
                pass
            time.sleep(0.02)
        raise RuntimeError("backend не ответил на /api/health")

    def rss(self) -> float | None:
        return rss_mb(self.proc.pid)

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(5)


def make_profile(root: Path) -> tuple[Path, Path]:
    data, downloads = root / "data", root / "downloads"
    for d in (data, downloads):
        d.mkdir(parents=True)
        (d / MARKER).write_text("Каталог создан tests/perf/measure.py. Можно удалить.\n", encoding="utf-8")
    return data, downloads


# ---------------------------------------------------------------------------
# Данные профиля
# ---------------------------------------------------------------------------
def track(i: int, prefix: str = "p") -> dict:
    vid = f"{prefix}{i:010d}"[-11:]
    return {"id": vid, "source": "yt", "title": f"Трек номер {i} — длинное название для проверки",
            "artist": f"Исполнитель {i % 97}", "artists": [f"Исполнитель {i % 97}"], "album": f"Альбом {i % 211}",
            "duration": 120 + i % 240, "cover": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg", "explicit": i % 13 == 0,
            "stream_url": f"/api/stream/yt/{vid}", "artist_id": f"UC{i % 97:022d}", "artist_ids": [f"UC{i % 97:022d}"]}


def post_json(url: str, body: dict, timeout: float = 120) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body, ensure_ascii=False).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    with OPENER.open(req, timeout=timeout) as r:
        return json.loads(r.read())


def seed(backend: Backend, data_dir: Path, favorites: int, playlist: int, history: int) -> None:
    """Избранное и плейлист — через POST /api/library/import (настоящий контракт); история — прямой вставкой
    в базу временного профиля в том же формате, что пишет POST /api/history (импорт историю не переносит)."""
    now = time.time()
    doc = {"format": "wave-library", "version": 1,
           "favorites": {"tracks": [{"added_at": now - i, "track": track(i)} for i in range(favorites)]},
           "playlists": [{"name": "perf", "created_at": now, "updated_at": now,
                          "tracks": [{"added_at": now - i, "track": track(i, "q")} for i in range(playlist)]}],
           "settings": {}}
    post_json(f"{backend.url}/api/library/import", doc)
    backend.stop()
    db = sqlite3.connect(data_dir / "wave.db")
    distinct = max(1, history // 4)
    rows = [(f"yt:{track(i % distinct, 'h')['id']}", json.dumps(track(i % distinct, "h"), ensure_ascii=False),
             now - history + i) for i in range(history)]
    with db:
        db.executemany("INSERT INTO history (key, data, played_at) VALUES (?, ?, ?)", rows)
    db.close()


def api_ms(url: str, n: int = 10) -> float:
    times = []
    for _ in range(n):
        t0 = time.perf_counter()
        with OPENER.open(url, timeout=60) as r:
            r.read()
        times.append((time.perf_counter() - t0) * 1000)
    return round(statistics.median(times), 1)


# ---------------------------------------------------------------------------
# Браузер
# ---------------------------------------------------------------------------
def mock_track(i: int) -> dict:
    t = track(i, "m")
    return t


def route_handler(route, request):
    url = request.url
    m = re.match(r"^http://127\.0\.0\.1:\d+(/[^?]*)(?:\?(.*))?$", url)
    if not m:                                              # внешний адрес: обложки — 1×1 PNG, остальное не нужно
        if re.search(r"\.(jpe?g|png|webp)(\?|$)", url) or "ytimg" in url or "googleusercontent" in url:
            return route.fulfill(status=200, body=PNG_1PX, headers={"Content-Type": "image/png"})
        return route.abort()
    path = m.group(1)
    if path == "/api/recommendations":
        body = {"tracks": [mock_track(i) for i in range(30)], "personalized": False, "radio": False, "page": 0,
                "failed_sources": [], "source_status": {"yt": "ok"}}
        return route.fulfill(status=200, content_type="application/json", body=json.dumps(body, ensure_ascii=False))
    if path == "/api/search":
        q = dict(p.split("=", 1) for p in (m.group(2) or "").split("&") if "=" in p)
        off = int(q.get("offset", 0))
        body = {"query": "perf", "count": 20, "tracks": [mock_track(1000 + off + i) for i in range(20)],
                "failed_sources": [], "has_more": off + 20 < 100}
        return route.fulfill(status=200, content_type="application/json", body=json.dumps(body, ensure_ascii=False))
    if path.startswith("/api/search/") or path.startswith("/api/lyrics") or path.startswith("/api/artist/"):
        return route.fulfill(status=200, content_type="application/json", body='{"items": [], "has_more": false}')
    if path.startswith("/api/stream/"):
        return route.fulfill(status=204)
    return route.continue_()


# Сколько строк должно появиться, чтобы список считался показанным. До S16 все строки рисовались одним заходом
# (первая строка появлялась вместе с последней), с S16 — порциями по 200 (frontend/js/views/list.js, RENDER_CHUNK).
# Порог 200 одинаково честен для обеих версий: это момент, когда пользователь видит заполненный список.
FIRST_SCREEN = 200
# Открывает экран действием `act` (строка JS) и ждёт, пока в #tracks станет `rows` строк, затем два кадра
TIME_ROWS = """async ([act, rows]) => {
  const t0 = performance.now();
  (new Function(act))();
  await new Promise((ok, fail) => {
    const stop = Date.now() + 60000;
    const tick = () => {
      if (document.querySelectorAll('#tracks .row').length >= rows) return ok();
      if (Date.now() > stop) return fail(new Error('rows: ' + document.querySelectorAll('#tracks .row').length));
      requestAnimationFrame(tick);
    };
    tick();
  });
  await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
  return performance.now() - t0;
}"""
# Прокручивает #main до конца, пока в #tracks не станет `rows` строк (до S16 они уже все есть)
SCROLL_ALL = """async (rows) => {
  const main = document.querySelector('#main'), t0 = performance.now(), stop = Date.now() + 120000;
  while (document.querySelectorAll('#tracks .row').length < rows) {
    if (Date.now() > stop) throw new Error('scroll rows: ' + document.querySelectorAll('#tracks .row').length);
    main.scrollTop = main.scrollHeight;
    await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
  }
  const ms = performance.now() - t0;
  main.scrollTop = 0;
  return ms;
}"""
SNAPSHOT = """() => { if (window.gc) window.gc();
  return { heap_mb: performance.memory ? +(performance.memory.usedJSHeapSize / 1048576).toFixed(1) : null,
           dom_nodes: document.getElementsByTagName('*').length }; }"""
CLICK = "document.querySelector('.nav__item[data-view={v}]').click()"


def ui_measure(url: str, n_fav: int, n_pl: int, repeat: int) -> dict:
    from playwright.sync_api import sync_playwright

    out: dict[str, list] = {}

    def add(name, ms, snap):
        out.setdefault(name, []).append({"ms": round(ms, 1), **snap})

    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--js-flags=--expose-gc", "--enable-precise-memory-info"])
        for _ in range(repeat):
            ctx = browser.new_context(viewport={"width": 1440, "height": 900}, locale="ru-RU")
            ctx.route("**/*", route_handler)
            page = ctx.new_page()
            page.goto(url + "/")
            page.wait_for_function("document.querySelector('#hero').dataset.feed === 'ready'", timeout=60000)
            boot = page.evaluate("performance.now()")
            page.wait_for_timeout(300)
            add("boot", boot, page.evaluate(SNAPSHOT))

            add("liked", page.evaluate(TIME_ROWS, [CLICK.format(v="liked"), min(n_fav, FIRST_SCREEN)]), page.evaluate(SNAPSHOT))
            page.wait_for_timeout(200)
            add("liked_scroll", page.evaluate(SCROLL_ALL, n_fav), page.evaluate(SNAPSHOT))
            page.wait_for_timeout(200)
            add("history", page.evaluate(TIME_ROWS, [CLICK.format(v="history"), 20]), page.evaluate(SNAPSHOT))
            page.wait_for_timeout(200)
            page.evaluate(CLICK.format(v="playlists"))
            page.wait_for_selector("#cards .card--playlist", timeout=15000)
            open_pl = ("[...document.querySelectorAll('#cards .card--playlist')]"
                       ".find(x=>x.textContent.includes('perf')).click()")
            add("playlist", page.evaluate(TIME_ROWS, [open_pl, min(n_pl, FIRST_SCREEN)]), page.evaluate(SNAPSHOT))
            page.wait_for_timeout(200)
            search = ("const i=document.querySelector('#search-input'); i.value='perf';"
                      "document.querySelector('#search-form').requestSubmit()")
            try:
                add("search", page.evaluate(TIME_ROWS, [search, 20]), page.evaluate(SNAPSHOT))
            except Exception as exc:                                  # noqa: BLE001 — замер, не тест
                out.setdefault("search_error", []).append(str(exc)[:200])
            ctx.close()
        browser.close()
    result = {}
    for k, v in out.items():
        if k.endswith("_error"):
            result[k] = v[0]
            continue
        result[k] = {f: round(statistics.median([x[f] for x in v if x[f] is not None]), 1)
                     for f in v[0] if any(x[f] is not None for x in v)}
    return result


# ---------------------------------------------------------------------------
def med(values: list[float | None]) -> float | None:
    vals = [v for v in values if v is not None]
    return round(statistics.median(vals), 1) if vals else None


def main() -> int:
    ap = argparse.ArgumentParser(description="Замеры производительности Wave (S16), не тест CI")
    ap.add_argument("--root", type=Path, default=HERE.parents[2])
    ap.add_argument("--repeat", type=int, default=5)
    ap.add_argument("--favorites", type=int, default=5000)
    ap.add_argument("--playlist", type=int, default=2000)
    ap.add_argument("--history", type=int, default=10000)
    ap.add_argument("--json", type=Path)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()
    root = args.root.resolve()

    tmp = Path(tempfile.mkdtemp(prefix="wave-perf-"))
    log = tmp / "backend.log"
    res: dict = {"env": {"python": platform.python_version(), "os": platform.platform(terse=True),
                         "machine": platform.machine(), "cpus": os.cpu_count(), "root": root.name,
                         "head": subprocess.run(["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
                                                capture_output=True, text=True).stdout.strip(),
                         "repeat": args.repeat, "favorites": args.favorites, "playlist": args.playlist,
                         "history": args.history}}
    try:
        # 1. Холодный старт с пустым профилем: каждый повтор — новый профиль
        ready, rss = [], []
        for i in range(args.repeat):
            data, dl = make_profile(tmp / f"empty{i}")
            b = Backend(root, data, dl, log)
            try:
                ready.append(b.wait_ready())
                time.sleep(0.3)
                rss.append(b.rss())
            finally:
                b.stop()
        res["backend.cold_empty"] = {"ready_ms": med(ready), "rss_mb": med(rss), "runs_ms": [round(x) for x in ready]}

        # 2. Большой профиль
        data, dl = make_profile(tmp / "large")
        b = Backend(root, data, dl, log)
        b.wait_ready()
        seed(b, data, args.favorites, args.playlist, args.history)
        ready, rss = [], []
        for _ in range(args.repeat):
            b = Backend(root, data, dl, log)
            try:
                ready.append(b.wait_ready())
                time.sleep(0.3)
                rss.append(b.rss())
            finally:
                b.stop()
        res["backend.cold_large"] = {"ready_ms": med(ready), "rss_mb": med(rss), "runs_ms": [round(x) for x in ready]}

        # 3. API и браузер на большом профиле, один процесс backend
        b = Backend(root, data, dl, log)
        try:
            b.wait_ready()
            with OPENER.open(f"{b.url}/api/playlists") as r:
                pl_id = json.loads(r.read())["items"][0]["id"]
            res["api"] = {"favorites_ms": api_ms(f"{b.url}/api/favorites"),
                          "playlist_ms": api_ms(f"{b.url}/api/playlists/{pl_id}"),
                          "history_p0_ms": api_ms(f"{b.url}/api/history?limit=20&offset=0"),
                          "history_p1000_ms": api_ms(f"{b.url}/api/history?limit=20&offset=1000")}
            res["backend.rss_before_ui_mb"] = b.rss()
            if not args.no_browser:
                ui = ui_measure(b.url, args.favorites, args.playlist, args.repeat)
                res.update({f"ui.{k}": v for k, v in ui.items()})
            res["backend.rss_after_mb"] = b.rss()
        finally:
            b.stop()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    text = json.dumps(res, ensure_ascii=False, indent=2)
    print(text)
    if args.json:
        args.json.write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
