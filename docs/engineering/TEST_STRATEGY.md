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

### Состояние после S6 (2026-10-13, ветка `claude/s6-sqlite`, подробности — [reports/S6.md](reports/S6.md))

| Набор | Результат | Среда |
|---|---|---|
| `python -m pytest` | **PASS**: 765 passed + 9 skipped + 7 xfail (было 712 + 9 + 10 на `072d614`; сняты M5, M6 ×2). Skipped — только Windows-тесты и тесты с настоящим Deno без `WAVE_TEST_REAL_DENO` | Linux, Python 3.12.3; CI `ubuntu-latest` (772 passed + 2 skipped), `windows-latest` (774 passed, 0 skipped), run 38431447697 |
| `tests/e2e_smoke.py` + `e2e_settings.py` | **PASS**: 137 + 27 (было 136 + 27; + сохранение `explicit`/`plays`/`released` при лайке из строки списка) | Linux Chromium; CI Linux и Windows Chromium (не WebView2) |
| `python tests/run_checks.py` | **PASS** все шаги | Linux; CI Linux и Windows |
| Миграция базы, записанной кодом S5 (вручную, вне pytest) | **PASS**: строки всех таблиц совпали, `user_version` 1, WAL, копия `-v0` | Linux |
| `windows-build`: сборка, установщик, 17 проверок установщика, e2e против установленного `Wave.exe` (137) | **PASS** | CI `windows-latest` |
| Обновление установленной Wave поверх S5 на Windows с настоящим профилем | **NOT RUN** — нужна Windows-машина (ручной пункт в reports/S6.md §7) | — |

### Состояние после S7 (2026-10-13, ветка `claude/s7-provider-adapters`, подробности — [reports/S7.md](reports/S7.md))

| Набор | Результат | Среда |
|---|---|---|
| `python -m pytest` | **PASS**: 888 passed + 9 skipped + 6 xfail (было 765 + 9 + 7 на `34bb073`; снят M3; +118 в новых файлах, `test_search_isolation` 31 → 35). Live: 12 (было 9; + LIVE-5 ×3) | Linux, Python 3.12.3; CI — reports/S7.md §8 |
| `tests/e2e_smoke.py` + `e2e_settings.py` | **PASS**: 137 + 27 (не изменилось: e2e подменяет ответы API в браузере, формы ответов только дополнены) | Linux Chromium; CI Linux и Windows Chromium |
| `python tests/run_checks.py` | **PASS** все шаги | Linux; CI Linux и Windows |
| LIVE-1 | артисты — **PASS**; треки и альбомы — **BLOCKED**: YouTube Music отдаёт облачной среде (регион US, выход через прокси) пустые разделы песен и альбомов уже в «сыром» ответе, одинаково для прежнего `YTMusic()` и клиента адаптера | облачная среда, прокси среды |
| LIVE-5 | поиск — **PASS**; отказ API — **PASS**; прогрессивный поток — **FAIL, нестабильно** (4 из 9; повтор 2026-10-14 — 3 из 9; всего 7 из 18): SoundCloud в части ответов отдаёт только HLS (наблюдение N4, S8) | облачная среда, прокси среды |
| LIVE-6 | **PASS** | облачная среда, прокси среды |
| WIN-10 (офлайн и ограниченная сеть) | **NOT RUN** — нужна Windows-машина | — |

Приёмка S7: принят владельцем 2026-10-14 с ограничениями — проверенный коммит `2f731f8`, CI 38500085142 SUCCESS; статусы LIVE-1, LIVE-5 и WIN-10 в таблице выше приёмкой не меняются ([reports/S7.md §10](reports/S7.md)).

### Состояние после S8 (2026-10-14, ветка `claude/s8-hls-playback`, подробности — [reports/S8.md](reports/S8.md))

| Набор | Результат | Среда |
|---|---|---|
| `python -m pytest` | **PASS**: 1209 passed + 9 skipped + 1 xfail (было 888 + 9 + 6 на `b1ba727`; сняты 3 xfail L2; новые файлы — 263 теста). Live: 17 (было 12; + LIVE-S8 ×5) | Linux, Python 3.13; CI `ubuntu-latest` и `windows-latest` (Windows: 1218 passed, 0 skipped) |
| `tests/e2e_smoke.py` + `e2e_settings.py` | **PASS**: 144 + 27 (smoke +7: раздел «SoundCloud: трек только по HLS»; прежние проверки не менялись) | Linux Chromium (3 прогона подряд); CI Linux и Windows Chromium; e2e против установленного `Wave.exe` — 144 |
| `tests/api/test_hls_browser.py` | **PASS**: 4 — настоящий Chromium против настоящего backend (uvicorn) с HLS | Linux; CI Linux и Windows (с `76c980f`; раньше пропускались, см. §3) |
| `python tests/run_checks.py` | **PASS** 7/7 | Linux; CI Linux и Windows |
| LIVE-3 | **PASS** | облачная среда, прокси среды, Deno 2.9.7 |
| LIVE-5 | **PASS**: поиск, поток (принимает и progressive, и HLS), отказ API | облачная среда |
| LIVE-S8 | **PASS**: HLS mp3 и AAC через прокси, загрузка mp3-HLS (5 из 5) и AAC-HLS, 6 из 6 резолвов воспроизводимы | облачная среда |
| LIVE-8a / 8b | **PASS** | облачная среда |
| LIVE-1 | артисты — **PASS**; треки и альбомы — **BLOCKED** (как в S7) | облачная среда |
| Windows 11 / WebView2, ручной чек-лист S8 | владелец подтвердил воспроизведение и загрузку YouTube Music и SoundCloud — **PASS**; остальные пункты [checklists/S8_WINDOWS_CHECK.md](checklists/S8_WINDOWS_CHECK.md) отдельно не подтверждены ([reports/S8.md §16](reports/S8.md)) | Windows 11 владельца, собранное приложение; для CDN SoundCloud потребовалась настройка Zapret на машине владельца |

**После доработки S8 по независимому аудиту** (`adfcfd7`, `4539f65`; [reports/S8.md §15](reports/S8.md)):

| Набор | Результат | Среда |
|---|---|---|
| `python -m pytest` | **PASS**: 1235 passed + 9 skipped + 1 xfail (+22 `unit/test_netguard_pinning.py`; `api/test_hls_proxy.py` 56 → 60; один тест `unit/test_netguard.py` заменён на противоположное ожидание). Live: 17 | Linux; CI — в сообщении владельцу |
| e2e, `run_checks.py` | **PASS**: 144 + 27; 7/7 | Linux |
| LIVE-S8 AAC, LIVE-S8-3, LIVE-8a/8b, LIVE-2…5 | **PASS** | облачная среда |
| LIVE-S8 mp3 | **BLOCKED**: SoundCloud в день проверки отдавал только AAC-HLS (трек `343609555` и 8 других публичных треков) | облачная среда |
| LIVE-1 | **BLOCKED / нестабильно** (YouTube Music в этой сети, как в S7) | облачная среда |
| HLS через настоящий прокси пользователя | **NOT RUN** (туннель проверен на поддельном прокси) | — |
| CI на `623b50a` ([38564429932](https://github.com/Andrey3269/Wave/actions/runs/38564429932)) и `3eadcba` ([38566178720](https://github.com/Andrey3269/Wave/actions/runs/38566178720)) | **SUCCESS**: Linux, Windows, сборка, установщик; e2e против установленного `Wave.exe` — 144 | GitHub Actions |

Приёмка S8: **ACCEPTED BY OWNER** 2026-10-14 с ограничениями — проверенный коммит `3eadcba`. Статусы BLOCKED и NOT RUN в таблицах выше приёмкой не меняются ([reports/S8.md §16](reports/S8.md)).

**Правило монотонности.** Число проверок e2e не уменьшается. Изменение проверки допускается только вместе с изменением поведения, которое она проверяет, в том же коммите и с пояснением.

### Состояние после S9 (принято владельцем 2026-10-16; ветка `claude/s9-app-js-modules`, подробности — [reports/S9.md](reports/S9.md))

| Набор | Результат | Среда |
|---|---|---|
| `python -m pytest` | **PASS**: 1251 passed + 9 skipped + 1 xfail (было 1235 + 9 + 1; новые — `test_frontend_static.py`, `test_run_checks_jstest.py`) | Linux, Python 3.13; CI `ubuntu-latest` (1258) и `windows-latest` (1260, 0 skipped) |
| `node --test "tests/js/*.test.mjs"` | **PASS**: 37 тестов — `queue` 15 (из них M11 — 4), `format` 9, `api` 6, `emitter` 2, `modules` 5 | Linux Node 22; CI Linux и Windows Node 22 |
| `tests/e2e_smoke.py` + `e2e_settings.py` | **PASS**: 152 + 27 (было 144 + 27; +1 «пропуск при смене списка», +7 M11 — оба раздела падают на коде до исправления; прежние проверки не менялись) | Linux Chromium 141; CI Linux и Windows Chromium; e2e против установленного `Wave.exe` — 152 |
| `python tests/run_checks.py` | **PASS** 8/8 (новый шаг `jstest`) перед каждым из 15 коммитов и на HEAD | Linux; CI Linux и Windows |
| `tests/ui_snapshots.py` (19 экранов × 980/1366/1440/1920) | **PASS**: 76 из 76 совпадают с допуском 20/255; без допуска различия — сглаживание на краю аватара и текста, такой же шум между двумя снятиями одного коммита | Linux Chromium 141 headless |
| Окно WebView2, ручной чек-лист [checklists/S9_WINDOWS_CHECK.md](checklists/S9_WINDOWS_CHECK.md) | **PASS** — владелец подтвердил 11/11 пунктов 2026-10-16 | Windows 11, WebView2 |

Приёмка S9: **ACCEPTED BY OWNER — 2026-10-16**. Все автоматические проверки и CI указаны выше; ручной чек-лист Windows 11/WebView2 пройден по подтверждению владельца (11/11 PASS). Это не означает слияния в `main`.

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
| Unit frontend | `node --test "tests/js/*.test.mjs"` для `player/queue.js`, `core/*` и графа модулей (S9); в `run_checks.py` — шаг `jstest`. В Node 21+ `node --test tests/js` не работает: каталог аргументом не принимается | любая ОС | нет |
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
| `tests/unit/test_stream_facade.py` (S8) | фасад `stream.py`: имена — объекты модулей `streaming/*`, присваивание (monkeypatch) доходит до владельца; код приложения фасад не импортирует |
| `tests/unit/test_format_choice.py` (S8) | `resolver.classify` / `choose_format` на живом списке форматов SoundCloud: progressive → HLS mp3 → HLS AAC, превью последним, opus и шифрование не выбираются, честные ошибки; `PROGRESSIVE_ONLY` фронтенда = источники без HLS |
| `tests/unit/test_hls_playlist.py` (S8) | разбор и переписывание HLS-плейлистов: master/media, относительные URI, query, MEDIA-SEQUENCE, BYTERANGE, EXT-X-MAP, шифрование, LL-HLS, лимиты, списки атрибутов |
| `tests/unit/test_netguard.py` (S8) | защита от SSRF: публичные/внутренние IP (включая встроенные в IPv6), схемы, локальные имена, DNS, проверка каждого редиректа; имя, не разрешающееся локально, отклоняется и при настроенном `YTDLP_PROXY` (`Unverifiable`) |
| `tests/unit/test_netguard_pinning.py` (S8, аудит) | закрепление IP на настоящих asyncio-серверах 127.0.0.1: поддельный HTTP-прокси с собственным DNS получает только `CONNECT <проверенный IP>:443` и ничего не разрешает; SNI по имени хоста через туннель; авторизация прокси; имена с частным, metadata-, loopback-адресом и неразрешимые не доходят до прокси; https/socks-прокси отклоняются; плохие ответы прокси; прямой режим соединяется с проверенным IP; DNS rebinding между проверкой и соединением блокируется; IPv6-only без прокси — ошибка; устройство клиента (`trust_env=False`, без редиректов, `retries=0`) |
| `tests/unit/test_containers.py` (S8) | кадры MPEG audio (формулы стандарта), ID3, склейка через границы сегментов, fMP4-боксы init и фрагментов |
| `tests/unit/test_frontend_vendor.py` (S8) | hls.js: SHA-256 и версия по `vendor.lock.json`, лицензия и уведомление, без CDN, в сборке и в раздаче backend |
| `tests/api/test_hls_proxy.py` (S8) | `/playback` (local, progressive, hls), HLS-прокси: плейлисты, сегменты, init, master, BYTERANGE; 409 `hls_required`; сеансы, TTL, лимиты, освобождение; SSRF и CORS; протухшие ссылки, смена потока, шифрование, плохие плейлисты, таймауты; N2 сегмента; лимит `HLS_PARALLEL` на всё время использования ответа — 12 параллельных запросов с удерживаемыми телами, пик активных передач ≤ 6; освобождение слота при отключении клиента посреди тела и на каждом пути ошибки; ограниченное ожидание слота (503 `hls_busy`); непроверяемый хост через прокси (502 `upstream_unverifiable`); подписи не в журнале |
| `tests/api/test_hls_browser.py` (S8) | Chromium + настоящий backend: HLS через hls.js, перемотка, пауза, переключение и освобождение, конец трека, восстановление после потери сеанса, ограниченные повторы, ошибка декодера |
| `tests/api/test_hls_downloads.py` (S8) | загрузка HLS-only: `.mp3` / `.m4a`, master, ID3, кадры через границы, TS и мусор — отказ, длительность, live, обрывы, перерезолв, SSRF, превью, прогресс, офлайн |
| `tests/api/test_stream_lifespan.py` | последовательные lifespan в одном процессе, конкурентные запросы → без `RuntimeError` (M1, исправлено в S2b); остановка посреди прогрева; состояние резолвера отказывает в чужом цикле |
| `tests/api/test_search_isolation.py` | `yt` OK + `sc` падает → 200, `failed_sources=["sc"]`, результат не кэшируется; `sc` в cooldown не опрашивается; оба падают → 502; таймаут провайдера; пагинация `offset`/`has_more`; пустой и слишком длинный `q` → 422/400; S7: cooldown YouTube (45 с, M3), восстановление, повторный сбой, пустая выдача — не сбой, при «висящей» сети таймаут ждётся один раз |
| `tests/unit/test_provider_base.py` (S7) | `ProviderError` (8 типов, текст с названием источника), какие сбои открывают cooldown (поиск/страница/необязательная часть), отказ в паузе без запроса, сброс паузы успехом, жёсткий предел пула, таймаут не ждёт зависший поток, 40 таймаутов подряд не множат потоки, переполнение — сразу и без паузы, отмена клиентом снимает задачу из очереди, зависший источник не задерживает другой, остановка отменяет очередь, `snapshot()` без секретов |
| `tests/unit/test_ttl_cache.py` (S7) | LRU-вытеснение, TTL при чтении и перезаписи, устаревшие вытесняются раньше живых, 10 000 разных ключей при лимите 64, потокобезопасность |
| `tests/unit/test_provider_adapters.py` (S7) | классификация исключений ytmusicapi/requests и yt-dlp (HTTP-коды, транспорт, таймаут, исчезнувший внутренний метод yt-dlp → internal), цепочка причин без зацикливания; **настоящие** таймауты: `ytmusicapi.search` и `_call_api` yt-dlp против молчащего локального сервера завершаются по таймауту клиента, поток свободен; клиент YouTube Music с сессией и таймаутами без сети при создании; свой экземпляр yt-dlp SoundCloud в каждом потоке |
| `tests/api/test_provider_isolation.py` (S7) | источник «висит» 30 с: второй источник, каталог, рекомендации, избранное, история, плейлисты, настройки отвечают < 0,75 с; пул зависшего не выше предела, лишние — сразу 502 busy; все вызовы источников идут в своих пулах (`provider-yt`, `provider-sc`); таймаут каталога — понятная ошибка; сбой разбора в адаптере — internal этого источника; при сбое единственного источника поиска `detail` — его причина |
| `tests/api/test_source_cooldown.py` (S7) | пауза действует на каталог и рекомендации (без запросов к источнику); сбой поиска SoundCloud ставит на паузу его каталог, YouTube работает; «не найдено» и сбой одной страницы паузу не открывают, таймаут страницы — открывает; успех снимает паузу; сбой необязательной части страницы не обрывает её; запасной путь альбома после таймаута `get_album` |
| `tests/api/test_bounded_caches.py` (S7) | лимиты и TTL кэшей поиска, артистов, текстов; LRU-вытеснение при многих разных запросах; неполная страница артиста не кэшируется и грузится один раз; нормализация запроса; временный сбой LRCLIB не кэшируется; «Очистить кэш» |
| `tests/api/test_source_status.py` (S7) | `source_status` в поиске: все ok, частичный сбой (cooldown), переполнение (degraded), полный сбой (502), всё на паузе, из кэша, `disabled`; карточки, страницы артиста (в том числе альбомы SoundCloud: нет — ok, 503 — degraded), альбома (запасной путь — degraded); `GET /api/sources` |
| `tests/api/test_recommendations.py` | скоринг по таблице `W` на фиксированных данных; ограничение `MAX_PER_ARTIST`; исключение недавних и `exclude`; дубли по `_sig`; четыре исхода (S2c): результаты, пусто без сбоя, частичный и полный сбой (`failed_sources`, `source_status`); честные `personalized` и `radio` |
| `tests/api/test_userdata.py` | избранное: добавить, повторить, удалить, порядок по `added_at`; история: `GROUP BY`, пагинация, `skip`, очистка обоими эндпоинтами; `explicit`/`plays`/`released` сохраняются, старые снимки читаются как есть, запись с «/» в `id` удаляется (M6, исправлено в S6); ретеншн истории: лимит по умолчанию, проверка значения, обрезка при записи, при уменьшении лимита и при запуске, другие таблицы не тронуты (S6) |
| `tests/api/test_settings.py` | ключи и валидация, неизвестный ключ → 400, `downloads_dir`: относительный путь → 400, недоступный для записи → 400, `WAVE_DOWNLOADS_DIR` блокирует; S2a: ключи интерфейса, атомарность (N1), испорченные строки, однократный перенос из localStorage, `boot.js`, **два запуска uvicorn на разных портах с одним профилем**; S6: ключ `history_limit` в значениях по умолчанию |
| `tests/api/test_playlists.py` | CRUD; дубль трека → `added: false`; позиции; переименование пустым именем → 400; удаление каскадом; конкурентное добавление одного трека и 16 параллельных POST — без 500, без дублей, позиции без пропусков (M5, исправлено в S6); добавление в удалённый плейлист → 404; откат добавления, удаления трека и плейлиста при сбое; полный снимок трека; удаление трека с «/» в `id` |
| `tests/unit/test_db_migrations.py` (S6) | базы «как в 5a7f27c» (схема снята с кода коммита, `tests/fixtures/db/schema_5a7f27c.sql`), «старее, без `downloads.root` и плейлистов» и актуальная → миграция до v1 без потери строк (все таблицы целиком), данные видны через API; копия до миграции совпадает с исходной базой; без копии миграция не идёт; сбой после CREATE/ALTER → откат схемы, данных, `user_version`, режима журнала, приложение не стартует; неизвестная старая структура → понятная ошибка; повторные запуски идемпотентны; имена копий без перезаписи, хранятся три, оборванная копия не вытесняет целые; база v1 открывается кодом до S6 |
| `tests/unit/test_db.py` (S6) | PRAGMA (WAL, NORMAL, foreign_keys, busy_timeout); одиночные запросы сразу видны другому соединению; `transaction()` — commit и rollback; вложенный вызов → `NestedTransactionError` без взаимной блокировки; 8 потоков × 50 read-modify-write без потерь; запрос до открытия базы; повторный `init` закрывает прежнее соединение; повреждённый файл и база от более новой Wave → понятная ошибка, файл не изменён |
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
| `tests/e2e_smoke.py` | 137 проверок после S6 (+ лайк из строки списка сохраняет `explicit`, `plays`, `released`), 136 после S5 (123 после S3): + честные названия (S2e), сообщение о недоступном источнике с «Повторить», частичный сбой, пустой ответ (S2c), отдельное сообщение об отсутствии компонента воспроизведения YouTube (S3); S5 — раздел «Загрузки»: этапы «Подготовка»/«Скачивание N%»/«Обработка», «Скачано», причина недоступного потока без повторного запроса, меню без «Скачать» для недоступного трека, повтор после временной ошибки, список «Скачанных»; дальше — клавиатурная навигация (S12) |
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
| Фронтенд | + `node --test "tests/js/*.test.mjs"`; e2e 109+; скриншоты 980/1366/1440/1920; клавиатура и фокус; нет горизонтального переполнения |
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
| LIVE-5 | SoundCloud: поиск и стрим (с S8 — progressive или HLS); поведение при отказе API | S7, S8 |
| LIVE-S8 | SoundCloud HLS через настоящее приложение: воспроизведение через `/playback` и HLS-прокси (mp3-HLS и AAC-HLS), загрузка HLS-only трека в `.mp3` / `.m4a` (ffprobe, если есть), несколько резолвов подряд — все воспроизводимы | S8 |
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

**LIVE-1 и LIVE-5 (S7).** LIVE-1 (`tests/live/test_live_sources.py`) с S7 идёт через адаптер YouTube Music: настоящий клиент с таймаутами сессии, пул, нормализация. LIVE-5 — `tests/live/test_live_soundcloud.py`: (1) поиск треков и артистов через адаптер; (2) штатный резолвер (`stream._extract_sync`) даёт прогрессивный поток, CDN отвечает 206 на `bytes=0-65535`; (3) отказ API — несуществующий пользователь → `not_found` без паузы, API недоступен (отклонённое соединение) → `unavailable` и пауза только у SoundCloud, YouTube Music отвечает. Цели — `soundcloud_query`, `soundcloud_track_id`, `ytmusic_query` в `tests/live_targets.json`. Запуск: `python -m pytest -m live -rP tests/live/test_live_sources.py tests/live/test_live_soundcloud.py` (вывод `-rP` — строка JSON на проверку, без ссылок и client_id). Результат 2026-10-13 в облачной среде (через прокси среды, подставленный временным плагином pytest вне репозитория, как в S5): LIVE-1 артисты — PASS, треки и альбомы — BLOCKED (пустые разделы у YouTube Music для этой сети, и у прежнего клиента тоже); LIVE-5 поиск и отказ — PASS, поток — FAIL нестабильно (4 из 9, N4); LIVE-6 — PASS.

**LIVE-8 (S5)** — `tests/live/test_live_downloads.py`, цели `youtube_video_id` и `soundcloud_track_id` в `tests/live_targets.json`; запуск как у LIVE-2 (нужен Deno для 8a). Результат 2026-10-13 в облачной среде: **8a PASS** (`audio.m4a`, 3,4 МБ, 6,1 с), **8b PASS** (`audio.mp3`, 6,4 МБ, 6,6 с), оба играют с диска без резолва. Среда пускает в сеть только через свой прокси, а тесты намеренно убирают `YTDLP_PROXY` из окружения, поэтому прокси и его сертификат подставлялись временным плагином pytest вне репозитория; код Wave не менялся (reports/S5.md §5).

**LIVE-S8 (S8)** — `tests/live/test_live_hls.py`, трек `soundcloud_track_id`. Настоящее приложение (TestClient с lifespan), настоящие yt-dlp, DNS и netguard. Для LIVE-S8-1/2 выбор формата сужается до HLS нужного кодека из настоящего списка форматов (иначе SoundCloud сам решает, отдать ли прогрессивный файл); если в этом резолве SoundCloud такого HLS не предложил — тест SKIPPED с пометкой BLOCKED и списком форматов. Запуск: `python -m pytest -m live -rP tests/live/test_live_hls.py`. Результат 2026-10-14 (облачная среда, сертификат прокси среды — временным плагином вне репозитория): LIVE-S8-1 mp3 и AAC — PASS; LIVE-S8-2 mp3 — PASS 5 из 5 (`audio.mp3` 6 355 068 байт — размер прогрессивного mp3 того же трека), AAC — PASS (`audio.m4a`, ffprobe AAC 397,2 с); LIVE-S8-3 — 6 из 6 (3 HLS-AAC, 3 progressive).

**Браузерные тесты HLS в pytest (S8).** `tests/api/test_hls_browser.py` запускает Chromium (Playwright) против `ApiHarness.serve()` — настоящий uvicorn, подменены только резолвер, CDN и DNS; браузер не выходит за `127.0.0.1`. `wavetest/guard.py` фиксирует путь к браузерам Playwright до подмены `HOME`/`LOCALAPPDATA` (иначе в CI они не находились и тесты пропускались). В CI отсутствие Chromium для этих тестов — FAIL, локально — SKIPPED с пометкой BLOCKED. AAC/fMP4 в этом Chromium не проверить (сборка без проприетарных кодеков) — только mp3-HLS; AAC — на WebView2 владельца.

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
- Не входят в CI: окно WebView2, воспроизведение в собранном приложении, Inno Setup (S4), live-тесты, ручной Windows-чек-лист §8. С S9 в `checks` входит шаг `jstest` (`node --test` по `tests/js/*.test.mjs`, Node 22 через `actions/setup-node`), а `windows-build` сверяет, что каждый файл `frontend\js` попал в `dist` побайтно и старого `app.js` нет.
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

После S8 остался 1 (pytest: `1 xfailed`):

| ID | Тест | Что происходит сейчас | Этап |
|---|---|---|---|
| M7 | `api/test_local_api_security.py::test_cross_origin_mutation_is_refused` | `POST /api/settings/clear-history` с чужого `Origin` выполняется | S13 |

Сняты (тест проходит без маркера):

| ID | Тест | Коммит |
|---|---|---|
| H3 | `api/test_settings.py::test_ui_settings_are_persisted_by_backend` | `1070aa8` (S2a) |
| M1 | `api/test_stream_lifespan.py::test_restart_after_contention_keeps_resolving` | `b72b6cd` (S2b) |
| M4 | `api/test_recommendations.py::test_all_candidate_sources_failed_is_reported` | `21ddcab` (S2c) |
| M8 | `unit/test_lyrics.py::test_non_json_response_is_temporary_failure`, `api/test_lyrics_api.py::test_non_json_service_response_is_handled`, `api/test_downloads.py::test_broken_lyrics_response_does_not_fail_download` | `cd4a028` (S5; таблица здесь обновлена на S6) |
| M5 | `api/test_playlists.py::test_concurrent_add_of_same_track` | `ebb44d6` (S6) |
| M6 | `api/test_userdata.py::test_favorite_keeps_all_track_fields`, `test_saved_favorite_can_always_be_removed` | `dc070fd` (S6) |
| M3 | `api/test_search_isolation.py::test_youtube_failure_enters_cooldown` | `42424f0` (S7) |
| L2 | `api/test_stream_proxy.py::test_empty_range_is_not_forwarded`, `api/test_stream_local.py::test_malformed_range_is_ignored` (×3, плюс `items=0-10` и `bytes=0-9,abc`), `api/test_stream_local.py::test_head_request_supported` | `d77c187` (S8) |

`test_concurrent_add_of_same_track` в S1 держал барьер после отдельного запроса проверки дубля — такого запроса в S6 больше нет. Теперь барьер выпускает оба потока к записи одновременно; дополнительно `test_concurrent_api_adds_keep_order_and_no_duplicates` шлёт 16 параллельных POST (на коде S5 падает с HTTP 500 в 3 из 3 запусков).

Тесты «как есть», заменённые вместе с поведением: N1 `test_partial_write_before_error` → `test_write_is_atomic` (S2); M4 `test_all_candidate_sources_failed_returns_empty_200` → `test_all_candidate_sources_failed_is_reported` (S2); M13 `personalized` в `test_radio_of_current_track` → `False` + `radio: True` (S2); L6 `test_network_failure_message_is_misleading` → `test_network_failure_is_not_blamed_on_cookies` (S2); M5 `test_no_schema_version_yet` → `unit/test_sqlite_schema.py::test_schema_version_is_current` (S6); снимок избранного без новых полей → `test_favorite_stored_snapshot_fields` с `explicit`/`plays`/`released` (S6); M3 `test_youtube_is_not_put_in_cooldown` → `test_youtube_cooldown_is_short` (S7, `42424f0`); N2 `test_upstream_break_mid_stream_ends_response` — с «как есть» (потеря до 64 КБ) на ожидаемое поведение, проверка по сообщениям ASGI (S8, `d77c187`); N4 `unit/test_ranges.py::test_soundcloud_requires_progressive_http` → `test_soundcloud_hls_only_resolves_as_hls` (S8, `a174239`). Тестов «как есть» для M-находок больше нет.

S7 также изменил закреплённые проверки вместе с поведением: тексты ошибок каталога называют источник (`test_catalog_api.py`: было «Источник временно недоступен…», теперь «YouTube Music/SoundCloud временно недоступен…» плюс `code`, `source`, `source_status`); контрактные тесты поиска принимают аддитивное поле `source_status` (прежние поля на месте); `test_cache_expires` меняет срок у самого кэша (`main._cache.ttl`), а не модульную переменную; точки подмены `ApiHarness` перенесены на границу адаптеров (`providers.youtube_music.create_client`, `providers.soundcloud.search_raw`/`api_get`), сброс состояния — `providers.reset()`.

Сброс `stream._resolve_sem` в `tests/conftest.py:reset_backend_state` удалён в S2b: семафор принадлежит `app.state.resolver` и создаётся заново в каждом lifespan. С S6 тот же сброс закрывает соединение `persistence.db` вместо `library._db`.

## 12. Наблюдения S1, которых не было в аудите

Не дефекты высокого приоритета; закреплены тестами «как есть» и требуют решения на указанном этапе.

| ID | Наблюдение | Тест | Этап и статус |
|---|---|---|---|
| N1 | `PUT /api/settings` применял ключи по очереди: ключи до ошибочного уже сохранялись, хотя ответ 400 | `api/test_settings.py::test_write_is_atomic` (×4), `test_failed_downloads_dir_leaves_other_keys_untouched`, `test_invalid_key_prevents_downloads_dir_change` | **исправлено в S2a** (`1070aa8`): все ключи проверяются заранее и пишутся одной транзакцией |
| N2 | При обрыве CDN посередине ответа прокси теряет уже полученные, но ещё не отданные байты (читает кусками по 64 КБ). Плеер видит короткое тело и перезапрашивает диапазон | `api/test_stream_proxy.py::test_upstream_break_mid_stream_ends_response`, `test_upstream_break_over_real_server_lets_player_continue` (настоящий uvicorn, с длиной и chunked), `test_upstream_closing_early_without_error_is_not_success`; сегменты HLS — `api/test_hls_proxy.py::test_segment_break_keeps_received_bytes`; загрузки — `api/test_downloads.py::test_upstream_break_mid_portion_resumes_without_loss` | **исправлено в S8** (`d77c187`): байты отдаются по мере получения, обрыв прерывает ответ без завершающего сообщения |
| N4 | (S7, живая проверка) SoundCloud отдаёт прогрессивный mp3 лишь в части ответов: 4 из 9 резолвов трека `soundcloud_track_id`, в остальных — только HLS (`m3u8_native`). Плеер требует прогрессивный поток, резолв падает с `Requested format is not available`, пользователь видит текст «изменил формат ответа / yt-dlp устарел» (`extractor`) | `unit/test_format_choice.py` (живой список форматов), `api/test_hls_proxy.py`, `api/test_hls_browser.py`, `api/test_hls_downloads.py`; live — `live/test_live_hls.py` (LIVE-S8-3: 6 из 6 резолвов воспроизводимы) | **исправлено в S8** (`a174239`, `eae548a`, `620d340`): HLS через backend-прокси и hls.js, загрузка HLS-only в `.mp3` / `.m4a`, честные ошибки для неподдерживаемых вариантов |
| N3 | Запись индекса загрузок с `folder`, выходящим за каталог загрузок (`../`), даёт HTTP 500: чужой файл не отдаётся (`library.folder_path` бросает `ValueError`), но ответ — ошибка сервера, а не отказ | `api/test_stream_local.py::test_index_row_outside_downloads_is_not_served` (как есть: 500, посторонний файл не прочитан) | **S11 и S13**, критерии безопасности в ROADMAP: ни один эндпоинт не читает, не отдаёт и не удаляет файлы вне разрешённых каталогов, ответ — отказ, а не 500 |
