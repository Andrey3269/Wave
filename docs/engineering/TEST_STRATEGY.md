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

### Состояние после S2 (2026-10-13, ветка `claude/s2-p0-fixes`)

| Набор | Результат | Среда |
|---|---|---|
| `python -m pytest` | **PASS**: 469 passed, 13 xfailed (strict; сняты H3, M1, M4), 0 failed; 4 live-теста не выбираются | Linux, Python 3.12.3; CI `ubuntu-latest`, `windows-latest` |
| `tests/e2e_smoke.py` | **PASS**: 122 проверки (было 109; все прежние сохранены; у трёх обновлены только подписи вместе с названиями S2e), 0 ошибок консоли | Linux Chromium 141; CI Linux и Windows Chromium (не WebView2) |
| `tests/e2e_settings.py` (новый, S2a) | **PASS**: 24 проверки в трёх запусках backend — `set` на порту A, `verify` после перезапуска на порту B с тем же профилем, `migrate` на чистом профиле. После `claude/s2-e2e-stability` — 27: `set` ждёт фактическую запись (до 10 с) вместо паузы 900 мс, печатает фактические значения при провале и детерминированно проверяет запоздавшие ответы `/api/settings` (reports/S2.md §9) | то же |
| `python tests/run_checks.py` | **PASS** все шаги | Linux; CI Linux и Windows |
| WIN-5 (настройки в WebView2), WIN-12 (сторож) | **NOT RUN** | нужна Windows-машина |

### Состояние после S3 (2026-10-13, ветка `claude/s3-ytdlp-deno`)

| Набор | Результат | Среда |
|---|---|---|
| `python -m pytest` | **PASS**: 589 тестов (было 482). Linux: 574 passed + 2 skipped (только Windows) + 13 xfail; Windows: 576 passed, 0 skipped + 13 xfail. Тесты с настоящим Deno 2.9.7 (`test_jsruntime_real.py`) выполнены на обеих ОС; Job Object и отсутствие окна консоли — на Windows | Linux, Python 3.12.3; CI `ubuntu-latest`, `windows-latest` (итог pytest — аннотацией) |
| `tests/e2e_smoke.py` + `e2e_settings.py` | **PASS**: 123 (было 122; + сообщение об отсутствии компонента воспроизведения YouTube) + 27, 0 ошибок консоли | Linux Chromium; CI Linux и Windows Chromium (не WebView2) |
| `python tests/run_checks.py` | **PASS** все шаги | Linux; CI Linux и Windows |
| `windows-build`: Deno по lock со сверкой SHA-256, PyInstaller, состав папки, `Wave.exe --diagnostics` | **PASS**: `deno 2.9.7 supported=true`, путь с кириллицей, без runtime → код 1 с понятным сообщением и без системного Deno, `deno.exe` не остаётся; `dist\Wave` 147,1 МБ (Deno 92,9 МБ) | CI `windows-latest` (без WebView2) |
| LIVE-2, LIVE-3, LIVE-4 | **BLOCKED** в облачной среде (прокси отвечает 403); **PASS** у владельца на Windows 11 x64 при приёмке S3 (reports/S3.md §13) | облачная среда; Windows 11 x64 владельца |
| WIN-6, WIN-7 | **PASS** у владельца при приёмке S3 (воспроизведение, перемотка, следующий трек, нет окна консоли, `deno.exe` завершается) | Windows 11 x64 владельца |
| WIN-1 (полный, с установщиком) | **NOT RUN** на S3 — этап S4 | — |

### Состояние после S4 (2026-10-13, ветка `claude/s4-installer`, подробности — [reports/S4.md](reports/S4.md))

| Набор | Результат | Среда |
|---|---|---|
| `python -m pytest` | **PASS**: 680 тестов (было 589). Linux: 665 passed + 2 skipped (только Windows) + 13 xfail; Windows: 667 passed, 0 skipped + 13 xfail | CI `ubuntu-latest`, `windows-latest` (run 38388874628); локально Linux |
| `tests/e2e_smoke.py` + `e2e_settings.py` | **PASS**: 123 + 27 (не изменилось) | локально Chromium; CI Linux и Windows Chromium |
| `build_windows.bat` целиком (WIN-1) | **PASS**: лок сборки (43 пакета), Deno 2.9.7, PyInstaller, `--diagnostics`, WebView2 bootstrapper (подпись Microsoft), Inno Setup 6.7.3 → `Wave-Setup-0.1.0.exe` 53,0 МБ; `dist\Wave` 147,1 МБ | CI `windows-latest` |
| `tests/installer/run_installer_checks.ps1` (WIN-2, 3, 4, 8, 9, 13, 15) | **PASS** все 17 проверок, в том числе установка под стандартной учётной записью и `e2e_smoke.py` против установленного `Wave.exe` (123 проверки) | CI `windows-latest` (окна не видны) |
| Ручной чек-лист [checklists/S4_WINDOWS_CHECK.md](checklists/S4_WINDOWS_CHECK.md): сборка, интерактивная установка, «Пуск», воспроизведение, второй запуск, обновление, удаление, сообщения установщика | **PASS**: все ручные проверки пройдены, `build_windows.bat` — код 0, установщик 54 974 824 байта, версия 0.1.0 (подтверждение владельца при приёмке S4 2026-10-13; результаты по отдельным пунктам в репозиторий не вносились). Машина без встроенного WebView2 (п. 8б) в подтверждении отдельно не указана | Windows 11 x64 владельца |

**Правило монотонности.** Число проверок e2e не уменьшается. Изменение проверки допускается только вместе с изменением поведения, которое она проверяет, в том же коммите и с пояснением.

## 3. Изоляция данных (обязательно с этапа S0)

Тесты никогда не трогают `%LOCALAPPDATA%\Wave`, `~/.local/share/wave`, `~/Downloads/Wave`.

1. **pytest** (реализовано в S1: `tests/wavetest/guard.py`, `tests/conftest.py`):
   - **до** импорта любого модуля `backend` выставляет `WAVE_DATA_DIR`, `WAVE_DOWNLOADS_DIR` и «домашние» переменные `HOME`, `USERPROFILE`, `LOCALAPPDATA`, `APPDATA`, `XDG_*` во временный каталог сессии. Если тест сотрёт `WAVE_DATA_DIR`, запасной путь `paths.data_dir()` всё равно окажется во временном каталоге;
   - убирает из окружения секреты и прокси (`YTDLP_COOKIES`, `YTDLP_PROXY`, `*_PROXY`); прокси возвращаются только live-тестам;
   - аудит-хук Python (`sys.addaudithook`) блокирует запись файлов, `mkdir`, `rename`, `remove`, `rmtree` и `sqlite3.connect` вне системного временного каталога, а также любые сетевые соединения, кроме loopback. Нарушение записывается и роняет тест (ошибка в teardown), даже если код приложения перехватил исключение;
   - перед каждым тестом проверяет, что `paths.data_dir()` и `paths.downloads_dir()` указывают в каталог этого теста, иначе `pytest.exit(returncode=2)`;
   - каждый тест получает свой профиль (`wave_profile`: свои `data/` и `downloads/`) и сброшенное модульное состояние backend (кэши, задачи загрузок, соединение SQLite); асинхронное состояние резолвера с S2b создаёт и закрывает lifespan (`app.state.resolver`), сбрасывать его не нужно; фикстура `db` инициализирует `library`, `userdata`, `playlists`;
   - в конце сессии сверяет реальные каталоги пользователя (существование, размер и время изменения `wave.db`) с состоянием до запуска;
   - `api`/`harness`: приложение с подменёнными `YTMusic`, httpx-клиентом (`FakeNet` на `httpx.MockTransport`), резолвером yt-dlp и SoundCloud API; запрос к неизвестному хосту роняет тест. `harness.serve()` (S2a) запускает то же приложение настоящим uvicorn на свободном порту loopback — для проверок, где важен порт;
   - самопроверки защиты: `tests/unit/test_isolation.py`;
   - S3: запись в устройства, а не в файлы профиля, разрешена — `os.devnull` (`subprocess.DEVNULL` при запуске JS-runtime) и на Windows `CONOUT$`/`CONIN$` (их открывает `YoutubeDL.__init__`); файл с таким именем в реальном каталоге по-прежнему блокируется;
   - S3: статус JS-runtime по умолчанию — «исправен» (`conftest.healthy_runtime_status`), чтобы тесты резолвера не зависели от того, установлен ли Deno; состояния «нет», «сломан», «старый» проверяются явно. Поиск Deno в `PATH` в тестах `jsruntime` отключён.
2. **e2e** (`tests/e2e_smoke.py`):
   - перед первым действием запрашивает `GET /api/settings`;
   - продолжает только если `paths.data` содержит файл-метку `.wave-test-profile`, созданный раннером, либо лежит внутри `tempfile.gettempdir()`;
   - скриншоты по умолчанию пишет во временный каталог, а не в `tests/shots`.
3. **Раннер** `tests/run_e2e.py` (реализован в S0, расширен в S2a), одна команда:
   1. создаёт временный профиль и метку;
   2. запускает backend на свободном порту;
   3. ждёт `/api/health`;
   4. запускает `e2e_smoke.py`, затем `e2e_settings.py set` на том же порту;
   5. перезапускает backend на **другом** порту с тем же профилем и запускает `e2e_settings.py verify` (в WebView другой порт = другой localStorage);
   6. на отдельном чистом профиле запускает `e2e_settings.py migrate`;
   7. останавливает backend и удаляет временные данные.
   `e2e_settings.py` проверяет профиль так же, как `e2e_smoke.py`.
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
| `tests/api/test_stream_lifespan.py` | последовательные lifespan в одном процессе, конкурентные запросы → без `RuntimeError` (M1, исправлено в S2b); остановка посреди прогрева; состояние резолвера отказывает в чужом цикле |
| `tests/api/test_search_isolation.py` | `yt` OK + `sc` падает → 200, `failed_sources=["sc"]`, результат не кэшируется; `sc` в cooldown не опрашивается; оба падают → 502; таймаут провайдера; пагинация `offset`/`has_more`; пустой и слишком длинный `q` → 422/400 |
| `tests/api/test_recommendations.py` | скоринг по таблице `W` на фиксированных данных; ограничение `MAX_PER_ARTIST`; исключение недавних и `exclude`; дубли по `_sig`; четыре исхода (S2c): результаты, пусто без сбоя, частичный и полный сбой (`failed_sources`, `source_status`); честные `personalized` и `radio` |
| `tests/api/test_userdata.py` | избранное: добавить, повторить, удалить, порядок по `added_at`; история: `GROUP BY`, пагинация, `skip`, очистка обоими эндпоинтами; потеря `explicit`/`plays`/`released` (M6: фиксируем, после S6/S13 ждём сохранения) |
| `tests/api/test_settings.py` | ключи и валидация, неизвестный ключ → 400, `downloads_dir`: относительный путь → 400, недоступный для записи → 400, `WAVE_DOWNLOADS_DIR` блокирует; S2a: ключи интерфейса, атомарность (N1), испорченные строки, однократный перенос из localStorage, `boot.js`, **два запуска uvicorn на разных портах с одним профилем** |
| `tests/api/test_playlists.py` | CRUD; дубль трека → `added: false`; позиции; переименование пустым именем → 400; удаление каскадом; конкурентное добавление одного трека не даёт 500 (M5: xfail до S6) |
| `tests/unit/test_db_migrations.py` (S6) | фикстура базы «как в 5a7f27c» и «старее, без `downloads.root`» → миграция до последней версии без потери строк; бэкап создан; повторный запуск идемпотентен; ошибка миграции → откат и исходная база цела |
| `tests/api/test_downloads.py` | старт → `jobs` → done (CDN через `MockTransport`); апстрим без Range (200); `Content-Range */*`; ошибка посередине → каталог `.part` удалён, индекс не тронут; сбой и не-JSON ответ LRCLIB не отменяют загрузку (M8, закрыто на S5); прогресс по полученным байтам (`received`, `total`, `progress`) перед каждой порцией; повторный запрос (готовый → `done`, активный → та же задача); сохранность загрузок после перезапуска |
| `tests/unit/test_lyrics.py` | `_parse_lrc`: таймкоды `mm:ss.xx`, пустые строки, без таймкодов; 404 → пустой результат; не-JSON → `None`, без исключения |
| `tests/unit/test_jsruntime.py` (S3) | поддельный Deno в путях с пробелами и кириллицей: найден и совместим, отсутствует, не запускается, неожиданный ответ, старая версия, граница 2.3.0, таймаут; порядок поиска (env без подмены → bundle → PATH только в разработке), frozen не берёт системный Deno и cwd; настоящий `YoutubeDL` получает наш путь или пустой `js_runtimes`; `DENO_DIR`/`DENO_NO_UPDATE_CHECK`; пересоздание `YoutubeDL` при смене runtime; сторож процессов (таймаут, завершение, счётчик `run`); только Windows: нет окна консоли, Job Object завершает процесс; логгер yt-dlp |
| `tests/unit/test_jsruntime_real.py` (S3) | **настоящий** Deno из `WAVE_TEST_REAL_DENO` (в CI — после `fetch_runtime.py`): закреплённая версия, путь с кириллицей, yt-dlp его принимает, скрипты yt-dlp-ejs исполняются без сети с флагами yt-dlp, кэш только в `DENO_DIR`, зависший процесс завершается |
| `tests/unit/test_fetch_runtime.py` (S3) | `deno.lock` полный и указывает на официальный релиз; уведомления, `wave.spec`, `.gitignore` согласованы с lock; загрузка с подменённым скачиванием: проверка и распаковка, повторное использование, восстановление подменённого файла, несовпадение хэша архива и исполняемого файла, ошибка загрузки без частичных файлов, битый архив, небезопасный lock отклоняется до загрузки, `--check` |
| `tests/unit/test_dependencies.py` (S3) | версия yt-dlp-ejs = extra `[default]` = версии решателя yt-dlp; requirements и constraints закрепляют установленные версии; GPL-зависимость не подтягивается |
| `tests/unit/test_launcher_cli.py` (S3) | `--diagnostics` в отдельном процессе: без импорта webview, коды 0/1, JSON, зависший runtime завершается; режим диагностики без модальных окон; отчёт в UTF-8 при stdout в cp1252 (причина зависания CI); обычный запуск (и с посторонними аргументами) создаёт окно |
| `tests/api/test_diagnostics.py` (S3) | `/api/diagnostics`: исправный и отсутствующий runtime, расхождение версии в frozen, yt-dlp-ejs отсутствует / другой версии / не читается — критично, без секретов и окружения; `--live` с подменёнными резолвером и CDN не раскрывает ссылку |
| `tests/api/test_js_runtime_errors.py` (S3) | нет runtime → `js_runtime` (503), а не «Трек недоступен»; настоящие причины (удалён, нет сети, возраст) не подменяются; сбой решения JS-задачи → `js_runtime_failed`; SoundCloud и скачанные треки не затронуты; остановка backend завершает дочерние процессы |
| `tests/live/test_live_youtube_runtime.py` (S3) | LIVE-2, LIVE-3, LIVE-4 (§7) |
| `tests/unit/test_ci_annotate.py` (S3) | аннотации CI для упавших тестов и итог pytest со списком пропущенных |
| `tests/api/test_download_policy.py` (S5) | какие треки скачиваются: YouTube Music и SoundCloud — штатный поток плеера в исходном формате, без проверки лицензии и `downloadable`, один резолв на проверку и загрузку; недоступный поток (unavailable, age_restricted, bot_check, rate_limited, network, no_direct_stream) — та же причина, что у плеера, без файлов и запросов к CDN; фрагмент SoundCloud Go+ → `preview_only`; повтор после временной ошибки; скачанное SoundCloud играет с диска без источника; старые загрузки не проверяются |
| `tests/api/test_download_paths.py` (S5, N3) | строки индекса с `../`, абсолютным путём, symlink наружу не читаются и не удаляются ни одним эндпоинтом, без 500; своя папка удаляется целиком |
| `tests/unit/test_download_sources.py` (S5) | признак «только фрагмент» (`Resolved.preview`) берётся из того же извлечения yt-dlp, что и ссылка: `format_id` с `preview`, `requested_formats`, отсутствие `format_id`; поток YouTube фрагментом не считается |
| `tests/api/test_origin_guard.py` (S13) | мутирующий запрос с чужим `Origin` → 403; без `Origin` и со своим → OK; GET не затронут |
| `tests/js/queue.test.mjs` (S9) | модель очереди: add/next/prev/remove/move/clear, индекс текущего при перестановках, shuffle без повторов и с историей для «назад», repeat off/one/all, сериализация и восстановление |
| `tests/js/format.test.mjs` (S9) | `fmtTime`, `plural`, `thumb` |
| `tests/e2e_smoke.py` | 136 проверок после S5 (123 после S3): + честные названия (S2e), сообщение о недоступном источнике с «Повторить», частичный сбой, пустой ответ (S2c), отдельное сообщение об отсутствии компонента воспроизведения YouTube (S3); S5 — раздел «Загрузки»: этапы «Подготовка»/«Скачивание N%»/«Обработка», «Скачано», причина недоступного потока без повторного запроса, меню без «Скачать» для недоступного трека, повтор после временной ошибки, список «Скачанных»; дальше — клавиатурная навигация (S12) |
| `tests/e2e_settings.py` (S2a) | 24 проверки: настройки интерфейса через UI на порту A → те же значения после перезапуска на порту B в новом окне (тема до отрисовки, имя, громкость, тексты, обложки миксов); однократный перенос из localStorage, старые ключи не удаляются |

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

**Изменения в S2** (число тестов pytest после этапа): `test_settings.py` 19 → 64 (S2a); `test_stream_lifespan.py` 6 → 9 (S2b); `test_recommendations.py` 17 → 21 (S2c); `test_ranges.py` 48 → 81 и `test_stream_proxy.py` 38 → 40 (S2d: тексты и типы ошибок стрима, реальные сообщения yt-dlp для Linux и Windows, отсутствие технических подробностей и секретов). Всего 482 теста pytest (469 PASS + 13 xfail), было 395.

**Изменения в S3** (число тестов после этапа): новые `test_jsruntime.py` 37, `test_fetch_runtime.py` 26, `test_js_runtime_errors.py` 13, `test_diagnostics.py` 11, `test_launcher_cli.py` 8, `test_jsruntime_real.py` 5 (только с настоящим Deno), `test_dependencies.py` 4, `test_ci_annotate.py` 2; `test_isolation.py` 12 → 13; live: + 3 (LIVE-2/3/4). Всего 589 тестов pytest без live (было 482); xfail по-прежнему 13. e2e smoke 122 → 123.

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
| LIVE-2 | yt-dlp с `yt-dlp-ejs` и Deno: резолв публичного трека YouTube, прямой URL, `deno run` действительно запускался для JS-задач (`jsruntime.GUARD.runs`) | S3 |
| LIVE-3 | Стрим YouTube через настоящее приложение: 206 на `bytes=0-`, перемотка в середину, перерезолв после принудительной инвалидации | S3, S8 |
| LIVE-4 | Без JS-runtime: задокументировать фактическую деградацию (какие форматы и ошибки); при недоступности YouTube — FAIL с пометкой BLOCKED, а не PASS | S3 |
| LIVE-5 | SoundCloud: поиск и стрим прогрессивного формата; поведение при отказе API | S7 |
| LIVE-8 | Настоящая загрузка через приложение без подмен: (a) YouTube Music, (b) SoundCloud — `done`, аудио и `metadata.json` на диске, трек в «Скачанных», воспроизведение с диска (206) без нового резолва | S5 |
| LIVE-6 | LRCLIB: синхронный текст для известного трека | S1 |
| LIVE-7 | Рекомендации: радио трека YouTube и SoundCloud непустые | S14 |

**LIVE-2/3/4 (S3)** реализованы в `tests/live/test_live_youtube_runtime.py`, трек — публичный `youtube_video_id` из `tests/live_targets.json`. Запуск на машине с доступом к YouTube:

```bat
python packaging\fetch_runtime.py --platform windows-x86_64
set WAVE_JS_RUNTIME=%CD%\packaging\runtime\deno\deno.exe
python -m pytest -m live -rP tests\live\test_live_youtube_runtime.py
```

Вывод `-rP` содержит итог каждой проверки одной строкой JSON (время резолва, число решений JS-задач, размер трека, у LIVE-4 — форматы или тип ошибки); ссылки на поток в вывод не попадают. В собранном приложении то же самое для одного трека проверяет `Wave.exe --diagnostics --live <videoId>` (поле `js_challenge_runs`). В облачной среде S3 все три — **BLOCKED**: прокси отвечает `Tunnel connection failed: 403 Forbidden` на соединение с YouTube (reports/S3.md §9).

**LIVE-8 (S5)** — `tests/live/test_live_downloads.py`, цели `youtube_video_id` и `soundcloud_track_id` в `tests/live_targets.json`; запуск как у LIVE-2 (нужен Deno для 8a). Результат 2026-10-13 в облачной среде: **8a PASS** (`audio.m4a`, 3,4 МБ, 6,1 с), **8b PASS** (`audio.mp3`, 6,4 МБ, 6,6 с), оба играют с диска без резолва. Среда пускает в сеть только через свой прокси, а тесты намеренно убирают `YTDLP_PROXY` из окружения, поэтому прокси и его сертификат подставлялись временным плагином pytest вне репозитория; код Wave не менялся (reports/S5.md §5).

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

**Что из WIN-сценариев покрывает CI после S4.** Job `windows-build` выполняет настоящий `build_windows.bat` (WIN-1) и затем `tests/installer/run_installer_checks.ps1` на одноразовом раннере: WIN-15 (состав `dist\Wave`, установленная папка совпадает с ним файл в файл), WIN-2 (тихая установка для текущего пользователя и под только что созданной стандартной учётной записью, ярлыки, запись удаления только в HKCU, манифест `asInvoker`), WIN-13 (установленный `Wave.exe` на `WAVE_PORT` с временным профилем и закрытой сетью; `e2e_smoke.py` против него), WIN-4 (второй экземпляр не открывает порт), WIN-8 (тихое обновление при запущенной ВОЛНЕ отказывает по AppMutex; закрытие окна; установка поверх заменяет файлы программы и не трогает данные; данные после перезапуска), WIN-9 (тихое удаление при запущенной ВОЛНЕ отказывает; удаление сохраняет каталог данных, загрузки и настоящий `%LOCALAPPDATA%\Wave`), WIN-3 (тестовые сборки установщика `/DTestWebView2=ok|fail`: ожидание bootstrapper, повторная проверка реестра, код 100 при сбое). Не покрывает: окна и сообщения глазами, SmartScreen, запуск из «Пуска» пользователем, обновление с другой версии, настоящую машину без WebView2 — ручной [чек-лист S4](checklists/S4_WINDOWS_CHECK.md), выполненный владельцем на Windows 11 при приёмке S4 (2026-10-13). Обновление с другой версии и машина без встроенного WebView2 остаются непроверенными до следующего выпуска.

**Что из WIN-1, WIN-6, WIN-7 покрывает CI после S3, а что — нет.** Job `windows-build` на `windows-latest` выполняет загрузку Deno со сверкой SHA-256, PyInstaller и `Wave.exe --diagnostics` собранной папки (из `dist`, из пути с пробелами и кириллицей, без упакованного Deno — понятная ошибка и код 1, Deno из `PATH` и текущего каталога не подхватывается, после выхода нет `deno.exe`), а также команды запуска диагностики из README (`start /wait`, `Start-Process -Wait`). Тесты `test_jsruntime.py` на `windows-latest` проверяют Job Object и отсутствие окна консоли у дочернего процесса. Это **не** WIN-1 целиком (нет чистой машины и ISCC — установщик на S4), **не** WIN-6 (нет окна WebView2 и воспроизведения YouTube — на раннере нет доступа к YouTube и проверки звука) и **не** WIN-7 (закрытие окна приложения пользователем). Эти пункты выполняет владелец; пошаговая инструкция с командами PowerShell и ожидаемыми результатами — [checklists/S3_WINDOWS_CHECK.md](checklists/S3_WINDOWS_CHECK.md). Кратко:

1. **WIN-1 (часть S3):** на Windows 11 x64 без установленного Deno запустить `build_windows.bat`; ожидается «Готово: Deno 2.9.7 … SHA-256 …», сборка `dist\Wave`, «Диагностика собранного приложения: OK». Шаг ISCC упадёт до S4 — это ожидаемо.
2. **WIN-6:** запустить `dist\Wave\Wave.exe`, включить трек YouTube, дождаться движения позиции, перемотать в середину, переключить на следующий трек. В `%LOCALAPPDATA%\Wave\logs\wave.log` должна быть строка `JS-runtime для yt-dlp: deno 2.9.7 (bundled, <app>\runtime\deno\deno.exe)`. Окно консоли не должно мелькать. Дополнительно: `start /wait dist\Wave\Wave.exe --diagnostics --live <videoId> --output live.json` → код 0, `js_challenge_runs` > 0.
3. **WIN-7:** во время воспроизведения закрыть окно; в диспетчере задач (или `tasklist /fi "imagename eq deno.exe"`) не должно остаться `Wave.exe` и `deno.exe`.
4. **Сообщение без runtime:** в копии `dist\Wave` удалить `_internal\runtime\deno\deno.exe`, запустить, включить трек YouTube — тост «Компонент воспроизведения YouTube (Deno) не найден или повреждён. Переустановите приложение.»

## 9. CI (решение владельца ADR-8; реализовано в S1: `.github/workflows/ci.yml`)

- Запускается на push в `main` и `claude/**`, на pull request и вручную. Секретов не использует, к живым источникам не обращается, права — только `contents: read`.
- `checks (ubuntu-latest)` и `checks (windows-latest)`: Python 3.12, установка с `constraints.txt`, Playwright Chromium, затем `python tests/run_checks.py` — hygiene, compileall, pyflakes, `node --check`, pytest, e2e через раннер, проверка неизменности рабочего каталога. Шаг успешен только при коде 0, то есть все шаги PASS (BLOCKED даёт код 3).
- `windows-build`: команды `build_windows.bat` без шага установщика — `svg_to_ico.py`, `PyInstaller packaging/wave.spec`; проверка, что в `dist\Wave` есть `Wave.exe` и `frontend\index.html`, а баз, журналов, `.env`, cookies и медиа нет. Это проверка spec, а не запуск приложения.
- S3: в `checks` перед тестами шаг `fetch_runtime.py` скачивает закреплённый Deno (Linux или Windows) со сверкой SHA-256 для `test_jsruntime_real.py`; итог pytest со списком пропущенных тестов и упавшие проверки выводятся аннотациями (`tests/ci_annotate.py`) — они доступны через API check-runs, даже когда полный журнал запуска недоступен. В `windows-build` — загрузка Deno, проверка состава папки (хэш `deno.exe` = `deno.lock`, лицензии, скрипты yt-dlp-ejs, нет `mutagen`), размеры аннотацией, `Wave.exe --diagnostics` в нескольких сценариях (§8) с таймаутами и сбором улик при зависании.
- Не входят в CI: окно WebView2, воспроизведение в собранном приложении, Inno Setup (S4), live-тесты, ручной Windows-чек-лист §8. `node --test` появится вместе с JS-модулями (S9).
- Первый запуск: https://github.com/Andrey3269/Wave/actions/runs/38310405338 — все три job'а зелёные.

## 10. Формат отчёта по этапу

1. Изменённые файлы.
2. Изменённое поведение.
3. Таблица команд со статусами PASS/FAIL/BLOCKED/NOT RUN и средой (ОС, браузер или WebView2).
4. Число проверок e2e до и после.
5. Оставшиеся риски.
6. Следующий шаг.

## 11. Известные дефекты, закреплённые `xfail(strict=True)`

Тест описывает **правильное** поведение и сейчас падает по указанной причине (проверено запуском с `--runxfail`). Когда дефект исправят, тест начнёт проходить, `strict` превратит это в FAIL — значит, пора снять маркер в том же коммите, что и исправление.

После S2 осталось 13:

| ID | Тест | Что происходит сейчас | Этап |
|---|---|---|---|
| L2 | `api/test_stream_proxy.py::test_empty_range_is_not_forwarded` | `Range: bytes=-` уходит на CDN как есть | S8 |
| L2 | `api/test_stream_local.py::test_malformed_range_is_ignored` ×3 | битый Range на скачанном файле → 400 вместо 200 | S8 |
| L2 | `api/test_stream_local.py::test_head_request_supported` | `HEAD /api/stream/...` → 404 | S8 |
| M3 | `api/test_search_isolation.py::test_youtube_failure_enters_cooldown` | YouTube опрашивается снова при каждом поиске после сбоя | S7 |
| M5 | `api/test_playlists.py::test_concurrent_add_of_same_track` | гонка двух добавлений → `IntegrityError` (через API — 500) | S6 |
| M6 | `api/test_userdata.py::test_favorite_keeps_all_track_fields` | `explicit`, `plays`, `released` теряются при сохранении | S6 |
| M6 | `api/test_userdata.py::test_saved_favorite_can_always_be_removed` | запись с `/` в `id` сохраняется, удалить её нельзя | S6, S13 |
| M7 | `api/test_local_api_security.py::test_cross_origin_mutation_is_refused` | `POST /api/settings/clear-history` с чужим `Origin` выполняется | S13 |
| M8 | `unit/test_lyrics.py::test_non_json_response_is_temporary_failure` | не-JSON ответ LRCLIB → `JSONDecodeError` | S11 |
| M8 | `api/test_lyrics_api.py::test_non_json_service_response_is_handled` | то же через `/api/lyrics` → 500 | S11 |
| M8 | `api/test_downloads.py::test_broken_lyrics_response_does_not_fail_download` | загрузка падает на шаге `processing`, скачанное аудио удаляется | S11 |

Сняты в S2 (тест проходит без маркера):

| ID | Тест | Коммит |
|---|---|---|
| H3 | `api/test_settings.py::test_ui_settings_are_persisted_by_backend` | `1070aa8` (S2a) |
| M1 | `api/test_stream_lifespan.py::test_restart_after_contention_keeps_resolving` | `b72b6cd` (S2b) |
| M4 | `api/test_recommendations.py::test_all_candidate_sources_failed_is_reported` | `21ddcab` (S2c) |

Тесты «как есть», заменённые в S2 вместе с поведением: N1 `test_partial_write_before_error` → `test_write_is_atomic`; M4 `test_all_candidate_sources_failed_returns_empty_200` → проверка `failed_sources`/`source_status` в `test_all_candidate_sources_failed_is_reported`; M13 `personalized` в `test_radio_of_current_track` → `False` + `radio: True`; L6 `test_network_failure_message_is_misleading` → `test_network_failure_is_not_blamed_on_cookies` и соседние. Остаются «как есть»: M3 `test_youtube_is_not_put_in_cooldown`, M5 `test_no_schema_version_yet`.

Сброс `stream._resolve_sem` в `tests/conftest.py:reset_backend_state` удалён в S2b: семафор принадлежит `app.state.resolver` и создаётся заново в каждом lifespan.

## 12. Наблюдения S1, которых не было в аудите

Не дефекты высокого приоритета; закреплены тестами «как есть» и требуют решения на указанном этапе.

| ID | Наблюдение | Тест | Этап и статус |
|---|---|---|---|
| N1 | `PUT /api/settings` применял ключи по очереди: ключи до ошибочного уже сохранялись, хотя ответ 400 | `api/test_settings.py::test_write_is_atomic` (×4), `test_failed_downloads_dir_leaves_other_keys_untouched`, `test_invalid_key_prevents_downloads_dir_change` | **исправлено в S2a** (`1070aa8`): все ключи проверяются заранее и пишутся одной транзакцией |
| N2 | При обрыве CDN посередине ответа прокси теряет уже полученные, но ещё не отданные байты (читает кусками по 64 КБ). Плеер видит короткое тело и перезапрашивает диапазон | `api/test_stream_proxy.py::test_upstream_break_mid_stream_ends_response` (как есть) | **S8**, критерий приёмки 4 в ROADMAP |
| N3 | Запись индекса загрузок с `folder`, выходящим за каталог загрузок (`../`), даёт HTTP 500: чужой файл не отдаётся (`library.folder_path` бросает `ValueError`), но ответ — ошибка сервера, а не отказ | `api/test_stream_local.py::test_index_row_outside_downloads_is_not_served` (как есть: 500, посторонний файл не прочитан) | **S11 и S13**, критерии безопасности в ROADMAP: ни один эндпоинт не читает, не отдаёт и не удаляет файлы вне разрешённых каталогов, ответ — отказ, а не 500 |
