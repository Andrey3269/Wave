"""
Разбор и переписывание HLS-плейлистов (RFC 8216) для прокси Wave (S8). Чистые функции: без сети.

Плейлист разбирается построчно по структуре HLS — теги, списки атрибутов (с кавычками и запятыми внутри
строк), URI после #EXTINF / #EXT-X-STREAM-INF — а не поиском и заменой текста. Относительные URI
разрешаются от итогового адреса плейлиста (после редиректов) по RFC 3986; собственные query-параметры
сегментов (подписи CDN) сохраняются как есть.

При переписывании каждый внешний URI заменяется на локальный адрес прокси, который адресует ресурс по его
роли и номеру (вариант N, сегмент с порядковым номером S, init-секция N), а не по URL: фронтенд никогда не
видит и не передаёт адреса источника.

Что поддержано (нужно для реальных источников, проверено на SoundCloud 2026-10):
  * media playlist VOD: #EXTINF + URI, #EXT-X-MEDIA-SEQUENCE, #EXT-X-ENDLIST, #EXT-X-DISCONTINUITY и прочие
    информационные теги (сохраняются);
  * #EXT-X-MAP (init-секция fMP4), в том числе с BYTERANGE;
  * #EXT-X-BYTERANGE у сегментов (диапазон выполняет прокси, в плейлисте для плеера тег убирается);
  * master playlist: #EXT-X-STREAM-INF (варианты), #EXT-X-MEDIA с URI (альтернативные дорожки);
  * #EXT-X-KEY / #EXT-X-SESSION-KEY с METHOD=NONE.
Сознательно не поддержано (ошибка, а не тихая порча): шифрование (METHOD=AES-128 / SAMPLE-AES / любой,
кроме NONE) — Wave не воспроизводит зашифрованные потоки; #EXT-X-DEFINE (подстановка переменных в URI);
URI не http(s). Low-Latency HLS (#EXT-X-PART, #EXT-X-PRELOAD-HINT, #EXT-X-RENDITION-REPORT), I-frame
плейлисты и #EXT-X-SESSION-DATA с URI для аудио не нужны — такие строки из плейлиста убираются.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import urljoin, urlsplit

MAX_SEGMENTS = 20_000
MAX_VARIANTS = 64
MAX_MAPS = 64
MAX_LINES = 120_000

# Значение — строка в кавычках (может содержать запятые) или значение без пробелов, кавычек и запятых
_ATTR_RE = re.compile(r'([A-Z0-9-]+)=("[^"\r\n]*"|[^",\s]*)')
_BYTERANGE_RE = re.compile(r"^(\d+)(?:@(\d+))?$")
# Строки LL-HLS, I-frame и данные сеанса с внешним URI: для воспроизведения аудио не нужны, убираются
_DROP_TAGS = ("#EXT-X-PART:", "#EXT-X-PRELOAD-HINT:", "#EXT-X-RENDITION-REPORT:", "#EXT-X-I-FRAME-STREAM-INF:",
              "#EXT-X-SKIP:", "#EXT-X-PART-INF:", "#EXT-X-SERVER-CONTROL:")


class PlaylistError(Exception):
    """Плейлист некорректен или использует то, что Wave не поддерживает."""


class PlaylistEncrypted(PlaylistError):
    """Плейлист требует расшифровки (METHOD не NONE)."""


@dataclass(slots=True)
class Resource:
    uri: str                                   # абсолютный адрес у источника
    byterange: tuple[int, int] | None = None   # (смещение, длина) или None — весь файл


@dataclass(slots=True)
class Segment(Resource):
    seq: int = 0                               # порядковый номер (EXT-X-MEDIA-SEQUENCE + индекс)
    duration: float = 0.0
    map_index: int | None = None               # какая init-секция действует для сегмента


@dataclass
class MediaPlaylist:
    segments: list[Segment] = field(default_factory=list)
    maps: list[Resource] = field(default_factory=list)
    endlist: bool = False

    def segment(self, seq: int) -> Segment | None:
        if not self.segments:
            return None
        i = seq - self.segments[0].seq
        if 0 <= i < len(self.segments) and self.segments[i].seq == seq:
            return self.segments[i]
        return next((s for s in self.segments if s.seq == seq), None)

    def signature(self) -> tuple:
        """Признаки содержимого: по ним видно, что после перерезолва это тот же поток (нумерация совпадает)."""
        first = self.segments[0].seq if self.segments else 0
        return ("media", first, len(self.segments), len(self.maps))


@dataclass
class MasterPlaylist:
    variants: list[Resource] = field(default_factory=list)     # EXT-X-STREAM-INF
    renditions: list[Resource] = field(default_factory=list)   # EXT-X-MEDIA с URI

    def signature(self) -> tuple:
        return ("master", len(self.variants), len(self.renditions))


Playlist = MediaPlaylist | MasterPlaylist


# ---------------------------------------------------------------------------
# Списки атрибутов
# ---------------------------------------------------------------------------
def parse_attrs(text: str) -> list[tuple[str, str]]:
    """Список атрибутов RFC 8216 §4.2 с сохранением порядка; значения как в тексте (кавычки сохраняются)."""
    out: list[tuple[str, str]] = []
    pos, n = 0, len(text)
    while pos < n:
        m = _ATTR_RE.match(text, pos)
        if not m:
            raise PlaylistError("некорректный список атрибутов")
        out.append((m.group(1), m.group(2)))
        pos = m.end()
        while pos < n and text[pos] in " \t":           # нестрогие плейлисты ставят пробел после запятой
            pos += 1
        if pos < n:
            if text[pos] != ",":
                raise PlaylistError("некорректный список атрибутов")
            pos += 1
            while pos < n and text[pos] in " \t":
                pos += 1
    return out


def _unquote(value: str) -> str:
    return value[1:-1] if len(value) >= 2 and value[0] == value[-1] == '"' else value


def _attr(attrs: list[tuple[str, str]], name: str) -> str | None:
    return next((_unquote(v) for k, v in attrs if k == name), None)


def _format_attrs(attrs: list[tuple[str, str]]) -> str:
    return ",".join(f"{k}={v}" for k, v in attrs)


def _resolve_uri(base: str, uri: str) -> str:
    uri = uri.strip()
    if not uri:
        raise PlaylistError("пустой URI")
    absolute = urljoin(base, uri)
    if urlsplit(absolute).scheme not in ("http", "https"):
        raise PlaylistError("URI не http(s)")
    return absolute


def _byterange(value: str, prev_end: int | None) -> tuple[int, int]:
    m = _BYTERANGE_RE.fullmatch(value.strip())
    if not m:
        raise PlaylistError("некорректный BYTERANGE")
    length = int(m.group(1))
    if m.group(2) is not None:
        offset = int(m.group(2))
    elif prev_end is not None:
        offset = prev_end
    else:
        raise PlaylistError("BYTERANGE без смещения в начале ресурса")
    if length <= 0:
        raise PlaylistError("нулевой BYTERANGE")
    return offset, length


def _check_key(attrs: list[tuple[str, str]]) -> None:
    method = (_attr(attrs, "METHOD") or "").upper()
    if method != "NONE":
        raise PlaylistEncrypted(f"шифрование {method or '?'}")


# ---------------------------------------------------------------------------
# Разбор
# ---------------------------------------------------------------------------
def _lines(text: str) -> list[str]:
    if text.startswith("﻿"):
        text = text[1:]
    lines = text.splitlines()
    if len(lines) > MAX_LINES:
        raise PlaylistError("слишком длинный плейлист")
    if not lines or lines[0].strip() != "#EXTM3U":
        raise PlaylistError("нет заголовка #EXTM3U")
    return lines


def parse(text: str, base_url: str) -> Playlist:
    lines = _lines(text)
    if any(ln.startswith("#EXT-X-DEFINE") for ln in lines):
        raise PlaylistError("#EXT-X-DEFINE (подстановка переменных) не поддерживается")
    is_master = any(ln.startswith("#EXT-X-STREAM-INF:") for ln in lines)
    if is_master and any(ln.startswith("#EXTINF:") for ln in lines):
        raise PlaylistError("плейлист одновременно master и media")
    return _parse_master(lines, base_url) if is_master else _parse_media(lines, base_url)


def _parse_master(lines: list[str], base: str) -> MasterPlaylist:
    pl = MasterPlaylist()
    expect_variant = False
    for raw in lines[1:]:
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-STREAM-INF:"):
            parse_attrs(line.split(":", 1)[1])
            expect_variant = True
        elif line.startswith("#EXT-X-MEDIA:"):
            attrs = parse_attrs(line.split(":", 1)[1])
            uri = _attr(attrs, "URI")
            if uri is not None:
                pl.renditions.append(Resource(_resolve_uri(base, uri)))
        elif line.startswith("#EXT-X-SESSION-KEY:"):
            _check_key(parse_attrs(line.split(":", 1)[1]))
        elif line.startswith("#"):
            continue
        elif expect_variant:
            pl.variants.append(Resource(_resolve_uri(base, line)))
            expect_variant = False
        else:
            raise PlaylistError("URI без #EXT-X-STREAM-INF")
        if len(pl.variants) > MAX_VARIANTS or len(pl.renditions) > MAX_VARIANTS:
            raise PlaylistError("слишком много вариантов")
    if not pl.variants:
        raise PlaylistError("в master-плейлисте нет вариантов")
    return pl


def _parse_media(lines: list[str], base: str) -> MediaPlaylist:
    pl = MediaPlaylist()
    seq = 0
    duration: float | None = None
    pending_range: tuple[int, int] | None = None
    range_end: dict[str, int] = {}              # конец предыдущего BYTERANGE того же ресурса
    last_uri_for_range: str | None = None
    current_map: int | None = None
    raw_range: str | None = None
    for raw in lines[1:]:
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            if pl.segments:
                raise PlaylistError("#EXT-X-MEDIA-SEQUENCE после сегментов")
            try:
                seq = int(line.split(":", 1)[1])
            except ValueError as exc:
                raise PlaylistError("некорректный #EXT-X-MEDIA-SEQUENCE") from exc
        elif line.startswith("#EXTINF:"):
            try:
                duration = float(line.split(":", 1)[1].split(",", 1)[0])
            except ValueError as exc:
                raise PlaylistError("некорректный #EXTINF") from exc
        elif line.startswith("#EXT-X-BYTERANGE:"):
            raw_range = line.split(":", 1)[1]
        elif line.startswith("#EXT-X-MAP:"):
            attrs = parse_attrs(line.split(":", 1)[1])
            uri = _attr(attrs, "URI")
            if uri is None:
                raise PlaylistError("#EXT-X-MAP без URI")
            br = _attr(attrs, "BYTERANGE")
            byterange = _byterange(br, None) if br is not None else None
            pl.maps.append(Resource(_resolve_uri(base, uri), byterange))
            if len(pl.maps) > MAX_MAPS:
                raise PlaylistError("слишком много #EXT-X-MAP")
            current_map = len(pl.maps) - 1
        elif line.startswith("#EXT-X-KEY:"):
            _check_key(parse_attrs(line.split(":", 1)[1]))
        elif line.startswith("#EXT-X-ENDLIST"):
            pl.endlist = True
        elif line.startswith("#"):
            continue
        else:
            if duration is None:
                raise PlaylistError("URI сегмента без #EXTINF")
            uri = _resolve_uri(base, line)
            if raw_range is not None:
                prev = range_end.get(uri) if last_uri_for_range == uri else None
                pending_range = _byterange(raw_range, prev)
                range_end[uri] = pending_range[0] + pending_range[1]
                last_uri_for_range = uri
            pl.segments.append(Segment(uri=uri, byterange=pending_range, seq=seq, duration=duration,
                                       map_index=current_map))
            if len(pl.segments) > MAX_SEGMENTS:
                raise PlaylistError("слишком много сегментов")
            seq += 1
            duration, pending_range, raw_range = None, None, None
    if not pl.segments:
        raise PlaylistError("в плейлисте нет сегментов")
    return pl


# ---------------------------------------------------------------------------
# Переписывание
# ---------------------------------------------------------------------------
def _replace_uri(line: str, new_uri: str, drop: tuple[str, ...] = ()) -> str:
    tag, _, rest = line.partition(":")
    attrs = [(k, f'"{new_uri}"' if k == "URI" else v) for k, v in parse_attrs(rest) if k not in drop]
    return f"{tag}:{_format_attrs(attrs)}"


def rewrite_media(text: str, pl: MediaPlaylist, segment_url: Callable[[Segment], str],
                  map_url: Callable[[int], str]) -> str:
    """Media playlist для плеера: адреса сегментов и init-секций заменены, BYTERANGE выполняет прокси."""
    out: list[str] = []
    seg_i = map_i = 0
    for raw in _lines(text):
        line = raw.strip()
        if line.startswith(_DROP_TAGS) or line.startswith("#EXT-X-BYTERANGE:"):
            continue
        if line.startswith("#EXT-X-MAP:"):
            out.append(_replace_uri(line, map_url(map_i), drop=("BYTERANGE",)))
            map_i += 1
        elif line and not line.startswith("#"):
            out.append(segment_url(pl.segments[seg_i]))
            seg_i += 1
        else:
            out.append(line)
    if seg_i != len(pl.segments):
        raise PlaylistError("рассинхрон разбора и переписывания")
    return "\n".join(out) + "\n"


def rewrite_master(text: str, pl: MasterPlaylist, variant_url: Callable[[int], str],
                   rendition_url: Callable[[int], str]) -> str:
    """Master playlist для плеера: варианты и дорожки с URI заменены локальными адресами."""
    out: list[str] = []
    var_i = rend_i = 0
    for raw in _lines(text):
        line = raw.strip()
        if line.startswith(_DROP_TAGS) or (line.startswith("#EXT-X-SESSION-DATA:") and "URI=" in line):
            continue
        if line.startswith("#EXT-X-MEDIA:") and _attr(parse_attrs(line.split(":", 1)[1]), "URI") is not None:
            out.append(_replace_uri(line, rendition_url(rend_i)))
            rend_i += 1
        elif line and not line.startswith("#"):
            out.append(variant_url(var_i))
            var_i += 1
        else:
            out.append(line)
    if var_i != len(pl.variants) or rend_i != len(pl.renditions):
        raise PlaylistError("рассинхрон разбора и переписывания")
    return "\n".join(out) + "\n"
