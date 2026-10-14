# Целевая архитектура «ВОЛНА»

Документ описывает, куда эволюционно двигается код. Это не план одномоментной переписки. Порядок и критерии — в [ROADMAP.md](ROADMAP.md), проверки — в [TEST_STRATEGY.md](TEST_STRATEGY.md).

## 0. Неизменные рамки

- Стек остаётся: Python ≥ 3.10 (сборка на 3.12), FastAPI, uvicorn, pywebview/WebView2, SQLite через `sqlite3`, vanilla HTML/CSS/JS.
- Без решения владельца не вводим React/Vue/Svelte, TypeScript-сборку, бандлер, ORM, async-SQL, облако, аккаунты и телеметрию.
- Публичные контракты сохраняются: `Track`, URL-схема `/api/...`, `stream_url = /api/stream/{source}/{id}`, ID в DOM, на которые опирается e2e. Изменения только **аддитивные**. Несовместимое изменение — только по отдельному плану миграции (раздел 11).
- Данные пользователя (`wave.db`, каталог загрузок) не удаляются и не перезаписываются. Любая миграция схемы предваряется резервной копией.

## 1. Backend: целевые границы

Переносим постепенно, сохраняя старые модули как фасады, пока на них есть ссылки.

```
backend/
  launcher.py            оболочка Windows: окно, single-instance, watchdog, --diagnostics
  main.py                только композиция: create_app(), middleware, lifespan, include_router
  version.py             __version__ — единый источник версии (API, spec, iss)
  config.py              чтение и валидация env (WAVE_*, SEARCH_*, YTDLP_*), значения по умолчанию
  paths.py, applog.py    без изменения ролей (applog: + маскирование трейсбеков)
  domain/
    models.py            Track (+ explicit/plays/released в TrackIn), ArtistCard, AlbumCard,
                         Page[T], SourceStatus, Capabilities
    errors.py            ProviderError(kind, source, user_message), коды ошибок API
  providers/
    base.py              Protocol MusicProvider + реестр + circuit breaker (cooldown)
    youtube_music.py     ytmusicapi + нормализация (_normalize_yt из main.py, _yt_* из catalog.py)
    soundcloud.py        API-клиент + нормализация (_normalize_sc, _sc_*), политика атрибуции
    policy.py            матрица возможностей источников (download_allowed, mixing_allowed …)
  jsruntime.py           поиск и проверка JS-runtime для yt-dlp (раздел 4)
  streaming/             (S8, сделано — §7)
    config.py            YTDLP_COOKIES / YTDLP_PROXY
    errors.py            тексты ошибок S2d, X-Wave-Error
    ranges.py            чистые функции: parse_single, cap, is_valid_for_file
    resolver.py          yt-dlp, кэш URL, inflight, ResolverState (lifespan), classify / choose_format
    proxy.py             progressive: _connect/_body, перерезолв, прогрев, HEAD, обрыв источника (N2)
    local.py             локальные файлы (FileResponse, MIME, битый Range игнорируется)
    netguard.py          защита HLS-прокси от SSRF
    hls.py               разбор и переписывание плейлистов
    hls_proxy.py         сеансы-токены, плейлисты и сегменты через backend
    hls_download.py      загрузка HLS-only трека
    containers.py        проверка MPEG audio и fMP4 перед сохранением
    router.py            /api/stream/{source}/{id} (GET, HEAD, /playback, ?warm=1), /api/stream/hls/...
  persistence/
    db.py                соединение, PRAGMA, transaction(), бэкап
    migrations.py        список миграций, PRAGMA user_version
    repos.py             favorites / history / playlists / downloads / settings
  services/
    search.py, catalog.py, recommendations.py, library.py, playlists.py, settings.py
  jobs/
    downloads.py         DownloadManager: очередь, лимит, отмена, восстановление
  api/                   роутеры: только валидация, вызов сервиса, стабильный ответ
```

### Соответствие текущих файлов целевым

| Сейчас | Куда | Комментарий |
|---|---|---|
| `main.py` (Track, нормализация, поиск, кэш) | `domain/models.py`, `providers/*`, `services/search.py`, `api/search.py` | `main.py` становится тонкой композицией |
| `catalog.py` | `providers/*` (вызовы источников), `services/catalog.py`, `api/catalog.py` | `_artist_cache` → ограниченный LRU+TTL |
| `soundcloud.py` | `providers/soundcloud.py` | приватные методы yt-dlp — только как временный путь с флагом, см. раздел 6 |
| `stream.py` | `streaming/*` | глобальные `_cache/_inflight/_resolve_sem` → `app.state.resolver` (исправляет M1, S2b); разделение на модули — **сделано в S8**, `stream.py` оставлен фасадом совместимости (прежние имена и monkeypatch в тестах) |
| `recs.py` | `services/recommendations.py` | + `failed_sources`, сигналы исключения (S14) |
| `library.py`, `userdata.py`, `playlists.py` | `persistence/*`, `services/*`, `api/*` | `_q` остаётся адаптером на время перехода |
| `downloads.py` | `jobs/downloads.py`, `api/downloads.py` | отмена, лимит, политика источника |
| `lyrics.py` | `providers/lyrics_lrclib.py`, `api/lyrics.py` | ограниченный кэш, защищённый `json()` |

**Сделано в S7** (ветка `claude/s7-provider-adapters`, [reports/S7.md](reports/S7.md)): `providers/base.py`, `providers/cache.py`, `providers/youtube_music.py`, `providers/soundcloud.py`, `domain/models.py` (`Track` и карточки; `main.Track` — тот же класс). Нормализация из `main.py` и `catalog.py`, сборка страниц артиста и альбома, «радио» — в адаптерах; `main.py`, `catalog.py`, `recs.py` вызывают только адаптеры; `soundcloud.py` — фасад для совместимости. `_artist_cache` и кэш текстов — ограниченные LRU+TTL (текст песен остаётся в `lyrics.py`: LRCLIB не музыкальный источник, отдельный адаптер не нужен). Не сделано: `services/*`, `api/*`, `config.py` — маршруты и проверка входа пока в `main.py` и `catalog.py`; `providers/policy.py` — проверка загрузок остаётся в `download_policy.py` (S5).

## 2. Состояние, привязанное к циклу событий (M1)

Объекты asyncio (Semaphore, Task, словари inflight) и `httpx.AsyncClient` создаются **в `lifespan`** и кладутся в `app.state`:

- `app.state.resolver`
- `app.state.downloads`
- `app.state.providers`
- `app.state.http`

Модульного асинхронного состояния нет. При перезапуске uvicorn watchdog'ом получается новый цикл и новые объекты. `ThreadPoolExecutor` можно переиспользовать: он не привязан к циклу.

Критерий проверки: два последовательных `lifespan` в одном процессе (`TestClient` дважды) обслуживают конкурентные запросы к стриму без `RuntimeError`.

## 3. Настройки (H3, решение: вариант А)

Постоянные настройки UI хранятся в SQLite через существующий `/api/settings`. localStorage остаётся только кэшем для мгновенной отрисовки.

### Контракт (аддитивный)

`GET /api/settings`. Текущие поля сохраняются, в `values` добавляются новые ключи:

```json
{
  "values": {
    "default_source": "yt", "autoplay": true, "theme": "auto",
    "volume": 0.8, "last_volume": 0.8, "lyrics_visible": true,
    "profile_name": "", "mix_covers": {"mood": "https://…"}
  },
  "paths": {"downloads": "…", "data": "…", "downloads_locked": false}
}
```

`PUT /api/settings` принимает частичный объект, как сейчас. Валидация:

| Ключ | Тип в API | Хранение | Правило |
|---|---|---|---|
| `volume`, `last_volume` | число 0..1 | TEXT | округление до 0,01; `last_volume` ≥ 0,05 |
| `lyrics_visible` | bool | `"0"`/`"1"` | — |
| `profile_name` | строка | TEXT | trim, ≤ 24 символа, без управляющих символов |
| `mix_covers` | объект `{theme_id: url}` | JSON TEXT ≤ 4 КБ | ключи — известные id тем, URL `https` с хостов из `COVER_HOSTS` |

Неизвестный ключ, как и сейчас, даёт 400. Значит, старый фронтенд с новым backend работает. Новый фронтенд со старым backend получает 400 только на новые ключи; фронтенд это переживает и продолжает с localStorage.

### Поведение фронтенда

- При старте `fetchSettings()` применяет серверные значения.
- Однократная миграция: если на сервере ключа нет, а в localStorage значение есть (разработка на фиксированном порту), оно отправляется на сервер.
- Громкость сохраняется по `commit` ползунка, по Mute и по стрелкам с debounce 500 мс, а не на каждый `volumechange`. Так нет шторма записей в SQLite.
- Тема без мигания: `index.html` синхронно подключает `GET /api/boot.js` (same-origin, < 1 КБ). Скрипт выставляет `data-theme` до первой отрисовки. Инлайн-скрипт с localStorage остаётся запасным вариантом.

## 4. JS-runtime для yt-dlp (H1)

Факты подтверждены по установленному yt-dlp 2026.8.19:

- extra `default` тянет `yt-dlp-ejs==0.8.0`;
- опция `js_runtimes` принимает словарь вида `{'deno': {'path': ...}}`, по умолчанию включён только `deno`;
- минимальная версия Deno — 2.3.0;
- `yt_dlp/__pyinstaller/hook-yt_dlp.py` уже собирает JS-файлы `yt_dlp_ejs`;
- runtime запускается через `yt_dlp.utils.Popen` со скрытым окном (STARTUPINFO).

### Проектное решение

1. **Зависимость.** `yt-dlp[default]==<точная версия>` в runtime-зависимостях; `yt-dlp-ejs` закрепляется самим yt-dlp. Обновление yt-dlp — отдельный коммит с прогоном live-канарейки.
2. **Поставка runtime.** Официальный бинарник Deno для Windows x64 закрепленной версии (≥ 2.3). Скачивается скриптом сборки и проверяется по SHA-256 из `packaging/runtime/deno.lock` (версия, URL, хэш). Кладётся в `dist/Wave/runtime/deno/deno.exe`. В git бинарник не коммитится.
3. **Лицензии.** Deno распространяется под MIT. В сборку добавляется `licenses/`: `deno/LICENSE.md` и сторонние уведомления Deno из архива релиза, лицензии `yt-dlp` (Unlicense) и `yt-dlp-ejs` (Unlicense; встроенные компоненты под MIT и ISC). Инсталлятор показывает и устанавливает файл `THIRD_PARTY_NOTICES.txt`. Точный состав уведомлений сверяется с архивом выбранной версии на этапе S3. *S4: в архиве Deno сводных уведомлений нет; перечень Rust- и C/C++-компонентов составлен по исходникам (§12, «Реализация S4»).*
4. **Поиск runtime** (`backend/jsruntime.py`), по порядку:
   1. `WAVE_JS_RUNTIME` — путь из окружения, для разработки;
   2. `paths.resource_dir()/runtime/deno/deno.exe`;
   3. `shutil.which("deno")`.

   Для найденного бинарника выполняется `deno --version` с таймаутом 5 с, версия сравнивается с минимальной. Результат кэшируется на процесс. В `ydl_opts()` передаётся `js_runtimes={'deno': {'path': <путь>}}`. В окружение процесса добавляются `DENO_DIR=<data_dir>/deno-cache` (записываемый каталог пользователя) и `DENO_NO_UPDATE_CHECK=1`.
5. **Если runtime не найден.** Приложение работает: SoundCloud, локальные файлы и библиотека доступны. `/api/diagnostics` сообщает `youtube_playback: "degraded"`. Ошибка воспроизведения YouTube показывает пользователю: «Компонент воспроизведения YouTube не найден. Переустановите приложение». Детали — в журнал.
6. **Диагностика.**
   - `GET /api/diagnostics` возвращает версии приложения, yt-dlp и yt-dlp-ejs, а также найденный runtime: имя, версию, поддерживается ли, источник пути. Плюс статусы провайдеров. Без секретов и cookies.
   - `Wave.exe --diagnostics <файл.json>` выполняет ту же проверку без окна и завершается с кодом 0 или 1. Флаг `--live <videoId>` дополнительно резолвит публичный трек.
   - Используется в `build_windows.bat` для автоматической проверки собранной папки и в ручном чек-листе релиза.
7. **Процессы.** После закрытия приложения не должно оставаться `deno.exe`. Проверяется вручную на Windows. При необходимости потомки привязываются к Windows Job Object [предп.: потребность подтвердить тестом].
8. **Альтернатива** на случай неприемлемого размера: QuickJS-ng (значительно меньше, медленнее). Решение принимается только по измерениям размера и времени резолва на этапе S3.

### Реализация (S3, ветка `claude/s3-ytdlp-deno`, подробности — [reports/S3.md](reports/S3.md))

Решение выше реализовано со следующими уточнениями по фактам, установленным на S3:

- **Зависимость.** Не `yt-dlp[default]`, а `yt-dlp==2026.8.19` + `yt-dlp-ejs==0.8.0`: extra тянет `mutagen` (GPL-2.0-or-later), `pycryptodomex`, `brotli`, которые Wave не использует. **Решение владельца 2026-10-13:** `[default]` не подключать, версии закреплять отдельно, совместимость подтверждать тестами. Версия ejs ровно та, что объявлена в extra; согласованность проверяет `tests/unit/test_dependencies.py`.
- **yt-dlp-ejs обязателен.** В yt-dlp 2026.8.19 встроен только `yt.solver.core.js`; lib-скрипт для Deno без сети есть только в `yt-dlp-ejs` (встроенный `yt.solver.deno.lib.js` требует загрузки npm-пакетов). Без ejs — критическая проблема диагностики, `wave.spec` не собирается без его `.js`.
- **Поиск runtime.** `WAVE_JS_RUNTIME` (без молчаливой подмены другим) → `resource_dir()/runtime/deno/deno.exe` → `PATH` **только в разработке**. В собранном приложении системный Deno, текущий каталог и `PATH` не используются. Без пригодного runtime yt-dlp получает `js_runtimes={}` (иначе он сам ищет `deno` в cwd и `PATH`). Таймаут проверки версии 10 с (первый запуск 97-мегабайтного файла может проверять антивирус).
- **Окружение.** `DENO_DIR=<data_dir>/deno`, `DENO_NO_UPDATE_CHECK=1` (имя переменной сверено с `cli/tools/upgrade.rs` Deno 2.9.7; без неё Deno ходит в сеть за обновлениями). Переменные ставятся в окружение процесса: yt-dlp передаёт Deno копию `os.environ`.
- **Процессы.** Обёртка над `yt_dlp.utils.Popen`: `CREATE_NO_WINDOW`, сторож завершает процесс через 30 с (yt-dlp ждёт Deno без таймаута), Windows Job Object с `KILL_ON_JOB_CLOSE`, завершение при остановке backend и перед `os._exit`. Job Object и отсутствие окна консоли проверены тестами на `windows-latest`.
- **Диагностика.** `GET /api/diagnostics`; `Wave.exe --diagnostics [--live <videoId>] [--json] [--output <файл>]` в `launcher.py`, без окна и без модальных сообщений. Статусы провайдеров в отчёт не вошли (не относятся к runtime).
- **Поставка.** Deno 2.9.7, Windows x64: +92,9 МБ к `dist\Wave` (147,1 МБ против 54,0 МБ без добавлений S3; сжатый архив Deno — 42,6 МБ). **Размер принят владельцем 2026-10-13**: Deno поставляется вместе с приложением, без отдельной установки пользователем; альтернатива QuickJS-ng не реализовывалась.

## 5. Провайдеры: интерфейс, ошибки, возможности

```python
class Capabilities(BaseModel):
    search_tracks: bool; search_artists: bool; search_albums: bool
    artist_page: bool; album_page: bool; radio: bool; stream: bool
    download_allowed: bool          # см. providers/policy.py
    attribution_required: bool
    mixing_allowed: bool            # можно ли смешивать с другими источниками в одной очереди

class ProviderError(Exception):
    kind: Literal["unavailable", "timeout", "rate_limited", "not_found",
                  "restricted", "auth_required", "unsupported", "internal"]
    source: str
    user_message: str               # человеческий текст на русском

class MusicProvider(Protocol):
    id: Literal["yt", "sc"]
    capabilities: Capabilities
    async def search_tracks(self, q: str, limit: int, offset: int) -> Page[Track]: ...
    async def search_cards(self, kind: str, q: str, limit: int, offset: int) -> Page[dict]: ...
    async def artist(self, artist_id: str) -> ArtistPage: ...
    async def album(self, album_id: str, playlist_id: str | None) -> AlbumPage: ...
    async def radio(self, track_id: str, n: int) -> list[Track]: ...
    def stream_page_url(self, track_id: str) -> str: ...
```

Правила:

- **Свой пул потоков на провайдер** (например, yt — 4, sc — 2) с ограничением ожидающих задач. Зависший источник не занимает общий executor. Таймаут задаётся на уровне HTTP-клиента библиотеки, где это возможно [предп.: для ytmusicapi проверить передачу `requests_session` с таймаутами], и дублируется `wait_for`.
- **Cooldown для всех источников**: `yt` — короткий (30–60 с), `sc` — 300 с. В ответах статус: `ok | degraded | cooldown | disabled`.
- **Ограниченные кэши**: LRU с TTL и лимитом элементов для поиска, артистов, текстов. Очистка в настройках, как сейчас.
- **Ответы API аддитивны**. В `/api/search`, `/api/search/{kind}` и `/api/recommendations` добавляется `source_status: {"yt": "ok", "sc": "cooldown"}`; `failed_sources` сохраняется. Ошибки по-прежнему отдают строку `detail` (на неё опирается `app.js:211`) и дополнительно поля `code` и `source` на верхнем уровне. Это реализуется собственным exception handler.

### Реализация (S7, ветка `claude/s7-provider-adapters`, подробности — [reports/S7.md](reports/S7.md))

**Интерфейс.** Базовый класс `providers.base.Provider` (не `Protocol`: общая логика пула и cooldown в нём). Методы адаптеров принимают `state` (`app.state`, в нём клиент ytmusicapi текущего запуска) и `timeout`:
`search_tracks(state, q, limit, offset=0)`, `search_cards(state, kind, q, limit, offset) -> (items, has_more)`, `artist_info(state, id) -> (info, degraded)`, `artist_tracks(state, id, info, offset, limit) -> (tracks, has_more)`, `album(state, id, playlist_id) -> (card, tracks, unavailable, degraded)`, `radio(state, track_id, n)`. У SoundCloud дополнительно `search_tracks_plain` — прежний запрос рекомендаций (`search/tracks {q, limit}`). Ответы — прежние `Track` и карточки, не модели `Page[T]`: формы ответов API не менялись. Поток остаётся в `streaming/resolver.py:SOURCES` (S8; признак `hls` — может ли источник отдавать HLS).

**Возможности** (`Capabilities`, фактическое поведение Wave, не разрешение платформы): `search_tracks`, `search_artists`, `search_albums`, `artist_page`, `album_page`, `radio`, `stream`, `download` (ADR-10, решает `download_policy.py`), `attribution_required` (SoundCloud — да; Wave показывает автора и ссылку `url`, логотипа нет), `mixing_allowed` (оба — да: так работает Wave; условия API SoundCloud это запрещают, см. §6).

**Ошибки.** `ProviderError(kind, source, user_message)`, `kind` из восьми типов. Перевод исключений — в адаптере: ytmusicapi (`requests.Timeout` → timeout, `requests.RequestException` → unavailable, `YTMusicServerError` по коду HTTP: 429 → rate_limited, 400/404 → not_found, 401/403 → restricted, 5xx → unavailable; `YTMusicGatedError` → restricted; `YTMusicUserError` → not_found; не-JSON → unavailable), yt-dlp для SoundCloud (`HTTPError` 429 → rate_limited, 401/403 → auth_required, 404/410 → not_found, 5xx → unavailable; `TransportError` → timeout или unavailable). Всё прочее — internal. Ответ API: статус **502** (как до S7), `detail` — текст с названием источника, аддитивно `code`, `source`, `source_status`. Поиск треков при сбое всех выбранных источников: 502; для одного источника `detail` — его причина, для нескольких — прежний текст «Источники поиска недоступны».

**Пулы и таймауты.** `BoundedExecutor` на источник: YouTube Music 4 потока + 8 в очереди, SoundCloud 2 + 4 (`YTM_WORKERS`, `YTM_MAX_PENDING`, `SC_WORKERS`, `SC_MAX_PENDING`). Сверх предела — сразу `unavailable` (busy, статус degraded), без постановки в очередь. Настоящий предел времени — у HTTP-клиента: сессия requests для ytmusicapi (connect 5 с, read 10 с вместо 30 с по умолчанию; способ из документации ytmusicapi), `socket_timeout` 7 с для yt-dlp SoundCloud. Таймаут ожидания (`SEARCH_TIMEOUT` 6 с для поиска, 25 с каталог, 12 с рекомендации) отменяет задачу из очереди; работающий поток Python остановить нельзя — он помечается брошенным, занимает место в пуле до своего завершения по таймауту HTTP и учитывается в `/api/sources` (`abandoned`). Отмена запроса клиентом — так же. При остановке backend задачи из очередей отменяются (`providers.shutdown_pending()`). SoundCloud: вместо одного экземпляра yt-dlp под глобальной блокировкой — свой экземпляр в каждом потоке пула.

**Cooldown.** YouTube Music 45 с (`YT_SOURCE_COOLDOWN`), SoundCloud 300 с (`SOURCE_COOLDOWN`), действует на все операции источника. Открывают: unavailable, timeout, rate_limited, auth_required — всегда; internal, restricted — только поиск. Не открывают: not_found, unsupported, пустая выдача, переполнение пула и необязательные части страницы (`optional=True`: полный список релизов артиста, альбомы SoundCloud, полное представление альбома, `get_album` при наличии запасного плейлиста — иначе пауза обрывала остальные запросы той же страницы). Успешный вызов снимает паузу; по её окончании первый же запрос снова идёт к источнику.

**Статусы.** `source_status` в `/api/search`, `/api/search/{kind}`, `/api/artist/...`, `/api/album/...` и в теле ошибок источника: `ok`, `degraded` (страница собрана не полностью, источник занят или упал без паузы), `cooldown`, `disabled` (выключен `SEARCH_SOURCES`, только поиск треков). `/api/recommendations` сохраняет статусы S2 (`ok | partial | failed`). `GET /api/sources` — сводка по источникам (статус, остаток паузы, тип последней ошибки, загрузка пула, возможности).

**Кэши.** Поиск — 256 × 300 с, страницы артистов — 32 × 600 с (неполная страница не кэшируется), тексты — 512 × 24 ч (временный сбой LRCLIB не кэшируется). `streaming.resolver._cache` (ссылки на поток) ограничивается по сроку ссылок, а не по числу; HLS-сеансы (S8) — ≤ 16, 30 мин простоя, 6 ч.

## 6. Политика источников и скачивание

Решение владельца 2026-10-13 (S5, ADR-10, уточнено в тот же день): **сохранить прежнее поведение Wave**. Трек YouTube Music или SoundCloud можно скачать, если приложение штатно получает его аудиопоток для воспроизведения. Прежние ADR-5 (загрузки отключены) и ADR-6 (SoundCloud выключен) пересмотрены. Промежуточный вариант S5 с проверкой лицензии Creative Commons и признака `downloadable` SoundCloud отменён и в коде не используется.

### Реализовано (S5, ветка `claude/s5-downloads`, подробности — [reports/S5.md](reports/S5.md))

Проверка выполняется **на backend** в `backend/download_policy.py` на этапе задачи `preparing`, до первого байта аудио; прямой `POST /api/downloads` проходит её же. Целевое место модуля — `providers/policy.py` (раздел 1), перенос на этапе адаптеров.

| | YouTube Music (`yt`) | SoundCloud (`sc`) |
|---|---|---|
| Условие | `stream._resolve` вернул поток — тот же резолвер, кэш ссылок, JS-runtime и обработка ошибок, что у плеера | то же; поток не фрагмент (`Resolved.preview` = `False`) |
| Что сохраняется | поток резолвера в исходном формате (обычно `.m4a`) | поток плеера: прогрессивный в исходном формате (обычно `.mp3`) или, с S8, HLS — проверенные и собранные сегменты: mp3-HLS → `.mp3`, fMP4 (AAC) → `.m4a` (`streaming/hls_download.py`, `containers.py`) |
| Не проверяется | лицензия (Creative Commons) | `downloadable`, `has_downloads_left`, исходный файл автора |
| Подтверждено живой проверкой | LIVE-8a **PASS** 2026-10-13 (через прокси среды): загрузка, «Скачанные», воспроизведение с диска | LIVE-8b **PASS** 2026-10-13 (через прокси среды): то же |

Отказ — задача `error` с текстом для пользователя (`error`) и кодом (`code`, те же значения, что в `X-Wave-Error`). Ничего не записывается на диск и не запрашивается у CDN.

| `code` | Когда | Окончательный (UI не предлагает повторить) |
|---|---|---|
| `unavailable`, `age_restricted`, `no_direct_stream`, `no_playable_format`, `hls_encrypted` | поток трека штатно не получить (удалён, приватный, ограничен по возрасту; с S8 «только HLS» больше не причина — отказ только для форматов, которые Wave не играет: Opus-HLS, зашифрованные) — те же текст и код, что у плеера | да |
| `hls_container_unsupported`, `hls_live` (S8) | HLS с сегментами MPEG-TS или неизвестным контейнером (воспроизводится, но без перепаковки ffmpeg файл не собрать), прямая трансляция | да |
| `hls_bad_segment`, `upstream_blocked`, `hls_changed` (S8) | данные сегмента не прошли проверку контейнера или длительности; адрес из плейлиста отклонён защитой от SSRF; поток сменился во время загрузки | нет, UI предлагает повторить |
| `preview_only` | источник отдаёт лишь фрагмент: SoundCloud Go+ (yt-dlp помечает формат `…_preview`). Фрагмент не сохраняется под видом полного трека | да |
| `bot_check`, `rate_limited`, `network`, `proxy`, `extractor`, `resolve_failed`, `js_runtime`, `js_runtime_failed`, `internal` | временные сбои резолвера, CDN или приложения | нет, UI предлагает повторить |

- Уже скачанные треки не проверяются: `POST` для них сразу отвечает `done`, воспроизведение идёт с диска тем же `/api/stream/{source}/{id}` без обращения к источнику, удаляются они только пользователем.
- `metadata.json` — прежний формат (без новых полей).
- Задача в `/api/downloads/jobs` (аддитивно): `code`, `received`, `total` (байты; `total` = `null`, если источник не сообщил размер).
- Не добавлено и не добавляется: вход в аккаунты источников, обход DRM, платного доступа (Go+), возрастных ограничений и антибот-проверок. Если штатный резолвер их встречает, загрузка заканчивается понятной ошибкой.

### Ограничения, которые важно понимать

- **Это не разрешение платформ.** Wave сохраняет тот же поток, который воспроизводит. Это не означает, что YouTube, SoundCloud или правообладатели разрешили скачивание. Условия YouTube запрещают скачивание, кроме явно разрешённого сервисом; условия SoundCloud ограничивают сохранение контента вне его функций. Функция — решение владельца для личного использования в его приложении, а не официально одобренная интеграция. Пользователь отвечает за соблюдение авторских прав.
- Источники используются через неофициальные интерфейсы (`ytmusicapi`, внутренние методы yt-dlp для SoundCloud API v2, `client_id`). Они могут перестать работать без предупреждения; тогда перестанут работать и воспроизведение, и загрузка.
- Перед публичным распространением сборки этот вопрос нужно отдельно оценить (условия платформ, юрисдикция, описание функции для пользователей).
- `WAVE_SOURCE_OVERRIDES` не реализован: переопределять нечего.

### SoundCloud: официальный API (проверено в S7, 2026-10-13, находка M10)

Документация developers.soundcloud.com: регистрация приложения и получение ключей — только с подпиской Artist Pro; поиск и воспроизведение доступны через Client Credentials (OAuth 2.1), все клиенты считаются конфиденциальными, секрет клиента нельзя размещать в клиентском коде — то есть встроить его в распространяемое настольное приложение нельзя без собственного сервера-посредника. Условия API (разделы 03 и 05) запрещают сохранение и офлайн-доступ, а также воспроизведение вместе с контентом других сервисов (пример — YouTube) и агрегированные on-demand сервисы; требуется атрибуция автора и SoundCloud. Переход на официальный API несовместим с ADR-10 (загрузки) и с общей очередью Wave, поэтому в S7 способ подключения не менялся: интеграция изолирована в адаптере (свой пул, таймауты, cooldown, внутренние методы yt-dlp — только внутри `providers/soundcloud.py`). Решение — за владельцем (ADR-11).

## 7. Стриминг

**Сделано в S8** (ветка `claude/s8-hls-playback`, [reports/S8.md](reports/S8.md)).

- **Range** (`ranges.py`): `parse_single` — один диапазон или «игнорировать» (битый, пустой `bytes=-`, конец раньше начала, несколько диапазонов → 200 по RFC 9110); `cap` — обрезка до порции 1 МБ, суффикс без изменений; `is_valid_for_file` — проверка для `FileResponse` (верный multipart остаётся multipart). Перерезолв (401/403/404/410), отдача 206 порциями, прогрев — без изменений поведения.
- **HEAD** — отдельный маршрут: 200 и заголовки всего ресурса без тела (локальный файл, прогретая «голова» или запрос `bytes=0-0`), Range для HEAD не применяется.
- **N2** — байты отдаются по мере получения; обрыв источника (`UpstreamBroken`) прерывает ответ без завершающего сообщения ASGI, клиент видит неполный ответ и запрашивает продолжение.
- **Тип потока.** `Resolved.kind` = `progressive` | `hls`, `codec`. YouTube — только progressive (выбор yt-dlp не менялся). SoundCloud — `choose_format`: progressive → HLS mp3 → HLS AAC; Opus-HLS и зашифрованные — не выбираются. Progressive-адрес для HLS-трека отвечает 409 `hls_required`: m3u8 никогда не проксируется как аудиофайл.
- **HLS.** `GET /api/stream/{source}/{id}/playback` → `{kind, url}`; для HLS — сеанс с непрозрачным токеном и проверенный корневой плейлист. `GET /api/stream/hls/{token}/{pl}.m3u8` и `/{pl}/sSEQ`, `/{pl}/mN` — плейлисты с адресами прокси и сегменты потоком; `DELETE` освобождает сеанс. Фронтенд играет HLS через закреплённый hls.js 1.7.3 (light, без EME) из `frontend/vendor/` в тот же `<audio>`; источники без HLS (`PROGRESSIVE_ONLY`) — как раньше, без лишнего запроса.
- **Ресурсы.** Сеансов ≤ 16 (30 мин простоя, 6 ч), один на трек; плейлист ≤ 2 МБ / 20 с; ≤ 6 соединений с источником на запуск; сегменты — потоком. Фронтенд при смене трека отменяет запрос, уничтожает экземпляр hls.js и освобождает сеанс; восстановление — не больше 2 перезапусков с той же позиции.
- Метрики в диагностике (время резолва, доля перерезолвов, попадания head-кэша, сеансы HLS) — не сделано, S16.

## 8. Хранилище

Состояние на S6 — **сделано**: `backend/persistence/db.py`, `backend/persistence/migrations.py` (v1), атомарные плейлисты, ретеншн истории, полный снимок `TrackIn`. Уточнения к плану ниже:
- `N` в имени копии — версия схемы **в копии** (до обновления): `wave-20261013-193444-v0.db`. Совпадение по секунде даёт `-2`, `-3`; существующая копия не перезаписывается. Копия пишется в `.part`, проверяется `PRAGMA quick_check` и только потом получает имя, поэтому оборванная копия не вытесняет целые при ротации. Пустой файл (новая установка) обновляется без копии.
- База с `user_version` больше известного (от более новой Wave) не открывается и не меняется; при каждом запуске проверяется, что нужные таблицы и колонки на месте. Повреждённый файл даёт понятную ошибку без изменений файла. Текст ошибки показывает страница запуска лаунчера (`launcher.startup_error_page`).
- WAL включается после обновления схемы: копия снимается с базы в её исходном режиме журнала. База v1 открывается и версиями до S6 (их `CREATE TABLE IF NOT EXISTS` и `ALTER … root` в `try` не ломаются).
- Соединение в режиме autocommit; `transaction()` = `BEGIN IMMEDIATE … COMMIT/ROLLBACK`. Вызов `query()` или второго `transaction()` из потока, уже находящегося в транзакции, сразу даёт `NestedTransactionError`, а не взаимную блокировку.
- `repos.py` не выделен: SQL остаётся в `library.py`, `userdata.py`, `playlists.py`, `_q`/`transaction` — адаптеры. Выносить вместе с сервисным слоем.
- Ретеншн: настройка `history_limit` (1 000…100 000, по умолчанию 10 000) в `/api/settings`; обрезка — в той же транзакции, что и запись прослушивания, при уменьшении лимита и при запуске. Новая версия схемы для этого не понадобилась.

- `persistence/db.py`:
  - одно соединение и `threading.Lock` (подходит для настольного приложения);
  - `PRAGMA journal_mode=WAL`, `synchronous=NORMAL`, `foreign_keys=ON`, `busy_timeout=5000`;
  - контекстный менеджер `transaction()` для многошаговых операций.
- Миграции по `PRAGMA user_version`:
  - **v1** — фиксация текущей схемы: таблицы `downloads` (с колонкой `root`), `favorites`, `history`, `settings`, `playlists`, `playlist_tracks`. Миграция идемпотентно распознаёт существующую базу без версии.
  - **v2+** — по необходимости, например индекс `history(played_at)`.
  - Перед каждой миграцией база копируется через `sqlite3.backup()` в `data_dir()/backups/wave-<дата>-v<N>.db`; хранятся три последние копии. Миграция идёт в транзакции; при ошибке выполняется откат, приложение сообщает об ошибке и не стартует с полусостоянием.
- Ретеншн истории настраиваемый, по умолчанию последние 10 000 записей. Агрегаты для рекомендаций считаются по ним.
- Плейлисты: добавление и удаление выполняются в `transaction()`; повторное добавление возвращает `{"added": false}`, а не 500. Отдельный эндпоинт порядка появляется на этапе S15.

## 9. Фоновые задачи и жизненный цикл

Состояние на S5: сбой обложки или текста (включая не-JSON ответ LRCLIB, M8) загрузку не отменяет — **сделано**. Лимит параллельности, отмена, запись через `asyncio.to_thread`, отмена задач при выходе из `lifespan` и очистка `.part` во всех `root` — **не сделано**, остаются в S11.


- `DownloadManager` в `app.state`:
  - лимит параллельных загрузок 2;
  - отмена через `DELETE /api/downloads/jobs/{source}/{id}`;
  - запись на диск через `asyncio.to_thread`;
  - сбой получения текста или обложки не отменяет загрузку аудио.
- На выходе из `lifespan` активные задачи отменяются, `*.part` удаляются. `cleanup_partial` при старте чистит и текущий каталог, и каталоги из `downloads.root`.
- `launcher` сначала дожидается остановки backend (как сейчас, до 8 с), затем завершается. `os._exit` остаётся последним средством.

## 10. Безопасность локального API

- Middleware `OriginGuard` для `POST/PUT/PATCH/DELETE`. Заголовок `Origin`, если он есть, должен совпадать с `http://127.0.0.1:<порт>` или `http://localhost:<порт>`. Запросы без `Origin` (curl, тесты) разрешены. `TrustedHostMiddleware` остаётся.
- `TrackIn` валидирует данные на сервере:
  - `id` — по регулярке источника;
  - `stream_url` — всегда вычисляется сервером как `/api/stream/{source}/{id}`;
  - `cover` — `https` с разрешённых хостов либо `/api/local/...`;
  - необязательные `explicit`, `plays`, `released` добавляются в модель (исправление M6).
- Существующие невалидные строки не удаляются: при чтении они фильтруются из ответа и пишутся в журнал.
- `applog`: маскирование применяется и к тексту трейсбека; query-параметры URL googlevideo в журнал не пишутся.
- **HLS-прокси (S8, сделано):** фронтенд не передаёт адресов источника — только токен сеанса, выданный для трека, который проверило само приложение; ресурсы адресуются номерами. Каждый запрос к источнику и каждый редирект проходят `streaming/netguard.py`: только https, без логина в адресе, все IP имени публичные (запрещены loopback, частные, link-local включая метаданные облака, CGNAT, multicast/reserved и встроенные в IPv6 IPv4). В журнал — только хосты. Соединения HLS идут через отдельный клиент, который в момент соединения разрешает имя локально, проверяет IP и соединяется только с проверенным IP (через прокси пользователя — `CONNECT <IP>:<порт>`, имя прокси не получает); неразрешимое локально имя и прокси не `http://` — отказ `upstream_unverifiable`. Число одновременно используемых ответов источника ≤ 6 на всё время передачи тела (доработка по аудиту, [reports/S8.md §15](reports/S8.md)). progressive-прокси по-прежнему ходит по адресам yt-dlp без netguard.

## 11. Фронтенд

Нативные ES-модули без сборщика: `<script type="module" src="js/main.js">`. WebView2 и Chromium их поддерживают. Порядок выполнения как у `defer`.

```
frontend/js/
  core/      dom.js (mk, icon, button, $), format.js, api.js (api(), ошибки, trackPayload), emitter.js
  state/     store.js (единое состояние + подписки), settings.js (серверные настройки, раздел 3)
  player/    queue.js      — чистая модель очереди без DOM: add/remove/move/clear/next/prev,
                             shuffle без повторов с историей, repeat off|one|all, сериализация
             controller.js — единственный HTMLAudioElement, события, прогрев, Media Session
             sliders.js    — прогресс и громкость
  components/ track-row.js, cards.js, menu.js, dialogs.js, toast.js, skeleton.js
  views/     home.js, search.js, detail.js, collection.js, downloads.js, settings.js,
             now.js, fullscreen.js, queue-drawer.js
  main.js    загрузка, навигация (navStack), регистрация обработчиков
```

Правила:

- `views/*` не обращаются к `<audio>` напрямую, только к API `player/controller.js`.
- Очередь — **собственный массив** модели. Связь «очередь продолжает ленту» реализуется явным источником продолжения (`continuation: () => Promise<Track[]>`), а не общим массивом. Это устраняет побочный эффект `sortList` (M11).
- ID и классы DOM, на которые опирается `tests/e2e_smoke.py`, не меняются до этапа редизайна. На этапе редизайна тест обновляется вместе с разметкой, без уменьшения проверок.
- Модули `core/*` и `player/queue.js` не зависят от DOM и тестируются через `node --test` без новых зависимостей.
- Порядок выделения — от наименее рискованного: `core` → `components/dialogs, toast` → `player/queue` → `player/controller` → `views`. После каждого шага e2e должен проходить полностью (109/109 или больше).

## 12. Сборка и инсталлятор (H2)

- `packaging/wave.spec`:
  - добавить `runtime/deno` и `licenses/` в `datas`;
  - версия из `backend/version.py` в version-resource exe;
  - удалить мёртвый hidden-import `multipart`.
- `installer/wave.iss` (Inno Setup 6):
  - фиксированный `AppId` (GUID);
  - установка для текущего пользователя без прав администратора: `PrivilegesRequired=lowest`, `DefaultDirName={localappdata}\Programs\Wave`;
  - `ArchitecturesAllowed=x64compatible`;
  - `[Files]` — всё содержимое `dist\Wave\` рекурсивно;
  - ярлыки в «Пуске» и опционально на рабочем столе;
  - проверка WebView2 в `[Code]` по тем же ключам реестра, что и `launcher.webview2_installed()`; при отсутствии — тихий запуск `MicrosoftEdgeWebview2Setup.exe`;
  - `AppMutex` для закрытия запущенного приложения при обновлении [предп.: проверить работу с префиксом `Local\`];
  - деинсталляция **не удаляет** `%LOCALAPPDATA%\Wave` и папку загрузок;
  - имя файла `Wave-Setup-<версия>.exe` в `dist\installer\`.
- `build_windows.bat`:
  - создаёт `installer\` перед загрузкой bootstrapper;
  - скачивает Deno по `deno.lock` и сверяет хэш;
  - после PyInstaller запускает `dist\Wave\Wave.exe --diagnostics` и прерывает сборку при ошибке;
  - передаёт версию в ISCC: `/DAppVersion=...`.
- Подписи кода пока нет (ADR-8), поэтому будут предупреждения SmartScreen; это фиксируется в документации. В сборке предусматривается необязательный шаг подписи `Wave.exe` и установщика (`signtool` через `SignTool=` в Inno Setup). Он выполняется, только если задана команда подписи в окружении сборщика, и пропускается без ошибки, если её нет. Сертификаты и пароли в репозиторий и CI не попадают.

### Реализация (S4, ветка `claude/s4-installer`, подробности — [reports/S4.md](reports/S4.md))

План выше реализован со следующими уточнениями:

- **Версия.** `backend/version.py` (`0.1.0`, название, издатель, описание) → FastAPI, `GET /api/health` (аддитивное поле `version`), диагностика, version-resource `Wave.exe` (в `wave.spec`, только на Windows). `wave.iss` читает версию, название и издателя из собранного `Wave.exe` и останавливает компиляцию, если `/DAppVersion` другая.
- **`AppMutex`.** `Local\Wave.Desktop.SingleInstance` (`launcher.SINGLE_INSTANCE_MUTEX`); проверено в CI, что установщик и деинсталлятор находят запущенную ВОЛНУ. Интерактивно — просьба закрыть; в тихом режиме — отказ с кодом 1 без изменений. `CloseApplications=yes`, `RestartApplications=no`, `SetupMutex`.
- **Обновление.** `[InstallDelete] {app}\_internal` перед копированием; данные вне `{app}` не трогаются.
- **WebView2.** Ключи — `launcher.WEBVIEW2_KEYS` (сверяются тестом); bootstrapper в `[Files]` с `dontcopy`, запуск после копирования файлов, итог по реестру; при сбое — сообщение, нет «Запустить», код выхода 100. Bootstrapper скачивается `packaging/fetch_webview2.ps1` в `build\webview2\` и принимается только с подписью Microsoft. Тестовый вариант `/DTestWebView2=ok|fail` для CI.
- **Сборка.** Python 3.12 x64 обязателен; все зависимости сборки и приложения для Windows — `packaging/requirements-build.lock` с хэшами (`--require-hashes --no-deps`, сверка `verify_build_env.py`); Inno Setup 6.7.3; диагностика во временном профиле; `excludes` в `wave.spec` не пускают в приложение хуки PyInstaller и инструменты сборки.
- **Лицензии.** Перечень Rust-компонентов Deno (`licenses/deno/THIRD_PARTY_RUST.txt`) и C/C++-компонентов V8 (`licenses/deno/V8_COMPONENTS.txt`) — `packaging/deno_notices.py`; Python-пакеты — `packaging/python_notices.py` при каждой сборке; WebView2 SDK из pywebview — `licenses/webview2-sdk/`. В V8 по исходникам есть LGPL-2.1-код glibc (решение владельца).
- **`WAVE_PORT`** — только переменная окружения, строгая валидация, без запасного порта.
- **Проверка.** CI выполняет `build_windows.bat` и `tests/installer/run_installer_checks.ps1` (WIN-1, 2, 3, 4, 8, 9, 13, 15); интерактивная часть — ручной чек-лист, пройден владельцем на Windows 11, S4 принят 2026-10-13.
- **Не решено приёмкой S4 (до публичного выпуска).** Подпись кода (ADR-8: пока не обязательна, установщик не подписан собственным сертификатом), LGPL-2.1-код glibc в V8 внутри Deno, окончательное имя издателя, условия Inno Setup для коммерческого использования — [reports/S4.md](reports/S4.md) §12.

## 13. Наблюдаемость и производительность

- События журнала с именами: `startup.phase`, `provider.fail`, `stream.reresolve`, `download.done`. Без секретов. **S7:** `provider.fail source= op= kind=` (с отметкой `-> cooldown N с`, если сбой открыл паузу) и `provider.busy` (пул источника заполнен) пишет `providers/base.py`; client_id в тексте ошибок маскирует `applog.RedactFilter`.
- Тайминги холодного старта: импорт, lifespan, первый `/api/health`, первый ответ ленты.
- Лимиты: размеры кэшей, параллельность резолвов и загрузок, ретеншн истории.

## 14. Архитектурные решения

| № | Решение | Статус |
|---|---|---|
| ADR-1 | Настройки UI в SQLite через `/api/settings` (вариант А) | **принято** владельцем |
| ADR-2 | JS-runtime Deno встраивается в сборку с проверкой хэша, лицензиями и автодиагностикой; заявлять исправление только после теста на Windows | **принято**; реализовано на S3 (ветка не слита); +92,9 МБ к `dist\Wave` приняты владельцем 2026-10-13; воспроизведение в собранном приложении (WIN-6, WIN-7, LIVE-2/3/4) проверено владельцем на Windows 11, **S3 принят 2026-10-13** |
| ADR-3 | Инсталлятор пишется заново: per-user, без прав администратора, данные пользователя при удалении сохраняются | **принято**; реализовано на S4 (`installer/wave.iss`, ветка `claude/s4-installer` не слита); **S4 принят владельцем 2026-10-13** после ручной проверки на Windows 11 |
| ADR-4 | Описательные названия подборок вместо «Моя волна»; собственная функция бренда — после S14 | **принято** |
| ADR-5 | Новые загрузки с YouTube и SoundCloud в распространяемой версии отключены до подтверждения допустимого способа; запрет на backend (`download_allowed`); существующие загрузки и импорт разрешённых локальных файлов не затрагиваются | **пересмотрено** владельцем 2026-10-13 (S5) — заменено ADR-10; общий запрет в коде не реализовывался |
| ADR-6 | SoundCloud в распространяемой версии выключен (`enabled=False`) до подтверждения допустимости; адаптер сохраняется за интерфейсом; `client_id` и раздельные очереди не считаются достаточным основанием | **пересмотрено** владельцем 2026-10-13 (S5): SoundCloud остаётся включённым — поиск, воспроизведение и загрузка (ADR-10); выключение не реализовывалось |
| ADR-7 | Нативные ES-модули без бандлера для фронтенда | предложено |
| ADR-8 | Основная платформа Windows 11 x64, совместимость с Windows 10 22H2 проверяется по возможности; CI на GitHub Actions с Windows-сборкой; без секретов в репозитории и CI; подпись установщика пока не обязательна, но сборка предусматривает шаг подписи | **принято** 2026-10-12 |
| ADR-9 | Названия подборок описательные; утверждение о персонализации — только при реально работающем алгоритме | **принято** 2026-10-12 |
| ADR-10 | Прежнее поведение Wave: скачивается трек YouTube Music или SoundCloud, поток которого штатно получен для воспроизведения, в исходном формате; без проверки лицензии и `downloadable`; отказ на backend с понятной причиной, если поток недоступен или это лишь фрагмент (Go+); без входа в аккаунты и обхода DRM, платного доступа и антибот-защиты; существующие загрузки не затрагиваются. Не является разрешением платформ (§6) | **принято**: реализовано на S5 (ветка `claude/s5-downloads`, не слита), **S5 принят владельцем 2026-10-13**. Промежуточный вариант S5 (только Creative Commons / `downloadable`) отменён владельцем 2026-10-13 |
| ADR-11 | SoundCloud остаётся на неофициальном API v2 через внутренние методы yt-dlp, изолированном в `providers/soundcloud.py` (свой пул, таймауты, cooldown 300 с), пока владелец не выберет одно из: (а) так и оставить, принимая риск поломки и несоответствия условиям; (б) официальный API: подписка Artist Pro, собственный сервер-посредник для секрета, отказ от загрузок SoundCloud и от смешанной с YouTube очереди; (в) отключить SoundCloud в распространяемой сборке | **предложено** в S7 (2026-10-13), ждёт решения владельца. До решения поведение не меняется |
| ADR-12 | HLS (S8): воспроизведение через backend-прокси (токены сеанса, защита от SSRF, адреса источника не попадают во фронтенд) и закреплённый локальный hls.js 1.7.3 (light, Apache-2.0) в том же `<audio>`, без CDN; загрузка HLS-only — проверенные сегменты в `.mp3` / `.m4a`; зашифрованный HLS (METHOD ≠ NONE) и сохранение MPEG-TS не поддерживаются — честные ошибки | **реализовано в S8** по заданию владельца (hls.js и backend-прокси); отказ от зашифрованного HLS и от сохранения TS — решение исполнителя, ждёт подтверждения владельца при приёмке S8 |
