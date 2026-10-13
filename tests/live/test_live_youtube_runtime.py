"""
Живые проверки воспроизведения YouTube с JS-runtime (S3, TEST_STRATEGY §7). По умолчанию НЕ запускаются.

    python -m pytest -m live -rP tests/live/test_live_youtube_runtime.py

Нужны доступ к YouTube и Deno ≥ 2.3.0: переменная WAVE_JS_RUNTIME (например, packaging\\runtime\\deno\\deno.exe
после `python packaging/fetch_runtime.py`) или deno в PATH. Трек — публичный (tests/live_targets.json),
без входа в аккаунт. Профиль временный, как в остальных тестах. Ссылки на поток в вывод не попадают.

LIVE-2 — yt-dlp с yt-dlp-ejs и Deno: резолв публичного трека, прямой URL, JS-задачи решались в Deno.
LIVE-3 — стрим через приложение: 206 на `bytes=0-`, перемотка в середину, перерезолв после инвалидации.
LIVE-4 — без JS-runtime: фиксирует фактическую деградацию (сколько форматов, какая ошибка) — вывод -rP.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

import jsruntime
import stream

pytestmark = pytest.mark.live
TARGETS = json.loads((Path(__file__).resolve().parent.parent / "live_targets.json").read_text(encoding="utf-8"))
VIDEO = TARGETS["youtube_video_id"]


@pytest.fixture
def real_runtime():
    """Настоящая проверка runtime (в тестах по умолчанию подставлен «исправный» статус-заглушка)."""
    jsruntime.reset()
    st = jsruntime.status()
    if not st.ok:
        pytest.fail(f"Для LIVE-2/LIVE-3 нужен Deno ≥ 2.3.0: {st.problem} ({st.detail}). Задайте {jsruntime.ENV_PATH} "
                    "или выполните python packaging/fetch_runtime.py", pytrace=False)
    yield st
    jsruntime.GUARD.terminate_all()


def _counting_extract(monkeypatch) -> list:
    calls = []
    original = stream._extract_sync

    def wrapper(source, track_id):
        calls.append(track_id)
        return original(source, track_id)

    monkeypatch.setattr(stream, "_extract_sync", wrapper)
    return calls


def test_live2_resolve_with_ejs_and_deno(real_runtime):
    runs = jsruntime.GUARD.runs
    t0 = time.monotonic()
    res = stream._extract_sync("yt", VIDEO)
    took = time.monotonic() - t0
    assert res.url.startswith("https://") and res.expires_at > time.time()
    solved = jsruntime.GUARD.runs - runs
    print(json.dumps({"LIVE-2": "ok", "deno": real_runtime.version, "source": real_runtime.source,
                      "resolve_seconds": round(took, 2), "js_challenge_runs": solved}, ensure_ascii=False))
    assert solved > 0, "yt-dlp не запускал Deno для JS-задач (проверьте предупреждения в журнале)"


def test_live3_stream_range_seek_and_reresolve(real_runtime, monkeypatch):
    """Настоящее приложение без подмен: lifespan, httpx-клиент, резолвер yt-dlp, прокси с Range."""
    from fastapi.testclient import TestClient

    import main

    calls = _counting_extract(monkeypatch)
    with TestClient(main.app, base_url="http://127.0.0.1") as c:
        path = f"/api/stream/yt/{VIDEO}"
        first = c.get(path, headers={"Range": "bytes=0-"})
        assert first.status_code == 206, first.text
        total = int(first.headers["content-range"].rsplit("/", 1)[1])
        assert first.headers["content-range"].startswith("bytes 0-") and len(first.content) > 0
        mid = total // 2
        seek = c.get(path, headers={"Range": f"bytes={mid}-"})
        assert seek.status_code == 206 and seek.headers["content-range"].startswith(f"bytes {mid}-")
        assert len(calls) == 1                                                  # ссылка из кэша
        stream._invalidate("yt", VIDEO)
        stream._heads.clear()
        again = c.get(path, headers={"Range": f"bytes={mid}-"})
        assert again.status_code == 206 and again.content[:64] == seek.content[:64]
        assert len(calls) == 2                                                  # перерезолв после инвалидации
    print(json.dumps({"LIVE-3": "ok", "total_bytes": total, "resolves": len(calls)}))


def test_live4_degradation_without_runtime(monkeypatch):
    """Фиксирует, что происходит без JS-runtime (тест проходит при любом честном исходе, вывод — -rP)."""
    from yt_dlp import YoutubeDL
    from yt_dlp.utils import DownloadError

    monkeypatch.setattr(jsruntime, "_status", jsruntime.RuntimeStatus(False, problem="not_found", checked_at=float("inf")))
    opts = stream.ydl_opts(stream.SOURCES["yt"].fmt)
    assert opts["js_runtimes"] == {}
    outcome: dict = {"LIVE-4": None}
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={VIDEO}", download=False)
        formats = info.get("formats") or []
        outcome.update({"LIVE-4": "resolved_degraded", "formats": len(formats),
                        "audio_formats": sum(1 for f in formats if f.get("vcodec") == "none"),
                        "chosen": {k: info.get(k) for k in ("format_id", "ext", "acodec")}})
    except DownloadError as exc:
        text = str(exc)
        if any(s in text.lower() for s in ("unable to connect to proxy", "tunnel connection failed")) or \
                stream._resolve_error("yt", exc).headers.get(stream.ERROR_HEADER) == "network":
            pytest.fail(f"BLOCKED: нет доступа к YouTube, деградацию без runtime проверить нельзя ({text[:160]})",
                        pytrace=False)
        err = stream._runtime_error("yt", stream._resolve_error("yt", exc), None)
        outcome.update({"LIVE-4": "failed", "error_kind": err.headers.get(stream.ERROR_HEADER),
                        "user_message": err.detail, "yt_dlp": text[:300].replace(VIDEO, "<id>")})
        # Без runtime ошибка формата должна называть компонент, а не выдавать себя за «трек недоступен»
        assert outcome["error_kind"] in {"js_runtime", "bot_check", "rate_limited", "age_restricted", "unavailable"}, outcome
    print(json.dumps(outcome, ensure_ascii=False))
