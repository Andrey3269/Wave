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
| Python | разработка и тесты; сборка | ≥ 3.10 для разработки; **сборка — только CPython 3.12 x64** (под неё закреплён `packaging/requirements-build.lock`) |
| Node.js | только проверка синтаксиса фронтенда | любая актуальная LTS |
| Deno | воспроизведение YouTube при запуске из исходников (в сборку входит свой) | ≥ 2.3.0; закреплён 2.9.7 (`packaging/runtime/deno.lock`) |
| Inno Setup 6 | сборка установщика | 6.x, проверено с **6.7.3** (Inno Setup 7 не используется) |

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

Лаунчер запускает backend на свободном порту `127.0.0.1`, ждёт `/api/health` и открывает окно. Журнал пишется в `<каталог данных>\logs\wave.log`. Для тестов собранного приложения порт можно закрепить переменной `WAVE_PORT` (см. таблицу ниже).

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

## Установка (Windows 10/11 x64)

`Wave-Setup-<версия>.exe` ставит ВОЛНУ **для текущего пользователя, без прав администратора**, в `%LOCALAPPDATA%\Programs\Wave`, создаёт ярлык в меню «Пуск» (на рабочем столе — по желанию) и запись в «Установленных приложениях».

- **WebView2.** Если Microsoft Edge WebView2 Runtime нет (в Windows 11 он встроен), установщик запускает официальный загрузчик Microsoft и ждёт его; без интернета или при ошибке он объясняет, что делать, и не предлагает запустить ВОЛНУ. Тихая установка в этом случае завершается кодом 100.
- **Обновление** — запуском установщика новой версии поверх: если ВОЛНА открыта, установщик попросит её закрыть. Файлы программы заменяются, данные остаются.
- **Удаление** — «Параметры → Приложения → ВОЛНА (Wave)». Удаляются только программа и ярлыки; база, настройки, история (`%LOCALAPPDATA%\Wave`) и папка загрузок остаются. Открытую ВОЛНУ деинсталлятор тоже попросит закрыть.
- В тихом режиме (`/VERYSILENT /SUPPRESSMSGBOXES`) при открытой ВОЛНЕ установка и удаление отказываются с кодом 1 и ничего не меняют.
- **Подписи кода пока нет**, поэтому Windows SmartScreen может предупредить о неизвестном издателе («Подробнее» → «Выполнить в любом случае»). `Wave.exe` и `deno.exe` тоже не подписаны нашим сертификатом.
- Тихая установка для администраторов и сценариев: `Wave-Setup-<версия>.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART [/TASKS="desktopicon"] [/LOG="setup.log"]`.

## Сборка для Windows

```bat
build_windows.bat
```

Нужны Python 3.12 x64 (через py launcher; если его нет — `set WAVE_BUILD_PYTHON=C:\путь\к\python.exe` 3.12), Inno Setup 6 и интернет на время сборки. Права администратора не нужны. Скрипт работает из любой текущей папки, пишет только в `.venv-build\`, `build\` и `dist\` репозитория и при любой ошибке завершается с кодом 1. Шаги:

1. создаёт `build\`, `build\webview2\`, `dist\installer\`;
2. каждый раз заново создаёт `.venv-build` на Python 3.12 x64;
3. ставит **только** `packaging\requirements-build.lock` (`pip install --require-hashes --no-deps`): все пакеты приложения и сборки для Windows x64 закреплены вместе с хэшами; `pip check` и `packaging\verify_build_env.py` сверяют окружение с локом пакет в пакет;
4. скачивает закреплённый Deno (`packaging\fetch_runtime.py`: официальный архив из `packaging\runtime\deno.lock`, сверка SHA-256 до распаковки; повторная сборка заново не скачивает);
5. рисует иконку и собирает `dist\Wave\` через PyInstaller (`packaging\wave.spec`, версия файла `Wave.exe` — из `backend\version.py`);
6. проверяет собранное приложение командой `Wave.exe --diagnostics` (временный профиль `build\diagnostics-profile`, ваш `%LOCALAPPDATA%\Wave` не затрагивается) и останавливается при ненулевом коде;
7. сверяет версию `Wave.exe` с `backend\version.py`;
8. скачивает официальный загрузчик WebView2 (`packaging\fetch_webview2.ps1`) и принимает его только с действительной подписью Microsoft (закреплённого SHA-256 у этого файла быть не может: Microsoft обновляет его по той же ссылке);
9. находит Inno Setup 6 (переменная `ISCC`, `PATH`, стандартные папки установки «для всех» и «для меня») и компилирует `installer\wave.iss` с `/DAppVersion=<версия>`.

Результат: `dist\installer\Wave-Setup-<версия>.exe`, внутри — вся папка `dist\Wave`. Версия задаётся только в `backend\version.py` (`python backend\version.py` печатает её); установщик при компиляции сверяет её с версией в `Wave.exe`.

В `dist\Wave\_internal\` попадают `runtime\deno\deno.exe` (около 93 МБ), `runtime\deno.lock` и `licenses\`: `THIRD_PARTY_NOTICES.txt` (он же устанавливается рядом с `Wave.exe`), перечни и тексты лицензий Rust- и C/C++-компонентов Deno, Python-пакетов сборки (формируется при сборке) и Microsoft WebView2 SDK.

**Подпись (необязательно).** Если задана переменная `WAVE_SIGN_COMMAND` — команда подписи в формате Inno Setup `SignTool` (файл обозначается `$f`, кавычки — `$q`), установщик, деинсталлятор и `Wave.exe` в нём подписываются. Без переменной сборка идёт без подписи. Сертификаты и пароли в репозиторий и CI не попадают. Этот путь пока не проверялся (сертификата нет).

**Обновление зависимостей сборки** — отдельным коммитом: поменять версии в `packaging\requirements-build.txt` (или `constraints.txt`), перегенерировать лок командой из его шапки (`uv pip compile … --python-platform x86_64-pc-windows-msvc --python-version 3.12 --generate-hashes`), прогнать CI и ручной чек-лист S4.

**Диагностика собранного приложения** (без окна и без сети; проверяет конфигурацию, а не воспроизведение):

```bat
start /wait dist\Wave\Wave.exe --diagnostics --output diag.json
echo %errorlevel%
```

Код возврата 0 — компоненты в порядке, 1 — критическая проблема (подробности в `diag.json` и в журнале). `Wave.exe` — оконная программа, поэтому в cmd нужен `start /wait`; в PowerShell: `Start-Process .\dist\Wave\Wave.exe -ArgumentList '--diagnostics --output diag.json' -Wait -PassThru`. Текстовый отчёт (UTF-8) печатается в консоль или в перенаправленный вывод, `--json` — в JSON; для сценариев надёжнее `--output` и код возврата. Проверка настоящего трека (нужна сеть): `--live <videoId>`, ссылка на поток в отчёт не попадает. Тот же отчёт без сети отдаёт `GET /api/diagnostics`. Версию приложения показывают отчёт, `GET /api/health` и свойства файла `Wave.exe`.

**Проверка установщика.** CI (`windows-build`) выполняет `build_windows.bat` целиком и затем `tests\installer\run_installer_checks.ps1`: установка, запуск на `WAVE_PORT` с e2e-тестом, второй запуск, обновление, удаление, сценарии без WebView2. Ручная проверка на вашей машине — [checklists/S4_WINDOWS_CHECK.md](docs/engineering/checklists/S4_WINDOWS_CHECK.md); скрипт проверок CI на рабочем компьютере не запускайте (он ставит и удаляет ВОЛНУ в текущем профиле).

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
| `WAVE_PORT` | — (свободный порт) | **только для тестов собранного приложения**: фиксированный порт backend 1024–65535 на `127.0.0.1`. Неверное значение или занятый порт — ошибка запуска, а не другой порт |
| `WAVE_BUILD_PYTHON` | — (`py -3.12`) | сборка: путь к `python.exe` 3.12 x64, если нет py launcher |
| `ISCC` | — (поиск) | сборка: путь к `ISCC.exe` Inno Setup 6 |
| `WAVE_SIGN_COMMAND` | — | сборка: команда подписи кода (см. «Сборка для Windows»); без неё подписи нет |
| `WAVE_JS_RUNTIME` | — | путь к Deno (файл или папка). Если задан, используется только он; иначе — упакованный runtime, а при запуске из исходников ещё `deno` из `PATH`. Кэш Deno — `<каталог данных>\deno` |

## Источники музыки и ограничения

- **YouTube Music** — каталог через неофициальную библиотеку `ytmusicapi`, аудио через `yt-dlp`.
- **SoundCloud** — через внутренние методы `yt-dlp` и неофициальный API v2.

У обоих источников есть условия использования, которые ограничивают автоматизированный доступ и сохранение контента. Подробности — в разделе 5 [аудита](docs/engineering/REPOSITORY_AUDIT.md).

Скачивание (S5, ADR-10 в [TARGET_ARCHITECTURE.md](docs/engineering/TARGET_ARCHITECTURE.md) §6): трек сохраняется только если источник сам разрешил это для конкретного трека. Проверка выполняется на backend до загрузки; возможность воспроизведения разрешением не считается.
- **YouTube** — только видео под лицензией Creative Commons. У официальных релизов YouTube Music такой лицензии нет, поэтому для них скачивание недоступно.
- **SoundCloud** — только если автор включил скачивание (`downloadable`); сохраняется исходный файл автора. Без входа в аккаунт SoundCloud файл не выдаёт, а вход в Wave не поддерживается, поэтому сейчас такие попытки заканчиваются понятным отказом.
- Если скачать нельзя, приложение показывает причину вместо бесконечного прогресса.

Уже скачанные файлы остаются и воспроизводятся с диска без обращения к источнику. Выключение потокового воспроизведения SoundCloud (ADR-6) не реализовано и ждёт решения владельца. Импорт собственных локальных файлов запланирован на этап S15.

Интеграций Spotify и Яндекс Музыки нет и не будет без официального разрешённого способа доступа.

## Структура

```
backend/      FastAPI-приложение, лаунчер, провайдеры, стриминг, SQLite
frontend/     index.html, style.css, app.js, assets/
tests/        unit/, api/ (pytest), live/ (живые источники), fixtures/, wavetest/ (защита и подделки),
              e2e_smoke.py, e2e_settings.py (Playwright), run_e2e.py, run_checks.py
              installer/run_installer_checks.ps1 (проверки установщика в CI)
.github/      workflows/ci.yml (GitHub Actions: Linux и Windows, сборка и проверка установщика)
installer/    wave.iss (Inno Setup 6)
packaging/    wave.spec (PyInstaller), requirements-build.txt и requirements-build.lock (хэш-лок сборки),
              fetch_runtime.py и runtime/deno.lock (Deno), fetch_webview2.ps1, verify_build_env.py,
              python_notices.py и deno_notices.py (перечни лицензий), иконки
licenses/     лицензии компонентов сборки (Deno и его Rust/V8-части, yt-dlp, yt-dlp-ejs, WebView2 SDK),
              THIRD_PARTY_NOTICES.txt
docs/         инженерная документация
```

## Правила работы с репозиторием

- Изменения ведутся в отдельных ветках; `main` меняется только после согласования.
- Один логический шаг — один коммит. Перед коммитом выполняется `python tests/run_checks.py`.
- Не коммитьте `.env`, cookies, токены, пользовательские базы, журналы, скачанную музыку, окружения и результаты сборки. Список — в `.gitignore`; шаг `hygiene` раннера проверяет это автоматически.
- Инструкции для ассистента разработки — [CLAUDE.md](CLAUDE.md).
