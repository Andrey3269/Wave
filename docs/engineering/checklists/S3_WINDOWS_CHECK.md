# Ручная проверка S3 на Windows 11 x64 (WIN-1, WIN-6, WIN-7, LIVE-2/3/4)

Цель: подтвердить воспроизведение YouTube в собранном `Wave.exe` с упакованным Deno на машине, где Deno не установлен. Около 30–40 минут. Нужны: Windows 11 x64, интернет с доступом к YouTube, Python 3.12 (`py -3.12`), Git. Inno Setup не нужен.

Команды — для PowerShell (7 или встроенного 5.1), выполнять в одном окне по порядку. Профиль, загрузки и кэш yt-dlp на время проверки — во временной папке: ваш `%LOCALAPPDATA%\Wave` не затрагивается.

## 0. Подготовка

```powershell
# Ветка S3
git clone https://github.com/Andrey3269/Wave.git C:\src\Wave   # если репозитория ещё нет
cd C:\src\Wave
git fetch origin
git switch claude/s3-ytdlp-deno
git pull --ff-only
git log -1 --oneline        # совпадает с последним коммитом ветки на GitHub
git status --short          # пусто

# Системного Deno быть не должно
Get-Command deno -ErrorAction SilentlyContinue      # ожидается: пусто
# Если Deno установлен — убрать его из PATH только для этого окна:
$env:PATH = ($env:PATH -split ';' | Where-Object { $_ -and -not (Test-Path (Join-Path $_ 'deno.exe')) }) -join ';'

# WAVE_JS_RUNTIME проверяется раньше упакованного Deno (backend/jsruntime.py) — убрать её только из этого сеанса
# (PowerShell 5.1 и 7; системные и пользовательские переменные Windows не меняются)
Remove-Item Env:WAVE_JS_RUNTIME -ErrorAction SilentlyContinue
Test-Path Env:WAVE_JS_RUNTIME                       # ожидается: False
Get-Command deno -ErrorAction SilentlyContinue      # ожидается: пусто

# Временный профиль, загрузки и чистый кэш yt-dlp (иначе решённые ранее JS-задачи берутся из кэша)
$T = "$env:TEMP\wave-s3-check"
Remove-Item $T -Recurse -Force -ErrorAction SilentlyContinue
$env:WAVE_DATA_DIR      = "$T\data"
$env:WAVE_DOWNLOADS_DIR = "$T\downloads"
$env:XDG_CACHE_HOME     = "$T\cache"
$LOG = "$env:WAVE_DATA_DIR\logs\wave.log"
```

## 1. Сборка и `Wave.exe --diagnostics` (WIN-1, часть S3)

```powershell
cmd /c "chcp 65001 >nul && build_windows.bat"
$LASTEXITCODE
```

Ожидается в выводе, по порядку:
- `Загрузка https://github.com/denoland/deno/releases/download/v2.9.7/deno-x86_64-pc-windows-msvc.zip`
- `SHA-256 архива совпал: a0c3101b4158d1dfb7d6a78a7bf0f3de80c96bb423c152beec8beb22786f2238`
- `Готово: Deno 2.9.7 (windows-x86_64) -> ...\packaging\runtime\deno\deno.exe (92 МБ) ...`
- сборка PyInstaller без ошибок;
- `Диагностика собранного приложения: OK (build\diagnostics.json)`.

После этого скрипт **ожидаемо** падает на шаге установщика (загрузка WebView2 в отсутствующую папку `installer\` или нет Inno Setup), `$LASTEXITCODE` = 1. Это работа S4. Провал до строки «Диагностика … OK» — **FAIL**.

```powershell
(Get-FileHash .\dist\Wave\_internal\runtime\deno\deno.exe -Algorithm SHA256).Hash
# ожидается: E020F3E232BD16E33768DEE528E5983349C962952051CED0A5D58AD42F5D9B33

$p = Start-Process .\dist\Wave\Wave.exe -ArgumentList '--diagnostics','--output','diag.json' -WorkingDirectory $PWD -Wait -PassThru -RedirectStandardOutput "$PWD\diag.txt"
$p.ExitCode                                   # ожидается: 0
Get-Content diag.txt -Encoding UTF8
```

В `diag.txt` ожидается:
- `Сборка: собранное приложение`;
- `yt-dlp-ejs: 0.8.0 (yt-dlp ожидает 0.8.0); скрипты читаются`;
- `JS-runtime: deno 2.9.7 supported=true (минимум 2.3.0, закреплено 2.9.7)`;
- `источник: bundled; путь: <app>\runtime\deno\deno.exe`;
- `Итог: конфигурация в порядке.`

Окно приложения при этом не открывается.

## 2. Проверка настоящего трека: `--diagnostics --live` (WIN-6, `js_challenge_runs`)

Выполнять до шага 3, пока кэш yt-dlp пуст.

```powershell
$p = Start-Process .\dist\Wave\Wave.exe -ArgumentList '--diagnostics','--live','dQw4w9WgXcQ','--output','live.json' -WorkingDirectory $PWD -Wait -PassThru -RedirectStandardOutput "$PWD\live.txt"
$p.ExitCode                                   # ожидается: 0
Get-Content live.txt -Encoding UTF8 | Select-String 'Проверка трека'
(Get-Content live.json -Raw -Encoding UTF8 | ConvertFrom-Json).live | Format-List ok, js_challenge_runs, js_runtime_processes, resolve_seconds, range_request, error_kind, error
```

Ожидается:
- `ok : True`;
- **`js_challenge_runs` ≥ 1** — yt-dlp действительно решал JS-задачи в упакованном Deno;
- `range_request` со `status=206`;
- `error_kind` и `error` пустые.

Если `ok = True`, но `js_challenge_runs = 0` — не считать PASS: приложить `live.json` и `wave.log`. Возможно, ответ пришёл из кэша или YouTube отдал формат без JS-задачи.

## 3. Воспроизведение, перемотка, следующий трек (WIN-6)

```powershell
Start-Process .\dist\Wave\Wave.exe
```

1. Окно открылось, главная загрузилась.
2. В поиске: `Daft Punk Get Lucky`. Включить трек с источником **YouTube Music** (подпись источника в плеере).
3. Трек звучит, позиция движется. Первый старт может занять 5–10 с.
4. Перемотать ползунок примерно в середину: звук продолжается с нового места, позиция идёт дальше.
5. Нажать «Следующий»: начинает играть следующий трек очереди.
6. Всё это время **не должно мелькать чёрное окно консоли**.

Пока окно открыто, проверить журнал:

```powershell
Select-String -Path $LOG -Encoding UTF8 -Pattern 'JS-runtime для yt-dlp'
# ожидается строка: ... INFO wave.jsruntime: JS-runtime для yt-dlp: deno 2.9.7 (bundled, <app>\runtime\deno\deno.exe)
Select-String -Path $LOG -Encoding UTF8 -Pattern 'Резолв yt:'          # время резолва треков
Select-String -Path $LOG -Encoding UTF8 -Pattern 'WARNING|ERROR' | Select-Object -Last 20   # для отчёта
```

## 4. Закрытие приложения (WIN-7)

Во время воспроизведения закрыть окно крестиком. Затем:

```powershell
Start-Sleep -Seconds 5
Get-Process Wave, deno -ErrorAction SilentlyContinue | Format-Table Name, Id   # ожидается: пусто
tasklist /fi "imagename eq deno.exe"                                           # ожидается: задач нет
tasklist /fi "imagename eq Wave.exe"                                           # ожидается: задач нет
```

Процессы `msedgewebview2.exe` могут завершаться на несколько секунд позже — они в эту проверку не входят.

## 5. Сообщение без runtime

```powershell
$NO = "$T\Wave-no-deno"
Copy-Item .\dist\Wave $NO -Recurse
Remove-Item "$NO\_internal\runtime\deno\deno.exe"
$env:XDG_CACHE_HOME = "$T\cache-no-deno"          # свежий кэш, чтобы не помогли прошлые решения
$p = Start-Process "$NO\Wave.exe" -ArgumentList '--diagnostics' -WorkingDirectory $PWD -Wait -PassThru -RedirectStandardOutput "$PWD\nodeno.txt"
$p.ExitCode                                       # ожидается: 1
Get-Content nodeno.txt -Encoding UTF8 | Select-String 'НЕ ГОТОВ|Переустановите'
Start-Process "$NO\Wave.exe"
```

В окне включить трек YouTube. Возможны два исхода:
- **ошибка** — тогда тост «Компонент воспроизведения YouTube (Deno) не найден или повреждён. Переустановите приложение.», а **не** «Трек недоступен»;
- **трек всё же играет** — урезанный режим yt-dlp без JS тоже сработал. Это не ошибка, но отметить в отчёте.

Закрыть окно. Затем вернуть кэш основной проверки:

```powershell
$env:XDG_CACHE_HOME = "$T\cache"
```

## 6. LIVE-2, LIVE-3, LIVE-4

```powershell
py -3.12 -m venv .venv
Set-ExecutionPolicy -Scope Process Bypass -Force       # только для этого окна, если активация запрещена
.\.venv\Scripts\Activate.ps1
python -m pip install -r backend\requirements.txt -r requirements-dev.txt -c constraints.txt
$env:WAVE_JS_RUNTIME = "$PWD\packaging\runtime\deno\deno.exe"     # Deno, скачанный сборкой
python -m pytest -m live -rP tests\live\test_live_youtube_runtime.py
```

Тесты сами берут временный профиль и чистый кэш. Ожидается `3 passed`, а в выводе `-rP` по строке JSON на тест:
- LIVE-2: `{"LIVE-2": "ok", "deno": "2.9.7", "source": "env", ..., "js_challenge_runs": N}`, где N ≥ 1;
- LIVE-3: `{"LIVE-3": "ok", "total_bytes": ..., "resolves": 2}`;
- LIVE-4 — фактическая деградация без runtime. Либо `"resolved_degraded"` с числом форматов, либо `"failed"` с `error_kind` и текстом для пользователя. Строку сохранить как есть: это и есть результат LIVE-4.

`FAILED … BLOCKED: нет доступа к YouTube` означает проблему сети, а не Wave.

## 7. Что прислать

```powershell
deactivate
Compress-Archive -Force -DestinationPath "$env:USERPROFILE\Desktop\wave-s3-check.zip" -Path `
  build\diagnostics.json, diag.json, diag.txt, live.json, live.txt, nodeno.txt, $LOG
```

К архиву приложить:
- вывод `pytest -rP` (текстом);
- таблицу ниже;
- скриншоты, если что-то пошло не так.

В отчётах нет cookies, ссылок на поток и токенов. В `wave.log` могут встречаться пути с именем пользователя — просмотрите перед отправкой.

| Пункт | PASS / FAIL | Заметки |
|---|---|---|
| 1. Сборка до «Диагностика … OK», хэш `deno.exe`, `--diagnostics` = 0 | | |
| 2. `--live`: `ok`, `js_challenge_runs` ≥ 1, Range 206 | | |
| 3. Старт, движение позиции, перемотка, следующий трек; нет окна консоли | | |
| 3. Строка `JS-runtime для yt-dlp: deno 2.9.7 (bundled, …)` в журнале | | |
| 4. После закрытия нет `Wave.exe` и `deno.exe` | | |
| 5. Без runtime: код 1 и понятное сообщение / трек играет в урезанном режиме | | |
| 6. LIVE-2 / LIVE-3 / LIVE-4 | | |

После результатов статус S3 в ROADMAP меняется на «принят» или фиксируются дефекты.
