"""
Источники признаков разрешения на сохранение (S5): поле license из ответа yt-dlp и ссылка на исходный
файл автора SoundCloud (soundcloud.download_link). Внешних вызовов нет: yt-dlp и экстрактор подменены.
"""
from __future__ import annotations

import io

import pytest
from yt_dlp.networking import Response
from yt_dlp.networking.exceptions import HTTPError
from yt_dlp.utils import ExtractorError

import download_policy
import soundcloud
import stream

URL = "https://rr1.googlevideo.com/videoplayback?expire=2000000000"


class _YDL:
    def __init__(self, info):
        self.info = info

    def extract_info(self, url, download):
        return self.info


@pytest.mark.parametrize("raw, expected", [
    ("Creative Commons Attribution license (reuse allowed)", "Creative Commons Attribution license (reuse allowed)"),
    ("  Standard YouTube License ", "Standard YouTube License"),
    ("", None), ("   ", None), (None, None), (42, None),
])
def test_license_is_taken_from_the_same_extraction(monkeypatch, raw, expected):
    info = {"url": URL} if raw is None else {"url": URL, "license": raw}
    monkeypatch.setattr(stream, "_get_ydl", lambda source: _YDL(info))
    assert stream._extract_sync("yt", "AAAAAAAAAA1").license == expected


@pytest.mark.parametrize("lic, ok", [
    ("Creative Commons Attribution license (reuse allowed)", True), ("creative commons", True),
    ("Standard YouTube License", False), ("Not Creative Commons", False), ("", False), (None, False),
])
def test_creative_commons_detection(lic, ok):
    assert download_policy.is_creative_commons(lic) is ok


class _IE:
    _CLIENT_ID = "test-client-id"
    _HEADERS = {}

    def __init__(self, result):
        self.result, self.calls = result, []

    def _download_json(self, url, video_id, note=None, query=None, headers=None):
        self.calls.append((url, dict(query or {})))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _use_ie(monkeypatch, result) -> _IE:
    ie = _IE(result)
    monkeypatch.setattr(soundcloud, "_get_ie", lambda: ie)
    return ie


def test_download_link_returns_redirect(monkeypatch):
    ie = _use_ie(monkeypatch, {"redirectUri": "https://cf-media.sndcdn.com/orig.wav?Policy=x"})
    assert soundcloud.download_link("123") == "https://cf-media.sndcdn.com/orig.wav?Policy=x"
    assert ie.calls == [("https://api-v2.soundcloud.com/tracks/123/download", {"client_id": "test-client-id"})]


@pytest.mark.parametrize("status, kind", [(401, "auth_required"), (403, "not_available"), (404, "not_available"),
                                          (429, "rate_limited"), (500, "failed")])
def test_download_link_http_errors(monkeypatch, status, kind):
    err = ExtractorError("HTTP error", cause=HTTPError(_fake_response(status)))
    _use_ie(monkeypatch, err)
    with pytest.raises(soundcloud.DownloadLinkError) as exc:
        soundcloud.download_link("123")
    assert exc.value.kind == kind and exc.value.status == status


@pytest.mark.parametrize("data", [{}, {"redirectUri": ""}, {"redirectUri": None}, [], None])
def test_download_link_without_redirect(monkeypatch, data):
    _use_ie(monkeypatch, data)
    with pytest.raises(soundcloud.DownloadLinkError) as exc:
        soundcloud.download_link("123")
    assert exc.value.kind == "not_available"


def test_download_link_does_not_refresh_client_id_on_401(monkeypatch):
    """401 здесь — «нужен вход», а не устаревший client_id: повторных запросов и сброса клиента нет."""
    ie = _use_ie(monkeypatch, ExtractorError("x", cause=HTTPError(_fake_response(401))))
    soundcloud._ie = ie
    with pytest.raises(soundcloud.DownloadLinkError):
        soundcloud.download_link("123")
    assert len(ie.calls) == 1 and soundcloud._ie is ie


def _fake_response(status):
    return Response(io.BytesIO(b""), "https://api-v2.soundcloud.com/x", {}, status=status)
