# -*- mode: python ; coding: utf-8 -*-
# PyInstaller: python -m PyInstaller --noconfirm --clean packaging/wave.spec
# Режим onedir (папка dist/Wave): запускается быстрее onefile и реже вызывает ложные срабатывания антивирусов.
import os

from PyInstaller.utils.hooks import collect_all, collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))

datas = [(os.path.join(ROOT, "frontend"), "frontend")]    # интерфейс: backend раздаёт его из этой папки
binaries, hidden = [], []

# Пакеты с данными/динамическими импортами: забираем целиком
for pkg in ("yt_dlp", "yt_dlp_ejs", "ytmusicapi", "certifi", "webview", "pythonnet", "clr_loader"):
    try:
        d, b, h = collect_all(pkg)
    except Exception:
        continue                                          # необязательный пакет не установлен
    datas += d
    binaries += b
    hidden += h

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
