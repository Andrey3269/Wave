"""
Загрузка трека, который источник отдаёт только по HLS (S8). Правило S5 сохраняется: скачивается ровно тот
поток, который играет плеер (тот же резолвер, тот же выбор формата), а не какой-то другой.

Путь: корневой плейлист -> (для master — вариант с наибольшим BANDWIDTH) -> media playlist VOD -> init-секция
и сегменты по порядку через те же защищённые запросы, что и у HLS-прокси (netguard: https, публичные IP,
проверка редиректов; перерезолв протухшей ссылки). Каждый сегмент проверяется по своему контейнеру
(streaming.containers) и только потом пишется в файл; обрыв сегмента — повтор этого сегмента (файл
откатывается к его началу), без дублей и дыр. В итоге:
  * mp3-HLS  -> audio.mp3 (кадры MPEG audio; проверяется и итоговая длительность по плейлисту);
  * fMP4-HLS -> audio.m4a (init + фрагменты; проверяется структура боксов).
MPEG-TS и неизвестные контейнеры не сохраняются (честная ошибка), m3u8 под видом аудио — никогда.
Один сегмент держится в памяти целиком (до MAX_SEGMENT_BYTES; у SoundCloud — сотни КБ), чтобы проверить
его до записи.
"""
from __future__ import annotations

import logging
import secrets
from pathlib import Path

import httpx

from streaming import containers, errors, hls, hls_proxy

log = logging.getLogger("musicmeta.stream.hls")

MAX_SEGMENT_BYTES = 64 * 1024 * 1024
SEGMENT_RETRIES = 3               # повторов одного сегмента после обрыва сети


def _unsupported(label: str, what: str) -> Exception:
    return errors._err(415, "hls_container_unsupported",
                       f"{label} передаёт этот трек в формате, который Wave не умеет сохранять в файл ({what}). "
                       "Слушать трек можно, скачать — нет.")


def _bad_data(label: str, exc: Exception) -> Exception:
    log.warning("HLS-загрузка: данные не прошли проверку: %s", exc)
    return errors._err(502, "hls_bad_segment", f"{label} прислал повреждённые данные аудио, трек не сохранён. "
                                               "Попробуйте ещё раз.")


def _live(label: str) -> Exception:
    return errors._err(415, "hls_live", f"{label} передаёт этот трек как прямую трансляцию — сохранить его нельзя.")


async def _read_all(upstream: httpx.Response, expected: int | None) -> bytes | None:
    """Тело целиком или None при обрыве (меньше ожидаемого / ошибка сети)."""
    buf = bytearray()
    try:
        async for chunk in upstream.aiter_raw():
            buf += chunk
            if len(buf) > MAX_SEGMENT_BYTES:
                raise containers.ContainerError("сегмент слишком большой")
    except httpx.HTTPError as exc:
        log.info("HLS-загрузка: сегмент оборван после %d байт: %s", len(buf), type(exc).__name__)
        return None
    finally:
        await upstream.aclose()
    if expected is not None and len(buf) < expected:
        return None
    return bytes(buf)


async def _fetch(client, rs, s: hls_proxy.Session, pl: str, kind: str, num: int) -> bytes:
    """Сегмент (kind="s", num=seq) или init-секция (kind="m"): перерезолв при протухшей ссылке, повтор при обрыве."""
    label = hls_proxy._label(s)
    refresh, breaks = False, 0
    while True:
        _text, media = await hls_proxy._load_playlist(client, rs, s, pl, refresh=refresh)
        item = media.segment(num) if kind == "s" else (media.maps[num] if num < len(media.maps) else None)
        if item is None:
            raise hls_proxy._changed()
        extra = {}
        if item.byterange is not None:
            offset, length = item.byterange
            extra["Range"] = f"bytes={offset}-{offset + length - 1}"
        try:
            upstream = await hls_proxy._open(client, rs, s, item.uri, extra)
        except hls_proxy._Stale as st:
            if refresh:
                raise hls_proxy._stale(s, st.status) from None
            refresh = True
            continue
        status = upstream.status_code
        if item.byterange is None:
            ok, expected, cut = status == 200, None, None
            length_h = upstream.headers.get("content-length")
            expected = int(length_h) if length_h and length_h.isdigit() else None
        else:
            offset, length = item.byterange
            ok = status == 206 or (status == 200 and offset == 0)
            expected, cut = length, length
        if not ok:
            await upstream.aclose()
            raise hls_proxy._upstream_status(s, status)
        data = await _read_all(upstream, expected)
        if data is None:
            breaks += 1
            if breaks > SEGMENT_RETRIES:
                raise errors._err(503, "network", f"Сервер аудио {label} раз за разом обрывает соединение. "
                                                  "Попробуйте позже.")
            continue
        return data[:cut] if cut is not None else data


async def download(client, rs, source: str, track_id: str, dest: Path, job: dict) -> str:
    """Скачивает HLS-поток трека в dest; возвращает расширение файла (.mp3 | .m4a)."""
    s = hls_proxy.Session(token="dl-" + secrets.token_hex(8), source=source, track_id=track_id)   # вне реестра
    label = hls_proxy._label(s)
    _text, root = await hls_proxy._load_playlist(client, rs, s, "main")
    pl = "main"
    if isinstance(root, hls.MasterPlaylist):
        best = max(range(len(root.variants)), key=lambda i: root.variants[i].bandwidth)
        pl = f"v{best}"
        _text, media = await hls_proxy._load_playlist(client, rs, s, pl)
    else:
        media = root
    if not media.endlist:
        raise _live(label)
    if len({seg.map_index for seg in media.segments}) > 1:
        raise _unsupported(label, "несколько init-секций")

    has_map = bool(media.maps)
    container: str | None = None
    joiner: containers.Mp3Joiner | None = None
    written = 0
    try:
        with open(dest, "wb") as f:
            if has_map:
                init = await _fetch(client, rs, s, pl, "m", media.segments[0].map_index or 0)
                containers.check_init(init)
                f.write(init)
                written += len(init)
            for i, seg in enumerate(media.segments):
                data = await _fetch(client, rs, s, pl, "s", seg.seq)
                if container is None:
                    container = containers.sniff(data, has_map)
                    if container == containers.MPEGTS:
                        raise _unsupported(label, "HLS с сегментами MPEG-TS")
                    if container not in (containers.MP3, containers.FMP4):
                        raise _unsupported(label, "неизвестный контейнер сегментов")
                    if container == containers.MP3:
                        joiner = containers.Mp3Joiner()
                if joiner is not None:
                    out = joiner.feed(data)
                else:
                    containers.check_fragment(data)
                    out = data
                f.write(out)
                written += len(out)
                job["received"] = written
                job["progress"] = min(99, int((i + 1) * 100 / len(media.segments)))
            if joiner is not None:
                tail = joiner.finish()
                f.write(tail)
                expected = sum(seg.duration for seg in media.segments)
                if abs(joiner.seconds - expected) > max(2.0, 0.02 * expected):
                    raise containers.ContainerError(
                        f"длительность {joiner.seconds:.1f} с вместо {expected:.1f} с по плейлисту")
    except containers.ContainerError as exc:
        raise _bad_data(label, exc) from exc
    log.info("HLS-загрузка %s:%s: %s, %d сегментов, %d КБ", source, track_id, container, len(media.segments),
             written // 1024)
    return ".mp3" if container == containers.MP3 else ".m4a"
