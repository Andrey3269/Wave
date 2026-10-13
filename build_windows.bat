@echo off
rem Сборка Wave-Setup.exe одной командой. Запускать из корня репозитория на Windows 10/11 (x64).
rem Нужны: Python 3.12 (рекомендуется) и Inno Setup 6. Интернет нужен только на время сборки.
setlocal
cd /d "%~dp0"

if not exist ".venv-build\Scripts\python.exe" (
  py -3.12 -m venv .venv-build 2>nul || py -3 -m venv .venv-build || goto :fail
)
call ".venv-build\Scripts\activate.bat" || goto :fail

python -m pip install --upgrade pip || goto :fail
pip install -r backend\requirements.txt -r packaging\requirements-build.txt -c constraints.txt || goto :fail

rem JS-runtime Deno для YouTube: официальный архив по packaging\runtime\deno.lock, сверка SHA-256 до распаковки.
rem Повторная сборка ничего не скачивает, если подготовленный файл уже проверен.
python packaging\fetch_runtime.py --platform windows-x86_64 || goto :fail

python packaging\svg_to_ico.py frontend\assets\wave_icon.svg packaging\wave.ico || goto :fail
python -m PyInstaller --noconfirm --clean packaging\wave.spec || goto :fail

rem Самопроверка собранной папки без окна и без сети: упакованный Deno найден и поддерживается,
rem скрипты yt-dlp-ejs на месте. Воспроизведение этим не проверяется (TEST_STRATEGY: WIN-6, LIVE-2).
rem Wave.exe — оконная программа, поэтому start /wait (иначе cmd не ждёт и не получает код возврата).
if not exist "build" mkdir build
start "" /wait "dist\Wave\Wave.exe" --diagnostics --output "build\diagnostics.json"
if errorlevel 1 (
  echo Диагностика собранного приложения нашла проблему. Отчёт: build\diagnostics.json
  goto :fail
)
echo Диагностика собранного приложения: OK ^(build\diagnostics.json^)

if not exist "installer\MicrosoftEdgeWebview2Setup.exe" (
  powershell -NoProfile -Command "Invoke-WebRequest 'https://go.microsoft.com/fwlink/p/?LinkId=2124703' -OutFile 'installer\MicrosoftEdgeWebview2Setup.exe'" || goto :fail
)

set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" (
  echo Не найден Inno Setup 6. Установите: https://jrsoftware.org/isdl.php
  goto :fail
)
"%ISCC%" installer\wave.iss || goto :fail

echo.
echo Готово: dist\installer\Wave-Setup.exe
exit /b 0

:fail
echo.
echo Сборка не удалась. Смотрите сообщения выше.
exit /b 1