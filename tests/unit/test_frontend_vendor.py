"""
Сторонний JavaScript интерфейса (S8): hls.js поставляется с приложением, загружается с локального диска,
закреплён по версии и SHA-256, лицензия и уведомление на месте, CDN не используется.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FRONT = ROOT / "frontend"
LOCK = json.loads((FRONT / "vendor" / "vendor.lock.json").read_text(encoding="utf-8"))
HLS = LOCK["hls.js"]


def test_bundled_file_matches_lock():
    data = (FRONT / "vendor" / HLS["file"]).read_bytes()
    assert hashlib.sha256(data).hexdigest() == HLS["sha256"]
    assert f'"{HLS["version"]}"' in data.decode("utf-8")                # версия внутри самого файла
    assert b"\r\n" not in data                                            # git не менял окончания строк


def test_light_build_without_eme_is_used():
    assert HLS["file"] == "hls.light.min.js" and "EME" in HLS["why_light"]


def test_license_and_notice():
    lic = (ROOT / HLS["license_file"]).read_text(encoding="utf-8")
    assert "Apache License, Version 2.0" in lic and "Dailymotion" in lic and "Brightcove" in lic
    notices = (ROOT / "licenses" / "THIRD_PARTY_NOTICES.txt").read_text(encoding="utf-8")
    assert f"hls.js {HLS['version']}" in notices and HLS["sha256"] in notices and HLS["npm_integrity"] in notices
    assert "licenses\\hls.js\\LICENSE" in notices


def _frontend_js() -> str:
    """Весь JavaScript интерфейса (S9: ES-модули frontend/js/**; vendor — сторонние библиотеки, не наш код)."""
    files = sorted(p for p in FRONT.rglob("*.js") if "vendor" not in p.relative_to(FRONT).parts)
    assert files, "нет JavaScript интерфейса"
    return "\n".join(p.read_text(encoding="utf-8") for p in files)


def test_frontend_loads_hls_locally_and_uses_no_cdn():
    app = _frontend_js()
    index = (FRONT / "index.html").read_text(encoding="utf-8")
    assert f's.src = "vendor/{HLS["file"]}"' in app                       # относительный путь: тот же backend
    cdn = re.compile(r"https?://[^\"'\s]*(cdn|unpkg|jsdelivr|cdnjs|googleapis|cloudflare)", re.I)
    assert not cdn.search(app) and not cdn.search(index)
    assert not re.search(r"<script[^>]+src=[\"']https?:", index, re.I)    # внешних скриптов нет вовсе
    assert "import(" not in app                                           # и динамической загрузки модулей тоже


def test_packaging_bundles_whole_frontend_folder():
    spec = (ROOT / "packaging" / "wave.spec").read_text(encoding="utf-8")
    assert 'datas = [(os.path.join(ROOT, "frontend"), "frontend")]' in spec
    assert '(os.path.join(ROOT, "licenses"), "licenses")' in spec
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "vendor.lock.json" in ci and "licenses\\hls.js\\LICENSE" in ci  # сборка проверяет файл и его SHA-256


def test_hls_file_is_served_by_backend(api):
    r = api.get(f"/vendor/{HLS['file']}")
    assert r.status_code == 200 and r.headers["content-type"].startswith(("application/javascript", "text/javascript"))
    assert hashlib.sha256(r.content).hexdigest() == HLS["sha256"]
