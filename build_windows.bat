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
pip install -r backend\requirements.txt -r packaging\requirements-build.txt || goto :fail

python packaging\svg_to_ico.py frontend\assets\wave_icon.svg packaging\wave.ico || goto :fail
python -m PyInstaller --noconfirm --clean packaging\wave.spec || goto :fail

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