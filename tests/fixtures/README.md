# Фикстуры провайдеров

Ответы внешних сервисов для unit- и API-тестов. Все файлы составлены вручную: реальных ответов,
cookies, токенов, `client_id`, подписанных URL и персональных данных здесь нет. Идентификаторы
вымышленные, но соответствуют формату, который проверяет backend (videoId — 11 символов
`[A-Za-z0-9_-]`, id трека SoundCloud — число).

| Файл | Структура взята из | Что моделирует |
|---|---|---|
| `ytmusic/search_songs.json` | `YTMusic.search(filter="songs")`, ytmusicapi 1.12.3 (`parsers/search.py`) | обычная песня с несколькими артистами (один без `id`), формат длительности `ч:мм:сс`, элемент без `videoId`, пустые поля |
| `ytmusic/watch_playlist.json` | `YTMusic.get_watch_playlist` (`parsers/watch.py`: ключи `length`, `thumbnail`) | «радио» трека |
| `ytmusic/artist.json` | `YTMusic.get_artist` (`mixins/browsing.py`) | песни, альбомы, синглы с `params`, похожие артисты, элементы без `browseId` |
| `ytmusic/artist_albums.json` | `YTMusic.get_artist_albums` | полный список релизов (дубль и новый альбом) |
| `ytmusic/album.json` | `YTMusic.get_album` (в треках `album` — строка, `browsing.py:617`) | недоступный трек без `videoId`, трек без артистов |
| `ytmusic/search_artists.json`, `search_albums.json` | `YTMusic.search(filter="artists" / "albums")` | карточки, элементы без `browseId` и без названия |
| `soundcloud/search_tracks.json` | API v2 `search/tracks` (как использует `backend/soundcloud.py`) | `publisher_metadata`, `full_duration`, политики `SNIP`/`BLOCK`, `streamable=false`, не-трек, нет `id` |
| `soundcloud/search_users.json`, `search_albums.json`, `user.json` | API v2 `search/users`, `search/albums`, `users/{id}` | карточки артистов и альбомов |
| `soundcloud/playlist.json`, `tracks_batch.json` | API v2 `playlists/{id}` (полные данные только у первых треков) и `tracks?ids=` | догрузка «заглушек» треков альбома |
| `soundcloud/formats_2026-10.json` | список `formats` yt-dlp 2026.8.19 для публичного трека SoundCloud, снят 2026-10-14 (S8); адреса и подписи заменены | `full`: прогрессивный mp3, HLS mp3, HLS AAC 96/160; `hls_aac_only`: тот же трек в соседнем резолве (N4) |
| `lrclib/get_synced.json` | LRCLIB `GET /api/get` | синхронный и обычный текст, строки без таймкода |
| `recs_eval/baseline.json` | не ответ сервиса: метрики офлайн-оценки рекомендаций алгоритма до S14 (`37781f8`) | базовая линия для `tests/api/test_recs_eval.py`; сценарии истории — `tests/wavetest/recs_eval.py` |

Если провайдер изменил формат, сначала обновите фикстуру по реальной структуре (без секретов и
персональных данных) и укажите в этой таблице версию библиотеки или дату проверки.
