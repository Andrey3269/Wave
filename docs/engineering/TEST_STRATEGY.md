# Стратегия тестирования «ВОЛНА»

Связано с [REPOSITORY_AUDIT.md](REPOSITORY_AUDIT.md), [TARGET_ARCHITECTURE.md](TARGET_ARCHITECTURE.md), [ROADMAP.md](ROADMAP.md).

## 1. Статусы результата

| Статус | Значение |
|---|---|
| **PASS** | команда фактически выполнена, код возврата 0, проверки прошли |
| **FAIL** | команда выполнена и обнаружила дефект |
| **BLOCKED** | не хватает окружения: Windows, WebView2, сеть к источникам, секрет |
| **NOT RUN** | не запускалось |

`compileall` и `node --check` не доказывают, что приложение работает. Проверка на Linux/Chromium не заменяет проверку на Windows/WebView2. В отчёте указывается, где именно выполнялась проверка.

## 2. Исходная точка (2026-10-12, коммит `5a7f27c`)

- `python -m compileall -q backend packaging tests` — PASS.
- `node --check frontend/app.js` — PASS.
- `tests/e2e_smoke.py` — PASS, **109 проверок** (102 вызова `check()`), 0 ошибок консоли. Linux, Chromium 141, изолированные каталоги данных.
- Unit, API и интеграционных тестов нет.
- Проверки на Windows и живых источниках — NOT RUN / BLOCKED.

### Состояние после S1 (2026-10-13, ветка `claude/s1-regression-tests`)

| Набор | Результат | Среда |
|---|---|---|
| `python -m pytest` (unit + API, 20 файлов) | **PASS**: 379 passed, 16 xfailed (strict, ID находок в §11), 0 failed; 4 live-теста не выбираются | Linux, Python 3.12.3; CI `ubuntu-latest` и `windows-latest`, Python 3.12 |
| `tests/e2e_smoke.py` через `run_e2e.py` | **PASS**: 109 проверок (102 вызова `check()`, файл не менялся), 0 ошибок консоли | Linux Chromium 141; CI Linux и Windows Chromium (не WebView2) |
| `python tests/run_checks.py` | **PASS** все шаги: hygiene, compileall, pyflakes, node, pytest, e2e, worktree | Linux; CI Linux и Windows |
| Сборка `dist\Wave` (PyInstaller, `packaging/wave.spec`) | **PASS** в CI `windows-build`: Wave.exe и frontend на месте, баз/журналов/cookies/медиа нет | CI `windows-latest`. Запуск окна, WebView2 и установщик — NOT RUN |
| `python -m pytest -m live` (LIVE-1, LIVE-6) | **BLOCKED**: прокси среды отвечает 403 для music.youtube.com и lrclib.net | облачная среда |

Запуски CI ветки: https://github.com/Andrey3269/Wave/actions?query=branch%3Aclaude%2Fs1-regression-tests (первый — run 38310405338 на `83f6c6b`, все job'ы зелёные)

**Правило монотонности.** Число проверок e2e не уменьшается. Изменение проверки допускается только вместе с изменением поведения, которое она проверяет, в том же коммите и с пояснением.

## 3. Изоляция данных (обязательно с этапа S0)

Тесты никогда не трогают `%LOCALAPPDATA%\Wave`, `~/.local/share/wave`, `~/Downloads/Wave`.

1. **pytest** (реализовано в S1: `tests/wavetest/guard.py`, `tests/conftest.py`):
   - **до** импорта любого модуля `backend` выставляет `WAVE_DATA_DIR`, `WAVE_DOWNLOADS_DIR` и «домашние» переменные `HOME`, `USERPROFILE`, `LOCALAPPDATA`, `APPDATA`, `XDG_*` во временный каталог сессии. Если тест сотрёт `WAVE_DATA_DIR`, запасной путь `paths.data_dir()` всё равно окажется во временном каталоге;
   - убирает из окружения секреты и прокси (`YTDLP_COOKIES`, `YTDLP_PROXY`, `*_PROXY`); прокси возвращаются только live-тестам;
   - аудит-хук Python (`sys.addaudithook`) блокирует запись файлов, `mkdir`, `rename`, `remove`, `rmtree` и `sqlite3.connect` вне системного временного каталога, а также любые сетевые соединения, кроме loopback. Нарушение записывается и роняет тест (ошибка в teardown), даже если код приложения перехватил исключение;
   - перед каждым тестом проверяет, что `paths.data_dir()` и `paths.downloads_dir()` указывают в каталог этого теста, иначе `pytest.exit(returncode=2)`;
   - каждый тест получает свой профиль (`wave_profile`: свои `data/` и `downloads/`) и сброшенное модульное состояние backend (кэши, задачи загрузок, `_resolve_sem`, соединение SQLite); фикстура `db` инициализирует `library`, `userdata`, `playlists`;
   - в конце сессии сверяет реальные каталоги пользователя (существование, размер и время изменения `wave.db`) с состоянием до запуска;
   - `api`/`harness`: приложение с подменёнными `YTMusic`, httpx-клиентом (`FakeNet` на `httpx.MockTransport`), резолвером yt-dlp и SoundCloud API; запрос к неизвестному хосту роняет тест;
   - самопроверки защиты: `tests/unit/test_isolation.py`.
2. **e2e** (`tests/e2e_smoke.py`):
   - перед первым действием запрашивает `GET /api/settings`;
   - продолжает только если `paths.data` содержит файл-метку `.wave-test-profile`, созданный раннером, либо лежит внутри `tempfile.gettempdir()`;
   - скриншоты по умолчанию пишет во временный каталог, а не в `tests/shots`.
3. **Раннер** `tests/run_e2e.py` (реализован в S0), одна команда:
   1. создаёт временный профиль и метку;
   2. запускает backend на свободном порту;
   3. ждёт `/api/health`;
   4. запускает e2e;
   5. останавливает backend и удаляет временные данные.
Пункты 2 и 3 реализованы в S0 (`tests/e2e_smoke.py:ensure_test_profile`, `tests/run_e2e.py`). Все проверки запускаются командой `python tests/run_checks.py`.

4. Тест, который должен удалять или очищать данные (история, загрузки, плейлисты), дополнительно проверяет, что работает на временном профиле.

## 4. Уровни и инструменты

| Уровень | Инструмент | Где | Новая зависимость |
|---|---|---|---|
| Статика | `python -m compileall`, `pyflakes`, `node --check` для каждого JS-модуля | любая ОС | `pyflakes` (dev) |
| Unit backend | `pytest` 9.1.1 | любая ОС | `pytest` (dev, S1) |
| API | `fastapi.testclient.TestClient` | любая ОС | нет (использует `httpx`) |
| Интеграция стрима | `TestClient` + `httpx.MockTransport` вместо CDN; резолвер yt-dlp подменяется фикстурой | любая ОС | нет |
| Unit frontend | `node --test` для `player/queue.js` и `core/*` | любая ОС | нет |
| e2e | Playwright + Chromium (`tests/e2e_smoke.py`) | Linux/Windows | `playwright` (dev) |
| e2e в WebView2 | ручной чек-лист (раздел 8); позже автоматизация через CDP WebView2 | только Windows | — |
| Сборка | `build_windows.bat`, `Wave.exe --diagnostics` | только Windows | PyInstaller, Inno Setup |
| Живые источники | опциональные тесты с маркером `live`, запуск вручную | машина с доступом к сети | нет |

Dev-зависимости живут в `requirements-dev.txt` и не попадают в сборку. Новые зависимости сверх перечисленных — только с обоснованием.

## 5. Набор тестов по модулям

Файлы создаются на этапе S1, если не указано иное. В скобках — находки, которые тест закрепляет.

| Файл | Что проверяет |
|---|---|
| `tests/unit/test_normalize_yt.py` | `_normalize_yt`: нет `videoId` → `None`; несколько артистов → `artists`, `artist_ids` по порядку; альбом строкой и словарём; длительность из `duration_seconds`, `"3:45"`, `"1:02:10"`, мусора; обложка `=w544-h544`; `stream_url` |
| `tests/unit/test_normalize_sc.py` | `_normalize_sc`: `policy` BLOCK/SNIP и `streamable=false` → `None`; миллисекунды → секунды; `-large.` → `-t500x500.`; `publisher_metadata.artist` приоритетнее `username`; `artist_id` — строка |
| `tests/unit/test_catalog_cards.py` | `_artist_card`, `_album_card` (год обрезается до 4 символов), `_sc_album`, дедупликация альбомов артиста |
| `tests/unit/test_ranges.py` | `RANGE_RE` и `_cap_range`: `bytes=a-`, `a-b` с обрезкой до 1 МБ, `-n` без изменений, `bytes=-` (дефект), битые значения; `_serve_from_head`: начало внутри или вне головы, `end < start`, корректный `Content-Range` (L2) |
| `tests/api/test_stream_proxy.py` | `stream_track` с `MockTransport`: 206 с проксированными `Content-Range`/`Content-Length`; без Range → 200; апстрим 416 → 416; апстрим 403 → перерезолв и успех со второй попытки; два 403 → 502; ошибка соединения → перерезолв; отмена клиента закрывает апстрим; параллельные запросы одного трека → **один** вызов резолвера; `?warm=1` → 202/204/503 |
| `tests/api/test_stream_local.py` | локальный файл: 200, 206 (`0-`, `a-b`, `-n`, `x-` до конца), 416, MIME по расширению; запись в индексе есть, а файл удалён → запись исчезает, ответ не 500; путь `../` в `folder` → отказ (`folder_path`) |
| `tests/api/test_stream_lifespan.py` | два последовательных lifespan в одном процессе, конкурентные запросы → без `RuntimeError` (M1; xfail до S2b) |
| `tests/api/test_search_isolation.py` | `yt` OK + `sc` падает → 200, `failed_sources=["sc"]`, результат не кэшируется; `sc` в cooldown не опрашивается; оба падают → 502; таймаут провайдера; пагинация `offset`/`has_more`; пустой и слишком длинный `q` → 422/400 |
| `tests/api/test_recommendations.py` | скоринг по таблице `W` на фиксированных данных; ограничение `MAX_PER_ARTIST`; исключение недавних и `exclude`; дубли по `_sig`; сбой всех кандидатов → сейчас `200 []` (M4: фиксируем, после S2c ждём `failed_sources`) |
| `tests/api/test_userdata.py` | избранное: добавить, повторить, удалить, порядок по `added_at`; история: `GROUP BY`, пагинация, `skip`, очистка обоими эндпоинтами; потеря `explicit`/`plays`/`released` (M6: фиксируем, после S6/S13 ждём сохранения) |
| `tests/api/test_settings.py` | текущие ключи, валидация, неизвестный ключ → 400, `downloads_dir`: относительный путь → 400, недоступный для записи → 400, `WAVE_DOWNLOADS_DIR` блокирует; после S2a — новые ключи и **сохранение между перезапусками backend** |
| `tests/api/test_playlists.py` | CRUD; дубль трека → `added: false`; позиции; переименование пустым именем → 400; удаление каскадом; конкурентное добавление одного трека не даёт 500 (M5: xfail до S6) |
| `tests/unit/test_db_migrations.py` (S6) | фикстура базы «как в 5a7f27c» и «старее, без `downloads.root`» → миграция до последней версии без потери строк; бэкап создан; повторный запуск идемпотентен; ошибка миграции → откат и исходная база цела |
| `tests/api/test_downloads.py` | старт → `jobs` → done (CDN через `MockTransport`); апстрим без Range (200); `Content-Range */*`; ошибка посередине → каталог `.part` удалён, индекс не тронут; сбой LRCLIB не отменяет загрузку (M8: xfail до S11); после S5 — 403 `download_not_allowed` для запрещённых источников |
| `tests/unit/test_lyrics.py` | `_parse_lrc`: таймкоды `mm:ss.xx`, пустые строки, без таймкодов; 404 → пустой результат; не-JSON → `None`, без исключения |
| `tests/unit/test_jsruntime.py` (S3) | порядок поиска (env → bundle → PATH); парсинг `deno --version`; версия ниже 2.3.0 → не поддерживается; таймаут проверки; `ydl_opts()` содержит `js_runtimes` с путём |
| `tests/unit/test_policy.py` (S5) | матрица возможностей; API соблюдает `download_allowed` и `mixing_allowed` |
| `tests/api/test_origin_guard.py` (S13) | мутирующий запрос с чужим `Origin` → 403; без `Origin` и со своим → OK; GET не затронут |
| `tests/js/queue.test.mjs` (S9) | модель очереди: add/next/prev/remove/move/clear, индекс текущего при перестановках, shuffle без повторов и с историей для «назад», repeat off/one/all, сериализация и восстановление |
| `tests/js/format.test.mjs` (S9) | `fmtTime`, `plural`, `thumb` |
| `tests/e2e_smoke.py` | существующие 109 проверок; расширения: сохранение настроек при смене порта (S2a), честные названия (S2e), сообщение о недоступном источнике (S2c), запрет скачивания (S5), клавиатурная навигация (S12) |

**Фикстуры провайдеров** (`tests/fixtures/ytmusic/*.json`, `tests/fixtures/soundcloud/*.json`):
- вручную составленные или обезличенные ответы;
- без cookies, токенов, `client_id` и реальных подписанных URL;
- для каждого файла указывается, откуда взята структура.

**Реализовано в S1** (число тестов pytest, включая параметризацию):

| Файл | Тестов | Сверх плана / примечание |
|---|---|---|
| `tests/unit/test_isolation.py` | 12 | самопроверка защиты (§3) |
| `tests/unit/test_normalize_yt.py` | 20 | + «радио» `get_watch_playlist` |
| `tests/unit/test_normalize_sc.py` | 10 | |
| `tests/unit/test_catalog_cards.py` | 18 | + `_check` |
| `tests/unit/test_ranges.py` | 48 | + `_extract_sync` (фильтр заголовков), `ydl_opts`, ошибки резолвера → HTTP, LRU/TTL головы |
| `tests/unit/test_sqlite_schema.py` | 10 | снимок схемы v0, старая база без `downloads.root` — заготовка для `test_db_migrations` (S6) |
| `tests/unit/test_lyrics.py` | 14 | |
| `tests/api/test_track_contract.py` | 9 | новый: поля Track, OpenAPI, форма скачанного трека |
| `tests/api/test_stream_proxy.py` | 38 | + обрыв клиентом в двух вариантах (ASGI 2.3, как uvicorn), обрыв CDN посередине |
| `tests/api/test_stream_local.py` | 40 | + имена папок для Windows, запись индекса вне каталога загрузок |
| `tests/api/test_stream_lifespan.py` | 6 | |
| `tests/api/test_search_isolation.py` | 31 | + медленный источник не блокирует SQLite-эндпоинты |
| `tests/api/test_catalog_api.py` | 20 | новый: страницы артиста и альбома, изоляция сбоев каталога |
| `tests/api/test_recommendations.py` | 17 | |
| `tests/api/test_userdata.py` | 22 | |
| `tests/api/test_settings.py` | 19 | |
| `tests/api/test_playlists.py` | 16 | |
| `tests/api/test_local_api_security.py` | 8 | новый: TrustedHost, CORS, xfail M7 |
| `tests/api/test_downloads.py` | 28 | |
| `tests/api/test_lyrics_api.py` | 9 | новый |
| `tests/live/test_live_sources.py` | 4 | LIVE-1, LIVE-6; цели в `tests/live_targets.json` |

Описание фикстур и их происхождение: `tests/fixtures/README.md`. Подделки внешнего мира: `tests/wavetest/fakes.py`.

## 6. Обязательные проверки по группам изменений

| Изменение | Минимум перед коммитом |
|---|---|
| Любое | compileall, pyflakes, `node --check`, `pytest -m "not live"`, e2e через раннер |
| Backend и API | + тесты затронутого модуля; совместимость ответа (старые поля на месте) |
| SQLite | + `test_db_migrations` на фикстурах старых схем; изолированный профиль |
| Стрим и плеер | + `test_ranges`, `test_stream_proxy`, `test_stream_local`; e2e: seek, pause/resume, переключение треков, ошибки, локальные файлы |
| Фронтенд | + `node --test tests/js`; e2e 109+; скриншоты 980/1366/1440/1920; клавиатура и фокус; нет горизонтального переполнения |
| Провайдеры | + `test_search_isolation`; live-канарейка вручную при изменении yt-dlp или ytmusicapi |
| Сборка | + Windows-чек-лист (раздел 8) |

## 7. Что требует живых музыкальных источников

Тесты с маркером `@pytest.mark.live`. Не запускаются по умолчанию и в CI. Запуск: `pytest -m live` на машине с сетью. Используются только публичные треки; идентификаторы хранятся в `tests/live_targets.json`. LIVE-1 и LIVE-6 реализованы в S1 (`tests/live/test_live_sources.py`); в облачной среде — BLOCKED (прокси отвечает 403), владелец запускает их у себя.

| ID | Проверка | Этап |
|---|---|---|
| LIVE-1 | `ytmusicapi`: поиск треков, артистов, альбомов возвращает непустые нормализованные данные | S1, далее при обновлениях |
| LIVE-2 | yt-dlp с `yt-dlp-ejs` и Deno: резолв публичного трека YouTube, `requested_formats` с прямым URL | S3 |
| LIVE-3 | Стрим YouTube: 206 на `bytes=0-`, повторная перемотка в середину, перерезолв после принудительной инвалидации | S3, S8 |
| LIVE-4 | Без JS-runtime: задокументировать фактическую деградацию (какие форматы и ошибки) | S3 |
| LIVE-5 | SoundCloud: поиск и стрим прогрессивного формата; поведение при отказе API | S5, S7 |
| LIVE-6 | LRCLIB: синхронный текст для известного трека | S1 |
| LIVE-7 | Рекомендации: радио трека YouTube и SoundCloud непустые | S14 |

## 8. Что требует Windows

Выполняется на чистой ВМ Windows 10 22H2 и Windows 11 x64 под обычным пользователем без прав администратора. Отдельно проверяется профиль с кириллицей в имени пользователя.

| ID | Проверка | Этап |
|---|---|---|
| WIN-1 | `build_windows.bat` на чистой машине: venv, PyInstaller, загрузка Deno со сверкой хэша, `Wave.exe --diagnostics` = 0, ISCC → `Wave-Setup-<версия>.exe` | S3, S4 |
| WIN-2 | Установка без прав администратора, запуск из «Пуска», окно открывается, `/api/health` доступен | S4 |
| WIN-3 | Нет WebView2 → инсталлятор ставит bootstrapper; при отказе от установки приложение показывает понятное сообщение | S4 |
| WIN-4 | Single-instance: второй запуск не создаёт второй backend | S4 (L5 — S17) |
| WIN-5 | Настройки (громкость, имя, тексты, тема) переживают перезапуск в WebView2; тема без мигания | S2a |
| WIN-6 | Воспроизведение YouTube в собранном приложении: старт, перемотка, следующий трек; в журнале версия runtime; нет мигающего окна консоли | S3 |
| WIN-7 | После закрытия приложения нет процессов `Wave.exe` и `deno.exe` | S3 |
| WIN-8 | Обновление поверх предыдущей версии: данные и загрузки сохранены, запущенное приложение корректно закрывается (`AppMutex`) | S4 |
| WIN-9 | Деинсталляция: программа удалена, `%LOCALAPPDATA%\Wave` и загрузки остались | S4 |
| WIN-10 | Офлайн и ограниченная сеть: старт, понятные статусы источников, скачанные треки играют | S2c, S7 |
| WIN-11 | Медиаклавиши, системный оверлей и Media Session в WebView2 | S10 |
| WIN-12 | Watchdog: принудительная остановка backend → перезапуск → воспроизведение продолжает работать | S2b |
| WIN-13 | e2e-тест против собранного backend (`Wave.exe` на фиксированном порту через `WAVE_PORT`, только для теста) | S4 |
| WIN-14 | Длинные пути и кириллица в каталоге загрузок | S11 |
| WIN-15 | Сборка не содержит пользовательских баз, логов, cookies, `.env`, `__pycache__` вне PyInstaller-архива | S4, S19 |

## 9. CI (решение владельца ADR-8; реализовано в S1: `.github/workflows/ci.yml`)

- Запускается на push в `main` и `claude/**`, на pull request и вручную. Секретов не использует, к живым источникам не обращается, права — только `contents: read`.
- `checks (ubuntu-latest)` и `checks (windows-latest)`: Python 3.12, установка с `constraints.txt`, Playwright Chromium, затем `python tests/run_checks.py` — hygiene, compileall, pyflakes, `node --check`, pytest, e2e через раннер, проверка неизменности рабочего каталога. Шаг успешен только при коде 0, то есть все шаги PASS (BLOCKED даёт код 3).
- `windows-build`: команды `build_windows.bat` без шага установщика — `svg_to_ico.py`, `PyInstaller packaging/wave.spec`; проверка, что в `dist\Wave` есть `Wave.exe` и `frontend\index.html`, а баз, журналов, `.env`, cookies и медиа нет. Это проверка spec, а не запуск приложения.
- Не входят в CI: окно WebView2, воспроизведение в собранном приложении, `--diagnostics` (появится в S3), Inno Setup (S4), live-тесты, ручной Windows-чек-лист §8. `node --test` появится вместе с JS-модулями (S9).
- Первый запуск: https://github.com/Andrey3269/Wave/actions/runs/38310405338 — все три job'а зелёные.

## 10. Формат отчёта по этапу

1. Изменённые файлы.
2. Изменённое поведение.
3. Таблица команд со статусами PASS/FAIL/BLOCKED/NOT RUN и средой (ОС, браузер или WebView2).
4. Число проверок e2e до и после.
5. Оставшиеся риски.
6. Следующий шаг.

## 11. Известные дефекты, закреплённые `xfail(strict=True)` (S1)

Тест описывает **правильное** поведение и сейчас падает по указанной причине (проверено запуском с `--runxfail`). Когда дефект исправят, тест начнёт проходить, `strict` превратит это в FAIL — значит, пора снять маркер в том же коммите, что и исправление.

| ID | Тест | Что происходит сейчас | Этап |
|---|---|---|---|
| H3 | `api/test_settings.py::test_ui_settings_are_persisted_by_backend` | `PUT /api/settings {"profile_name": …}` → 400 «Неизвестная настройка» | S2a |
| L2 | `api/test_stream_proxy.py::test_empty_range_is_not_forwarded` | `Range: bytes=-` уходит на CDN как есть | S8 |
| L2 | `api/test_stream_local.py::test_malformed_range_is_ignored` ×3 | битый Range на скачанном файле → 400 вместо 200 | S8 |
| L2 | `api/test_stream_local.py::test_head_request_supported` | `HEAD /api/stream/...` → 404 | S8 |
| M1 | `api/test_stream_lifespan.py::test_restart_after_contention_keeps_resolving` | после перезапуска приложения в том же процессе конкурентные резолвы → 500 (`[206, 206, 206, 206, 500, 500]`) | S2b |
| M3 | `api/test_search_isolation.py::test_youtube_failure_enters_cooldown` | YouTube опрашивается снова при каждом поиске после сбоя | S7 |
| M4 | `api/test_recommendations.py::test_all_candidate_sources_failed_is_reported` | сбой всех источников → `200 {"tracks": []}` без `failed_sources` | S2c |
| M5 | `api/test_playlists.py::test_concurrent_add_of_same_track` | гонка двух добавлений → `IntegrityError` (через API — 500) | S6 |
| M6 | `api/test_userdata.py::test_favorite_keeps_all_track_fields` | `explicit`, `plays`, `released` теряются при сохранении | S6 |
| M6 | `api/test_userdata.py::test_saved_favorite_can_always_be_removed` | запись с `/` в `id` сохраняется, удалить её нельзя | S6, S13 |
| M7 | `api/test_local_api_security.py::test_cross_origin_mutation_is_refused` | `POST /api/settings/clear-history` с чужим `Origin` выполняется | S13 |
| M8 | `unit/test_lyrics.py::test_non_json_response_is_temporary_failure` | не-JSON ответ LRCLIB → `JSONDecodeError` | S11 |
| M8 | `api/test_lyrics_api.py::test_non_json_service_response_is_handled` | то же через `/api/lyrics` → 500 | S11 |
| M8 | `api/test_downloads.py::test_broken_lyrics_response_does_not_fail_download` | загрузка падает на шаге `processing`, скачанное аудио удаляется | S11 |

Текущее поведение, которое изменится на этапах, закреплено обычными тестами с пометкой в docstring: M3 `test_youtube_is_not_put_in_cooldown`, M4 `test_all_candidate_sources_failed_returns_empty_200`, M5 `test_no_schema_version_yet`, M13 `personalized` в `test_radio_of_current_track`, L6 `test_network_failure_message_is_misleading`. При исправлении их меняют в том же коммите.

`tests/conftest.py:reset_backend_state` пересоздаёт `stream._resolve_sem` перед каждым тестом, чтобы M1 не ломал соседние тесты. Сам дефект воспроизводится в `test_stream_lifespan.py`, где сброса между перезапусками нет. После S2b сброс убирается.

## 12. Наблюдения S1, которых не было в аудите

Не дефекты высокого приоритета; закреплены тестами «как есть» и требуют решения на указанном этапе.

| ID | Наблюдение | Тест | Предлагаемый этап |
|---|---|---|---|
| N1 | `PUT /api/settings` применяет ключи по очереди: ключи до ошибочного уже сохранены, хотя ответ 400 | `api/test_settings.py::test_partial_write_before_error` | S2a (сделать запись атомарной) |
| N2 | При обрыве CDN посередине ответа прокси теряет уже полученные, но ещё не отданные байты (читает кусками по 64 КБ). Плеер видит короткое тело и перезапрашивает диапазон | `api/test_stream_proxy.py::test_upstream_break_mid_stream_ends_response` | S8 |
| N3 | Запись индекса загрузок с `folder`, выходящим за каталог загрузок (`../`), даёт HTTP 500: чужой файл не отдаётся, но ответ — ошибка сервера, а не отказ | `api/test_stream_local.py::test_index_row_outside_downloads_is_not_served` | S6 (фильтрация невалидных строк при чтении) |

