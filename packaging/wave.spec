# -*- mode: python ; coding: utf-8 -*-
# PyInstaller: python -m PyInstaller --noconfirm --clean packaging/wave.spec
# Режим onedir (папка dist/Wave): запускается быстрее onefile и реже вызывает ложные срабатывания антивирусов.
import os
import sys

from PyInstaller.utils.hooks import collect_all, collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
sys.path.insert(0, os.path.join(ROOT, "packaging"))
import fetch_runtime  # noqa: E402  (packaging/fetch_runtime.py: проверка подготовленного Deno)

datas = [(os.path.join(ROOT, "frontend"), "frontend")]    # интерфейс: backend раздаёт его из этой папки
binaries, hidden = [], []

# JS-runtime Deno для yt-dlp (S3). Готовит его `python packaging/fetch_runtime.py` со сверкой SHA-256;
# здесь — только проверка, что подготовлен именно закреплённый в deno.lock файл. Без него сборка не нужна:
# YouTube в ней работал бы в урезанном режиме. В приложении: <_internal>/runtime/deno/deno.exe
# (jsruntime.bundled_path), lock — <_internal>/runtime/deno.lock (диагностика сверяет версию).
_lock = fetch_runtime.load_lock()
if not fetch_runtime.is_ready(_lock, "windows-x86_64"):
    raise SystemExit("Deno не подготовлен или не совпадает с packaging/runtime/deno.lock. "
                     "Выполните: python packaging/fetch_runtime.py")
datas += [
    (os.path.join(fetch_runtime.DEST, "deno.exe"), os.path.join("runtime", "deno")),
    (str(fetch_runtime.LOCK), "runtime"),
    (os.path.join(ROOT, "licenses"), "licenses"),          # тексты лицензий и THIRD_PARTY_NOTICES.txt
]

# Пакеты с данными/динамическими импортами: забираем целиком. Без обязательных сборка не продолжается.
REQUIRED = {"yt_dlp", "yt_dlp_ejs", "ytmusicapi", "certifi"}   # yt_dlp_ejs: скрипты решателя JS-задач YouTube
for pkg in ("yt_dlp", "yt_dlp_ejs", "ytmusicapi", "certifi", "webview", "pythonnet", "clr_loader"):
    try:
        d, b, h = collect_all(pkg)
    except Exception as exc:
        if pkg in REQUIRED:
            raise SystemExit(f"Обязательный пакет {pkg} не установлен или не собирается: {exc}")
        continue                                          # необязательный пакет не установлен
    if pkg in REQUIRED and not d and not h:
        raise SystemExit(f"Обязательный пакет {pkg} не найден (collect_all ничего не вернул)")
    datas += d
    binaries += b
    hidden += h
if not any(str(src).endswith(".js") and "yt_dlp_ejs" in str(src).replace("\\", "/") for src, _ in datas):
    raise SystemExit("Скрипты yt_dlp_ejs (*.js) не попали в сборку")

hidden += collect_submodules("uvicorn") + collect_submodules("anyio") + ["h11", "multipart"]

a = Analysis(
    [os.path.join(ROOT, "backend", "launcher.py")],
    pathex=[os.path.join(ROOT, "backend")],
    datas=datas,
    binaries=binaries,
    hiddenimports=hidden,
    excludes=["tkinter", "matplotlib", "numpy", "pandas", "scipy", "IPython", "pytest"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="Wave",
    icon=os.path.join(ROOT, "packaging", "wave.ico"),
    console=False,                                        # без чёрного окна консоли
)
coll = COLLECT(exe, a.binaries, a.datas, name="Wave")
