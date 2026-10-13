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


# ---------------------------------------------------------------------------
# Скачивание, разрешённое автором трека (S5)
# ---------------------------------------------------------------------------
# У трека в API v2 есть признаки `downloadable` и `has_downloads_left`: автор включил кнопку скачивания
# на SoundCloud и лимит скачиваний не исчерпан. Тогда эндпоинт tracks/{id}/download отдаёт ссылку на
# исходный файл автора (`redirectUri`). Так же поступает экстрактор yt-dlp 2026.8.19
# (SoundcloudBaseIE._extract_info_dict). Без входа в аккаунт SoundCloud эндпоинт обычно отвечает 401:
# исходный файл выдаётся только зарегистрированным пользователям.


class DownloadLinkError(Exception):
    """kind: auth_required (401) | not_available (403/404) | rate_limited (429) | failed (остальное)."""

    def __init__(self, kind: str, status: int | None = None):
        super().__init__(f"{kind} (HTTP {status})" if status else kind)
        self.kind, self.status = kind, status


_DL_KINDS = {401: "auth_required", 403: "not_available", 404: "not_available", 410: "not_available",
             429: "rate_limited"}


def download_link(track_id: str) -> str:
    """
    Ссылка на исходный файл автора. Блокирующий вызов (запускать в потоке).

    Не через _call_api: тот при 401/403 сбрасывает и заново получает client_id, а здесь 401 означает
    «нужен вход в аккаунт», а не устаревший client_id (как и в самом yt-dlp).
    """
    global _ie
    with _lock:
        try:
            ie = _get_ie()
            data = ie._download_json(API_BASE + f"tracks/{track_id}/download", f"soundcloud-download-{track_id}",
                                     note="Ссылка на файл SoundCloud", query={"client_id": ie._CLIENT_ID},
                                     headers=getattr(ie, "_HEADERS", None) or {})
        except ExtractorError as exc:
            status = getattr(exc.cause, "status", None) or getattr(exc.cause, "code", None)
            raise DownloadLinkError(_DL_KINDS.get(status, "failed"), status) from exc
        except Exception as exc:
            _ie = None
            raise DownloadLinkError("failed") from exc
    url = data.get("redirectUri") if isinstance(data, dict) else None
    if not isinstance(url, str) or not url:
        raise DownloadLinkError("not_available")
    return url
