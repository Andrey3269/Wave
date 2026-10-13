@echo off
setlocal EnableExtensions
rem ВОЛНА (Wave): сборка установщика dist\installer\Wave-Setup-<версия>.exe одной командой (этап S4).
rem Запуск из любой папки на Windows 10/11 x64, без прав администратора:  build_windows.bat
rem Нужны: Python 3.12 x64 (py launcher), Inno Setup 6 и интернет на время сборки.
rem Пошаговая проверка результата: docs\engineering\checklists\S4_WINDOWS_CHECK.md
rem Скрипт пишет только в .venv-build\, build\ и dist\ этого репозитория и ничего не удаляет за их пределами.

for /f "tokens=2 delims=:." %%c in ('chcp') do set "WAVE_OLD_CP=%%c"
chcp 65001 >nul
cd /d "%~dp0"

echo [1/9] Каталоги сборки
for %%d in (build build\webview2 dist dist\installer) do (
  if not exist "%%d" mkdir "%%d" || goto :fail
)

echo [2/9] Python 3.12 x64 и чистое окружение .venv-build
rem По умолчанию — py launcher (py -3.12). WAVE_BUILD_PYTHON — полный путь к python.exe 3.12, если py нет
rem (так делает CI); версию и разрядность в любом случае проверяет packaging\verify_build_env.py.
set "BASE_PY=py -3.12"
if defined WAVE_BUILD_PYTHON set BASE_PY="%WAVE_BUILD_PYTHON%"
%BASE_PY% -c "import struct, sys; sys.exit(0 if sys.version_info[:2] == (3, 12) and struct.calcsize('P') == 8 else 1)" >nul 2>&1
if errorlevel 1 (
  echo ОШИБКА: не найден Python 3.12 ^(64-бит^). Установите его с https://www.python.org/downloads/
  echo         ^(Windows installer 64-bit, с py launcher^) и запустите сборку снова.
  echo         Другие версии Python не используются: под 3.12 закреплён packaging\requirements-build.lock.
  goto :fail
)
rem Окружение пересоздаётся каждый раз: в нём не остаются пакеты прошлых сборок.
if exist ".venv-build" rmdir /s /q ".venv-build" || goto :fail
%BASE_PY% -m venv .venv-build || goto :fail
set "PY=%CD%\.venv-build\Scripts\python.exe"

echo [3/9] Зависимости: только packaging\requirements-build.lock, с проверкой хэшей
"%PY%" -m pip install --disable-pip-version-check --require-hashes --no-deps -r packaging\requirements-build.lock || goto :fail
"%PY%" -m pip check || goto :fail
"%PY%" packaging\verify_build_env.py || goto :fail

echo [4/9] Deno для YouTube: packaging\runtime\deno.lock, сверка SHA-256
rem Повторная сборка ничего не скачивает, если подготовленный файл уже проверен.
"%PY%" packaging\fetch_runtime.py --platform windows-x86_64 || goto :fail

echo [5/9] Иконка и сборка dist\Wave ^(PyInstaller^)
"%PY%" packaging\svg_to_ico.py frontend\assets\wave_icon.svg packaging\wave.ico || goto :fail
"%PY%" -m PyInstaller --noconfirm --clean packaging\wave.spec || goto :fail
if not exist "dist\Wave\Wave.exe" (
  echo ОШИБКА: PyInstaller не создал dist\Wave\Wave.exe
  goto :fail
)

echo [6/9] Самопроверка собранного приложения: Wave.exe --diagnostics
rem Без окна и без сети: упакованный Deno найден и поддерживается, скрипты yt-dlp-ejs на месте.
rem Воспроизведение этим не проверяется (ручной чек-лист). Wave.exe — оконная программа, поэтому
rem start /wait (иначе cmd не ждёт и не получает код возврата).
rem Профиль диагностики — во временной папке сборки: журнал и кэш Deno не пишутся в %LOCALAPPDATA%\Wave.
if exist "build\diagnostics.json" del /q "build\diagnostics.json"
set "WAVE_DATA_DIR=%CD%\build\diagnostics-profile"
set "WAVE_DOWNLOADS_DIR=%CD%\build\diagnostics-profile\downloads"
start "" /wait "dist\Wave\Wave.exe" --diagnostics --output "build\diagnostics.json"
set "DIAG_CODE=%ERRORLEVEL%"
set "WAVE_DATA_DIR="
set "WAVE_DOWNLOADS_DIR="
if not "%DIAG_CODE%"=="0" (
  echo ОШИБКА: диагностика собранного приложения нашла проблему ^(код %DIAG_CODE%^). Отчёт: build\diagnostics.json,
  echo         журнал: build\diagnostics-profile\logs\wave.log
  goto :fail
)
if not exist "build\diagnostics.json" (
  echo ОШИБКА: диагностика не записала отчёт build\diagnostics.json
  goto :fail
)
echo Диагностика собранного приложения: OK ^(build\diagnostics.json^)

echo [7/9] Версия: backend\version.py и Wave.exe
set "WAVE_VERSION="
for /f "delims=" %%v in ('call "%PY%" backend\version.py') do set "WAVE_VERSION=%%v"
if not defined WAVE_VERSION (
  echo ОШИБКА: не удалось получить версию из backend\version.py
  goto :fail
)
set "EXE_VERSION="
for /f "usebackq delims=" %%v in (`powershell -NoProfile -Command "(Get-Item 'dist\Wave\Wave.exe').VersionInfo.ProductVersion"`) do set "EXE_VERSION=%%v"
if not "%EXE_VERSION%"=="%WAVE_VERSION%" (
  echo ОШИБКА: версия Wave.exe "%EXE_VERSION%" не совпадает с backend\version.py "%WAVE_VERSION%"
  goto :fail
)
echo Версия: %WAVE_VERSION%

echo [8/9] Microsoft Edge WebView2 bootstrapper ^(проверка подписи Microsoft^)
powershell -NoProfile -ExecutionPolicy Bypass -File packaging\fetch_webview2.ps1 -Dest build\webview2\MicrosoftEdgeWebview2Setup.exe || goto :fail

echo [9/9] Установщик ^(Inno Setup 6^)
call :find_iscc
if not defined ISCC (
  echo ОШИБКА: не найден компилятор Inno Setup 6 ^(ISCC.exe^).
  echo         Установите Inno Setup 6.7.3 с https://jrsoftware.org/isdl.php ^(раздел Inno Setup 6^)
  echo         или укажите путь: set "ISCC=C:\путь\к\ISCC.exe" и запустите сборку снова.
  goto :fail
)
"%ISCC%" /? 2>&1 | findstr /c:"Inno Setup 6" >nul
if errorlevel 1 (
  echo ОШИБКА: "%ISCC%" — не Inno Setup 6. Установщик рассчитан и проверен на Inno Setup 6 ^(6.7.3^).
  goto :fail
)
echo Компилятор: %ISCC%
set "OUT=dist\installer\Wave-Setup-%WAVE_VERSION%.exe"
if exist "%OUT%" del /q "%OUT%" || goto :fail
rem Необязательная подпись: только если задана команда подписи (сертификаты в репозитории не хранятся).
rem Пример: set "WAVE_SIGN_COMMAND=signtool sign /a /fd sha256 /tr http://timestamp.example /td sha256 $f"
if defined WAVE_SIGN_COMMAND (
  echo Подпись: включена ^(WAVE_SIGN_COMMAND^)
  "%ISCC%" /Qp "/DAppVersion=%WAVE_VERSION%" "/DSignTool=wavesign" "/Swavesign=%WAVE_SIGN_COMMAND%" installer\wave.iss || goto :fail
) else (
  echo Подпись: нет ^(WAVE_SIGN_COMMAND не задана^) — Windows SmartScreen может предупредить о неизвестном издателе
  "%ISCC%" /Qp "/DAppVersion=%WAVE_VERSION%" installer\wave.iss || goto :fail
)
if not exist "%OUT%" (
  echo ОШИБКА: компилятор завершился без ошибки, но %OUT% не создан
  goto :fail
)
for %%f in ("%OUT%") do set "OUT_SIZE=%%~zf"

echo.
echo Готово: %OUT% ^(%OUT_SIZE% байт^)
call :restore_cp
exit /b 0

:find_iscc
rem Порядок: переменная ISCC, PATH, стандартные места установки Inno Setup 6 (для всех и для пользователя)
if defined ISCC if exist "%ISCC%" exit /b 0
set "ISCC="
for /f "delims=" %%i in ('where iscc.exe 2^>nul') do if not defined ISCC set "ISCC=%%i"
if defined ISCC exit /b 0
for %%p in ("%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" "%ProgramFiles%\Inno Setup 6\ISCC.exe" "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe") do (
  if not defined ISCC if exist "%%~p" set "ISCC=%%~p"
)
exit /b 0

:restore_cp
if defined WAVE_OLD_CP chcp %WAVE_OLD_CP: =% >nul
exit /b 0

:fail
echo.
echo Сборка не удалась. Смотрите сообщения выше.
call :restore_cp
exit /b 1
