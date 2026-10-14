"""
Разбор и переписывание HLS-плейлистов (backend/streaming/hls.py, S8). Плейлисты — в форме реальных ответов
SoundCloud (mp3-HLS и AAC-HLS fMP4, 2026-10-14) и типовых конструкций RFC 8216.
"""
from __future__ import annotations

import pytest

from streaming import hls
from wavetest.hlsfix import master_playlist, media_playlist

BASE = "https://cdn.example/track/aac_160k/uuid/playlist.m3u8?expires=1&Policy=P&Signature=S&Key-Pair-Id=K"

# Как у SoundCloud AAC-HLS: абсолютные подписанные адреса, EXT-X-MAP с init.mp4
SC_AAC = media_playlist(
    [f"https://cdn.example/track/aac_160k/uuid/data{i:03d}.m4s?expires=1&Policy=P&Signature=S&Key-Pair-Id=K"
     for i in range(3)],
    [10.0078, 10.0078, 3.88517],
    map_uri="https://cdn.example/track/aac_160k/uuid/init.mp4?expires=1&Policy=P&Signature=S&Key-Pair-Id=K",
    version=7)


def seg_url(seg):
    return f"/p/main/s{seg.seq}"


def map_url(i):
    return f"/p/main/m{i}"


def test_soundcloud_aac_media_playlist():
    pl = hls.parse(SC_AAC, BASE)
    assert isinstance(pl, hls.MediaPlaylist) and pl.endlist
    assert [s.seq for s in pl.segments] == [0, 1, 2]
    assert pl.segments[2].duration == pytest.approx(3.88517)
    assert pl.segments[0].uri.endswith("data000.m4s?expires=1&Policy=P&Signature=S&Key-Pair-Id=K")   # подпись цела
    assert pl.maps[0].uri.endswith("init.mp4?expires=1&Policy=P&Signature=S&Key-Pair-Id=K")
    assert {s.map_index for s in pl.segments} == {0}


def test_rewrite_replaces_every_upstream_uri():
    pl = hls.parse(SC_AAC, BASE)
    out = hls.rewrite_media(SC_AAC, pl, seg_url, map_url)
    assert "cdn.example" not in out and "Signature" not in out and "Policy" not in out
    assert '#EXT-X-MAP:URI="/p/main/m0"' in out
    assert [ln for ln in out.splitlines() if ln.startswith("/p/")] == ["/p/main/s0", "/p/main/s1", "/p/main/s2"]
    assert "#EXT-X-ENDLIST" in out and "#EXT-X-PLAYLIST-TYPE:VOD" in out and "#EXTINF:3.885170," in out
    assert hls.parse(out, "http://127.0.0.1/p/main.m3u8").segments[1].uri == "http://127.0.0.1/p/main/s1"


def test_relative_uris_resolve_against_final_playlist_url():
    text = media_playlist(["seg0.mp3?part=a", "../other/seg1.mp3", "/abs/seg2.mp3", "https://cdn2.example/x/seg3.mp3"])
    pl = hls.parse(text, "https://cdn.example/a/b/playlist.m3u8?token=T")
    assert [s.uri for s in pl.segments] == [
        "https://cdn.example/a/b/seg0.mp3?part=a",           # свой query у сегмента сохраняется
        "https://cdn.example/a/other/seg1.mp3",               # query плейлиста по RFC 3986 не наследуется
        "https://cdn.example/abs/seg2.mp3",
        "https://cdn2.example/x/seg3.mp3",
    ]


def test_media_sequence_numbers_segments():
    pl = hls.parse(media_playlist(["a.ts", "b.ts"], media_sequence=40), "https://x.example/p.m3u8")
    assert [s.seq for s in pl.segments] == [40, 41]
    assert pl.segment(41).uri.endswith("/b.ts") and pl.segment(39) is None and pl.segment(42) is None


def test_byteranges_explicit_and_continued():
    text = media_playlist(["all.mp3", "all.mp3", "all.mp3"], byteranges=["1000@0", "500", "250@4000"],
                          map_uri="all.mp3", map_byterange="100@0")
    pl = hls.parse(text, "https://x.example/p.m3u8")
    assert [s.byterange for s in pl.segments] == [(0, 1000), (1000, 500), (4000, 250)]
    assert pl.maps[0].byterange == (0, 100)
    out = hls.rewrite_media(text, pl, seg_url, map_url)
    assert "BYTERANGE" not in out                                # диапазон выполняет прокси
    assert '#EXT-X-MAP:URI="/p/main/m0"' in out


@pytest.mark.parametrize("bad", ["500", "0@0", "x@1", ""])
def test_bad_byterange_is_rejected(bad):
    text = media_playlist(["a.mp3"], byteranges=[bad])
    with pytest.raises(hls.PlaylistError):
        hls.parse(text, "https://x.example/p.m3u8")


def test_master_playlist_variants_and_renditions():
    text = master_playlist([("v1/index.m3u8", 'BANDWIDTH=160000,CODECS="mp4a.40.2"'),
                            ("https://other.example/v2.m3u8?s=1", 'BANDWIDTH=96000,CODECS="mp4a.40.2"')],
                           renditions=["alt/a.m3u8"])
    text = text.replace("#EXT-X-INDEPENDENT-SEGMENTS",
                        '#EXT-X-INDEPENDENT-SEGMENTS\n#EXT-X-I-FRAME-STREAM-INF:BANDWIDTH=1,URI="iframe.m3u8"\n'
                        '#EXT-X-SESSION-DATA:DATA-ID="x",URI="https://evil.example/data.json"\n'
                        '#EXT-X-SESSION-KEY:METHOD=NONE')
    pl = hls.parse(text, "https://cdn.example/m/master.m3u8")
    assert isinstance(pl, hls.MasterPlaylist)
    assert [v.uri for v in pl.variants] == ["https://cdn.example/m/v1/index.m3u8", "https://other.example/v2.m3u8?s=1"]
    assert [r.uri for r in pl.renditions] == ["https://cdn.example/m/alt/a.m3u8"]
    out = hls.rewrite_master(text, pl, lambda i: f"/p/v{i}.m3u8", lambda i: f"/p/a{i}.m3u8")
    assert "example" not in out                                   # ни одного адреса источника (и I-frame, и data)
    assert 'NAME="Alt 0, extra"' in out and 'URI="/p/a0.m3u8"' in out   # запятая в строке атрибута не ломает разбор
    assert out.splitlines().count("/p/v0.m3u8") == 1 and "/p/v1.m3u8" in out
    assert 'BANDWIDTH=160000,CODECS="mp4a.40.2"' in out


@pytest.mark.parametrize("key", ["METHOD=AES-128,URI=\"k.bin\"", "METHOD=SAMPLE-AES,URI=\"skd://x\"",
                                 "METHOD=SAMPLE-AES-CTR,URI=\"k\"", "URI=\"k\""])
def test_encrypted_media_playlist_is_refused(key):
    text = media_playlist(["a.ts"], extra_header=f"#EXT-X-KEY:{key}")
    with pytest.raises(hls.PlaylistEncrypted):
        hls.parse(text, "https://x.example/p.m3u8")


def test_method_none_key_is_allowed():
    text = media_playlist(["a.ts"], extra_header="#EXT-X-KEY:METHOD=NONE")
    assert len(hls.parse(text, "https://x.example/p.m3u8").segments) == 1


def test_encrypted_session_key_in_master_is_refused():
    text = master_playlist([("v.m3u8", "BANDWIDTH=1")]).replace("#EXT-X-VERSION:6",
                                                                 '#EXT-X-VERSION:6\n#EXT-X-SESSION-KEY:METHOD=AES-128,URI="k"')
    with pytest.raises(hls.PlaylistEncrypted):
        hls.parse(text, "https://x.example/m.m3u8")


@pytest.mark.parametrize("text", [
    "",                                                       # пусто
    "not a playlist\n",                                       # нет #EXTM3U
    "#EXTM3U\n#EXT-X-TARGETDURATION:10\n#EXT-X-ENDLIST\n",    # нет сегментов
    "#EXTM3U\nseg.ts\n",                                      # URI без #EXTINF
    "#EXTM3U\n#EXTINF:abc,\nseg.ts\n",                        # битый EXTINF
    "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nv.m3u8\n#EXTINF:1,\ns.ts\n",   # и master, и media
    "#EXTM3U\n#EXT-X-DEFINE:NAME=\"x\",VALUE=\"y\"\n#EXTINF:1,\n{$x}.ts\n",  # подстановка переменных
    "#EXTM3U\n#EXTINF:1,\ndata:audio/mpeg;base64,AAAA\n",    # URI не http(s)
    "#EXTM3U\n#EXTINF:1,\nfile:///etc/passwd\n",
    "#EXTM3U\n#EXT-X-MAP:BYTERANGE=\"1@0\"\n#EXTINF:1,\ns.mp4\n",   # MAP без URI
    "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\n",               # master без вариантов
    "#EXTM3U\n#EXT-X-MAP:URI=\"i.mp4\",,\n#EXTINF:1,\ns.mp4\n",    # битый список атрибутов
])
def test_invalid_playlists_are_rejected(text):
    with pytest.raises(hls.PlaylistError):
        hls.parse(text, "https://x.example/p.m3u8")


def test_low_latency_and_unknown_tags():
    text = media_playlist(["a.mp4"], extra_header="#EXT-X-SERVER-CONTROL:CAN-BLOCK-RELOAD=YES\n#EXT-X-CUSTOM:1")
    text = text.replace("#EXTINF", '#EXT-X-PART:DURATION=1,URI="part.mp4"\n#EXT-X-PRELOAD-HINT:TYPE=PART,URI="h.mp4"\n#EXTINF', 1)
    text = text.replace("#EXT-X-ENDLIST", "#EXT-X-DISCONTINUITY\n#EXTINF:2,\nb.mp4\n#EXT-X-ENDLIST")
    pl = hls.parse(text, "https://x.example/p.m3u8")
    out = hls.rewrite_media(text, pl, seg_url, map_url)
    assert "part.mp4" not in out and "h.mp4" not in out and "SERVER-CONTROL" not in out   # LL-HLS убран
    assert "#EXT-X-CUSTOM:1" in out and "#EXT-X-DISCONTINUITY" in out                      # прочее сохранено
    assert [s.seq for s in pl.segments] == [0, 1]


def test_crlf_and_bom_are_handled():
    text = "﻿" + media_playlist(["a.mp3", "b.mp3"]).replace("\n", "\r\n")
    pl = hls.parse(text, "https://x.example/p.m3u8")
    assert len(pl.segments) == 2 and pl.segments[1].uri == "https://x.example/b.mp3"


def test_limits(monkeypatch):
    monkeypatch.setattr(hls, "MAX_SEGMENTS", 3)
    with pytest.raises(hls.PlaylistError):
        hls.parse(media_playlist([f"s{i}.mp3" for i in range(4)]), "https://x.example/p.m3u8")
    monkeypatch.setattr(hls, "MAX_VARIANTS", 1)
    with pytest.raises(hls.PlaylistError):
        hls.parse(master_playlist([("a.m3u8", "BANDWIDTH=1"), ("b.m3u8", "BANDWIDTH=2")]), "https://x.example/m.m3u8")


@pytest.mark.parametrize("attrs, expected", [
    ('A=1,B="x,y",C=0x1F,D=1.5,E=ENUM', [("A", "1"), ("B", '"x,y"'), ("C", "0x1F"), ("D", "1.5"), ("E", "ENUM")]),
    ("RESOLUTION=640x360", [("RESOLUTION", "640x360")]),
    ('URI=""', [("URI", '""')]),
])
def test_attribute_list(attrs, expected):
    assert hls.parse_attrs(attrs) == expected


@pytest.mark.parametrize("attrs", ['A=1,,B=2', 'a=1', 'A="unterminated', "A=1 B=2"])
def test_bad_attribute_list(attrs):
    with pytest.raises(hls.PlaylistError):
        hls.parse_attrs(attrs)


def test_signature_detects_different_stream():
    a = hls.parse(media_playlist(["a.mp3", "b.mp3"]), "https://x.example/p.m3u8")
    b = hls.parse(media_playlist(["c.mp3", "d.mp3"]), "https://y.example/p.m3u8")            # новые адреса, та же форма
    c = hls.parse(media_playlist(["c.mp3", "d.mp3", "e.mp3"]), "https://y.example/p.m3u8")
    assert a.signature() == b.signature() != c.signature()
