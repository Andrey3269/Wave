"""
Снимки экранов интерфейса для сравнения «до» и «после» рефакторинга (S9: внешний вид не должен меняться).

    python tests/ui_snapshots.py capture ПАПКА [--root КОРЕНЬ_РЕПОЗИТОРИЯ]
    python tests/ui_snapshots.py compare ПАПКА_А ПАПКА_Б

capture — для каждой ширины окна (980, 1366, 1440, 1920) поднимает backend на новом временном профиле
          (как tests/run_e2e.py), подменяет интернет теми же ответами, что и e2e (tests/e2e_smoke.py: handle),
          проходит одинаковый сценарий по экранам и сохраняет PNG. Анимации на снимке выключены, пути
          временного профиля на экране настроек закрыты маской. --root — снять другую копию репозитория
          (например, `git worktree` исходного коммита), сценарий и подмена сети берутся из этого файла.
compare — попиксельно сравнивает одноимённые PNG двух папок в Chromium (canvas), без сторонних библиотек.
          Для различий выводит число пикселей, наибольшее отклонение канала и рамку области. --tolerance N —
          не считать отклонения канала до N (два снятия одного и того же коммита различаются сглаживанием
          текста до ±2, у буквы аватара — до ±20). Код выхода 0 — совпадают, 1 — различия или нет пары.

Это инструмент ручной проверки (в tests/run_checks.py не входит): совпадение снимков в одной среде доказывает,
что вёрстка не изменилась, но не заменяет проверку в WebView2 на Windows.
"""
from __future__ import annotations

import argparse
import base64
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

WIDTHS = [(980, 640), (1366, 768), (1440, 900), (1920, 1080)]
MARKER = ".wave-test-profile"
SCRUB_ENV = ("YTDLP_COOKIES", "YTDLP_PROXY", "WAVE_DATA_DIR", "WAVE_DOWNLOADS_DIR")


def _start_backend(root: Path, profile: Path):
    from run_e2e import free_port, wait_health   # noqa: E402  (тот же каталог tests/)
    data, downloads = profile / "data", profile / "downloads"
    for d in (data, downloads):
        d.mkdir(parents=True)
        (d / MARKER).write_text("Каталог создан tests/ui_snapshots.py. Можно удалить.\n", encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k not in SCRUB_ENV}
    env.update(WAVE_DATA_DIR=str(data), WAVE_DOWNLOADS_DIR=str(downloads), PYTHONDONTWRITEBYTECODE="1",
               PYTHONIOENCODING="utf-8")
    port = free_port()
    log = open(profile / "backend.log", "a", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "main:app", "--app-dir", str(root / "backend"),
                             "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
                            cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT)
    url = f"http://127.0.0.1:{port}"
    if not wait_health(url, proc, 60):
        proc.kill()
        raise SystemExit(f"backend не запустился, журнал: {profile / 'backend.log'}")
    return url, proc, log


def _settle(page, ms: int = 450) -> None:
    """Ждём, пока дорисуются карточки и загрузятся видимые картинки."""
    page.wait_for_timeout(ms)
    page.wait_for_function("[...document.images].every(i => i.hidden || !i.getAttribute('src') || i.complete)",
                           timeout=10000)


def _scenario(page, shot) -> None:
    """Одинаковый путь по экранам. Только существующие селекторы (их же использует e2e)."""
    page.wait_for_function("document.querySelector('#hero').dataset.feed === 'ready'", timeout=15000)
    page.wait_for_timeout(1500)                                 # «Миксы по артистам» дорисовываются по таймеру
    shot("01-home")
    page.fill("#search-input", "gspd")
    page.keyboard.press("Enter")
    page.wait_for_selector("#tracks .row:not(.row--skel)")
    shot("02-search")
    page.click("#tabs [data-tab=artists]")
    page.wait_for_selector("#cards .card--artist")
    shot("03-search-artists")
    page.locator("#cards .card--artist").first.click()
    page.wait_for_selector("#albums-row .card")
    page.wait_for_selector("#tracks .row:not(.row--skel)")
    shot("04-artist")
    page.locator("#albums-row .card").first.click()
    page.wait_for_function("document.querySelector('#d-kind').textContent === 'Альбом' && document.querySelectorAll('#tracks .row:not(.row--skel)').length === 8")
    shot("05-album")
    page.click(".nav__item[data-view=playlists]")
    page.wait_for_selector("#cards .card--new")
    shot("06-playlists")
    page.click(".nav__item[data-view=collection]")
    page.wait_for_selector("#col-tiles .tile")
    shot("07-collection")
    page.click(".nav__item[data-view=liked]")
    page.wait_for_selector("#empty:not([hidden])")
    shot("08-liked-empty")
    page.click(".nav__item[data-view=history]")
    page.wait_for_selector("#empty:not([hidden])")
    shot("09-history-empty")
    page.click(".nav__item[data-view=downloads]")
    page.wait_for_selector("#empty:not([hidden])")
    shot("10-downloads-empty")
    page.click(".nav__item[data-view=settings]")
    page.wait_for_function("document.querySelector('#set-paths').textContent.length > 0")
    shot("11-settings", mask=["#set-paths", "#set-dir"])

    # Воспроизведение: старт с баннера, сразу пауза и позиция 0 — время на снимке одинаковое
    page.click(".nav__item[data-view=home]")
    page.wait_for_function("document.querySelector('#hero').dataset.feed === 'ready'", timeout=15000)
    page.click("#hero-play")
    page.wait_for_function("!document.querySelector('#audio').paused && document.querySelector('#audio').currentTime > 0")
    page.click("#hero-play")
    page.wait_for_function("document.querySelector('#audio').paused")
    page.evaluate("document.querySelector('#audio').currentTime = 0")
    page.wait_for_timeout(1500)
    shot("12-home-paused")
    page.click("#hero-title")
    page.wait_for_selector("#np-lyrics .ly")
    page.wait_for_selector("#similar-row .tcard:not(.tcard--skel)")
    shot("13-now")
    page.click("[data-do=queue].rbtn")
    page.wait_for_selector("#queue:not([hidden]) .q")
    shot("14-queue-drawer")
    page.click("#queue-close")
    page.click("[data-do=expand]")
    page.wait_for_selector("#fs:not([hidden])")
    shot("15-fs-default")
    page.click("#fs-lyrics-btn")
    page.wait_for_selector("#fs-lyrics .ly")
    shot("16-fs-lyrics")
    page.click("#fs-lyrics-btn")
    page.click("#fs-queue-btn")
    page.wait_for_selector("#fs-q-list .fq__row")
    shot("17-fs-queue")
    page.keyboard.press("Escape")
    page.keyboard.press("Escape")
    page.wait_for_selector("#fs", state="hidden")
    page.fill("#search-input", "gspd")
    page.keyboard.press("Enter")
    page.click("#tabs [data-tab=tracks]")
    page.wait_for_selector("#tracks .row:not(.row--skel)")
    shot("18-search-with-player")
    page.click("#tracks .row:nth-child(1) .track__more")
    page.wait_for_selector("#menu:not([hidden])")
    shot("19-track-menu")


def capture(out: Path, root: Path) -> int:
    from playwright.sync_api import sync_playwright
    out.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="wave-ui-snapshots-"))
    sys.argv = [sys.argv[0], "http://127.0.0.1", str(work / "e2e-shots")]   # e2e_smoke читает argv при импорте
    import e2e_smoke                                                       # noqa: E402
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])
            for w, h in WIDTHS:
                url, proc, log = _start_backend(root, work / f"profile-{w}")
                errors: list[str] = []
                try:
                    ctx = browser.new_context(viewport={"width": w, "height": h}, color_scheme="dark")
                    ctx.route(e2e_smoke.re.compile(r".*"), e2e_smoke.handle)
                    page = ctx.new_page()
                    page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
                    page.goto(url)

                    def shot(name: str, mask: list[str] | None = None) -> None:
                        page.mouse.move(0, 0)                  # без «наведения» на только что нажатую кнопку
                        _settle(page)
                        page.screenshot(path=str(out / f"{w}-{name}.png"), animations="disabled", caret="hide",
                                        mask=[page.locator(s) for s in (mask or [])])
                    _scenario(page, shot)
                    ctx.close()
                finally:
                    proc.terminate()
                    proc.wait(10)
                    log.close()
                if errors:
                    print(f"{w}px: ошибки страницы: {errors}", file=sys.stderr)
                    return 1
                print(f"{w}x{h}: снято")
            browser.close()
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print(f"Снимки: {out} ({len(list(out.glob('*.png')))} файлов)")
    return 0


_DIFF_JS = """async ([a, b, tol]) => {
    const load = async (src) => { const i = new Image(); i.src = src; await i.decode(); return i; };
    const [ia, ib] = await Promise.all([load(a), load(b)]);
    if (ia.width !== ib.width || ia.height !== ib.height) return {size: [ia.width, ia.height, ib.width, ib.height], diff: -1};
    const px = (img) => { const c = document.createElement('canvas'); c.width = img.width; c.height = img.height;
        const g = c.getContext('2d'); g.drawImage(img, 0, 0); return g.getImageData(0, 0, img.width, img.height).data; };
    const da = px(ia), db = px(ib);
    let diff = 0, maxd = 0, x0 = 1e9, y0 = 1e9, x1 = -1, y1 = -1;
    for (let i = 0; i < da.length; i += 4) {
        const d = Math.max(Math.abs(da[i] - db[i]), Math.abs(da[i + 1] - db[i + 1]), Math.abs(da[i + 2] - db[i + 2]));
        if (d <= tol) continue;
        diff++; if (d > maxd) maxd = d;
        const x = (i / 4) % ia.width, y = Math.floor(i / 4 / ia.width);
        x0 = Math.min(x0, x); y0 = Math.min(y0, y); x1 = Math.max(x1, x); y1 = Math.max(y1, y);
    }
    return diff ? {diff, maxd, box: [x0, y0, x1, y1]} : {diff: 0};
}"""


def compare(a: Path, b: Path, tol: int = 0) -> int:
    from playwright.sync_api import sync_playwright
    names_a = {p.name for p in a.glob("*.png")}
    names_b = {p.name for p in b.glob("*.png")}
    bad = 0
    for missing in sorted(names_a ^ names_b):
        print(f"  НЕТ ПАРЫ  {missing}")
        bad += 1
    url = lambda p: "data:image/png;base64," + base64.b64encode(p.read_bytes()).decode()   # noqa: E731
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        for name in sorted(names_a & names_b):
            fa, fb = a / name, b / name
            if fa.read_bytes() == fb.read_bytes():
                print(f"  same      {name}")
                continue
            r = page.evaluate(_DIFF_JS, [url(fa), url(fb), tol])
            if r["diff"] == 0:
                print(f"  same      {name} (отличий больше ±{tol} по каналу нет)")
            else:
                bad += 1
                print(f"  DIFF      {name}: {r}")
        browser.close()
    print(f"\nСнимков: {len(names_a | names_b)}, различаются или без пары: {bad}")
    return 1 if bad else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Снимки экранов Wave до и после изменения")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("capture")
    c.add_argument("out", type=Path)
    c.add_argument("--root", type=Path, default=HERE.parent, help="корень репозитория, чей backend и frontend снимать")
    d = sub.add_parser("compare")
    d.add_argument("a", type=Path)
    d.add_argument("b", type=Path)
    d.add_argument("--tolerance", type=int, default=0,
                   help="допустимое отклонение канала 0–255 (сглаживание текста между запусками даёт до ±2)")
    args = ap.parse_args()
    if args.cmd == "capture":
        return capture(args.out.resolve(), args.root.resolve())
    return compare(args.a, args.b, args.tolerance)


if __name__ == "__main__":
    sys.exit(main())
