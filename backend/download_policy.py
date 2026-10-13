"""
Можно ли сохранить трек на устройство (S5). Решение принимает backend по признаку самого источника,
а не интерфейс: скрытая кнопка не защищает, прямой POST /api/downloads проходит ту же проверку.

Возможность воспроизвести трек разрешением на скачивание НЕ считается. Разрешение — это явный
признак, который сообщает источник о конкретном треке:

  yt  YouTube не даёт признака «файл можно сохранить», а его условия запрещают скачивание, кроме явно
      разрешённого сервисом или правообладателем. Единственный признак от правообладателя, который
      Wave видит, — лицензия Creative Commons у видео (поле `license`, его извлекает yt-dlp при том же
      резолве, что и для воспроизведения; отдельного запроса нет). Есть лицензия Creative Commons —
      сохраняем поток в исходном формате; нет или источник её не сообщил — отказ с причиной.
  sc  У трека SoundCloud есть признаки `downloadable` и `has_downloads_left`: автор сам включил
      скачивание. Тогда сохраняется исходный файл автора по ссылке из tracks/{id}/download, без
      перекодирования. Поток для воспроизведения при этом не сохраняется.

Уже скачанные треки политика не касается: они играют с диска и удаляются только пользователем.
Целевое место этой логики — providers/policy.py (TARGET_ARCHITECTURE §6); переносится на этапе адаптеров.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from urllib.parse import urlparse

from fastapi import HTTPException

import soundcloud
import stream

log = logging.getLogger("musicmeta.downloads")

# Заголовок с машинным кодом причины — как у потока (stream.ERROR_HEADER)
ERROR_HEADER = stream.ERROR_HEADER

BASIS_CC = "creative_commons"          # правообладатель разрешил повторное использование (YouTube)
BASIS_CREATOR = "creator_download"     # автор включил скачивание (SoundCloud)


@dataclass(frozen=True)
class Permission:
    """Разрешение на сохранение. url — откуда качать исходный файл (только sc), иначе поток резолвера."""
    basis: str
    license: str | None = None
    url: str | None = None


def refuse(status: int, code: str, detail: str) -> HTTPException:
    return HTTPException(status, detail, headers={ERROR_HEADER: code})


def is_creative_commons(lic: str | None) -> bool:
    return bool(lic) and lic.strip().lower().startswith("creative commons")


async def _check_yt(rs, track_id: str) -> Permission:
    res = await stream._resolve(rs, "yt", track_id)          # ошибки резолва (сеть, бот, возраст) — как есть
    if is_creative_commons(res.license):
        return Permission(BASIS_CC, license=res.license)
    if not (res.license or "").strip():
        raise refuse(403, "license_unknown", "YouTube не сообщил лицензию этого трека, поэтому сохранить его "
                                             "на устройство нельзя. Слушать его можно как обычно.")
    raise refuse(403, "download_not_permitted", "YouTube не разрешает сохранять этот трек: скачать можно только "
                                                "видео под лицензией Creative Commons. Слушать его можно как обычно.")


_SC_LINK_ERRORS = {
    "auth_required": (403, "auth_required", "SoundCloud отдаёт файл этого трека только вошедшим пользователям, "
                                            "а вход в аккаунт SoundCloud в Wave не поддерживается."),
    "not_available": (403, "download_not_permitted", "SoundCloud не выдал файл этого трека для скачивания."),
    "rate_limited": (503, "rate_limited", "SoundCloud временно ограничил число запросов. "
                                          "Повторите через несколько минут."),
    "failed": (502, "source_failed", "Не удалось получить файл от SoundCloud. Попробуйте позже."),
}


async def sc_original_url(track_id: str) -> str:
    """Свежая ссылка на исходный файл автора (её же берут заново, если старая протухла посреди загрузки)."""
    try:
        url = await asyncio.to_thread(soundcloud.download_link, track_id)
    except soundcloud.DownloadLinkError as exc:
        log.info("SoundCloud: ссылка на файл %s не получена: %s", track_id, exc)
        raise refuse(*_SC_LINK_ERRORS.get(exc.kind, _SC_LINK_ERRORS["failed"])) from exc
    if urlparse(url).scheme != "https":
        log.warning("SoundCloud: ссылка на файл %s не https", track_id)
        raise refuse(*_SC_LINK_ERRORS["failed"])
    return url


async def _check_sc(track_id: str) -> Permission:
    try:
        info = await asyncio.to_thread(soundcloud.api_get, f"tracks/{track_id}")
    except Exception as exc:
        log.info("SoundCloud: сведения о треке %s не получены: %r", track_id, exc)
        raise refuse(503, "source_unavailable", "SoundCloud не ответил, поэтому нельзя проверить, разрешил ли "
                                                "автор скачивание. Повторите позже.") from exc
    if not isinstance(info, dict) or "downloadable" not in info:
        raise refuse(403, "permission_unknown", "SoundCloud не сообщил, разрешено ли скачивание этого трека, "
                                                "поэтому сохранить его нельзя.")
    if info.get("downloadable") is not True:
        raise refuse(403, "download_not_permitted", "Автор не разрешил скачивание этого трека на SoundCloud. "
                                                    "Слушать его можно как обычно.")
    if info.get("has_downloads_left") is False:
        raise refuse(403, "download_limit_reached", "Лимит скачиваний этого трека, установленный автором на "
                                                    "SoundCloud, исчерпан.")
    lic = info.get("license") if isinstance(info.get("license"), str) else None
    return Permission(BASIS_CREATOR, license=lic, url=await sc_original_url(track_id))


async def check(rs, source: str, track_id: str) -> Permission:
    """Разрешение на сохранение трека или HTTPException с понятной причиной и кодом в X-Wave-Error."""
    if source == "yt":
        return await _check_yt(rs, track_id)
    if source == "sc":
        return await _check_sc(track_id)
    raise refuse(400, "unknown_source", "Некорректный трек")
