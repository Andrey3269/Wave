"""
Проверка контейнеров HLS-сегментов перед сохранением (backend/streaming/containers.py, S8): кадры MPEG audio
и боксы fragmented MP4. Склейка «как есть» без проверки не допускается.
"""
from __future__ import annotations

import struct

import pytest

from streaming import containers
from wavetest.hlsfix import MP3_FRAME_LEN, MP3_FRAME_SEC, box, fmp4_init, fmp4_segment, id3_tag, mp3_frames


@pytest.mark.parametrize("header, length, seconds", [
    (b"\xff\xfb\x90\xc4", 417, 1152 / 44100),         # MPEG-1 L3, 128 кбит/с, 44,1 кГц: 144*128000/44100
    (b"\xff\xfb\x92\x64", 418, 1152 / 44100),         # то же с padding (заголовок живого сегмента SoundCloud)
    (b"\xff\xfb\xa4\x00", 480, 1152 / 48000),         # MPEG-1 L3, 160 кбит/с, 48 кГц: 144*160000/48000
    (b"\xff\xf3\x64\x00", 144, 576 / 24000),          # MPEG-2 L3, 48 кбит/с, 24 кГц: 72*48000/24000
    (b"\xff\xfd\x90\x00", 522, 1152 / 44100),         # MPEG-1 L2, 160 кбит/с, 44,1 кГц: 144*160000/44100
    (b"\xff\xff\x90\x00", 312, 384 / 44100),          # MPEG-1 L1, 288 кбит/с: (12*288000/44100)*4
])
def test_mpeg_frame_lengths(header, length, seconds):
    assert containers.mpeg_frame(header) == (length, seconds)


@pytest.mark.parametrize("header", [
    b"\x00\x00\x00\x00", b"\xff\x00\x90\xc4",          # нет синхрослова
    b"\xff\xeb\x90\xc4",                                # версия «reserved»
    b"\xff\xf9\x90\xc4",                                # слой «reserved»
    b"\xff\xfb\xf0\xc4",                                # bitrate 15 — запрещён
    b"\xff\xfb\x00\xc4",                                # free format — не поддерживаем
    b"\xff\xfb\x9c\xc4",                                # частота «reserved»
    b"\xff\xfb",                                        # короткий
])
def test_mpeg_frame_rejects(header):
    assert containers.mpeg_frame(header) is None


def test_id3_size():
    tag = id3_tag()
    assert containers.id3_size(tag + b"\xff\xfb") == len(tag)
    assert containers.id3_size(b"\xff\xfb\x90\xc4") == 0
    assert containers.id3_size(b"ID3\x04\x00\x00\x80\x00\x00\x00") == 0     # не synchsafe — не ID3


@pytest.mark.parametrize("data, has_map, expected", [
    (mp3_frames(3), False, containers.MP3),
    (id3_tag() + mp3_frames(3), False, containers.MP3),
    ((b"\x47" + bytes(187)) * 3, False, containers.MPEGTS),
    (fmp4_segment(0), True, containers.FMP4),
    (fmp4_segment(0), False, containers.FMP4),
    (b"<html>error</html>", False, None),
    (b"OggS\x00\x02" + bytes(100), False, None),
])
def test_sniff(data, has_map, expected):
    assert containers.sniff(data, has_map) == expected


# ---------------------------------------------------------------------------
# MPEG audio: склейка с проверкой
# ---------------------------------------------------------------------------
def _join(segments):
    j = containers.Mp3Joiner()
    out = b"".join(j.feed(s) for s in segments) + j.finish()
    return out, j


def test_join_whole_frame_segments():
    segs = [mp3_frames(10), mp3_frames(5), mp3_frames(7)]
    out, j = _join(segs)
    assert out == b"".join(segs) and j.frames == 22 and j.seconds == pytest.approx(22 * MP3_FRAME_SEC)


def test_frame_split_across_segment_boundary():
    """Сегменты — байтовые срезы файла, граница может разрезать кадр: результат — исходный поток без потерь."""
    stream = mp3_frames(20)
    cuts = [0, 1000, 1001, 4000, 6500, len(stream)]
    out, j = _join([stream[a:b] for a, b in zip(cuts, cuts[1:])])
    assert out == stream and j.frames == 20


def test_id3_at_segment_starts_is_removed():
    segs = [id3_tag() + mp3_frames(4), id3_tag() + mp3_frames(4)]
    out, j = _join(segs)
    assert out == mp3_frames(8) and j.frames == 8


def test_id3v1_at_the_very_end_is_kept():
    tag = b"TAG" + bytes(125)
    out, j = _join([mp3_frames(3) + tag])
    assert out == mp3_frames(3) + tag


def test_truncated_last_frame_is_dropped():
    out, j = _join([mp3_frames(3) + mp3_frames(1)[:200]])
    assert out == mp3_frames(3) and j.frames == 3


@pytest.mark.parametrize("segments", [
    [mp3_frames(3) + b"garbage-in-the-middle" + mp3_frames(3)],
    [mp3_frames(3), b"<html>Error 403</html>" + bytes(1000)],
    [b"#EXTM3U\n#EXT-X-VERSION:3\n" + bytes(600)],                   # плейлист вместо аудио
    [mp3_frames(2) + bytes([0xFF, 0xF3, 0x64, 0x00]) + bytes(284)],   # другая версия MPEG посреди потока
])
def test_broken_mpeg_stream_is_rejected(segments):
    with pytest.raises(containers.ContainerError):
        _join(segments)


def test_empty_stream_is_rejected():
    with pytest.raises(containers.ContainerError):
        _join([b""])


# ---------------------------------------------------------------------------
# fragmented MP4
# ---------------------------------------------------------------------------
def test_valid_fmp4():
    containers.check_init(fmp4_init())
    containers.check_fragment(fmp4_segment(3))
    assert [k for k, _, _ in containers.boxes(fmp4_segment(1))] == [b"styp", b"moof", b"mdat"]


def test_64bit_box_size():
    payload = b"x" * 10
    big = struct.pack(">I4sQ", 1, b"mdat", 16 + len(payload)) + payload
    assert containers.boxes(big) == [(b"mdat", 0, 26)]


@pytest.mark.parametrize("init", [
    box(b"ftyp", b"iso5"),                                             # нет moov
    box(b"ftyp", b"iso5") + box(b"moov", box(b"mvhd", bytes(100)) + box(b"trak", b"")),   # нет mvex: не fMP4
    fmp4_init()[:-5],                                                  # обрезан
    fmp4_init() + box(b"mdat", b"x"),                                  # медиаданные в init
])
def test_bad_init(init):
    with pytest.raises(containers.ContainerError):
        containers.check_init(init)


@pytest.mark.parametrize("frag", [
    box(b"styp", b"msdh") + box(b"moof", b""),                         # нет mdat
    box(b"mdat", b"x") + box(b"moof", b""),                            # mdat раньше moof
    fmp4_segment(0) + box(b"moov", b""),                               # чужой бокс
    fmp4_segment(0)[:-3],                                              # обрезан
    struct.pack(">I4s", 4, b"mdat"),                                   # размер меньше заголовка
    struct.pack(">I4s", 9999, b"moof") + b"x",                         # выходит за границы
    struct.pack(">I4s", 16, b"\x00\x01\x02\x03") + bytes(8),           # тип бокса — не символы
])
def test_bad_fragment(frag):
    with pytest.raises(containers.ContainerError):
        containers.check_fragment(frag)


def test_reference_frame_length_constant():
    assert MP3_FRAME_LEN == containers.mpeg_frame(b"\xff\xfb\x90\xc4")[0]
