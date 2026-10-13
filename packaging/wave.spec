# -*- mode: python ; coding: utf-8 -*-
# PyInstaller: python -m PyInstaller --noconfirm --clean packaging/wave.spec
# Режим onedir (папка dist/Wave): запускается быстрее onefile и реже вызывает ложные срабатывания антивирусов.
import os
import sys

from PyInstaller.utils.hooks import collect_all, collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
sys.path.insert(0, os.path.join(ROOT, "packaging"))
import fetch_runtime  # noqa: E402  (packaging/fetch_runtime.py: проверка подготовленного Deno)
sys.path.insert(0, os.path.join(ROOT, "backend"))
import version as app_version  # noqa: E402  (backend/version.py: единственный источник версии, S4)
import python_notices  # noqa: E402  (packaging/python_notices.py: лицензии пакетов, попавших в сборку, S4)

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

hidden += collect_submodules("uvicorn") + collect_submodules("anyio") + ["h11"]

a = Analysis(
    [os.path.join(ROOT, "backend", "launcher.py")],
    pathex=[os.path.join(ROOT, "backend")],
    datas=datas,
    binaries=binaries,
    hiddenimports=hidden,
    # Хуки PyInstaller внутри pythonnet и pywebview (их собирает collect_all) импортируют сам PyInstaller,
    # а за ним в приложение попадали setuptools, pefile, altgraph, pywin32-ctypes (CI S4, PACKAGES.txt).
    # Во время работы приложения они не нужны: исключаются вместе с тестами certifi.
    excludes=["tkinter", "matplotlib", "numpy", "pandas", "scipy", "IPython", "pytest",
              "pythonnet._pyinstaller", "webview.__pyinstaller", "certifi.tests",
              "PyInstaller", "setuptools", "pkg_resources", "_distutils_hack", "pefile", "altgraph", "ordlookup",
              "win32ctypes"],
)
# Version-resource Wave.exe (свойства файла; installer/wave.iss читает отсюда версию, название и издателя).
# Модуль versioninfo PyInstaller импортируется только на Windows; на других ОС ресурса у exe нет.
_ver = app_version.version_tuple()                        # неверный формат версии останавливает сборку
VERSION_INFO = None
if sys.platform == "win32":
    from PyInstaller.utils.win32.versioninfo import (
        FixedFileInfo, StringFileInfo, StringStruct, StringTable, VarFileInfo, VarStruct, VSVersionInfo,
    )

    _text = ".".join(map(str, _ver[:3]))
    VERSION_INFO = VSVersionInfo(
        ffi=FixedFileInfo(filevers=_ver, prodvers=_ver, mask=0x3F, flags=0x0, OS=0x40004, fileType=0x1,
                          subtype=0x0, date=(0, 0)),
        kids=[
            StringFileInfo([StringTable("041904B0", [        # русский, Unicode
                StringStruct("CompanyName", app_version.PUBLISHER),
                StringStruct("FileDescription", app_version.DESCRIPTION),
                StringStruct("FileVersion", _text),
                StringStruct("InternalName", "Wave"),
                StringStruct("OriginalFilename", app_version.EXE_NAME),
                StringStruct("ProductName", app_version.PRODUCT_NAME),
                StringStruct("ProductVersion", _text),
            ])]),
            VarFileInfo([VarStruct("Translation", [0x0419, 1200])]),
        ],
    )

pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="Wave",
    icon=os.path.join(ROOT, "packaging", "wave.ico"),
    console=False,                                        # без чёрного окна консоли
    version=VERSION_INFO,
)
# Лицензии Python и пакетов, которые PyInstaller действительно положил в сборку (S4): _internal/licenses/python/
WEBVIEW2_SDK = os.path.join(ROOT, "licenses", "webview2-sdk")   # версия и лицензия SDK из pywebview
_sdk_version = open(os.path.join(WEBVIEW2_SDK, "VERSION"), encoding="utf-8").read().strip()
LICENSE_TOC = python_notices.collect(
    [name for name, _src, _kind in a.pure],
    [dest for dest, _src, _kind in a.binaries] + [dest for dest, _src, _kind in a.datas],
    os.path.join(workpath, "licenses"),
    extra=[("Microsoft.Web.WebView2 SDK", _sdk_version,
            "библиотеки Microsoft.Web.WebView2.Core.dll, .WinForms.dll и WebView2Loader.dll в составе pywebview; "
            "лицензия Microsoft (BSD-подобная), текст: licenses/webview2-sdk/LICENSE.txt")],
)
coll = COLLECT(exe, a.binaries, a.datas + LICENSE_TOC, name="Wave")
