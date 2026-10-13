"""
Поиск по SoundCloud.

yt-dlp умеет искать (scsearch:), но возвращает только ссылки без названий и обложек.
Поэтому мы вызываем API v2 SoundCloud сами, а client_id (который SoundCloud
периодически меняет) берём у экстрактора yt-dlp: он обновляет его автоматически
и хранит в своём дисковом кэше.

ВНИМАНИЕ: используются внутренние методы yt-dlp (get_info_extractor, _call_api).
Если после обновления yt-dlp поиск SoundCloud сломается, в консоли будет предупреждение
"Провайдер sc не ответил", а YouTube продолжит работать.
"""
from __future__ import annotations

import threading

from yt_dlp import YoutubeDL
from yt_dlp.utils import ExtractorError

from stream import ydl_opts

API_BASE = "https://api-v2.soundcloud.com/"
API_SEARCH = API_BASE + "search/tracks"
_NOTE = "Поиск на SoundCloud"


def _api_get(ie, url: str, query: dict) -> dict:
    """
    Запрос к API SoundCloud с client_id.

    В современном yt-dlp метод _call_api сам добавляет client_id и при ответе 401/403
    скачивает свежий. Метод _download_json client_id НЕ добавляет, и без него
    SoundCloud отвечает 401 Unauthorized.
    """
    call_api = getattr(ie, "_call_api", None)
    if call_api is not None:
        return call_api(url, "soundcloud-search", note=_NOTE, query=query)

    # Запасной путь для очень старых версий yt-dlp: подставляем client_id сами
    for attempt in range(2):
        try:
            return ie._download_json(
                url, "soundcloud-search", note=_NOTE, query={**query, "client_id": ie._CLIENT_ID}
            )
        except ExtractorError as exc:
            status = getattr(exc.cause, "status", None) or getattr(exc.cause, "code", None)
            if attempt == 0 and status in (401, 403):
                ie._update_client_id()   # скачать свежий client_id и повторить
                continue
            raise
    return {}


# Один постоянный клиент на весь процесс: client_id и соединения остаются в памяти,
# поэтому повторные поиски не тратят время на инициализацию и новые TLS-рукопожатия.
_lock = threading.Lock()
_ie = None


def _get_ie():
    global _ie
    if _ie is None:
        ydl = YoutubeDL(ydl_opts())
        ie = ydl.get_info_extractor("Soundcloud")
        ie.initialize()  # подтягивает client_id из кэша (или обновляет)
        _ie = ie
    return _ie


def search_tracks(query: str, limit: int) -> list[dict]:
    """Блокирующий вызов (запускать в потоке). Возвращает «сырые» объекты треков API."""
    global _ie
    with _lock:
        try:
            data = _api_get(
                _get_ie(),
                API_SEARCH,
                {"q": query, "limit": min(limit, 100), "offset": 0, "linked_partitioning": 1},
            )
        except Exception:
            _ie = None  # при сбое в следующий раз создадим клиент заново
            raise
    return (data or {}).get("collection") or []


def api_get(path: str, query: dict | None = None):
    """Любой GET к API v2 SoundCloud (users/{id}, playlists/{id}, search/users ...). Блокирующий."""
    global _ie
    with _lock:
        try:
            return _api_get(_get_ie(), API_BASE + path, query or {})
        except Exception:
            _ie = None
            raise