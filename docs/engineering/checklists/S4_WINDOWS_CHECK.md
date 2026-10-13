# Ручная проверка S4 на Windows 11 x64: установщик (WIN-1, 2, 3, 4, 8, 9, 15)

> **Статус: выполнено.** Владелец прошёл все ручные проверки этого чек-листа на Windows 11; **S4 принят 2026-10-13**. Подтверждённые факты и открытые вопросы публичного выпуска — [reports/S4.md](../reports/S4.md) §12–13. Результаты отдельных шагов в репозиторий не вносились. Чек-лист сохраняется для повторной проверки следующих версий установщика.

Цель: подтвердить, что собранный у вас `Wave-Setup-<версия>.exe` ставится без прав администратора, запускается из меню «Пуск», обновляется поверх с сохранением данных и удаляется, не трогая данные. Около 45–60 минут. CI выполняет те же сценарии автоматически на `windows-latest` (см. отчёт S4), но не видит окна, сообщений установщика и SmartScreen — это проверяется здесь.

**Ваш рабочий профиль ВОЛНЫ не используется и не удаляется.** Сборка идёт в обычной учётной записи (пишет только в папку репозитория). Установка, обновление и удаление — в **отдельной временной локальной учётной записи без прав администратора**: у неё свои `%LOCALAPPDATA%\Wave`, «Пуск» и папка загрузок. Подойдёт и отдельная виртуальная машина (Hyper-V, VirtualBox) — тогда шаги 2–9 выполняйте в ней.

Команды — PowerShell (7 или встроенный 5.1). Отмечайте результат каждого пункта: `PASS`, `FAIL` (что увидели) или `NOT RUN` (почему).

## 0. Подготовка (ваша обычная учётная запись)

Нужны: Git; **Python 3.12 x64** с python.org (установщик «Windows installer (64-bit)», галочка py launcher; на машине может оставаться и другой Python — сборка его не использует); **Inno Setup 6.7.3**: <https://jrsoftware.org/isdl.php>, раздел Inno Setup 6, файл `innosetup-6.7.3.exe`. Inno Setup 7 сборка не использует.

```powershell
# Python 3.12 x64 виден через py launcher
py -3.12 -c "import sys, struct; print(sys.version.split()[0], struct.calcsize('P') * 8)"
# ожидается: 3.12.x 64

# Подлинность установщика Inno Setup перед установкой (папка загрузок — ваша)
Get-AuthenticodeSignature "$env:USERPROFILE\Downloads\innosetup-6.7.3.exe" | Format-List Status, SignerCertificate
# ожидается: Status Valid, подписант CN=Pyrsys B.V.
(Get-FileHash "$env:USERPROFILE\Downloads\innosetup-6.7.3.exe" -Algorithm SHA256).Hash
# ожидается: 9C73C3BAE7ED48D44112A0F48E66742C00090BDB5BEF71D9D3C056C66E97B732

# Ветка S4
cd C:\src\Wave                  # ваш клон; если его нет: git clone https://github.com/Andrey3269/Wave.git C:\src\Wave
git fetch origin
git switch claude/s4-installer
git pull --ff-only
git log -1 --oneline            # совпадает с последним коммитом ветки на GitHub
git status --short              # пусто
```

## 1. Сборка установщика (WIN-1)

```powershell
cmd /c build_windows.bat
$LASTEXITCODE                   # ожидается: 0
```

Ожидается в выводе, по шагам `[1/9]`…`[9/9]`:
- `Окружение сборки: Python 3.12.x, 43 пакетов ровно как в requirements-build.lock`;
- `SHA-256 архива совпал: a0c3101b…` (или «уже проверен» при повторной сборке) — Deno 2.9.7;
- сборка PyInstaller без ошибок, `Диагностика собранного приложения: OK (build\diagnostics.json)`;
- `Версия: 0.1.0`;
- `Готово: …MicrosoftEdgeWebview2Setup.exe (… байт, подпись Microsoft действительна, …)`;
- `Компилятор: …\Inno Setup 6\ISCC.exe`, `Подпись: нет (WAVE_SIGN_COMMAND не задана) …`;
- последняя строка `Готово: dist\installer\Wave-Setup-0.1.0.exe (… байт)`.

```powershell
$SETUP = (Resolve-Path .\dist\installer\Wave-Setup-0.1.0.exe).Path
"{0:N1} MB" -f ((Get-Item $SETUP).Length / 1MB)                       # ожидается: около 55 МБ
(Get-Item $SETUP).VersionInfo | Format-List ProductName, ProductVersion # ВОЛНА (Wave), 0.1.0
(Get-Item .\dist\Wave\Wave.exe).VersionInfo | Format-List ProductName, ProductVersion, CompanyName
Get-AuthenticodeSignature $SETUP | Select-Object Status                 # NotSigned — подписи пока нет (ADR-8)
# Сборка не тронула ваш профиль: диагностика писала журнал в build\diagnostics-profile
Test-Path .\build\diagnostics-profile\logs\wave.log                     # True
```

Если Python 3.12 или Inno Setup не найдены, скрипт должен остановиться с понятной инструкцией и кодом 1 — это тоже стоит отметить.

Положите установщик туда, где его увидит временная учётная запись:

```powershell
New-Item -ItemType Directory -Force C:\Users\Public\WaveS4 | Out-Null
Copy-Item $SETUP C:\Users\Public\WaveS4\
```

## 2. Временная учётная запись без прав администратора

PowerShell **от имени администратора** (единственный шаг, где нужны права администратора):

```powershell
$pw = Read-Host -AsSecureString "Пароль для временной учётной записи WaveS4"
New-LocalUser -Name WaveS4 -Password $pw -FullName "Wave S4 test" -AccountNeverExpires
Add-LocalGroupMember -Group (Get-LocalGroup -SID S-1-5-32-545) -Member WaveS4   # группа «Пользователи»
Get-LocalGroupMember -Group (Get-LocalGroup -SID S-1-5-32-544) | Where-Object Name -like "*WaveS4"   # пусто: не администратор
```

Войдите в Windows под `WaveS4` (Пуск → значок пользователя → «Сменить пользователя»). Дальше всё — под ней.

## 3. Установка без прав администратора (WIN-2)

1. Откройте `C:\Users\Public\WaveS4\Wave-Setup-0.1.0.exe` двойным щелчком.
2. SmartScreen может показать «Windows защитила ваш компьютер»: установщик не подписан. «Подробнее» → «Выполнить в любом случае». Отметьте, было ли предупреждение.
3. **Ожидается: запроса прав администратора (UAC) нет.**
4. Мастер на русском; папка по умолчанию `C:\Users\WaveS4\AppData\Local\Programs\Wave`; галочка «Создать значок на рабочем столе» снята — поставьте её для проверки.
5. На последней странице отмечено «Запустить ВОЛНУ» → «Завершить». Окно ВОЛНЫ открывается.

Проверка (PowerShell под WaveS4):

```powershell
$APP = "$env:LOCALAPPDATA\Programs\Wave"
Test-Path "$APP\Wave.exe", "$APP\THIRD_PARTY_NOTICES.txt", "$APP\_internal\runtime\deno\deno.exe", "$APP\_internal\licenses\python\PACKAGES.txt"
# ожидается: True True True True
Get-ItemProperty "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\{314C4533-1390-4F2A-B0B8-7003E0F6F200}_is1" |
  Format-List DisplayName, DisplayVersion, Publisher, InstallLocation
# ВОЛНА (Wave), 0.1.0, Andrey3269, ...\AppData\Local\Programs\Wave\
```

В «Параметры → Приложения → Установленные приложения» есть «ВОЛНА (Wave)» 0.1.0. В меню «Пуск» — «ВОЛНА (Wave)», на рабочем столе — ярлык.

## 4. Запуск из «Пуска» и данные (WIN-2, подготовка WIN-8/9)

1. Закройте ВОЛНУ. Запустите её из меню «Пуск». Окно открывается без мигающего окна консоли.
2. Включите трек YouTube, перемотайте, переключите на следующий (как в S3).
3. Создайте данные, которые потом проверим: добавьте 2–3 трека в избранное, создайте плейлист «S4 проверка» с треком, измените громкость и имя профиля в настройках, послушайте 2–3 трека (история).
4. Положите тестовый файл в папку загрузок (новые загрузки ВОЛНОЙ не нужны):

```powershell
$DL = "$env:USERPROFILE\Downloads\Wave"
New-Item -ItemType Directory -Force $DL | Out-Null
Set-Content "$DL\s4-check.txt" "S4 $(Get-Date -Format o)"
Get-ChildItem "$env:LOCALAPPDATA\Wave" | Select-Object Name        # wave.db, logs, webview, deno ...
Select-String -Path "$env:LOCALAPPDATA\Wave\logs\wave.log" -Pattern "JS-runtime для yt-dlp: deno 2.9.7" | Select-Object -Last 1
```

## 5. Второй запуск (WIN-4)

При открытой ВОЛНЕ запустите её ещё раз из «Пуска».

- Ожидается сообщение «Wave уже запущен.»; после «ОК» второго окна нет.
- Процесс один:

```powershell
(Get-Process Wave).Count        # ожидается: 1
Select-String "$env:LOCALAPPDATA\Wave\logs\wave.log" -Pattern "второй экземпляр завершается" | Select-Object -Last 1
```

Известное ограничение (L5, этап S17): второй запуск не выводит уже открытое окно на передний план.

## 6. Обновление поверх (WIN-8)

Новой версии ещё нет, поэтому обновление проверяется установкой того же установщика поверх: это тот же путь Inno Setup (тот же AppId, замена файлов программы, удаление старой `_internal`).

1. **ВОЛНА открыта.** Запустите `Wave-Setup-0.1.0.exe` снова.
2. Ожидается сообщение установщика о том, что ВОЛНА запущена, с просьбой закрыть её. Закройте окно ВОЛНЫ (крестик), затем «ОК» в сообщении установщика. Если вместо этого появилась страница «Подготовка к установке» со списком приложений — отметьте, что именно предложено, и выберите автоматическое закрытие.
3. Установка завершается, «Запустить ВОЛНУ» → окно открывается.
4. Избранное, плейлист «S4 проверка», громкость, имя профиля и история на месте; `s4-check.txt` в папке загрузок на месте.
5. После закрытия окна нет процессов `Wave.exe` и `deno.exe` (Диспетчер задач или `Get-Process Wave, deno -ErrorAction SilentlyContinue` — пусто).

## 7. Удаление с сохранением данных (WIN-9)

1. Откройте ВОЛНУ и попробуйте удалить её: «Параметры → Приложения → ВОЛНА (Wave) → Удалить». Ожидается сообщение, что ВОЛНА запущена. Закройте ВОЛНУ, продолжите удаление.
2. После удаления:

```powershell
Test-Path "$env:LOCALAPPDATA\Programs\Wave\Wave.exe"     # False
Test-Path "$env:LOCALAPPDATA\Wave\wave.db"               # True — данные остались
Test-Path "$env:USERPROFILE\Downloads\Wave\s4-check.txt" # True — загрузки остались
Test-Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\{314C4533-1390-4F2A-B0B8-7003E0F6F200}_is1"   # False
```

Ярлыков в «Пуске» и на рабочем столе нет, в «Установленных приложениях» ВОЛНЫ нет.

3. Установите ВОЛНУ снова (шаг 3) и запустите: избранное, плейлист и настройки снова видны — данные пережили удаление.

## 8. Нет WebView2 (WIN-3)

**8а. Сообщение и поведение установщика (Windows 11).** В Windows 11 WebView2 встроен, поэтому отсутствие имитируется тестовой сборкой установщика (в выпускаемый установщик этот код не входит). В вашей обычной учётной записи, в папке репозитория после шага 1:

```powershell
& "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe" /Qp "/DAppVersion=0.1.0" "/DTestWebView2=fail" "/OC:\Users\Public\WaveS4" "/FWave-Setup-0.1.0-webview2-fail" installer\wave.iss
# если Inno Setup установлен «только для меня»: "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
```

Под WaveS4 удалите ВОЛНУ (шаг 7), запустите `C:\Users\Public\WaveS4\Wave-Setup-0.1.0-webview2-fail.exe`:
- после копирования файлов — сообщение на русском, что WebView2 Runtime установить не удалось (код 1603), с шагами: проверить интернет и запустить установщик снова, ссылка Microsoft, обращение к администратору;
- на последней странице **нет** галочки «Запустить ВОЛНУ».

Удалите эту тестовую установку (шаг 7, пункт 1).

**8б. Настоящее отсутствие WebView2** — только на машине без него (как правило, старая Windows 10 в виртуальной машине без Microsoft Edge). Если такой нет — `NOT RUN`. Если есть: выпускаемый установщик должен сам скачать и тихо поставить WebView2 (нужен интернет), после чего ВОЛНА запускается; без интернета — сообщение из 8а и нет «Запустить ВОЛНУ», а запуск ВОЛНЫ из «Пуска» показывает понятное сообщение о WebView2 со ссылкой.

## 9. Чистота дистрибутива (WIN-15)

В вашей обычной учётной записи, в папке репозитория:

```powershell
Get-ChildItem .\dist\Wave -Recurse -Force |
  Where-Object { $_.Name -match '\.(db|db-wal|db-shm|log|m4a|mp3|webm|ogg|opus|pem|key|pfx|pyc)$' -or
                 $_.Name -match '^(\.env.*|cookies.*\.txt|wave\.db|\.wave-test-profile)$' -or
                 ($_.PSIsContainer -and $_.Name -in '__pycache__', '.git', '.venv', 'node_modules') } |
  Select-Object FullName
# ожидается: пусто
Get-Content .\dist\Wave\_internal\licenses\python\PACKAGES.txt -Encoding utf8 | Select-Object -First 12
```

Установленная папка совпадает с `dist\Wave` (плюс `THIRD_PARTY_NOTICES.txt`, `unins000.exe`, `unins000.dat`) — это сверяет CI файл за файлом.

## 10. Уборка

Под WaveS4: удалите ВОЛНУ, если она установлена (шаг 7). Выйдите из учётной записи. В своей учётной записи, PowerShell от имени администратора:

```powershell
Remove-LocalUser -Name WaveS4
# Профиль временной учётной записи (только его!):
Get-CimInstance Win32_UserProfile | Where-Object LocalPath -like "*\WaveS4" | Remove-CimInstance
Remove-Item C:\Users\Public\WaveS4 -Recurse -Force
```

Ваш `%LOCALAPPDATA%\Wave` эти шаги не затрагивают. Результаты сборки в репозитории (`dist\`, `build\`, `.venv-build\`) в git не попадают; их можно удалить или оставить.

## Итог

Итог приёмки S4 (2026-10-13): владелец сообщил, что все ручные проверки на Windows 11 пройдены; `build_windows.bat` — код 0, диагностика пройдена, `Wave-Setup-0.1.0.exe` — 54 974 824 байта, версия 0.1.0, установщик не подписан собственным сертификатом. По пунктам ниже отдельные результаты не передавались; таблица — шаблон для следующих проверок.

| Пункт | Что подтверждает | Результат |
|---|---|---|
| 1 | WIN-1: чистая сборка установщика | |
| 3 | WIN-2: установка без прав администратора, SmartScreen (да/нет) | |
| 4 | WIN-2: запуск из «Пуска», воспроизведение | |
| 5 | WIN-4: второй запуск | |
| 6 | WIN-8: обновление при запущенной ВОЛНЕ, данные сохранены | |
| 7 | WIN-9: удаление, данные и загрузки остались | |
| 8а | WIN-3: сообщение без WebView2 (тестовая сборка) | |
| 8б | WIN-3: настоящая машина без WebView2 | |
| 9 | WIN-15: чистота дистрибутива | |

S4 принят владельцем 2026-10-13. Приёмка не решает вопросы публичного выпуска (подпись кода, LGPL-код в Deno/V8, издатель, условия Inno Setup) — [reports/S4.md](../reports/S4.md) §12.
