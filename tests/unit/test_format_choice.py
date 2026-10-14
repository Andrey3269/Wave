"""
Выбор формата потока (S8, N4): streaming.resolver.classify / choose_format и Resolved.kind.

Форматы — живой ответ yt-dlp для SoundCloud (tests/fixtures/soundcloud/formats_2026-10.json): в одном
резолве есть прогрессивный mp3, в соседнем — только AAC-HLS.
"""
from __future__ import annotations

import copy

import pytest

from streaming import resolver
from wavetest.fakes import load_fixture

FX = load_fixture("soundcloud/formats_2026-10.json")


def _pick(formats):
    f, kind, codec = resolver.choose_format({"formats": copy.deepcopy(formats)})
    return f["format_id"], kind, codec


def test_progressive_is_preferred_when_available():
    assert _pick(FX["full"]) == ("http_mp3_0_0", "progressive", "mp3")


def test_hls_mp3_when_progressive_missing():
    formats = [f for f in FX["full"] if f["protocol"] != "http"]
    assert _pick(formats) == ("hls_mp3_0_0", "hls", "mp3")


def test_hls_aac_only_response_plays_as_hls():
    """Живой ответ N4: только AAC-HLS — раньше «Requested format is not available», теперь HLS 160k."""
    assert _pick(FX["hls_aac_only"]) == ("hls_aac_160k", "hls", "aac")


@pytest.mark.parametrize("fmt, expected", [
    ({"url": "https://x/a.mp3", "protocol": "https", "acodec": "mp3"}, ("progressive", "mp3")),
    ({"url": "https://x/a.mp3", "protocol": "http", "ext": "mp3"}, ("progressive", "mp3")),
    ({"url": "https://x/p.m3u8", "protocol": "m3u8_native", "acodec": "mp4a.40.2"}, ("hls", "aac")),
    ({"url": "https://x/p.m3u8", "protocol": "m3u8", "ext": "m4a"}, ("hls", "aac")),
    ({"url": "https://x/playlist.m3u8?sig=1", "protocol": "https", "acodec": "mp3"}, ("hls", "mp3")),   # .m3u8 по https
    ({"url": "https://x/p.m3u8", "protocol": "m3u8_native", "acodec": "opus"}, ("hls", "opus")),
    ({"url": "https://x/p.m3u8", "protocol": "m3u8_native"}, ("hls", None)),
    ({"url": "https://x/p.m3u8", "protocol": "m3u8_native", "format_id": "hls-aes_aac_160k"}, ("encrypted", None)),
    ({"url": "https://x/p", "protocol": "m3u8_native", "has_drm": True}, ("encrypted", None)),
    ({"url": "https://x/m.mpd", "protocol": "http_dash_segments"}, None),
    ({"protocol": "https"}, None),
])
def test_classify(fmt, expected):
    assert resolver.classify(fmt) == expected


def test_opus_hls_is_not_chosen():
    """Opus-HLS SoundCloud — сегменты Ogg, hls.js их не воспроизводит: такой формат не выбирается."""
    formats = [{"format_id": "hls_opus_64", "url": "https://x/o.m3u8", "protocol": "m3u8_native", "acodec": "opus"},
               {"format_id": "hls_mp3_128", "url": "https://x/m.m3u8", "protocol": "m3u8_native", "acodec": "mp3"}]
    assert _pick(formats)[0] == "hls_mp3_128"
    with pytest.raises(resolver.NoPlayableFormat):
        _pick(formats[:1])


def test_only_encrypted_variants_raise_encrypted():
    formats = [{"format_id": "hls-aes_aac_160k", "url": "https://x/e.m3u8", "protocol": "m3u8_native"}]
    with pytest.raises(resolver.HlsEncrypted):
        _pick(formats)


def test_preview_is_last_resort():
    full = {"format_id": "hls_aac_160k", "url": "https://x/f.m3u8", "protocol": "m3u8_native", "acodec": "mp4a.40.2"}
    preview = {"format_id": "http_mp3_128_preview", "url": "https://x/p.mp3", "protocol": "http", "acodec": "mp3"}
    assert _pick([preview, full])[0] == "hls_aac_160k"
    assert _pick([preview])[0] == "http_mp3_128_preview"


def test_higher_bitrate_wins_within_group():
    formats = [f for f in FX["hls_aac_only"]]
    assert _pick(list(reversed(formats)))[0] == "hls_aac_160k"


# ---------------------------------------------------------------------------
# Через _extract_sync и HTTP-ответ резолвера
# ---------------------------------------------------------------------------
class _YDL:
    def __init__(self, info):
        self.info = info

    def extract_info(self, url, download):
        return copy.deepcopy(self.info)


def test_extract_sync_marks_kind_codec_and_preview(monkeypatch):
    monkeypatch.setattr(resolver, "_get_ydl", lambda source: _YDL({"formats": FX["hls_aac_only"],
                                                                    "http_headers": {"User-Agent": "UA", "Cookie": "x"}}))
    res = resolver._extract_sync("sc", "1000000001")
    assert (res.kind, res.codec, res.preview) == ("hls", "aac", False)
    assert res.url.startswith("https://playback.media-streaming.soundcloud.cloud/") and "aac_160k" in res.url
    assert res.headers == {"User-Agent": "UA"}
    assert res.expires_at == 2000000000 - resolver.EXPIRY_MARGIN          # ?expires= у плейлистов SoundCloud


@pytest.mark.parametrize("formats, status, kind, phrase", [
    ([{"format_id": "hls-aes_aac_160k", "url": "https://x/e.m3u8", "protocol": "m3u8_native"}],
     403, "hls_encrypted", "зашифрованном"),
    ([{"format_id": "hls_opus_64", "url": "https://x/o.m3u8", "protocol": "m3u8_native", "acodec": "opus"}],
     404, "no_playable_format", "не предложил формат"),
])
def test_unplayable_formats_give_honest_errors(monkeypatch, formats, status, kind, phrase):
    """Вместо «изменил формат ответа / yt-dlp устарел» (N4) — настоящая причина."""
    import asyncio

    from fastapi import HTTPException
    monkeypatch.setattr(resolver, "_get_ydl", lambda source: _YDL({"formats": formats}))

    async def go():
        return await resolver._resolve_uncached(resolver.ResolverState(), "sc", "1000000001")
    with pytest.raises(HTTPException) as e:
        asyncio.run(go())
    assert e.value.status_code == status and e.value.headers["X-Wave-Error"] == kind
    assert phrase in e.value.detail and "yt-dlp" not in e.value.detail
def test_sources_hls_flag_matches_frontend():
    """PROGRESSIVE_ONLY во frontend/app.js — ровно источники без HLS (иначе плеер не спросит /playback)."""
    import re
    from pathlib import Path
    app = (Path(__file__).resolve().parents[2] / "frontend" / "app.js").read_text(encoding="utf-8")
    m = re.search(r"const PROGRESSIVE_ONLY = new Set\(\[([^\]]*)\]\)", app)
    assert m, "в app.js нет PROGRESSIVE_ONLY"
    front = set(re.findall(r'"(\w+)"', m.group(1)))
    assert front == {name for name, cfg in resolver.SOURCES.items() if not cfg.hls}
