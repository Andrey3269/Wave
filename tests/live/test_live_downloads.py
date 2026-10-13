"""
LIVE-8 (S5): признаки разрешения на сохранение у настоящих источников. По умолчанию НЕ запускаются.

    python -m pytest -m live -rP tests/live/test_live_downloads.py

Нужен доступ к YouTube и SoundCloud; для YouTube — Deno, как в LIVE-2 (WAVE_JS_RUNTIME или deno в PATH).
Цели публичные (tests/live_targets.json), без входа в аккаунт. Профиль временный. Ссылки на файлы в вывод
не попадают: печатается только итог одной строкой JSON.

LIVE-8a — YouTube: видео под Creative Commons получает license «Creative Commons…», обычный клип — нет.
           Если YouTube перестал отдавать строку лицензии, тест падает: тогда загрузка YouTube в Wave
           фактически недоступна для всех треков, и это нужно знать (reports/S5.md).
LIVE-8b — SoundCloud: у трека есть поле downloadable; фиксируется, что отвечает tracks/{id}/download
           без входа в аккаунт (ожидаемо 401 — auth_required, либо ссылка на исходный файл).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import download_policy
import jsruntime
import soundcloud
import stream

pytestmark = pytest.mark.live
TARGETS = json.loads((Path(__file__).resolve().parent.parent / "live_targets.json").read_text(encoding="utf-8"))


@pytest.fixture
def real_runtime():
    jsruntime.reset()
    st = jsruntime.status()
    if not st.ok:
        pytest.fail(f"Для LIVE-8a нужен Deno ≥ 2.3.0: {st.problem} ({st.detail})", pytrace=False)
    yield st
    jsruntime.GUARD.terminate_all()


def test_live8a_youtube_license(real_runtime):
    cc = stream._extract_sync("yt", TARGETS["youtube_cc_video_id"]).license
    std = stream._extract_sync("yt", TARGETS["youtube_video_id"]).license
    print(json.dumps({"live": "8a", "cc_video_license": cc, "standard_video_license": std}, ensure_ascii=False))
    assert download_policy.is_creative_commons(cc), f"YouTube не сообщил лицензию CC для видео под CC: {cc!r}"
    assert not download_policy.is_creative_commons(std)


def test_live8b_soundcloud_creator_download():
    track_id = TARGETS["soundcloud_downloadable_track_id"]
    info = soundcloud.api_get(f"tracks/{track_id}")
    outcome = {"live": "8b", "downloadable": info.get("downloadable"),
               "has_downloads_left": info.get("has_downloads_left"), "license": info.get("license")}
    try:
        url = soundcloud.download_link(track_id)
        outcome["link"] = "получена (https)" if url.startswith("https://") else "получена (не https)"
    except soundcloud.DownloadLinkError as exc:
        outcome["link_error"] = exc.kind
        outcome["status"] = exc.status
    print(json.dumps(outcome, ensure_ascii=False))
    assert "downloadable" in info, "SoundCloud перестал отдавать поле downloadable"
    assert "link" in outcome or outcome["link_error"] in ("auth_required", "not_available", "rate_limited")
