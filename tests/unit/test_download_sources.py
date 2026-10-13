"""
Признак «источник отдаёт лишь фрагмент» (S5): берётся из того же извлечения yt-dlp, что и ссылка на поток
(stream._extract_sync -> Resolved.preview). У SoundCloud yt-dlp помечает превью треков Go+ суффиксом «preview»
в format_id (yt_dlp/extractor/soundcloud.py, 2026.8.19). Внешних вызовов нет: YoutubeDL подменён.
"""
from __future__ import annotations

import pytest

import stream

URL = "https://cf-media.sndcdn.com/abc.mp3?Policy=x"


class _YDL:
    def __init__(self, info):
        self.info = info

    def extract_info(self, url, download):
        return self.info


@pytest.mark.parametrize("info, preview", [
    ({"url": URL, "protocol": "https", "format_id": "http_mp3_128"}, False),
    ({"url": URL, "protocol": "https", "format_id": "http_mp3_128_preview"}, True),
    ({"url": URL, "protocol": "https", "format_id": "HTTP_MP3_1_0_PREVIEW"}, True),
    ({"url": URL, "protocol": "https"}, False),                                  # format_id не сообщён
    ({"requested_formats": [{"url": URL, "protocol": "https", "format_id": "http_aac_160_preview"}]}, True),
])
def test_preview_flag_comes_from_the_same_extraction(monkeypatch, info, preview):
    monkeypatch.setattr(stream, "_get_ydl", lambda source: _YDL(info))
    res = stream._extract_sync("sc", "123456789")
    assert res.url == URL and res.preview is preview


def test_youtube_stream_is_not_a_preview(monkeypatch):
    info = {"url": "https://rr1.googlevideo.com/videoplayback?expire=2000000000", "format_id": "140"}
    monkeypatch.setattr(stream, "_get_ydl", lambda source: _YDL(info))
    assert stream._extract_sync("yt", "AAAAAAAAAA1").preview is False
