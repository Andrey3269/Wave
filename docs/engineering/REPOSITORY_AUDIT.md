# Аудит репозитория «ВОЛНА» (Wave)

| | |
|---|---|
| Дата | 2026-10-12 |
| Объект | `github.com/Andrey3269/Wave`, ветка `main`, коммит `5a7f27c` |
| Режим | только чтение; код не менялся |
| Среда проверки | Linux, Python 3.13.16, Node 22.22, Chromium 141 (Playwright 1.56); Windows/WebView2 недоступны; YouTube и SoundCloud недоступны по сети |

Метки достоверности:

- **[код]** — подтверждено чтением кода (указаны файл и строки);
- **[проба]** — подтверждено запуском в изолированной среде;
- **[runtime]** — требует проверки на реальном окружении (Windows, WebView2, живые источники);
- **[предп.]** — предположение, требует проверки.

Строки указаны для коммита `5a7f27c`.

---

## 1. Состояние репозитория

- 28 отслеживаемых файлов, ~7 400 строк. В истории `main` три коммита от 2026-10-12. **[код]**
- Удалённые ветки: `main` и `product-engineering` (`657d5dd`). Дерево `product-engineering` побайтно совпадает с `main`. Отличие только в истории: CLAUDE.md добавлен другим коммитом. Несмотря на сообщение коммита, `.gitignore` в этой ветке нет. **[код]**
- `installer/` и `installer/wave.iss` отсутствуют во всех доступных ветках и во всей истории. Локальная рабочая копия пользователя из этой среды недоступна (компьютер не подключён). Если файл есть только у вас на диске, его нужно прислать до этапа S4. **[код]**
- `.gitignore` был в первом коммите (`venv/ .venv/ __pycache__/ *.pyc .idea/`) и удалён во втором (`2cb056b`). Сейчас его нет. **[код]**
- README, документации разработчика, `docs/` и CI нет. **[код]**
- `netcheck.py`, `zapret*.py` и `__pycache__` из архивного snapshot не встречаются ни в ветках, ни в истории. Это артефакты архива, а не часть проекта. **[код]**

## 2. Фактическая архитектура

```
Wave.exe (PyInstaller onedir, console=False)
└─ backend/launcher.py
   ├─ single_instance(): mutex Local\Wave.Desktop.SingleInstance       (launcher.py:113-120)
   ├─ webview2_installed(): реестр EdgeUpdate                          (launcher.py:88-107)
   ├─ webview.create_window(splash) → boot() в потоке                  (launcher.py:225-236)
   │   └─ Backend.start(prefer_port=0) → uvicorn в потоке wave-backend (launcher.py:153-167)
   │       сокет 127.0.0.1:<случайный порт>
   ├─ watchdog(): до 2 перезапусков backend                            (launcher.py:202-222)
   └─ закрытие окна → server.should_exit → os._exit(code)              (launcher.py:263-278)

backend/main.py  FastAPI + lifespan (YTMusic, httpx.AsyncClient, init SQLite, cleanup_partial)
├─ /api/search            main.py:321-378    провайдеры yt/sc, кэш TTL, cooldown только для sc
├─ catalog.py             /api/search/{artists|albums}, /api/artist/..., /api/album/...
├─ recs.py                /api/recommendations — детерминированный скоринг (без ML)
├─ stream.py              /api/stream/{source}/{id} — локальный файл | head-кэш | yt-dlp → httpx-прокси
├─ userdata.py            /api/favorites, /api/history, /api/settings
├─ playlists.py           /api/playlists...
├─ downloads.py           /api/downloads..., /api/local/.../cover
├─ lyrics.py              /api/lyrics (LRCLIB)
└─ StaticFiles("/")       frontend/ (монтируется последним)

SQLite: paths.data_dir()/wave.db, одно соединение + threading.Lock  (library.py:17-49)
frontend: index.html (561), style.css (769), app.js (2543, один IIFE)
```

### Цепочки данных

**Поиск.** `app.js:fetchPage` (`app.js:531`) → `GET /api/search?sources=<yt|sc>` → `main.search` (кэш `(q, limit, offset, sources)` 300 с; cooldown 300 с только для `sc`; таймаут 6 с) → `asyncio.to_thread(ytmusicapi.search)` или `soundcloud.search_tracks` (внутренний `_call_api` yt-dlp) → `_normalize_yt` / `_normalize_sc` → `Track`.

**Воспроизведение.** `<audio src=track.stream_url>` с `Range` → `stream_track` (`stream.py:424-490`):

1. `library.audio_path` → `FileResponse`.
2. head-кэш (24 × 1 МБ).
3. `_connect` → `_resolve` (кэш URL по `expire`, `_inflight`-дедупликация, `Semaphore(4)`, yt-dlp в отдельном пуле).
4. httpx (IPv4) → диапазон обрезается до 1 МБ → 206.
5. На 401/403/404/410 — один перерезолв.

**Пользовательские данные.** UI → REST → `library._q` (commit на каждый запрос) → `wave.db`. Трек хранится JSON-снимком `TrackIn`.

**Загрузки.** `POST /api/downloads` → `asyncio.create_task(_run)` → порции по 4 МБ через `_connect` → каталог `*.part` → `os.replace` → строка `downloads`. Прогресс хранится в памяти (`jobs`), UI опрашивает `/api/downloads/jobs` раз в 800 мс.

**Тексты.** `/api/lyrics` → локальный `lyrics.json` (для скачанных) или LRCLIB → кэш в памяти.

## 3. Выполненные проверки

| Команда / действие | Результат |
|---|---|
| `git status` в клоне | чистый, ветка `main` |
| `python -m compileall -q backend packaging tests` | **PASS** |
| `node --check frontend/app.js` | **PASS** |
| `pyflakes backend packaging tests` (в проекте не настроен) | 3 замечания: `main.py:20` (`Path`), `recs.py:16` (`random`), `downloads.py:145` (`exc`) |
| `pip install -r backend/requirements.txt` в venv | **PASS**; разрешилось fastapi 0.143.0, starlette 1.7.0, uvicorn 0.54.0, httpx 0.28.1, yt-dlp 2026.8.19, ytmusicapi 1.12.3, SQLAlchemy 2.1.4 |
| `uvicorn main:app` с `WAVE_DATA_DIR` / `WAVE_DOWNLOADS_DIR` во временных каталогах; `GET /api/health` | **PASS** |
| `python tests/e2e_smoke.py http://127.0.0.1:8765 <tmp>` | **PASS** — 109 проверок (102 вызова `check()`, часть в циклах), 0 ошибок консоли. Не WebView2 |
| Range на локальном файле (100 000 байт) | **PASS**: без Range → 200; `0-`, `100-199`, `-500`, `99990-` → 206 с верным `Content-Range`; `200000-` → 416 `bytes */100000`. Битый `bytes=abc` → 400; `HEAD` → 404 |
| Живой поиск, стрим, артисты | **BLOCKED** (сеть). Наблюдаемое поведение при сбое: `/api/search` → 502 за ~0,3 с; второй запрос к `sc` → 502 за 4 мс (cooldown); `/api/recommendations` → **200 с пустым списком**; `/api/stream/yt/...` → 502 за ~2 с |
| `Host: evil.example` | **PASS**: 400 (TrustedHost) |
| Кросс-доменный `POST /api/settings/clear-history` (`Origin: http://evil.example`, `text/plain`) | **выполнен, 200** (см. M7) |
| `POST /api/favorites` с `text/plain` | 422 — FastAPI не принимает JSON под чужим content-type |
| Эксперимент: модульный `asyncio.Semaphore` во втором `asyncio.run` | `RuntimeError: ... bound to a different event loop` (см. M1) |
| Сборка Windows, launcher, WebView2, single-instance, media keys | **NOT RUN** |

## 4. Находки

Серьёзность: **High** — блокирует выпуск или безопасный рефакторинг; **Medium** — дефект или риск, заметный пользователю или поддержке; **Low** — гигиена.

### 4.1 High

| ID | Находка | Доказательство | Метка | Этап |
|---|---|---|---|---|
| H1 | **YouTube без JS-runtime.** Начиная с yt-dlp 2025.11.12 полная поддержка YouTube требует пакет `yt-dlp-ejs` и внешний JS-runtime. Установленный yt-dlp 2026.8.19 объявляет extra `default` с `yt-dlp-ejs==0.8.0`; по умолчанию включён только `deno`, путь задаётся опцией `js_runtimes={'deno': {'path': ...}}`; минимальные версии: Deno 2.3.0, Node 22, Bun 1.2.11, QuickJS 2023-12-09. В проекте стоит голый `yt-dlp`, ejs не устанавливается, `collect_all("yt_dlp_ejs")` молча пропускается, runtime в сборку не входит. В README yt-dlp сказано, что без runtime поддержка YouTube устаревшая и будет ухудшаться | `backend/requirements.txt:7`; `packaging/wave.spec:14-18`; в site-packages: `yt_dlp/YoutubeDL.py:543-549`, `yt_dlp/utils/_jsruntime.py:90,105,120,135`, `yt_dlp-*.dist-info/METADATA:41` | [код]; работоспособность — [runtime] | S3 |
| H2 | **Сборка инсталлятора не завершается.** `Invoke-WebRequest -OutFile installer\MicrosoftEdgeWebview2Setup.exe` пишет в несуществующий каталог; `ISCC installer\wave.iss` ссылается на отсутствующий файл | `build_windows.bat:18-28` | [код] | S4 |
| H3 | **Настройки в localStorage теряются при каждом запуске.** Backend стартует на случайном порту, а localStorage привязан к origin `http://127.0.0.1:<порт>`. Теряются имя профиля, громкость, последняя слышимая громкость, вкл/выкл текстов, обложки миксов; тема мигает до ответа `/api/settings`. Комментарий обещает обратное | `launcher.py:228` (`backend.start()` → порт 0), `:258-261`; `app.js:203-206, 679, 706, 1311, 1323, 1457, 2360-2367`; `index.html:7` | [код]; эффект в WebView2 — [runtime] | S2a |
| H4 | **Нет сети регрессионных тестов.** Ни одного unit/API-теста для stream и Range, провайдеров, нормализации и SQLite. В e2e внешние эндпоинты и `/api/stream` замоканы (WAV-ответ). Изоляцию данных e2e не проверяет: при запуске backend без `WAVE_DATA_DIR` тест пишет лайки, историю, плейлист, тему и имя в реальный профиль. Скриншоты по умолчанию идут в `tests/shots` внутри репозитория | `tests/e2e_smoke.py:8-12, 26, 82-90, 498-506` | [код] | S0, S1 |
| H5 | **Зависимости невоспроизводимы.** Только нижние границы, нет lock. `sqlalchemy` не импортируется нигде. `certifi` импортируется напрямую (`stream.py:29`), но не объявлен. `playwright` не объявлен. `pywebview` нужен для запуска `launcher.py` в разработке, но есть только в build-зависимостях. Минимальная версия Python (≥ 3.10: `dataclass(slots=True)`, `X \| Y` в pydantic-моделях) не указана. При старых fastapi/starlette `FileResponse` может не поддерживать Range [предп.: проверить changelog Starlette] | `backend/requirements.txt`, `packaging/requirements-build.txt` | [код] | S0 |

### 4.2 Medium

| ID | Находка | Доказательство | Метка | Этап |
|---|---|---|---|---|
| M1 | **Watchdog создаёт новый uvicorn и новый event loop, но модульные `_resolve_sem` и `_inflight` остаются от старого.** После конкуренции семафор привязывается к первому циклу и во втором бросает `RuntimeError` (воспроизведено отдельно) | `stream.py:143-144`; `launcher.py:215` | механизм — [проба]; сценарий — [runtime] | S2b |
| M2 | **Зависшие вызовы провайдеров не отменяются.** `asyncio.wait_for(asyncio.to_thread(...))` бросает таймаут, а поток продолжает работать. Все вызовы SoundCloud идут под глобальным `threading.Lock`. yt-dlp делает `retries=3` при `socket_timeout=7` | `catalog.py:58-59`; `soundcloud.py:56-94`; `stream.py:164-167` | [код]; эффект — [runtime] | S7 |
| M3 | **YouTube не входит в cooldown** (`OPTIONAL_SOURCES = {"sc"}`). При «висящей» сети каждый поиск ждёт таймаут 6 с | `main.py:226-231` | [код] | S7 |
| M4 | **Рекомендации прячут сбой.** Любые ошибки кандидатов превращаются в `200 {"tracks": [], "personalized": true}`. UI показывает «пока нечего предложить» вместо «источник недоступен» | `recs.py:137-145, 178` | [проба] | S2c |
| M5 | **SQLite без версионирования и транзакций.** Нет `PRAGMA user_version`; «миграция» — `ALTER TABLE` в try/except. Многошаговые операции не атомарны: `add_track` — проверка, вычисление позиции и вставка тремя запросами, гонка даёт `IntegrityError` → 500; `delete_playlist` — два запроса. `history` растёт без ограничений. Нет WAL, `busy_timeout`, бэкапа перед изменением схемы | `library.py:32-49`; `playlists.py:82-111`; `userdata.py:166-170` | [код] | S6 |
| M6 | **Дрейф контракта Track.** `TrackIn` не содержит `explicit`, `plays`, `released`: при сохранении в избранное, историю и плейлисты они теряются. `id` не проверяется регуляркой источника, `stream_url` и `cover` принимаются любыми. Запись с `/` в `id` сохраняется, но удалить её через `DELETE /api/favorites/{source}/{id}` нельзя | `userdata.py:37-49, 155-158` | [проба] | S6, S13 |
| M7 | **CSRF на локальный API.** `POST /api/settings/clear-history` без тела выполняется с чужого origin. Остальные мутирующие JSON-эндпоинты защищает только поведение FastAPI с content-type. Риск снижает случайный порт | `userdata.py:128-131`; `main.py:293-303` | [проба] | S13 |
| M8 | **Загрузки.** Нет отмены активной загрузки и лимита параллельности. Запись файла синхронная внутри event loop. `r.json()` в LRCLIB без защиты: битый ответ роняет загрузку на шаге `processing`, хотя аудио уже скачано. `cleanup_partial` чистит только текущий каталог загрузок | `downloads.py:60-87, 109-150, 153-156, 165-177`; `lyrics.py:50` | [код] | S11 |
| M9 | **Кэши в памяти без лимита.** `_artist_cache` хранит до 500 «сырых» треков на артиста; `lyrics._cache` не ограничен | `catalog.py:28, 172, 185`; `lyrics.py:23, 70` | [код] | S7, S16 |
| M10 | **SoundCloud через внутренние методы yt-dlp** (`_call_api`, `_update_client_id`) и неофициальный API v2 с `client_id` веб-клиента. Ломается при обновлении yt-dlp; противоречит условиям SoundCloud API (см. §5) | `soundcloud.py:27-51, 60-67` | [код] | S5, S7 |
| M11 | **Фронтенд-монолит.** 2543 строки в одном IIFE, глобальный мутабельный `state`. Очередь намеренно делит массив со списком на экране: `sortList` меняет и очередь (`app.js:356-360`). Больше 8 обработчиков на `document` | `app.js` | [код] | S9 |
| M12 | **Доступность.** Ссылки на артистов — `span.who` внутри `button.track`, с клавиатуры недоступны. `div.more[role=button]` не обрабатывает Enter и Space. В меню нет навигации стрелками и не возвращается фокус к кнопке-якорю | `index.html:536-552`; `app.js:394-404, 1961-2002, 2494-2495` | [код] | S12 |
| M13 | **Честность названий.** «Моя волна» — название фирменной функции Яндекс Музыки. «Открытия недели — новые имена и свежие треки» на деле поиск `новинки <год>`. Флаг `personalized` равен `true` уже при наличии текущего трека | `app.js:672-678, 983, 993`; `index.html` (тексты «Моя волна»); `recs.py:178` | [код] | S2e |
| M14 | **Неточные сигналы и поведение плеера.** Прослушивание пишется в историю в момент старта, так что ошибки и мгновенные пропуски тоже считаются. Shuffle выбирает с повторами, «назад» его не учитывает. Режима «повтор очереди» нет, `repeat` — это только `audio.loop`. «Открыть альбом» ищет по имени и берёт первый результат. Событие `offline` принудительно уводит на «Скачанные» | `app.js:1037-1050, 1071-1089, 1104-1108, 2036-2046, 2524` | [код] | S10 |

### 4.3 Low

| ID | Находка | Доказательство |
|---|---|---|
| L1 | `RedactFilter` не маскирует трейсбеки (`exc_info`); URL googlevideo содержат IP пользователя | `applog.py:23-32` |
| L2 | `HEAD /api/stream/...` → 404 (мимо маршрута в StaticFiles); битый `Range` на локальном файле → 400, а RFC 9110 предписывает игнорировать | [проба] |
| L3 | Неиспользуемые импорты и переменные (3 замечания pyflakes); docstring «Шаг 1» в `main.py:1-11` устарел; CORS указывает на dev-порты 8000 и 5500 | `main.py`, `recs.py`, `downloads.py` |
| L4 | `packaging/make_icon.py` дублирует `svg_to_ico.py` (рисует другую иконку); в spec лишний hidden-import `multipart` | `packaging/` |
| L5 | Повторный запуск показывает MessageBox вместо фокуса на уже открытом окне | `launcher.py:244-246` |
| L6 | При отсутствии сети показывается «проверьте yt-dlp/куки» | `stream.py:234` |
| L7 | Палитра: коралловый акцент `#FF654A` без сине-бирюзовой шкалы, заявленной в айдентике «ВОЛНЫ» (задача дизайна, не дефект) | `style.css:13-40` |
| L8 | Комментарий launcher «дочерних процессов нет» перестанет быть правдой после H1: yt-dlp запускает JS-runtime через `subprocess` | `launcher.py:8`; `yt_dlp/.../jsc/_builtin/deno.py:101-110` |

## 5. Внешние источники и условия использования

Ниже — техническая сводка, не юридическое заключение. Формулировки условий меняются; перед выпуском нужна проверка юристом.

| Источник | Как используется | Официальный статус | Ограничения из условий | Риск |
|---|---|---|---|---|
| YouTube Music, метаданные | `ytmusicapi`: эмуляция веб-клиента без авторизации | неофициальная библиотека; официального API YouTube Music нет | Условия YouTube (ред. 15.12.2023) запрещают автоматизированный доступ, кроме разрешённых случаев | средний: может перестать работать, юридически спорно |
| YouTube, аудиопоток | yt-dlp резолвит прямую ссылку, httpx проксирует | неофициально | те же условия: запрет на доступ и загрузку контента, кроме явно разрешённых сервисом способов, и на обход защиты от копирования | высокий для загрузок; средний для потоковой передачи |
| YouTube, скачивание | `downloads.py` сохраняет файл на диск | — | скачивание запрещено, кроме разрешённых сервисом функций | **высокий** |
| SoundCloud, метаданные и поток | API v2 с `client_id` веб-клиента, взятым yt-dlp | официальный API существует, но требует регистрации приложения и собственного `client_id`; v2 — внутренний | Условия SoundCloud API требуют зарегистрированный `client_id` и атрибуцию автора и SoundCloud; запрещают объединять контент SoundCloud с контентом других сервисов в одном воспроизведении и строить агрегированные on-demand сервисы | **высокий** |
| SoundCloud, скачивание | `downloads.py` | — | Условия API запрещают функцию сохранения файлов и офлайн-доступ, даже если автор разрешил скачивание на сайте | **высокий** |
| LRCLIB, тексты | публичный API без ключа | официальный публичный API | нужен корректный User-Agent (сейчас `Wave/0.1`) | низкий |

Вывод для продукта. Существующие загрузки с YouTube и SoundCloud противоречат текущим условиям этих платформ. По решению владельца функцию не удаляем, а ставим за политику возможностей источника (`download_allowed`). Уже скачанные пользователем файлы продолжают воспроизводиться, данные не удаляются. Детали — в TARGET_ARCHITECTURE.md §6 и ROADMAP.md (этап S5).

## 6. Сильные стороны (сохранить)

- Один `<audio>`, одна очередь, делегирование событий через `data-do`; весь рендер через `textContent`, без `innerHTML` (`app.js:21-26`).
- `inert` и ловушка фокуса в полноэкранном плеере, `prefers-reduced-motion`, `:focus-visible`.
- `TrustedHostMiddleware`, сокет только на `127.0.0.1`, `SO_EXCLUSIVEADDRUSE` на Windows.
- Защита от выхода за пределы каталога в `library.folder_path`; атомарная сборка загрузки в `*.part`.
- Дедупликация резолвов, перерезолв протухших ссылок, IPv4 для googlevideo.
- Изоляция сбоя SoundCloud через cooldown; честные сообщения о недоступных альбомах (Go+/регион).
- e2e-тест широкий и стабильный: 109 проверок проходят.

## 7. Открытые вопросы

1. Есть ли у владельца локально `installer/wave.iss` или другие неотправленные изменения?
2. Можно ли получить зарегистрированный `client_id` SoundCloud API и согласиться с его условиями (атрибуция, запрет смешивания источников)?
3. Целевая минимальная версия Windows: 10 22H2 или 11? Нужна ли поддержка ARM64?
4. Размер дистрибутива: допустимо ли +60–120 МБ за счёт Deno? [предп.: точный размер измерить на этапе S3]
