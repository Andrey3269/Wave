"""
Можно ли скачать трек (S5, решение владельца 2026-10-13). Проверка выполняется на backend на этапе
`preparing`, до первого байта аудио; прямой POST /api/downloads проходит её же.

Правило: трек YouTube Music или SoundCloud скачивается, если его аудиопоток штатно получен тем же
резолвером, что и для воспроизведения (streaming.resolver._resolve: yt-dlp, кэш ссылок, JS-runtime, обработка
ошибок). Сохраняется ровно этот поток в исходном формате. Лицензия Creative Commons и признак
`downloadable` SoundCloud условием НЕ являются.

S8: поток может быть прогрессивным файлом или HLS (SoundCloud часть треков отдаёт только так) — загрузчик
сохраняет тот же поток, что играет плеер (streaming/hls_download.py проверяет и собирает сегменты).

Отказ — только если поток получить нельзя:
  * ошибки резолвера передаются как есть (unavailable, age_restricted, no_direct_stream, bot_check,
    rate_limited, network, js_runtime …): тот же текст и код, что видит плеер;
  * источник отдаёт лишь фрагмент (SoundCloud Go+: 30-секундный превью) — `preview_only`: фрагмент не
    сохраняется под видом полного трека.

Здесь нет ни входа в аккаунты, ни обхода DRM, платного доступа или антибот-проверок: если штатный
резолвер их встречает, загрузка заканчивается понятной ошибкой. Такая загрузка не означает разрешения
самих платформ на скачивание (TARGET_ARCHITECTURE §6).
Уже скачанные треки здесь не проверяются: они играют с диска и удаляются только пользователем.
"""
from __future__ import annotations

from fastapi import HTTPException

from streaming import errors, resolver

# Заголовок с машинным кодом причины — как у потока (streaming.errors.ERROR_HEADER)
ERROR_HEADER = errors.ERROR_HEADER


def refuse(status: int, code: str, detail: str) -> HTTPException:
    return HTTPException(status, detail, headers={ERROR_HEADER: code})


async def check(rs, source: str, track_id: str) -> resolver.Resolved:
    """
    Поток трека штатно доступен целиком — или HTTPException с понятной причиной и кодом в X-Wave-Error.
    Возвращает полученный поток (S8: загрузчик по нему выбирает путь — progressive или HLS).
    """
    if source not in resolver.SOURCES:
        raise refuse(400, "unknown_source", "Некорректный трек")
    res = await resolver._resolve(rs, source, track_id)        # ошибки резолва (сеть, бот, возраст …) — как есть
    if res.preview:
        raise refuse(403, "preview_only", f"{resolver.SOURCES[source].label} отдаёт только короткий фрагмент этого "
                                          "трека (полная версия — по подписке), поэтому он не сохранён.")
    return res
