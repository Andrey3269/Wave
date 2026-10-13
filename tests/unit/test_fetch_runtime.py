"""
packaging/fetch_runtime.py и packaging/runtime/deno.lock (S3): воспроизводимое получение Deno для сборки.

Сеть не используется: загрузка подменяется функцией, которая «скачивает» поддельный zip из временной папки.
Настоящая загрузка со сверкой SHA-256 выполняется отдельным шагом CI и в build_windows.bat.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "packaging"))
import fetch_runtime as fr  # noqa: E402

EXE = b"MZ fake deno executable " * 64


def _zip(member: str = "deno.exe", data: bytes = EXE) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(member, data)
    return buf.getvalue()


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _lock(blob: bytes, *, member="deno.exe", data=EXE, **override) -> dict:
    entry = {"target": "x86_64-pc-windows-msvc", "archive": "zip",
             "url": "https://github.com/denoland/deno/releases/download/v2.9.7/deno-x86_64-pc-windows-msvc.zip",
             "size": len(blob), "sha256": _sha(blob), "member": member, "member_size": len(data),
             "member_sha256": _sha(data)}
    entry.update(override)
    return {"schema": 1, "name": "deno", "version": "2.9.7", "platforms": {"windows-x86_64": entry}}


class Downloader:
    """Подмена загрузки: пишет заданные байты (или падает) и считает вызовы."""

    def __init__(self, payload: bytes | None = None, fail_after: int | None = None):
        self.payload, self.fail_after, self.calls = payload, fail_after, 0

    def __call__(self, url: str, target: Path) -> None:
        self.calls += 1
        if self.fail_after is not None:
            target.write_bytes((self.payload or b"")[: self.fail_after])     # частично скачанный файл
            raise OSError("соединение оборвано (тест)")
        target.write_bytes(self.payload)


@pytest.fixture
def dirs(tmp_path):
    return tmp_path / "Wave Сборка" / "deno", tmp_path / "Wave Сборка" / "downloads"


def _leftovers(*folders: Path) -> list[str]:
    return sorted(p.name for f in folders if f.exists() for p in f.iterdir() if p.name.endswith(".part")
                  or p.name.startswith(".extract-"))


# ---------------------------------------------------------------------------
# Настоящий lock-файл
# ---------------------------------------------------------------------------
def test_real_lock_is_complete_and_official():
    lock = fr.load_lock()
    assert re.fullmatch(r"\d+\.\d+\.\d+", lock["version"])
    for name, member in (("windows-x86_64", "deno.exe"), ("linux-x86_64", "deno")):
        entry = fr.platform_entry(lock, name)                    # проверяет URL, хэши, размеры
        assert entry["member"] == member
        assert entry["url"].startswith(f"https://github.com/denoland/deno/releases/download/v{lock['version']}/")
        assert entry["size"] > 10_000_000 and entry["member_size"] > entry["size"]


def test_real_lock_version_is_supported_and_used_by_diagnostics(backend):
    js = backend["jsruntime"]
    lock = fr.load_lock()
    assert tuple(int(x) for x in lock["version"].split(".")) >= js.MIN_VERSION
    assert js.expected_version() == lock["version"]


def test_notices_match_pinned_versions():
    """THIRD_PARTY_NOTICES называет те же версии, что закреплены (не устаревает при обновлении)."""
    from importlib import metadata

    text = (ROOT / "licenses" / "THIRD_PARTY_NOTICES.txt").read_text(encoding="utf-8")
    lock = fr.load_lock()
    assert f"Deno {lock['version']}" in text and lock["platforms"]["windows-x86_64"]["sha256"] in text
    assert f"yt-dlp {metadata.version('yt-dlp')}" in text and f"yt-dlp-ejs {metadata.version('yt-dlp-ejs')}" in text
    for f in ("deno/LICENSE.md", "v8/LICENSE", "yt-dlp/LICENSE", "yt-dlp-ejs/LICENSE", "yt-dlp-ejs/BUNDLED_DEPENDENCIES.txt"):
        assert (ROOT / "licenses" / f).stat().st_size > 500, f
    assert "MIT License" in (ROOT / "licenses" / "deno" / "LICENSE.md").read_text(encoding="utf-8")


def test_spec_bundles_runtime_lock_and_licenses():
    spec = (ROOT / "packaging" / "wave.spec").read_text(encoding="utf-8")
    assert "fetch_runtime.is_ready" in spec and '"windows-x86_64"' in spec
    assert 'os.path.join("runtime", "deno")' in spec and "str(fetch_runtime.LOCK)" in spec
    assert '(os.path.join(ROOT, "licenses"), "licenses")' in spec
    assert re.search(r"REQUIRED = \{[^}]*\"yt_dlp_ejs\"", spec)


def test_gitignore_keeps_binaries_out_of_git():
    text = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "packaging/runtime/deno/" in text and "packaging/runtime/downloads/" in text


# ---------------------------------------------------------------------------
# Загрузка и проверка
# ---------------------------------------------------------------------------
def test_fetch_verifies_and_extracts(dirs):
    dest, cache = dirs
    archive = _zip()
    dl = Downloader(archive)
    exe = fr.fetch(_lock(archive), "windows-x86_64", dest, cache, download=dl, log=lambda m: None)
    assert exe == dest / "deno.exe" and exe.read_bytes() == EXE
    assert json.loads((dest / fr.MARKER).read_text(encoding="utf-8"))["member_sha256"] == _sha(EXE)
    assert (cache / "deno-x86_64-pc-windows-msvc.zip").read_bytes() == archive
    assert dl.calls == 1 and _leftovers(dest, cache) == []


def test_second_run_reuses_verified_runtime(dirs):
    dest, cache = dirs
    archive = _zip()
    lock = _lock(archive)
    fr.fetch(lock, "windows-x86_64", dest, cache, download=Downloader(archive), log=lambda m: None)
    again = Downloader(archive)
    fr.fetch(lock, "windows-x86_64", dest, cache, download=again, log=lambda m: None)
    assert again.calls == 0 and fr.is_ready(lock, "windows-x86_64", dest)


def test_tampered_runtime_is_restored_from_verified_archive(dirs):
    dest, cache = dirs
    archive = _zip()
    lock = _lock(archive)
    fr.fetch(lock, "windows-x86_64", dest, cache, download=Downloader(archive), log=lambda m: None)
    (dest / "deno.exe").write_bytes(b"tampered")
    assert not fr.is_ready(lock, "windows-x86_64", dest)
    again = Downloader(archive)
    fr.fetch(lock, "windows-x86_64", dest, cache, download=again, log=lambda m: None)
    assert again.calls == 0 and (dest / "deno.exe").read_bytes() == EXE


def test_archive_hash_mismatch_stops_and_leaves_nothing(dirs):
    dest, cache = dirs
    archive = _zip()
    lock = _lock(archive, sha256=_sha(b"something else"))
    with pytest.raises(fr.FetchError) as e:
        fr.fetch(lock, "windows-x86_64", dest, cache, download=Downloader(archive), log=lambda m: None)
    assert e.value.code == 3 and "SHA-256 не совпадает" in str(e.value)
    assert not (dest / "deno.exe").exists() and not (dest / fr.MARKER).exists()
    assert not list(cache.glob("*.zip")) and _leftovers(dest, cache) == []


def test_download_error_stops_and_leaves_no_partial_file(dirs):
    dest, cache = dirs
    archive = _zip()
    with pytest.raises(fr.FetchError) as e:
        fr.fetch(_lock(archive), "windows-x86_64", dest, cache, download=Downloader(archive, fail_after=100),
                 log=lambda m: None)
    assert e.value.code == 2
    assert not (dest / "deno.exe").exists() and not list(cache.glob("*.zip")) and _leftovers(dest, cache) == []


def test_executable_hash_mismatch_is_rejected(dirs):
    dest, cache = dirs
    archive = _zip()
    with pytest.raises(fr.FetchError) as e:
        fr.fetch(_lock(archive, member_sha256=_sha(b"other exe")), "windows-x86_64", dest, cache,
                 download=Downloader(archive), log=lambda m: None)
    assert e.value.code == 3 and not (dest / "deno.exe").exists() and _leftovers(dest, cache) == []


def test_member_size_mismatch_is_rejected(dirs):
    dest, cache = dirs
    archive = _zip()
    with pytest.raises(fr.FetchError) as e:
        fr.fetch(_lock(archive, member_size=len(EXE) + 1), "windows-x86_64", dest, cache,
                 download=Downloader(archive), log=lambda m: None)
    assert e.value.code == 5 and not (dest / "deno.exe").exists()


def test_corrupted_archive_is_rejected(dirs):
    dest, cache = dirs
    garbage = b"not a zip archive" * 10
    with pytest.raises(fr.FetchError) as e:
        fr.fetch(_lock(garbage), "windows-x86_64", dest, cache, download=Downloader(garbage), log=lambda m: None)
    assert e.value.code == 5 and not (dest / "deno.exe").exists() and _leftovers(dest, cache) == []


def test_corrupted_cached_archive_is_downloaded_again(dirs):
    dest, cache = dirs
    archive = _zip()
    cache.mkdir(parents=True)
    (cache / "deno-x86_64-pc-windows-msvc.zip").write_bytes(b"broken cache")
    dl = Downloader(archive)
    fr.fetch(_lock(archive), "windows-x86_64", dest, cache, download=dl, log=lambda m: None)
    assert dl.calls == 1 and (dest / "deno.exe").read_bytes() == EXE


@pytest.mark.parametrize("override, field", [
    ({"sha256": ""}, "sha256"),
    ({"sha256": "A0C3101B4158D1DFB7D6A78A7BF0F3DE80C96BB423C152BEEC8BEB22786F2238"}, "sha256"),   # не нижний регистр
    ({"member_sha256": None}, "member_sha256"),
    ({"url": "http://github.com/denoland/deno/releases/download/v2.9.7/x.zip"}, "url"),
    ({"url": "https://evil.test/denoland/deno/releases/download/v2.9.7/x.zip"}, "url"),
    ({"url": "https://github.com/denoland/deno/releases/download/v2.9.6/x.zip"}, "url"),   # другая версия
    ({"member": "../deno.exe"}, "member"),
    ({"member": "bin/deno.exe"}, "member"),
    ({"archive": "tar.gz"}, "archive"),
    ({"size": 0}, "size"),
])
def test_incomplete_or_unsafe_lock_is_refused_before_download(dirs, override, field):
    dest, cache = dirs
    archive = _zip()
    dl = Downloader(archive)
    with pytest.raises(fr.FetchError) as e:
        fr.fetch(_lock(archive, **override), "windows-x86_64", dest, cache, download=dl, log=lambda m: None)
    assert e.value.code == 4 and field in str(e.value) and dl.calls == 0


def test_unknown_platform_is_refused(dirs):
    with pytest.raises(fr.FetchError) as e:
        fr.platform_entry(_lock(_zip()), "macos-arm64")
    assert e.value.code == 4


def test_check_mode(dirs, tmp_path, capsys):
    dest, cache = dirs
    archive = _zip()
    lock_file = tmp_path / "deno.lock"
    lock_file.write_text(json.dumps(_lock(archive)), encoding="utf-8")
    assert fr.main(["--lock", str(lock_file), "--dest", str(dest), "--check"]) == 6
    fr.fetch(json.loads(lock_file.read_text(encoding="utf-8")), "windows-x86_64", dest, cache,
             download=Downloader(archive), log=lambda m: None)
    assert fr.main(["--lock", str(lock_file), "--dest", str(dest), "--check"]) == 0
    lock_file.write_text("{broken", encoding="utf-8")
    assert fr.main(["--lock", str(lock_file), "--dest", str(dest), "--check"]) == 4
