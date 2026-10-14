"""
Тестовые HLS-потоки без сети и без бинарных файлов в git (S8).

  mp3_frames(n)        — n настоящих кадров MPEG-1 Layer III (44,1 кГц, 128 кбит/с, моно, тишина): их
                         декодирует любой mp3-декодер, в том числе Chromium через hls.js (MSE audio/mpeg);
  fmp4_init/segment    — минимальные, но структурно правильные боксы ISO BMFF (ftyp/moov/mvex, styp/moof/mdat);
  media_playlist(...)  — текст media playlist (VOD) с сегментами, опционально EXT-X-MAP и BYTERANGE;
  master_playlist(...) — текст master playlist.

Реальная структура взята из живых ответов SoundCloud 2026-10-14: mp3-HLS — сырые MPEG-кадры по ~2–10 с,
AAC-HLS — init.mp4 + dataNNN.m4s (fMP4).
"""
from __future__ import annotations

import struct

MP3_HEADER = bytes([0xFF, 0xFB, 0x90, 0xC4])     # MPEG-1 Layer III, без CRC, 128 кбит/с, 44,1 кГц, моно
MP3_FRAME_LEN = 417                               # 144 * 128000 / 44100, без padding
MP3_FRAME_SEC = 1152 / 44100


def mp3_frames(n: int) -> bytes:
    """n кадров тишины: заголовок, нулевая side info (17 байт для моно), нулевые основные данные."""
    frame = MP3_HEADER + bytes(MP3_FRAME_LEN - len(MP3_HEADER))
    return frame * n


def id3_tag(payload: bytes = b"PRIV\x00\x00\x00\x08\x00\x00com.x\x00\x00\x01") -> bytes:
    """ID3v2.4 в начале сегмента (так HLS иногда передаёт метку времени): размер — synchsafe."""
    size = len(payload)
    synchsafe = bytes([(size >> 21) & 0x7F, (size >> 14) & 0x7F, (size >> 7) & 0x7F, size & 0x7F])
    return b"ID3\x04\x00\x00" + synchsafe + payload


def box(kind: bytes, payload: bytes = b"") -> bytes:
    return struct.pack(">I4s", 8 + len(payload), kind) + payload


def fmp4_init() -> bytes:
    trak = box(b"trak", box(b"tkhd", bytes(84)) + box(b"mdia", box(b"mdhd", bytes(24)) + box(b"hdlr", bytes(25))))
    moov = box(b"moov", box(b"mvhd", bytes(100)) + trak + box(b"mvex", box(b"trex", bytes(24))))
    return box(b"ftyp", b"iso5\x00\x00\x02\x00iso5iso6mp41") + moov


def fmp4_segment(seq: int, payload_len: int = 2000) -> bytes:
    moof = box(b"moof", box(b"mfhd", struct.pack(">II", 0, seq + 1)) + box(b"traf", box(b"tfhd", bytes(8))))
    mdat = box(b"mdat", bytes((seq * 31 + i) % 251 for i in range(payload_len)))
    return box(b"styp", b"msdh\x00\x00\x00\x00msdhmsix") + moof + mdat


def media_playlist(uris: list[str], durations: list[float] | None = None, *, map_uri: str | None = None,
                   map_byterange: str | None = None, byteranges: list[str] | None = None, media_sequence: int = 0,
                   extra_header: str = "", endlist: bool = True, version: int = 6) -> str:
    lines = ["#EXTM3U", f"#EXT-X-VERSION:{version}", "#EXT-X-PLAYLIST-TYPE:VOD",
             f"#EXT-X-TARGETDURATION:{int(max(durations or [10])) + 1}", f"#EXT-X-MEDIA-SEQUENCE:{media_sequence}"]
    if extra_header:
        lines.append(extra_header)
    if map_uri:
        attrs = f'URI="{map_uri}"' + (f',BYTERANGE="{map_byterange}"' if map_byterange else "")
        lines.append(f"#EXT-X-MAP:{attrs}")
    for i, uri in enumerate(uris):
        lines.append(f"#EXTINF:{(durations or [10.0] * len(uris))[i]:.6f},")
        if byteranges:
            lines.append(f"#EXT-X-BYTERANGE:{byteranges[i]}")
        lines.append(uri)
    if endlist:
        lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


def master_playlist(variants: list[tuple[str, str]], renditions: list[str] | None = None) -> str:
    """variants: [(uri, "BANDWIDTH=...,CODECS=...")]; renditions — URI альтернативных аудиодорожек."""
    lines = ["#EXTM3U", "#EXT-X-VERSION:6", "#EXT-X-INDEPENDENT-SEGMENTS"]
    for i, uri in enumerate(renditions or []):
        lines.append(f'#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",NAME="Alt {i}, extra",DEFAULT=NO,URI="{uri}"')
    for uri, attrs in variants:
        lines.append(f"#EXT-X-STREAM-INF:{attrs}")
        lines.append(uri)
    return "\n".join(lines) + "\n"
