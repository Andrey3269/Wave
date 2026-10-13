# ВОЛНА (Wave)

Настольный музыкальный клиент для Windows: окно на Microsoft Edge WebView2 (pywebview), встроенный локальный сервер FastAPI/uvicorn, интерфейс на HTML/CSS/JavaScript без фреймворков, пользовательские данные в SQLite.

Проект в активной инженерной доработке. Текущее состояние, известные ограничения и план описаны в [`docs/engineering/`](docs/engineering/):

- [аудит](docs/engineering/REPOSITORY_AUDIT.md);
- [продуктовый анализ](docs/engineering/PRODUCT_GAP_ANALYSIS.md);
- [целевая архитектура](docs/engineering/TARGET_ARCHITECTURE.md);
- [стратегия тестов](docs/engineering/TEST_STRATEGY.md);
- [дорожная карта](docs/engineering/ROADMAP.md).

## Требования

| Что | Для чего | Версия |
|---|---|---|
| Windows 11 x64 (основная платформа), Windows 10 22H2 (совместимость проверяется) | запуск приложения | — |
| Microsoft Edge WebView2 Runtime | окно приложения | актуальная |
| Python | разработка и тесты | ≥ 3.10; **сборка выпусков — 3.12** |
| Node.js | только проверка синтаксиса фронтенда | любая актуальная LTS |
| Deno | воспроизведение YouTube при запуске из исходников (в сборку входит свой) | ≥ 2.3.0; закреплён 2.9.7 (`packaging/runtime/deno.lock`) |
| Inno Setup 6 | сборка установщика | 6.x |

Разработку backend и тесты можно вести и на Linux или macOS. Окно приложения (`launcher.py`) рассчитано на Windows.

## Установка для разработки

Windows (PowerShell или cmd):

```bat
py -3.12 -m venv .venv
.venv\Scripts\activate
pip install -r backend\requirements.txt -r requirements-dev.txt -c constraints.txt
python -m playwright install chromium
```

Linux / macOS:

```sh
python3.12 -m venv .venv
. .venv/bin/activate
pip install -r backend/requirements.txt -r requirements-dev.txt -c constraints.txt
python -m playwright install chromium
```

`constraints.txt` закрепляет проверенные версии всех пакетов. Устанавливайте зависимости всегда с `-c constraints.txt`, тогда у всех окажется один и тот же набор. Обновление версий делается отдельным коммитом; порядок описан в заголовке `constraints.txt`.

**JS-runtime для YouTube.** yt-dlp решает JS-задачи плеера YouTube во внешнем runtime; Wave использует Deno ≥ 2.3.0 (`backend/jsruntime.py`). Для запуска из исходников подойдёт установленный Deno из `PATH` либо закреплённая версия, подготовленная скриптом сборки:

```bat
python packaging\fetch_runtime.py --platform windows-x86_64
set WAVE_JS_RUNTIME=%CD%\packaging\runtime\deno\deno.exe
```

(на Linux: `--platform linux-x86_64` и `export WAVE_JS_RUNTIME=$PWD/packaging/runtime/deno/deno`). Без runtime YouTube работает в урезанном режиме, а ошибка воспроизведения называет причину. Проверить: `python backend/launcher.py --diagnostics`.

## Запуск в разработке

> **Важно.** Без переменной `WAVE_DATA_DIR` приложение работает с вашим настоящим профилем: `%LOCALAPPDATA%\Wave` на Windows, `~/.local/share/wave` на Linux. Для экспериментов задайте отдельный каталог.

Только backend и интерфейс в обычном браузере:

```bat
set WAVE_DATA_DIR=%USERPROFILE%\wave-dev\data
set WAVE_DOWNLOADS_DIR=%USERPROFILE%\wave-dev\downloads
python -m uvicorn main:app --app-dir backend --host 127.0.0.1 --port 8765
```

Затем откройте <http://127.0.0.1:8765>. Документация API: <http://127.0.0.1:8765/docs>.

Полноценное окно приложения (Windows, нужен pywebview из зависимостей сборки):

```bat
pip install -r packaging\requirements-build.txt -c constraints.txt
python backend\launcher.py
```

Лаунчер запускает backend на свободном порту `127.0.0.1`, ждёт `/api/health` и открывает окно. Журнал пишется в `<каталог данных>\logs\wave.log`.

## Тесты и проверки

Все проверки одной командой:

```sh
python tests/run_checks.py            # гигиена git, compileall, pyflakes, node --check, pytest, e2e
python tests/run_checks.py --no-e2e   # без браузерного теста
```

Только unit- и API-тесты (pytest, без сети, на временном профиле):

```sh
python -m pytest                      # живые источники выключены
python -m pytest tests/api/test_stream_proxy.py -q
python -m pytest -m live              # живые источники (LIVE-1, LIVE-6): нужен доступ в сеть
```

Тесты с пометкой `xfail` закрепляют известные дефекты из аудита (идентификатор в причине) и исправляются на своих этапах дорожной карты. Список — в [TEST_STRATEGY.md](docs/engineering/TEST_STRATEGY.md) §11.

Только браузерный тест:

```sh
python tests/run_e2e.py               # одноразовый профиль, свободный порт, уборка после теста
python tests/run_e2e.py --keep --shots shots-out   # оставить профиль и сохранить скриншоты в папку
```

`run_e2e.py` прогоняет `e2e_smoke.py`, затем `e2e_settings.py`: настройки интерфейса выставляются на одном порту, backend перезапускается на другом порту с тем же профилем, и значения проверяются в новом окне (в приложении порт меняется при каждом запуске, а с ним и localStorage окна). Отдельный чистый профиль проверяет однократный перенос старых значений из localStorage.

Статусы шагов:

| Статус | Значение |
|---|---|
| `PASS` | шаг выполнен и прошёл |
| `FAIL` | шаг выполнен и нашёл дефект |
| `BLOCKED` | нет нужного инструмента: pytest, Playwright, Chromium, Node, git |
| `NOT RUN` | шаг пропущен флагом |

Коды выхода: `0` — всё прошло, `1` — есть FAIL, `3` — есть BLOCKED.

Защита данных: e2e-тест меняет избранное, историю, плейлисты и настройки. Поэтому до первого действия он проверяет через `/api/settings`, что backend работает на тестовом профиле: каталоги лежат во временной папке системы или содержат файл `.wave-test-profile`. Иначе тест останавливается с кодом 2 и ничего не меняет. `run_e2e.py` создаёт такой профиль сам. Секреты из окружения (`YTDLP_COOKIES`, `YTDLP_PROXY`) тестовому backend не передаются.

pytest защищён сильнее: до импорта backend каталоги данных, загрузок и «домашний» каталог указывают во временную папку, а аудит-хук Python блокирует запись файлов и SQLite вне временной папки и любые сетевые соединения, кроме loopback. Нарушение роняет тест, даже если код приложения перехватил исключение (`tests/wavetest/guard.py`).

Внешние источники (YouTube Music, SoundCloud, LRCLIB, обложки, аудио) в e2e и pytest подменены. Проверки на Windows/WebView2 и на живых источниках описаны в [TEST_STRATEGY.md](docs/engineering/TEST_STRATEGY.md) §7–8 и выполняются отдельно.

## Сборка для Windows

```bat
build_windows.bat
```

Скрипт создаёт `.venv-build`, ставит зависимости с `constraints.txt`, скачивает закреплённый Deno (`packaging\fetch_runtime.py`: официальный архив из `packaging\runtime\deno.lock`, сверка SHA-256 до распаковки, без совпадения хэша сборка останавливается), рисует иконку, собирает `dist\Wave\` через PyInstaller (`packaging\wave.spec`), проверяет собранное приложение командой `Wave.exe --diagnostics` и затем запускает Inno Setup. Интернет нужен только на время сборки; повторная сборка Deno заново не скачивает.

В `dist\Wave\_internal\` попадают `runtime\deno\deno.exe` (около 93 МБ), `runtime\deno.lock` и `licenses\` (тексты лицензий, `THIRD_PARTY_NOTICES.txt`).

**Диагностика собранного приложения** (без окна и без сети; проверяет конфигурацию, а не воспроизведение):

```bat
start /wait dist\Wave\Wave.exe --diagnostics --output diag.json
echo %errorlevel%
```

Код возврата 0 — компоненты в порядке, 1 — критическая проблема (подробности в `diag.json` и в журнале). `Wave.exe` — оконная программа, поэтому в cmd нужен `start /wait`; в PowerShell: `Start-Process .\dist\Wave\Wave.exe -ArgumentList '--diagnostics --output diag.json' -Wait -PassThru`. Текстовый отчёт (UTF-8) печатается в консоль или в перенаправленный вывод, `--json` — в JSON; для сценариев надёжнее `--output` и код возврата. Проверка настоящего трека (нужна сеть): `--live <videoId>`, ссылка на поток в отчёт не попадает. Тот же отчёт без сети отдаёт `GET /api/diagnostics`.

**Известное ограничение (этап S4 дорожной карты).** Скрипта установщика `installer\wave.iss` в репозитории нет, поэтому шаг установщика завершается ошибкой. Папка `dist\Wave\` к этому моменту уже собрана и проверена диагностикой.

## Каталоги и переменные окружения

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `WAVE_DATA_DIR` | `%LOCALAPPDATA%\Wave` (Windows), `~/.local/share/wave` | база `wave.db`, журналы, кэш окна |
| `WAVE_DOWNLOADS_DIR` | `~/Downloads/Wave` или папка из настроек | скачанные треки; если задана, папку нельзя сменить в настройках |
| `WAVE_DEBUG` | — | любое непустое значение включает инструменты разработчика в окне |
| `SEARCH_SOURCES` | `yt,sc` | какие источники поиска включены |
| `SEARCH_TIMEOUT` | `6` | таймаут одного источника поиска, с |
| `SOURCE_COOLDOWN` | `300` | пауза после сбоя необязательного источника, с |
| `SEARCH_CACHE_TTL` | `300` | время жизни кэша поиска, с |
| `STREAM_CHUNK_KB` | `1024` | размер порции аудио в ответе на Range-запрос |
| `ALLOWED_ORIGINS` | dev-адреса localhost | CORS для разработки фронтенда отдельным сервером |
| `YTDLP_COOKIES` | — | путь к cookies.txt для yt-dlp. **Секрет: не коммитить и не прикладывать к отчётам** |
| `YTDLP_PROXY` | — | прокси для yt-dlp и аудио. Может содержать пароль: не коммитить |
| `WAVE_JS_RUNTIME` | — | путь к Deno (файл или папка). Если задан, используется только он; иначе — упакованный runtime, а при запуске из исходников ещё `deno` из `PATH`. Кэш Deno — `<каталог данных>\deno` |

## Источники музыки и ограничения

- **YouTube Music** — каталог через неофициальную библиотеку `ytmusicapi`, аудио через `yt-dlp`.
- **SoundCloud** — через внутренние методы `yt-dlp` и неофициальный API v2.

У обоих источников есть условия использования, которые ограничивают автоматизированный доступ и сохранение контента. Подробности — в разделе 5 [аудита](docs/engineering/REPOSITORY_AUDIT.md).

Решения владельца (ADR-5, ADR-6 в [TARGET_ARCHITECTURE.md](docs/engineering/TARGET_ARCHITECTURE.md)) будут реализованы на этапе S5:
- в распространяемой версии новые загрузки с YouTube и SoundCloud отключаются;
- потоковое воспроизведение SoundCloud выключается до подтверждения допустимости.

**В текущем коде эти функции пока работают как раньше.** Уже скачанные файлы останутся и продолжат воспроизводиться. Импорт собственных локальных файлов запланирован на этап S15.

Интеграций Spotify и Яндекс Музыки нет и не будет без официального разрешённого способа доступа.

## Структура

```
backend/      FastAPI-приложение, лаунчер, провайдеры, стриминг, SQLite
frontend/     index.html, style.css, app.js, assets/
tests/        unit/, api/ (pytest), live/ (живые источники), fixtures/, wavetest/ (защита и подделки),
              e2e_smoke.py, e2e_settings.py (Playwright), run_e2e.py, run_checks.py
.github/      workflows/ci.yml (GitHub Actions: Linux и Windows)
packaging/    wave.spec (PyInstaller), fetch_runtime.py и runtime/deno.lock (Deno для сборки), иконки, зависимости сборки
licenses/     лицензии компонентов, входящих в сборку (Deno, V8, yt-dlp, yt-dlp-ejs), THIRD_PARTY_NOTICES.txt
docs/         инженерная документация
```

## Правила работы с репозиторием

- Изменения ведутся в отдельных ветках; `main` меняется только после согласования.
- Один логический шаг — один коммит. Перед коммитом выполняется `python tests/run_checks.py`.
- Не коммитьте `.env`, cookies, токены, пользовательские базы, журналы, скачанную музыку, окружения и результаты сборки. Список — в `.gitignore`; шаг `hygiene` раннера проверяет это автоматически.
- Инструкции для ассистента разработки — [CLAUDE.md](CLAUDE.md).
