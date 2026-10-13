# Дорожная карта «ВОЛНА»

Основание: [REPOSITORY_AUDIT.md](REPOSITORY_AUDIT.md), [PRODUCT_GAP_ANALYSIS.md](PRODUCT_GAP_ANALYSIS.md), [TARGET_ARCHITECTURE.md](TARGET_ARCHITECTURE.md), [TEST_STRATEGY.md](TEST_STRATEGY.md).

## Правила исполнения

1. Работа ведётся в отдельных ветках от `main`. В `main` — только после согласования владельцем. Без force-push.
2. **Один этап — одна ветка, один логический слой — один коммит.** Архитектурные, визуальные и функциональные изменения не смешиваются.
3. Каждый этап начинается с тестов, которые фиксируют текущее поведение. Затем идёт изменение, затем отчёт по формату TEST_STRATEGY §10.
4. Следующий этап начинается только после подтверждения владельца.
5. Этап закрыт, когда выполнены все критерии приёмки. Критерии со статусом BLOCKED (Windows или live) явно перечисляются в отчёте. Этап считается «закрыт с ограничениями», пока они не выполнены на реальном окружении.
6. Если этап открывает новую зависимость или рискованный поворот архитектуры, сначала обновляется этот документ, затем согласование.

## Граф зависимостей

```mermaid
graph TD
  S0[S0 Гигиена и воспроизводимость] --> S1[S1 Регрессионные тесты]
  S1 --> S2[S2 P0-исправления a–e]
  S1 --> S3[S3 YouTube: yt-dlp-ejs + Deno]
  S1 --> S5[S5 Политика источников]
  S3 --> S4[S4 Инсталлятор]
  S2 --> S4
  S1 --> S6[S6 SQLite]
  S1 --> S7[S7 Адаптеры провайдеров]
  S5 --> S7
  S7 --> S8[S8 Движок стриминга]
  S1 --> S9[S9 Декомпозиция app.js]
  S2 --> S9
  S9 --> S10[S10 Очередь и плеер]
  S6 --> S10
  S8 --> S11[S11 Загрузки]
  S5 --> S11
  S6 --> S11
  S9 --> S12[S12 Доступность]
  S7 --> S13[S13 Безопасность API]
  S6 --> S13
  S7 --> S14[S14 Рекомендации]
  S6 --> S14
  S10 --> S14
  S6 --> S15[S15 Библиотека]
  S9 --> S15
  S8 --> S16[S16 Производительность]
  S9 --> S17[S17 Дизайн-система]
  S12 --> S17
  S17 --> S18[S18 Редизайн экранов]
  S10 --> S18
  S15 --> S18
  S4 --> S19[S19 Release gate]
  S18 --> S19
  S13 --> S19
  S16 --> S19
```

---

## P0 — честный работоспособный выпуск и безопасное развитие

### S0. Гигиена репозитория и воспроизводимость

Закрывает H4 (защита данных в e2e) и H5. Окружение: нет.

**Статус: выполнено 2026-10-12, ветка `claude/s0-repository-hygiene`; отчёт — [reports/S0.md](reports/S0.md).**

| | |
|---|---|
| Изменения | `.gitignore` (сборка, окружения, базы, журналы, медиа, секреты, скриншоты). `.gitattributes`: LF в репозитории, CRLF при checkout для `.bat/.cmd/.iss`. `README.md`: требования, установка, запуск, тесты, сборка, переменные окружения, ограничения. Зависимости: runtime в `backend/requirements.txt` без `sqlalchemy`, с явным `certifi`, нижние границы не менялись; `requirements-dev.txt` (playwright 1.56.0, pyflakes 3.4.0); `constraints.txt` — снимок чистого venv на Python 3.12; `build_windows.bat` ставит зависимости с `-c constraints.txt`. Зависимости сборки пока не закреплены, перенесено в S3–S4: закрепление без проверки сборки на Windows было бы непроверенным. Защита e2e от реального профиля, скриншоты во временную папку. Раннеры `tests/run_e2e.py` и `tests/run_checks.py` (гигиена git, compileall, pyflakes, node, e2e). Удалены три неиспользуемых импорта и переменная (L3). `pytest` появится в S1 вместе с тестами. |
| Модули | `.gitignore`, `.gitattributes`, `README.md`, `backend/requirements.txt`, `requirements-dev.txt`, `constraints.txt`, `packaging/requirements-build.txt`, `build_windows.bat` (одна строка), `tests/e2e_smoke.py` (только защита и путь скриншотов), `tests/run_e2e.py`, `tests/run_checks.py`, `backend/main.py`, `backend/recs.py`, `backend/downloads.py` (импорты) |
| Тесты | `tests/run_checks.py`; отказ e2e на нетестовом профиле; BLOCKED без Playwright и pyflakes |
| Критерии приёмки | (1) `git status` чистый после всех проверок — **PASS**. (2) Чистый venv: `pip install … -c constraints.txt` → **PASS**, `pip freeze` совпадает с `constraints.txt`. (3) e2e через раннер 109/109 — **PASS** (Linux, Chromium 141). (4) e2e против профиля без метки → код 2, база не изменена — **PASS**. (5) README по шагам на Linux — **PASS**; на Windows — **NOT RUN**. (6) Поведение приложения не изменилось — **PASS** (в коде приложения удалены только импорты; e2e 109/109). |
| Коммиты | `8c7e502` chore: restore .gitignore · `f25786a` build: split runtime/dev dependencies and add tested constraints · `055817e` chore: add .gitattributes · `9f5b172` test: guard e2e against the real user profile, add runners · `c742b02` chore: remove unused imports · `cd71320` docs: add README · документация и отчёт — следующим коммитом |

### S1. Регрессионные тесты (characterization)

Закрывает H4. Зависит от S0. Окружение: нет; LIVE-1 и LIVE-6 — по возможности.

**Статус: выполнено 2026-10-13, ветка `claude/s1-regression-tests` (от `claude/s0-repository-hygiene`); отчёт — [reports/S1.md](reports/S1.md).** Итог: 395 тестов pytest (379 PASS, 16 strict xfail с ID находок, 0 FAIL), e2e 109/109, CI зелёный на Linux и Windows. Код приложения не менялся. Отклонения от плана: маркер `windows` не заведён (Windows-only автотестов пока нет, Windows-job гоняет весь набор); добавлены тесты, которых не было в плане: `test_isolation`, `test_track_contract`, `test_catalog_api`, `test_local_api_security`, `test_sqlite_schema`, `test_lyrics_api`.

| | |
|---|---|
| Изменения | `tests/conftest.py` (изоляция §3 стратегии), фикстуры провайдеров, тесты из TEST_STRATEGY §5 с пометкой «S1». Код приложения **не меняется**. Дефекты, которые обнаружат тесты, фиксируются как `xfail(strict=True, reason="<ID находки>")` и исправляются на своём этапе. **CI (ADR-8)**: GitHub Actions — `ubuntu-latest`: `run_checks.py` (с pytest и e2e); `windows-latest`: `run_checks.py` (с pytest и e2e в Chromium) и сборка `dist\Wave` через PyInstaller как проверка spec. Шаг установщика добавляется в S4. Без секретов и без обращений к живым источникам. |
| Модули | `tests/**`, `pytest.ini` (маркеры `live`, `windows`), `requirements-dev.txt` (+ pytest), `constraints.txt`, `tests/run_checks.py` (+ шаг pytest), `.github/workflows/ci.yml` (новый) |
| Тесты | `test_normalize_yt/sc`, `test_catalog_cards`, `test_ranges`, `test_stream_proxy`, `test_stream_local`, `test_stream_lifespan` (xfail M1), `test_search_isolation`, `test_recommendations`, `test_userdata`, `test_settings`, `test_playlists`, `test_downloads`, `test_lyrics` |
| Результат по критериям | (1) `python -m pytest` → **PASS** на Linux (Python 3.12.3) и в CI на `ubuntu-latest` и `windows-latest`; xfail только с ID: H3, L2, M1, M3, M4, M5, M6, M7, M8. (2) Ветки Range покрыты — **PASS**: `test_ranges`, `test_stream_proxy`, `test_stream_local`. (3) Сеть: аудит-хук блокирует любые не-loopback соединения, нарушение роняет тест даже при перехвате исключения в коде приложения; `FakeNet` роняет тест на неизвестный хост — **PASS** (`test_isolation`). (4) Запись вне временного каталога заблокирована тем же хуком; реальные каталоги сверяются в конце сессии — **PASS**. (5) e2e 109/109 — **PASS** (Linux Chromium; в CI ещё и Windows Chromium, не WebView2). (6) CI — **PASS**: [run 38310405338](https://github.com/Andrey3269/Wave/actions/runs/38310405338) (`checks (ubuntu-latest)`, `checks (windows-latest)`, `windows-build`). LIVE-1, LIVE-6 — **BLOCKED** (сеть среды: прокси отвечает 403 для music.youtube.com и lrclib.net). |
| Критерии приёмки | (1) `pytest -m "not live"` → PASS на Linux; xfail только с ID находок. (2) Покрыты все ветки Range: 200/206/416, суффикс, открытый конец, битый заголовок, протухшая ссылка, параллельный резолв = один вызов. (3) Тесты не обращаются в сеть: проверяется подменой транспорта, сетевой вызов роняет тест. (4) Ни один тест не пишет вне временного каталога. (5) e2e 109/109. (6) Оба job'а CI зелёные на ветке; ссылка на запуск в отчёте этапа. |
| Коммиты | `d230205` test: add isolated pytest harness · `25cbc0c` test: track normalization and Track contract · `334e678` test: range parsing, stream proxy, local playback and restarts · `f5d2471` test: sqlite user data, settings and playlists · `c871d23` test: provider failure isolation, catalog and recommendations · `7f7c49d` test: downloads and lyrics · `83f6c6b` ci: GitHub Actions checks on Linux and Windows, PyInstaller spec build · `0334321` test: default Content-Type when the CDN sends none · документация и отчёт — следующим коммитом |

### S2. P0-исправления

Каждый пункт — отдельный коммит с тестом. Зависит от S1.

**Статус: выполнено 2026-10-13, принято владельцем 2026-10-13 с ограничениями (WIN-5, WIN-12 — NOT RUN), ветка `claude/s2-p0-fixes` (от `claude/s1-regression-tests`, `6401e94`); отчёт — [reports/S2.md](reports/S2.md).** Итог: pytest 469 PASS + 13 strict xfail (снято 3: H3, M1, M4), e2e: smoke 122 проверки (было 109) + новый `e2e_settings.py` 24 проверки (три запуска backend: порт A, порт B, чистый профиль), 0 FAIL. Коммиты: `1070aa8` S2a · `b72b6cd` S2b · `21ddcab` S2c · `259599c` S2d · `4df5b50` S2e. WIN-5 и WIN-12 — **NOT RUN** (нужна Windows с WebView2), этап «закрыт с ограничениями» до их выполнения.

Отклонения от плана в таблице ниже:
- **S2a.** Кроме ключей добавлены служебная отметка переноса (`meta.local_ui_migrated`) и отдельный `POST /api/settings/migrate-local`: перенос выполняется на сервере один раз на профиль и пишет только отсутствующие ключи, поэтому устаревший localStorage другого порта не затирает новые значения. Запись `PUT /api/settings` атомарна (N1). Тело `PUT` теперь `dict[str, Any]`: число в прежнем ключе даёт 400 «Недопустимое значение настройки.» вместо 422 FastAPI. e2e смены порта вынесен в `tests/e2e_settings.py` (раннер перезапускает backend на другом порту с тем же профилем); в pytest — `ApiHarness.serve()`, два настоящих uvicorn на разных портах.
- **S2b.** Название `stream.ResolverState` (`app.state.resolver`); кроме семафора в нём задачи резолва, ключи прогрева и фоновые задачи. Ссылки (`_cache`) и «головы» (`_heads`) остались данными процесса.
- **S2c.** Кроме `failed_sources` и `source_status` добавлено поле `radio` (план из PRODUCT_GAP_ANALYSIS §3: «радио трека отдельным полем»).
- **S2d.** Статусы: нет сети, ограничение частоты и проверка «не бот» — 503 (было 502); «Requested format is not available» — 502 `extractor` (было 404 «Трек недоступен»). Новый заголовок `X-Wave-Error` с типом ошибки.
- **S2e.** Тексты из PRODUCT_GAP_ANALYSIS §3 с уточнениями: заголовок ряда миксов зависит от того, есть ли в нём радио по истории («Миксы по вашим трекам» / «Тематические миксы»); подпись ленты: «Подобрано по вашей истории» / «Похоже на текущий трек» / «С чего начать» / «Подборка». **Тексты утверждены владельцем 2026-10-13.**

| Пункт | Что | Модули | Тесты и проверки | Критерии приёмки | Окружение |
|---|---|---|---|---|---|
| **S2a** (H3) | Настройки UI в SQLite через `/api/settings`: `volume`, `last_volume`, `lyrics_visible`, `profile_name`, `mix_covers`; однократная миграция из localStorage; debounce записи громкости; `GET /api/boot.js` для темы без мигания | `backend/userdata.py`, `backend/main.py` (boot.js), `frontend/app.js` (LS → settings), `frontend/index.html` | `test_settings.py`: снять xfail `test_ui_settings_are_persisted_by_backend` (H3), новые ключи, валидация, сохранение между двумя запусками backend на одном профиле (образец — `test_settings_survive_backend_restart`); **e2e**: выставить настройки на порту A, перезапустить backend на порту B с тем же профилем → значения на месте | API аддитивен, старые ключи и ответы не изменились; e2e-проверка смены порта PASS; WIN-5 PASS на Windows | Windows — WIN-5 |
| **S2b** (M1) | Асинхронное состояние резолвера в `lifespan` (`app.state.resolver`) | `backend/stream.py`, `backend/main.py` | `test_stream_lifespan.py::test_restart_after_contention_keeps_resolving` из xfail → PASS; убрать сброс `_resolve_sem` в `tests/conftest.py:reset_backend_state`; весь `test_stream_proxy` PASS | два lifespan подряд без `RuntimeError`; WIN-12 PASS | Windows — WIN-12 |
| **S2c** (M4) | `/api/recommendations` возвращает `failed_sources` и `source_status`; при пустом результате из-за сбоя фронтенд пишет «Источник сейчас недоступен» с кнопкой «Повторить»; `personalized` честный | `backend/recs.py`, `frontend/app.js` | `test_recommendations.py::test_all_candidate_sources_failed_is_reported` из xfail → PASS, `test_all_candidate_sources_failed_returns_empty_200` обновить; e2e: мок 502 от источника → видимое сообщение | пользователь отличает «нет данных» от «источник недоступен»; поле аддитивно | — |
| **S2d** (L6) | Человеческие тексты ошибок стрима: «нет сети» отдельно от «yt-dlp/куки» | `backend/stream.py` | `test_ranges.py::test_network_failure_message_is_misleading` заменить проверкой новых текстов по типам ошибок | нет вводящих в заблуждение сообщений | — |
| **S2e** (M13) | Описательные названия вместо «Моя волна», «Открытия недели», «Встряхнуть мою волну», «Под твою волну» (тексты — PRODUCT_GAP_ANALYSIS §3, финал утверждает владелец) | `frontend/index.html`, `frontend/app.js`, `tests/e2e_smoke.py` (условие готовности в строке 176 — в том же коммите, см. PRODUCT_GAP_ANALYSIS §3) | e2e 109/109 с обновлёнными ожиданиями | в UI нет фирменных названий конкурентов; число проверок не уменьшилось | — |

### S3. YouTube в распространяемой версии: `yt-dlp-ejs` и Deno

Закрывает H1 и L8. Зависит от S1. Окружение: **Windows и live обязательны**.

| | |
|---|---|
| Изменения | `yt-dlp[default]==<версия>` в зависимостях. `backend/jsruntime.py`: поиск, проверка версии, кэш (TARGET_ARCHITECTURE §4). `ydl_opts()` передаёт `js_runtimes`; задаются `DENO_DIR` и `DENO_NO_UPDATE_CHECK`. `GET /api/diagnostics`. `launcher.py --diagnostics [--live <id>]`. `packaging/runtime/deno.lock` (версия, URL, SHA-256), загрузка и сверка хэша в `build_windows.bat`, `licenses/` и `THIRD_PARTY_NOTICES.txt`, `wave.spec` (`runtime/`, `licenses/`). Понятное сообщение UI, если runtime не найден. |
| Модули | `backend/requirements.txt`, `constraints.txt`, `backend/jsruntime.py` (новый), `backend/stream.py` (`ydl_opts`), `backend/soundcloud.py` (общие `ydl_opts`), `backend/main.py` (diagnostics), `backend/launcher.py`, `packaging/wave.spec`, `packaging/runtime/deno.lock`, `build_windows.bat`, `licenses/**`, `frontend/app.js` (сообщение) |
| Тесты | `test_jsruntime.py`; `test_stream_proxy` без регрессий; LIVE-2, LIVE-3, LIVE-4; WIN-1, WIN-6, WIN-7 |
| Критерии приёмки | (1) Unit-тесты PASS на Linux и Windows. (2) `Wave.exe --diagnostics` в собранной папке сообщает runtime `deno <версия ≥ 2.3.0> supported=true` и `yt_dlp_ejs` с версией. (3) **LIVE-2/LIVE-3 PASS в собранном приложении на Windows**: публичный трек стартует, перемотка работает. (4) WIN-7: после закрытия нет `deno.exe`. (5) Нет окна консоли при резолве. (6) Лицензии в комплекте. (7) Размер дистрибутива до и после зафиксирован в отчёте. **До выполнения 3–5 проблема H1 не считается исправленной.** |
| Коммиты | `build: require yt-dlp[default] (yt-dlp-ejs)`; `feat(stream): resolve and verify JS runtime for yt-dlp`; `feat: diagnostics endpoint and --diagnostics flag`; `build: bundle pinned Deno with checksum and licenses` |
| Реализация (2026-10-13) | Ветка `claude/s3-ytdlp-deno` от `claude/s2-e2e-stability` @ `a9e1479`, отчёт [reports/S3.md](reports/S3.md). Отличия от плана: вместо `yt-dlp[default]` — явные `yt-dlp==2026.8.19` + `yt-dlp-ejs==0.8.0` (extra тянет GPL-пакет `mutagen`; **решение владельца 2026-10-13: оставить отдельные закрепления**); Deno 2.9.7; загрузка и сверка хэша — `packaging/fetch_runtime.py` (вызывается из `build_windows.bat` и CI), а не код в `.bat`; лицензии в `licenses/` (включая `THIRD_PARTY_NOTICES.txt`); `--live <videoId>` и LIVE-2/3/4 реализованы, но в облачной среде BLOCKED. Критерии: (1) PASS на Linux и Windows; (2) PASS в CI на собранном `Wave.exe` (`deno 2.9.7 supported=true`, yt-dlp-ejs 0.8.0); (3) LIVE-2/3 в собранном приложении — **NOT RUN/BLOCKED**; (4) WIN-7 — **NOT RUN** (в CI после диагностики `deno.exe` не остаётся, тест Job Object PASS на Windows); (5) окно консоли — CREATE_NO_WINDOW, тест PASS на Windows, визуально в WebView2 — **NOT RUN**; (6) лицензии в комплекте — PASS (полный перечень для всего дистрибутива — S4); (7) размер: 54,0 → 147,1 МБ. **H1 не считается исправленной до выполнения 3–5 на Windows.** |

### S4. Инсталлятор

Закрывает H2. Зависит от S3 (состав сборки) и S2 (версия и настройки). Окружение: **Windows обязательно**.

| | |
|---|---|
| Изменения | `installer/wave.iss` по TARGET_ARCHITECTURE §12: per-user установка, WebView2-bootstrapper, `AppMutex`, данные пользователя сохраняются. `backend/version.py` — единый источник версии. `build_windows.bat`: `mkdir installer`, проверка `--diagnostics`, `/DAppVersion`. `WAVE_PORT` (только переменная окружения, для WIN-13). Удаление `make_icon.py` (L4) — после подтверждения, что он не используется. |
| Модули | `installer/wave.iss` (новый), `build_windows.bat`, `packaging/wave.spec`, `backend/version.py` (новый), `backend/main.py` (версия API), `backend/launcher.py` (`WAVE_PORT`), `packaging/make_icon.py` |
| Тесты | WIN-1…WIN-4, WIN-8, WIN-9, WIN-13, WIN-15 |
| Критерии приёмки | (1) Чистая машина: `build_windows.bat` даёт `dist\installer\Wave-Setup-<версия>.exe`. (2) Установка без прав администратора; запуск; воспроизведение (вместе с S3). (3) Обновление поверх сохраняет данные. (4) Удаление не трогает `%LOCALAPPDATA%\Wave` и загрузки. (5) В дистрибутиве нет пользовательских данных и секретов (WIN-15). (6) Инсталлятор устанавливает настоящую собранную папку `dist\Wave`, а не заглушку. |
| Коммиты | `build: single version source`; `build(installer): add Inno Setup script`; `build: fix build_windows.bat installer steps and add diagnostics gate` |

### S5. Политика источников и скачивание

Зависит от S1. Окружение: LIVE-5 не требуется, SoundCloud выключен. Решения владельца: ADR-5, ADR-6 (TARGET_ARCHITECTURE §6, §14).

| | |
|---|---|
| Изменения | `providers/policy.py`: `yt` — `enabled=True, download_allowed=False`; `sc` — `enabled=False, download_allowed=False`; `local` — разрешён. Проверки политики на backend во всех точках входа: `POST /api/downloads` → 403 `download_not_allowed`; поиск, каталог, рекомендации, радио и `/api/stream/sc/...` для выключенного источника → 403 `source_disabled`. Уже скачанные файлы (включая `sc`) по-прежнему отдаются с диска. Фронтенд: пояснение вместо кнопки «Скачать», SoundCloud в переключателе помечен «недоступен в этой версии», понятные тексты ошибок. Адаптер SoundCloud остаётся в коде за интерфейсом. Переопределение политики `WAVE_SOURCE_OVERRIDES` действует только в dev-запуске и игнорируется в собранной версии. |
| Модули | `backend/providers/policy.py` (новый), `backend/main.py`, `backend/catalog.py`, `backend/recs.py`, `backend/stream.py`, `backend/downloads.py`, `backend/soundcloud.py`, `frontend/app.js`, `frontend/index.html`, `tests/e2e_smoke.py` |
| Тесты | `test_policy.py` (матрица; обход прямым запросом невозможен; `WAVE_SOURCE_OVERRIDES` игнорируется в frozen-режиме); `test_downloads` (403, существующая загрузка играет); `test_search_isolation` (выключенный `sc` не опрашивается); e2e: пояснение у кнопки скачивания, недоступный SoundCloud в переключателе, «Скачанные» с существующим файлом воспроизводятся |
| Критерии приёмки | (1) Ни один API-запрос не запускает загрузку с `yt` или `sc` и не обращается к SoundCloud (проверка подменой транспорта: сетевой вызов `sc` роняет тест). (2) Существующие загрузки не удалены и играют. (3) Импорт и воспроизведение локальных файлов политикой не блокируются. (4) Тексты UI не обещают недоступных функций. (5) Число проверок e2e не уменьшилось: проверки, которые опирались на скачивание или SoundCloud, заменены проверками нового поведения. |
| Коммиты | `feat(policy): backend source capability policy`; `feat(api): enforce download and source policy on all entry points`; `feat(ui): explain unavailable downloads and disabled SoundCloud` |

---

## P1 — качество основных сценариев

### S6. Хранилище SQLite

Закрывает M5 и M6 (хранение). Зависит от S1.

- **Изменения.** `persistence/db.py` (PRAGMA, `transaction()`), `migrations.py` (`user_version`, v1 — текущая схема, бэкап `sqlite3.backup()`, хранятся три копии), репозитории. Атомарные операции с плейлистами. Ретеншн истории. Поля `explicit`, `plays`, `released` в `TrackIn`.
- **Модули.** `backend/library.py`, `backend/userdata.py`, `backend/playlists.py`, новые `backend/persistence/*`.
- **Тесты.** `test_db_migrations.py` на фикстурах баз «5a7f27c» и «без `downloads.root`»; `test_playlists` (конкурентное добавление → без 500, xfail снимается); `test_userdata` (поля сохраняются).
- **Критерии приёмки.**
  1. Миграция реальной структуры базы без потери строк, по количеству и выборочному содержимому.
  2. Бэкап создаётся до миграции.
  3. Сбой миграции даёт откат.
  4. Повторный запуск идемпотентен.
  5. e2e 109+.

### S7. Адаптеры провайдеров

Закрывает M2, M3, M9, M10 (частично). Зависит от S1 и S5. Live: LIVE-1, LIVE-5.

- **Изменения.** `providers/base.py` (Protocol, ошибки, cooldown для всех, пул потоков на провайдер), `youtube_music.py`, `soundcloud.py`, ограниченные LRU+TTL-кэши, `source_status` в ответах поиска.
- **Модули.** `backend/main.py`, `backend/catalog.py`, `backend/soundcloud.py`, `backend/recs.py`, `backend/lyrics.py`, новые `backend/providers/*`.
- **Тесты.** `test_search_isolation` (зависший провайдер не блокирует второй и не исчерпывает общий пул; xfail M3 `test_youtube_failure_enters_cooldown` снимается, `test_youtube_is_not_put_in_cooldown` удаляется вместе с поведением), тесты кэшей (лимит и TTL), прежние тесты нормализации без изменений.
- **Критерии приёмки.**
  1. Зависание одного источника на 30 с не задерживает ответы другого источника и SQLite-эндпоинтов.
  2. YouTube получает cooldown.
  3. Кэши ограничены.
  4. Ответы аддитивны: `failed_sources` сохранён.
  5. LIVE-1 PASS.

### S8. Движок стриминга

Закрывает L2 и структуру M1. Зависит от S7.

- **Изменения.** `streaming/ranges.py` (чистые функции), `resolver.py`, `proxy.py`, `local.py`, `router.py`. Поддержка `HEAD`; битый Range → 200.
- **Тесты.** Все тесты стрима; снимаются xfail L2: `test_stream_proxy.py::test_empty_range_is_not_forwarded`, `test_stream_local.py::test_malformed_range_is_ignored`, `test_stream_local.py::test_head_request_supported`; LIVE-3. **N2** (наблюдение S1): `test_stream_proxy.py::test_upstream_break_mid_stream_ends_response` переводится с «как есть» на ожидаемое поведение.
- **Критерии приёмки.**
  1. Поведение 200/206/416 и перерезолва идентично зафиксированному в S1; тексты и типы ошибок (`X-Wave-Error`) — как в S2d.
  2. `HEAD` → 200 с заголовками.
  3. e2e: seek, pause/resume, переключение, локальные файлы — PASS.
  4. **N2:** при обрыве CDN посередине ответа уже полученные байты отдаются клиенту полностью (нет потери до 64 КБ), ответ завершается так, что плеер корректно запрашивает продолжение.

### S9. Декомпозиция `frontend/app.js`

Закрывает M11. Зависит от S1 и S2.

- **Изменения.** Переход на ES-модули по TARGET_ARCHITECTURE §11, в порядке `core` → `components` → `player/queue` → `player/controller` → `views`. Без визуальных и функциональных изменений.
- **Модули.** `frontend/index.html` (`type="module"`), `frontend/app.js` → `frontend/js/**`.
- **Тесты.** `node --test tests/js` (`queue`, `format`); e2e 109/109 **после каждого коммита**; скриншоты до и после на 980/1366/1440/1920 совпадают визуально.
- **Критерии приёмки.**
  1. Каждый шаг — отдельный коммит с зелёным e2e.
  2. Модель очереди не делит массив со списком экрана (тест: сортировка списка не меняет очередь).
  3. Нет потери функций по чек-листу экранов.

### S10. Очередь и плеер

Закрывает M14. Зависит от S9 и S6. Windows: WIN-11.

- **Изменения.** Shuffle без повторов с историей для «назад»; repeat off/one/all; восстановление очереди и позиции после перезапуска (сериализация модели в SQLite через settings или отдельную таблицу — решается на этапе); запись в историю после порога (30 с или 50 % длительности, что раньше) с сохранением сигнала пропуска; «Открыть альбом» по ID, где источник его даёт; событие `offline` больше не уводит с экрана, а показывает баннер со ссылкой на «Скачанные».
- **Тесты.** `queue.test.mjs` (все режимы); e2e: повтор очереди, shuffle без повторов на 10 треках, восстановление после перезагрузки страницы; `test_userdata` (порог записи).
- **Критерии приёмки.** Перечисленное поведение подтверждено тестами; WIN-11 PASS.

### S11. Загрузки (для разрешённого контента)

Закрывает M8. Зависит от S8, S5, S6. Windows: WIN-14.

- **Изменения.** `DownloadManager` (лимит, отмена, `to_thread` для записи, восстановление и очистка `.part` во всех `root`), изоляция сбоев текстов и обложек, отмена в UI.
- **Тесты.** `test_downloads`: отмена, лимит, сбой LRCLIB (xfail снимается), завершение приложения посередине → чистый старт; **N3**: `test_stream_local.py::test_index_row_outside_downloads_is_not_served` и аналоги для обложки, текста и удаления.
- **Критерии приёмки.**
  1. Отмена освобождает ресурсы и удаляет `.part`.
  2. Не больше N параллельных загрузок.
  3. Битый ответ текстов не ломает загрузку.
  4. **Безопасность (N3):** запись индекса загрузок с `folder`/`root`, выходящими за каталог загрузок (`../`, абсолютный путь, другой диск), не приводит к чтению или удалению посторонних файлов ни одним эндпоинтом (`/api/stream`, `/api/local/.../cover`, тексты, `DELETE /api/downloads`) и не даёт 500: запись пропускается при чтении индекса или ответ 404, событие пишется в журнал.

### S12. Доступность

Закрывает M12. Зависит от S9.

- **Изменения.** Ссылки на артистов — фокусируемые элементы вне `button.track`, без вложенных интерактивных элементов; «Загрузить ещё» — настоящая `button`; меню: стрелки, Home/End, возврат фокуса; проверка контраста токенов.
- **Тесты.** e2e: Tab-обход ключевых экранов, открытие артиста с клавиатуры, меню стрелками; автоматическая проверка ролей и имён (сценарий Playwright без новых зависимостей).
- **Критерии приёмки.** Все основные сценарии выполнимы без мыши; e2e PASS.

### S13. Безопасность локального API

Закрывает M7, M6 (валидация), L1. Зависит от S7 и S6.

- **Изменения.** `OriginGuard`, серверная валидация `TrackIn` (`stream_url` вычисляется сервером, URL обложек из allowlist), маскирование трейсбеков, фильтрация невалидных старых строк при чтении (включая индекс загрузок, N3).
- **Тесты.** `test_origin_guard` (xfail M7 `test_local_api_security.py::test_cross_origin_mutation_is_refused` снимается), `test_userdata` (валидация; xfail M6 `test_saved_favorite_can_always_be_removed`), тест маскирования журнала, проверка N3 на всех эндпоинтах, читающих файлы по индексу.
- **Критерии приёмки.**
  1. Кросс-доменный POST → 403.
  2. Фронтенд и e2e работают без изменений.
  3. В журнале нет подписей URL и IP из googlevideo.
  4. **N3:** ни одна запись базы (индекс загрузок, `downloads_dir` в настройках) не позволяет прочитать, отдать или удалить файл вне разрешённых каталогов; ответ — отказ (404/400), а не 500.

---

## P2 — развитие продукта

| Этап | Содержание | Зависимости | Ключевые критерии приёмки |
|---|---|---|---|
| **S14. Рекомендации** | Явное «не рекомендовать трек/артиста» (таблица исключений), сброс сигналов, разнообразие (ограничение по артисту и альбому, новизна), видимые причины «почему этот трек», офлайн-оценка на фикстурах истории (метрики: доля новых артистов, повторы, доля пропусков). Собственная функция бренда «ВОЛНА» — после утверждения названия. Без ML и без выдуманных жанров и трендов. **Заголовок «Новое для тебя»** (ряд ленты на главной) заменить на нейтральное «Ещё из подборки» (решение владельца 2026-10-13); персональное название — только когда персонализация подтверждена реальными данными (метрики этапа). Если S18 начнётся раньше S14 — замена делается в S18 | S7, S6, S10 | детерминированные тесты скоринга; метрики на фикстурах не хуже базовой линии; LIVE-7; в UI нет «Новое для тебя», пока персонализация не подтверждена; e2e обновлён без уменьшения числа проверок |
| **S15. Библиотека** | Избранные альбомы и артисты, поиск и фильтры по коллекции, ручной порядок в плейлистах (`PATCH .../tracks/order`), удаление записи истории, экспорт и импорт JSON (избранное, плейлисты, настройки), импорт локальных аудиофайлов как источник `local` | S6, S9 | экспорт → импорт в чистый профиль восстанавливает данные 1:1; миграции схемы по S6; e2e |
| **S16. Наблюдаемость и производительность** | Тайминги холодного старта, лимиты памяти кэшей, профилирование длинных списков (виртуализация при необходимости), экран «О программе / Диагностика» | S8 | холодный старт и память измерены и зафиксированы; нет регрессии относительно базовой линии |
| **S17. Дизайн-система «ВОЛНА»** | Токены: глубокий графит, сине-бирюзовая шкала, точечные золотисто-оранжевые акценты, фирменная волна; типографика, отступы, движение, состояния фокуса; компоненты. Без изменения поведения. Фокус на уже открытом окне при повторном запуске (L5) | S9, S12 | контраст AA для текста; e2e 109+; скриншоты всех экранов в двух темах |
| **S18. Редизайн экранов** | Главная, поиск, артист/альбом, коллекция, плеер, очередь, тексты, плейлисты, загрузки, настройки, все состояния (пусто, загрузка, ошибка, повтор). Свой визуальный язык, без копирования конкурентов. «Новое для тебя» → «Ещё из подборки», если не сделано в S14 | S17, S10, S15 | e2e обновлён вместе с разметкой без уменьшения проверок; визуальный обзор на 980–1920; клавиатура |
| **S19. Release gate** | Полный прогон: unit, API, e2e, live-канарейки, Windows-чек-лист WIN-1…15 на двух ОС, чистая установка и обновление, итоговые документы и честный отчёт | S4, S18, S13, S16 | все критерии PASS или явно согласованные исключения |

---

## Сводка затрагиваемых модулей

| Модуль | Этапы |
|---|---|
| `backend/main.py` | S0, S2a, S2b, S3, S4, S5, S7 |
| `backend/launcher.py` | S3, S4, S17 |
| `backend/stream.py` → `streaming/*` | S2b, S2d, S3, S8 |
| `backend/catalog.py`, `soundcloud.py` → `providers/*` | S3, S5, S7 |
| `backend/recs.py` | S0, S2c, S7, S14 |
| `backend/library.py`, `userdata.py`, `playlists.py` → `persistence/*` | S2a, S6, S13, S15 |
| `backend/downloads.py` → `jobs/*` | S0, S2b, S5, S11 |
| `backend/lyrics.py` | S7, S11 |
| `backend/applog.py` | S13 |
| `backend/paths.py` | S3 (`resource_dir` для runtime), S15 |
| новые: `jsruntime.py`, `diagnostics.py` (S3), `version.py`, `providers/policy.py`, `persistence/*`, `streaming/*` | S3, S4, S5, S6, S8 |
| `frontend/app.js` → `frontend/js/**` | S2a, S2c, S2e, S3, S5, S9–S12, S14–S18 |
| `frontend/index.html`, `style.css` | S2a, S2e, S5, S9, S12, S17, S18 |
| `tests/e2e_smoke.py`, `tests/e2e_settings.py` | S0, S2a, S2c, S2e, S5, S10, S12, S18 |
| `packaging/wave.spec`, `build_windows.bat`, `installer/wave.iss` | S3, S4 |
| зависимости | S0, S3 |

## Что требует Windows и живых источников

| Этап | Windows | Живые источники |
|---|---|---|
| S0, S1 | — | LIVE-1, LIVE-6 (по возможности) |
| S2 | WIN-5 (S2a), WIN-12 (S2b) | — |
| S3 | WIN-1, WIN-6, WIN-7 — **обязательно** | LIVE-2, LIVE-3, LIVE-4 — **обязательно** |
| S4 | WIN-1…4, 8, 9, 13, 15 — **обязательно** | — |
| S5 | — | — (SoundCloud выключен политикой; LIVE-5 — только при будущем включении) |
| S7 | WIN-10 | LIVE-1, LIVE-5 |
| S8 | — | LIVE-3 |
| S10 | WIN-11 | — |
| S11 | WIN-14 | — |
| S14 | — | LIVE-7 |
| S19 | все WIN | все LIVE |

В текущей облачной среде Windows недоступна, а YouTube и SoundCloud заблокированы сетью. Эти проверки выполняет владелец на своей машине по чек-листу TEST_STRATEGY §7–8 либо в настроенном CI или на ВМ с доступом к сети. Результаты вносятся в таблицу статуса ниже.

## Статус этапов

| Этап | Статус | Ветка | Примечание |
|---|---|---|---|
| Аудит и документация | согласовано владельцем 2026-10-12 | `claude/engineering-docs` | решения ADR-1…ADR-9 в TARGET_ARCHITECTURE §14 |
| S0 | **выполнено**, подтверждено владельцем 2026-10-12 | `claude/s0-repository-hygiene` | [reports/S0.md](reports/S0.md); Windows-сборка и e2e в Chromium на Windows теперь проверяются CI (S1) |
| S1 | **выполнено**, подтверждено владельцем 2026-10-13 | `claude/s1-regression-tests` | [reports/S1.md](reports/S1.md); 379 PASS + 16 xfail, e2e 109/109, CI зелёный; LIVE-1/6 — BLOCKED, WebView2 — NOT RUN |
| S2 | **выполнено**, принято владельцем 2026-10-13 **с ограничениями** | `claude/s2-p0-fixes` | [reports/S2.md](reports/S2.md); 469 PASS + 13 xfail, e2e smoke 122 + настройки 24 (Chromium); тексты S2e утверждены. **WIN-5, WIN-12 — NOT RUN** до проверки на Windows с WebView2: эти сценарии не считаются проверенными |
| S2, стабильность e2e настроек | **исправлено**, принято владельцем 2026-10-13 | `claude/s2-e2e-stability` (не слита) | [reports/S2.md §9](reports/S2.md): причина — запоздавшие ответы `/api/settings` перезаписывали правки (гонка в `frontend/app.js`), воспроизведена детерминированно; `e2e_settings.py set` 8 → 11; CI на `f40d6b9` 5/5, на `17621a8` PASS (Chromium). WIN-5, WIN-12 — NOT RUN |
| S3 | **выполнено и принято владельцем 2026-10-13** | `claude/s3-ytdlp-deno` (не слита) | [reports/S3.md](reports/S3.md); pytest 589 (Linux и Windows CI, включая настоящий Deno 2.9.7), e2e 123 + 27. На Windows 11 x64: S3-часть сборки и `Wave.exe --diagnostics` — PASS; YouTube в собранном приложении, перемотка, следующий трек, отсутствие консольного окна и завершение `deno.exe` — PASS; LIVE-2/3/4 — PASS. Полный WIN-1 с установщиком и WIN-2…4, 8, 9, 13, 15 остаются в S4. H1 закрыта; `main` не менялся |
| S4–S19 | не начато | — | S4 начинается только по отдельной команде владельца |
