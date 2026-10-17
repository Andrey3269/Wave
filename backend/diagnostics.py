"""
Диагностика воспроизведения YouTube (этап S3): GET /api/diagnostics и `Wave.exe --diagnostics`.

Обычная диагностика работает без сети и быстро: проверяет версии yt-dlp и yt-dlp-ejs, наличие скриптов
решателя JS-задач, найденный JS-runtime (запуск `deno --version`) и каталог его кэша. Она подтверждает
КОНФИГУРАЦИЮ, а не воспроизведение: настоящий трек проверяет только явный режим `--live <videoId>`.

В отчёте нет секретов: ни cookies, ни прокси, ни переменных окружения целиком, ни ссылок на поток.
Пути показываются относительно папки приложения (<app>), каталога данных (<data>) и домашнего каталога (~).
"""
from __future__ import annotations

import os
import platform
import re
import sys
import time
from datetime import datetime, timezone

import jsruntime
from version import __version__

SCHEMA = 1
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
NOT_VERIFIED_NOTE = ("Проверена конфигурация компонентов. Воспроизведение YouTube этой проверкой не проверялось: "
                     "для этого нужна сеть (режим --live <videoId>).")


def _issue(code: str, severity: str, message: str, hint: str | None = None) -> dict:
    return {"code": code, "severity": severity, "message": message, "hint": hint}


def _yt_dlp_info() -> dict:
    from yt_dlp.version import __version__

    return {"version": __version__}


def _solver_info() -> dict:
    """
    Скрипты решателя JS-задач. Рабочий набор для Deno без сети — только из пакета yt-dlp-ejs (core + lib).
    В yt-dlp 2026.8.19 встроен лишь core (yt.solver.lib.js в пакет не входит), а запасной lib для Deno
    (yt.solver.deno.lib.js) требует загрузки npm-пакетов, которую Wave не разрешает. Поэтому без yt-dlp-ejs
    нужной версии YouTube работает в урезанном режиме. Встроенный набор проверяется на случай будущих версий.
    """
    from yt_dlp.extractor.youtube.jsc._builtin import vendor

    builtin = all(vendor.load_script(name) for name in ("yt.solver.core.js", "yt.solver.lib.js"))
    info = {"builtin_version": vendor.VERSION, "builtin_scripts_readable": builtin,
            "ejs_installed": False, "ejs_version": None, "ejs_scripts_readable": False, "usable": builtin}
    try:
        import yt_dlp_ejs
        import yt_dlp_ejs.yt.solver as solver
    except ImportError:
        return info
    info["ejs_installed"] = True
    info["ejs_version"] = yt_dlp_ejs.version
    try:
        info["ejs_scripts_readable"] = bool(solver.core()) and bool(solver.lib())
    except (OSError, ModuleNotFoundError, ValueError):
        info["ejs_scripts_readable"] = False
    # yt-dlp принимает скрипты пакета, только если их версия совпадает с той, под которую он собран
    ejs_usable = info["ejs_scripts_readable"] and info["ejs_version"] == info["builtin_version"]
    info["usable"] = ejs_usable or builtin
    return info


def _runtime_info(st: jsruntime.RuntimeStatus) -> dict:
    return {
        "name": st.name,
        "ok": st.ok,
        "source": st.source,
        "path": jsruntime.display_path(st.path),
        "version": st.version,
        "min_version": ".".join(map(str, jsruntime.MIN_VERSION)),
        "expected_version": jsruntime.expected_version(),
        "problem": st.problem,
        "detail": st.detail,
        "configured_for_yt_dlp": st.ok,
        "candidates": [{"source": p.source, "path": jsruntime.display_path(p.path), "ok": p.ok,
                        "version": p.version, "problem": p.problem} for p in st.probes],
    }


def _deno_dir_info() -> dict:
    d = jsruntime.deno_dir()
    try:
        d.mkdir(parents=True, exist_ok=True)
        writable = os.access(d, os.W_OK)
    except OSError:
        writable = False
    return {"path": jsruntime.display_path(d), "writable": writable}


def collect(refresh: bool = True) -> dict:
    """Полный отчёт без сети. refresh=True: runtime проверяется заново, а не берётся из кэша."""
    t0 = time.monotonic()
    st = jsruntime.status(refresh=refresh)
    solver = _solver_info()
    runtime = _runtime_info(st)
    deno_dir = _deno_dir_info()
    issues: list[dict] = []

    if not st.ok:
        issues.append(_issue("js_runtime_unavailable", "critical", jsruntime.user_message(st),
                             f"{st.problem}: {st.detail}" if st.problem else None))
    else:
        expected = runtime["expected_version"]
        if st.source == "bundled" and expected and st.version != expected:
            issues.append(_issue("js_runtime_version_mismatch", "warning",
                                 f"Упакован Deno {st.version}, а для сборки закреплён {expected}.",
                                 "Пересоберите приложение: build_windows.bat загружает версию из packaging/runtime/deno.lock."))
        if jsruntime.is_frozen() and st.source != "bundled":
            issues.append(_issue("js_runtime_not_bundled", "warning",
                                 f"Используется Deno не из комплекта приложения (источник: {st.source}).",
                                 f"Переменная {jsruntime.ENV_PATH} переопределяет упакованный runtime."))

    severity = "warning" if solver["usable"] else "critical"
    reinstall = "Переустановите приложение." if jsruntime.is_frozen() else "pip install -r backend/requirements.txt -c constraints.txt"
    if not solver["ejs_installed"]:
        issues.append(_issue("ejs_missing", severity,
                             "Пакет yt-dlp-ejs (скрипты решателя JS-задач YouTube) не найден.", reinstall))
    elif solver["ejs_version"] != solver["builtin_version"]:
        issues.append(_issue("ejs_version_mismatch", severity,
                             f"yt-dlp-ejs {solver['ejs_version']} не совпадает с версией, под которую собран yt-dlp "
                             f"({solver['builtin_version']}): yt-dlp его не примет.",
                             "Обновляйте yt-dlp и yt-dlp-ejs вместе (backend/requirements.txt)."))
    elif not solver["ejs_scripts_readable"]:
        issues.append(_issue("ejs_scripts_unreadable", severity,
                             "Скрипты yt-dlp-ejs не читаются (возможно, не попали в сборку).", reinstall))
    if not deno_dir["writable"]:
        issues.append(_issue("deno_dir_not_writable", "warning", "Каталог кэша Deno недоступен для записи."))

    critical = any(i["severity"] == "critical" for i in issues)
    return {
        "schema": SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "duration_ms": round((time.monotonic() - t0) * 1000),
        "app": {"version": __version__, "frozen": jsruntime.is_frozen(), "python": platform.python_version(), "platform": sys.platform,
                "os": platform.platform(terse=True), "machine": platform.machine()},
        "youtube_playback": "degraded" if critical else "configured",
        "playback_verified": False,
        "note": NOT_VERIFIED_NOTE,
        "yt_dlp": _yt_dlp_info(),
        "solver": solver,
        "js_runtime": runtime,
        "deno_dir": deno_dir,
        "processes": {"timeout_seconds": jsruntime.PROCESS_TIMEOUT, "windows_job_object": jsruntime.IS_WIN},
        "settings": {"cookies_configured": bool(os.getenv("YTDLP_COOKIES")),
                     "proxy_configured": bool(os.getenv("YTDLP_PROXY"))},
        "issues": issues,
    }


# ---------------------------------------------------------------------------
# Явная проверка настоящего трека (только по флагу --live, нужна сеть)
# ---------------------------------------------------------------------------
_URL_RE = re.compile(r"https?://\S+")


def _sanitize(text: str, limit: int = 300) -> str:
    """Текст ошибки yt-dlp для отчёта: без ссылок (в них подписи и параметры потока)."""
    import applog

    return applog.redact(_URL_RE.sub("<url>", text))[:limit]


def _http_client():
    """Клиент для одного Range-запроса (тот же транспорт, что у плеера: IPv4, прокси из YTDLP_PROXY)."""
    import ssl

    import certifi
    import httpx

    from streaming import config

    ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    transport = httpx.HTTPTransport(verify=ssl_ctx, retries=1, proxy=config.YTDLP_PROXY or None,
                                    local_address=None if config.YTDLP_PROXY else "0.0.0.0")
    return httpx.Client(transport=transport, timeout=httpx.Timeout(15.0), follow_redirects=True)


def live_check(video_id: str) -> dict:
    """
    Резолв публичного трека тем же кодом, что у плеера (streaming.resolver._extract_sync), и один запрос
    `Range: bytes=0-1023` к полученной ссылке. Ссылка в отчёт не попадает.
    """
    report: dict = {"video_id": video_id, "ok": False, "resolve_seconds": None, "js_runtime_processes": 0,
                    "js_challenge_runs": 0, "range_request": None, "error_kind": None, "error": None}
    if not VIDEO_ID_RE.fullmatch(video_id or ""):
        report.update(error_kind="bad_id", error="Идентификатор видео YouTube — 11 символов [A-Za-z0-9_-].")
        return report

    from yt_dlp.utils import DownloadError

    from streaming import errors, resolver

    spawned, runs = jsruntime.GUARD.spawned, jsruntime.GUARD.runs
    t0 = time.monotonic()
    try:
        res = resolver._extract_sync("yt", video_id)
    except DownloadError as exc:
        err = errors._runtime_error("yt", errors._resolve_error("yt", exc), getattr(exc, "wave_js_issue", None))
        report.update(error_kind=err.headers.get(errors.ERROR_HEADER), error=f"{err.detail} ({_sanitize(str(exc))})")
        return report
    except Exception as exc:                                      # noqa: BLE001 — отчёт, а не падение
        report.update(error_kind="internal", error=_sanitize(f"{type(exc).__name__}: {exc}"))
        return report
    finally:
        report["resolve_seconds"] = round(time.monotonic() - t0, 2)
        report["js_runtime_processes"] = jsruntime.GUARD.spawned - spawned
        report["js_challenge_runs"] = jsruntime.GUARD.runs - runs      # >0: yt-dlp действительно решал JS-задачи

    try:
        with _http_client() as client:
            r = client.get(res.url, headers={**res.headers, "Accept-Encoding": "identity", "Range": "bytes=0-1023"})
            report["range_request"] = {"status": r.status_code, "content_range": bool(r.headers.get("content-range")),
                                       "bytes": len(r.content)}
    except Exception as exc:                                      # noqa: BLE001
        report.update(error_kind="network", error=_sanitize(f"{type(exc).__name__}: {exc}"))
        return report
    report["ok"] = report["range_request"]["status"] in (200, 206)
    if not report["ok"]:
        report["error_kind"] = "upstream_status"
    return report


def exit_code(report: dict) -> int:
    """0 — конфигурация в порядке (и --live прошёл, если запрошен); 1 — критическая проблема."""
    critical = any(i["severity"] == "critical" for i in report.get("issues", []))
    live = report.get("live")
    return 1 if critical or (live is not None and not live.get("ok")) else 0


def format_text(report: dict) -> str:
    """Отчёт для человека (вывод `Wave.exe --diagnostics`)."""
    rt, solver, app = report["js_runtime"], report["solver"], report["app"]
    lines = ["Wave — диагностика воспроизведения YouTube",
             f"Версия приложения: {app['version']}",
             f"Сборка: {'собранное приложение' if app['frozen'] else 'запуск из исходников'}; "
             f"Python {app['python']}; {app['os']} ({app['machine']})",
             f"yt-dlp: {report['yt_dlp']['version']}"]
    if solver["ejs_installed"]:
        lines.append(f"yt-dlp-ejs: {solver['ejs_version']} (yt-dlp ожидает {solver['builtin_version']}); "
                     f"скрипты {'читаются' if solver['ejs_scripts_readable'] else 'НЕ читаются'}")
    else:
        lines.append(f"yt-dlp-ejs: не установлен; встроенные скрипты yt-dlp {solver['builtin_version']} "
                     f"{'читаются' if solver['builtin_scripts_readable'] else 'НЕ читаются'}")
    if rt["ok"]:
        lines.append(f"JS-runtime: deno {rt['version']} supported=true (минимум {rt['min_version']}, "
                     f"закреплено {rt['expected_version'] or '—'})")
    else:
        lines.append(f"JS-runtime: НЕ ГОТОВ ({rt['problem']}: {rt['detail']}); минимум deno {rt['min_version']}")
    lines.append(f"  источник: {rt['source'] or '—'}; путь: {rt['path'] or '—'}")
    for c in rt["candidates"]:
        lines.append(f"  проверено: {c['source']} {c['path']} -> "
                     f"{'ok ' + (c['version'] or '') if c['ok'] else c['problem']}")
    lines.append(f"Кэш Deno: {report['deno_dir']['path']} "
                 f"({'запись разрешена' if report['deno_dir']['writable'] else 'запись НЕ разрешена'})")
    live = report.get("live")
    if live is not None:
        if live["ok"]:
            rr = live["range_request"]
            lines.append(f"Проверка трека {live['video_id']}: OK — резолв {live['resolve_seconds']} с, "
                         f"решений JS-задач в Deno {live['js_challenge_runs']} (процессов {live['js_runtime_processes']}), "
                         f"Range-запрос HTTP {rr['status']}")
        else:
            lines.append(f"Проверка трека {live['video_id']}: ОШИБКА [{live['error_kind']}] {live['error']}")
    if report["issues"]:
        lines.append("Проблемы:")
        for i in report["issues"]:
            mark = "критично" if i["severity"] == "critical" else "внимание"
            lines.append(f"  [{mark}] {i['message']}" + (f" ({i['hint']})" if i["hint"] else ""))
    verdict = "конфигурация в порядке" if report["youtube_playback"] == "configured" else "YouTube будет работать с ошибками"
    lines.append(f"Итог: {verdict}.")
    if live is None:
        lines.append(report["note"])
    return "\n".join(lines)
