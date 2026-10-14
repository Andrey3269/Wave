"""
Проверка контейнеров HLS-сегментов перед сохранением в файл (S8, загрузки HLS-only треков).

Сегменты не склеиваются вслепую: каждый проверяется по структуре своего формата, и результат собирается
только для форматов, у которых склейка даёт корректный самостоятельный файл:

  * MPEG audio (mp3-HLS SoundCloud): сегмент — последовательность кадров MPEG-1/2/2.5 Layer I–III
    (впереди может стоять тег ID3v2 с меткой времени HLS — он отбрасывается). Склейка кадров — обычный
    mp3-файл; проверяется непрерывность кадров через границы сегментов и итоговая длительность.
  * fragmented MP4 (AAC-HLS SoundCloud, #EXT-X-MAP): init-секция — ftyp + moov с mvex, сегменты — боксы
    styp/sidx/moof/mdat. init + сегменты по порядку — корректный фрагментированный MP4 (.m4a), так его
    читают и WebView2, и ffmpeg (проверено на живом потоке 2026-10-14).

MPEG-TS и прочие контейнеры распознаются и честно отклоняются: из TS без перепаковки (ffmpeg) не получить
файл, который играет <audio>, а дополнительного runtime в Wave нет.
"""
from __future__ import annotations

import struct

MP3 = "mp3"
FMP4 = "fmp4"
MPEGTS = "mpegts"

# Таблицы MPEG audio (ISO/IEC 11172-3, 13818-3): кбит/с по [версия][слой][индекс]
_BITRATES = {
    (1, 1): (0, 32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448),
    (1, 2): (0, 32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384),
    (1, 3): (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320),
    (2, 1): (0, 32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256),
    (2, 2): (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
    (2, 3): (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
}
_SAMPLE_RATES = {1: (44100, 48000, 32000), 2: (22050, 24000, 16000), 25: (11025, 12000, 8000)}
_FRAGMENT_BOXES = {b"styp", b"sidx", b"ssix", b"prft", b"emsg", b"moof", b"mdat", b"free", b"skip", b"uuid"}
MAX_FRAME = 2881                     # самый длинный кадр MPEG audio (Layer II, 384 кбит/с, 8 кГц + padding)


class ContainerError(Exception):
    """Данные не соответствуют заявленному контейнеру — сохранять их как аудиофайл нельзя."""


# ---------------------------------------------------------------------------
# Определение формата
# ---------------------------------------------------------------------------
def id3_size(data: bytes) -> int:
    """Длина тега ID3v2 в начале данных (0, если тега нет)."""
    if len(data) >= 10 and data[:3] == b"ID3" and data[3] < 0xFF and not any(b & 0x80 for b in data[6:10]):
        size = (data[6] << 21) | (data[7] << 14) | (data[8] << 7) | data[9]
        footer = 10 if data[5] & 0x10 else 0
        return 10 + size + footer
    return 0


def mpeg_frame(header: bytes) -> tuple[int, float] | None:
    """(длина кадра в байтах, длительность в секундах) по 4 байтам заголовка или None — это не кадр."""
    if len(header) < 4 or header[0] != 0xFF or (header[1] & 0xE0) != 0xE0:
        return None
    ver_bits, layer_bits = (header[1] >> 3) & 3, (header[1] >> 1) & 3
    if ver_bits == 1 or layer_bits == 0:
        return None
    version = {3: 1, 2: 2, 0: 25}[ver_bits]
    layer = 4 - layer_bits
    br_idx, sr_idx, pad = header[2] >> 4, (header[2] >> 2) & 3, (header[2] >> 1) & 1
    if br_idx in (0, 15) or sr_idx == 3:
        return None
    bitrate = _BITRATES[(1 if version == 1 else 2, layer)][br_idx] * 1000
    rate = _SAMPLE_RATES[version][sr_idx]
    if layer == 1:
        return (12 * bitrate // rate + pad) * 4, 384 / rate
    samples = 1152 if (layer == 2 or version == 1) else 576
    return samples // 8 * bitrate // rate + pad, samples / rate


def sniff(first_segment: bytes, has_map: bool) -> str | None:
    """Контейнер по первому сегменту: mp3 | fmp4 | mpegts | None (не распознан)."""
    if has_map:
        return FMP4
    if len(first_segment) >= 189 and first_segment[0] == 0x47 and first_segment[188] == 0x47:
        return MPEGTS
    body = first_segment[id3_size(first_segment):]
    if mpeg_frame(body[:4]) is not None:
        return MP3
    if len(body) >= 8 and body[4:8] in (b"styp", b"moof", b"ftyp"):
        return FMP4
    return None


# ---------------------------------------------------------------------------
# MPEG audio
# ---------------------------------------------------------------------------
class Mp3Joiner:
    """
    Принимает сегменты по порядку и отдаёт байты для файла: кадры MPEG audio без тегов ID3v2 в начале
    сегментов. Кадры проверяются подряд, в том числе кадр, разрезанный границей сегментов; любая
    рассинхронизация — ContainerError. После последнего сегмента finish() проверяет хвост.
    """

    def __init__(self):
        self.frames = 0
        self.seconds = 0.0
        self._carry = b""                  # начало кадра, которое пришло в конце прошлого сегмента
        self._signature: bytes | None = None

    def feed(self, segment: bytes) -> bytes:
        if not self._carry:
            segment = segment[id3_size(segment):]
        data = self._carry + segment
        pos, n = 0, len(data)
        while pos + 4 <= n:
            frame = mpeg_frame(data[pos:pos + 4])
            if frame is None:
                if data[pos:pos + 3] == b"TAG" and n - pos <= 128:      # ID3v1 в самом конце файла
                    break
                raise ContainerError(f"нет кадра MPEG audio на смещении {pos}")
            sig = bytes([data[pos + 1] & 0xFE, data[pos + 2] & 0x0C])    # версия, слой, частота — не меняются
            if self._signature is None:
                self._signature = sig
            elif sig != self._signature:
                raise ContainerError("параметры потока MPEG audio меняются посреди файла")
            length, seconds = frame
            if pos + length > n:
                break
            pos += length
            self.frames += 1
            self.seconds += seconds
        out, self._carry = data[:pos], data[pos:]
        if len(self._carry) > MAX_FRAME + 128:
            raise ContainerError("рассинхронизация кадров MPEG audio")
        return out

    def finish(self) -> bytes:
        tail, self._carry = self._carry, b""
        if self.frames == 0:
            raise ContainerError("в сегментах нет ни одного кадра MPEG audio")
        if tail and not (tail[:3] == b"TAG" and len(tail) <= 128) and len(tail) >= 4 and mpeg_frame(tail[:4]) is None:
            raise ContainerError("мусор в конце потока MPEG audio")
        return tail if tail[:3] == b"TAG" else b""          # обрезанный последний кадр не сохраняем


# ---------------------------------------------------------------------------
# ISO BMFF (fragmented MP4)
# ---------------------------------------------------------------------------
def boxes(data: bytes, start: int = 0, end: int | None = None) -> list[tuple[bytes, int, int]]:
    """Боксы верхнего уровня [start, end): (тип, смещение, размер). ContainerError, если структура неверна."""
    end = len(data) if end is None else end
    out, pos = [], start
    while pos < end:
        if end - pos < 8:
            raise ContainerError("обрезанный заголовок бокса")
        size, kind = struct.unpack(">I4s", data[pos:pos + 8])
        header = 8
        if size == 1:
            if end - pos < 16:
                raise ContainerError("обрезанный 64-битный размер бокса")
            size = struct.unpack(">Q", data[pos + 8:pos + 16])[0]
            header = 16
        elif size == 0:
            size = end - pos                   # «до конца» — допустимо только для последнего бокса
        if size < header or pos + size > end:
            raise ContainerError(f"размер бокса {kind!r} выходит за границы данных")
        if not all(32 <= b < 127 for b in kind):
            raise ContainerError("недопустимый тип бокса")
        out.append((kind, pos, size))
        pos += size
    return out


def check_init(data: bytes) -> None:
    top = boxes(data)
    kinds = [k for k, _, _ in top]
    if b"ftyp" not in kinds or b"moov" not in kinds:
        raise ContainerError("в init-секции нет ftyp и moov")
    _, off, size = next(b for b in top if b[0] == b"moov")
    inner = [k for k, _, _ in boxes(data, off + 8, off + size)]
    if b"mvex" not in inner or b"trak" not in inner:
        raise ContainerError("init-секция не фрагментированного MP4 (нет mvex/trak)")
    if b"mdat" in kinds or b"moof" in kinds:
        raise ContainerError("в init-секции есть медиаданные")


def check_fragment(data: bytes) -> None:
    kinds = [k for k, _, _ in boxes(data)]
    unknown = [k for k in kinds if k not in _FRAGMENT_BOXES]
    if unknown:
        raise ContainerError(f"неожиданные боксы во фрагменте: {unknown[:3]}")
    if b"moof" not in kinds or b"mdat" not in kinds or kinds.index(b"mdat") < kinds.index(b"moof"):
        raise ContainerError("во фрагменте нет пары moof + mdat")
