"""
HTTP Range (RFC 9110 §14) — чистые функции без сети и FastAPI.

Плеер (<audio>) шлёт один диапазон: `bytes=a-` при старте и перемотке, `bytes=a-b`, изредка суффикс
`bytes=-n`. Непонятный или синтаксически неверный Range по RFC 9110 игнорируется: ответ — весь ресурс, 200.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")         # одиночный диапазон (без multipart)
CONTENT_RANGE_RE = re.compile(r"^bytes (\d+)-(\d+)/(\d+|\*)$")
_SPEC_RE = re.compile(r"^\s*(\d*)\s*-\s*(\d*)\s*$")   # один range-spec внутри списка (для локальных файлов)


@dataclass(frozen=True, slots=True)
class ByteRange:
    """Один корректный диапазон: start..end включительно (end=None — до конца) или суффикс последних n байт."""
    start: int | None
    end: int | None
    suffix: int | None = None

    def header(self) -> str:
        if self.suffix is not None:
            return f"bytes=-{self.suffix}"
        return f"bytes={self.start}-" + ("" if self.end is None else str(self.end))


def parse_single(raw: str) -> ByteRange | None:
    """
    Один диапазон байтов или None, если заголовок нужно игнорировать (пустой, другая единица, несколько
    диапазонов, `bytes=-`, конец раньше начала). Пустой диапазон `bytes=-` раньше пересылался источнику
    как есть (находка L2) — теперь он, как и любой битый, означает «весь файл».
    """
    m = RANGE_RE.fullmatch(raw or "")
    if not m:
        return None
    a, b = m.group(1), m.group(2)
    if not a and not b:
        return None
    if not a:
        return ByteRange(None, None, suffix=int(b))
    start = int(a)
    end = int(b) if b else None
    if end is not None and end < start:
        return None
    return ByteRange(start, end)


def cap(raw: str, chunk: int) -> str:
    """
    Обрезает запрошенный диапазон до одной порции:
      bytes=a-   ->  bytes=a-(a+N-1)
      bytes=a-b  ->  bytes=a-min(b, a+N-1)
    Хвостовой диапазон (bytes=-n) оставляем как есть: плееры используют его для
    маленького кусочка в конце файла. Строку, которая не похожа на диапазон, возвращаем без изменений
    (вызывающий код до этого отбрасывает битые диапазоны через parse_single).
    """
    m = RANGE_RE.fullmatch(raw)
    if not m or not m.group(1):
        return raw
    start = int(m.group(1))
    last_allowed = start + chunk - 1
    end = min(int(m.group(2)), last_allowed) if m.group(2) else last_allowed
    return f"bytes={start}-{end}"


def is_valid_for_file(raw: str) -> bool:
    """
    Можно ли отдать заголовок Range как есть обработчику файлов (Starlette FileResponse), включая
    несколько диапазонов (multipart/byteranges). False — заголовок синтаксически неверен и по RFC 9110
    игнорируется: `bytes=abc`, `bytes=-`, `bytes=500-100`, другая единица. Неудовлетворимый, но верный
    диапазон (`bytes=99999-`) — True: на него ответ 416.
    """
    unit, sep, specs = (raw or "").partition("=")
    if not sep or unit.strip().lower() != "bytes" or not specs.strip():
        return False
    for spec in specs.split(","):
        m = _SPEC_RE.fullmatch(spec)
        if not m:
            return False
        a, b = m.group(1), m.group(2)
        if not a and not b:
            return False
        if a and b and int(b) < int(a):
            return False
    return True
