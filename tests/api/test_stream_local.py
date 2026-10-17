"""
Воспроизведение скачанного трека тем же /api/stream/{source}/{id} (FileResponse с диска).
Файлы лежат во временном каталоге загрузок теста.
"""
from __future__ import annotations

import pytest

import library
import stream
from wavetest.fakes import audio_bytes

YT = "AAAAAAAAAA1"
PATH = f"/api/stream/yt/{YT}"
SIZE = 5000
DATA = audio_bytes(SIZE, seed=3)


def add_download(profile, source="yt", track_id=YT, ext=".mp3", data=DATA, folder=None, cover=None, lyrics=None):
    folder = folder or library.safe_folder("Артист", "Песня", source, track_id)
    d = profile.downloads / folder
    d.mkdir(parents=True, exist_ok=True)
    (d / f"audio{ext}").write_bytes(data)
    library.add(source=source, track_id=track_id, title="Песня", artist="Артист", album=None, duration=60,
                folder=folder, audio=f"audio{ext}", cover=cover, lyrics=lyrics, size=len(data), created_at=1.0,
                root=str(profile.downloads))
    return d


@pytest.fixture
def local(api):
    add_download(api.profile)
    return api


def _no_network(api):
    assert api.resolver.total() == 0, "скачанный трек не должен резолвиться"
    assert not api.net.requests, "скачанный трек не должен обращаться к сети"


def test_no_range_returns_whole_file(local):
    r = local.get(PATH)
    assert r.status_code == 200
    assert r.content == DATA
    assert r.headers["content-type"] == "audio/mpeg"
    assert r.headers["accept-ranges"] == "bytes"
    _no_network(local)


@pytest.mark.parametrize("raw, content_range, lo, hi", [
    ("bytes=0-", "bytes 0-4999/5000", 0, 5000),
    ("bytes=100-199", "bytes 100-199/5000", 100, 200),
    ("bytes=4000-", "bytes 4000-4999/5000", 4000, 5000),        # перемотка: до конца файла
    ("bytes=-500", "bytes 4500-4999/5000", 4500, 5000),          # суффикс
    ("bytes=100-999999", "bytes 100-4999/5000", 100, 5000),      # конец за пределами файла обрезается
])
def test_ranges(local, raw, content_range, lo, hi):
    r = local.get(PATH, headers={"Range": raw})
    assert r.status_code == 206
    assert r.headers["content-range"] == content_range
    assert r.headers["content-length"] == str(hi - lo)
    assert r.content == DATA[lo:hi]
    _no_network(local)


@pytest.mark.parametrize("raw", ["bytes=5000-", "bytes=99999-100000"])
def test_unsatisfiable_range(local, raw):
    r = local.get(PATH, headers={"Range": raw})
    assert r.status_code == 416
    assert r.headers["content-range"] == "bytes */5000"
    _no_network(local)


@pytest.mark.parametrize("raw", ["bytes=abc", "bytes=-", "bytes=500-100", "items=0-10"])
def test_malformed_range_never_serves_wrong_bytes(local, raw):
    r = local.get(PATH, headers={"Range": raw})
    assert r.status_code < 500
    assert r.status_code in (200, 400)
    if r.status_code == 200:
        assert r.content == DATA


@pytest.mark.parametrize("raw", ["bytes=abc", "bytes=-", "bytes=500-100", "items=0-10", "bytes=0-9,abc"])
def test_malformed_range_is_ignored(local, raw):
    """L2 (закрыто в S8): битый Range на локальном файле игнорируется по RFC 9110 — 200 и файл целиком."""
    r = local.get(PATH, headers={"Range": raw})
    assert r.status_code == 200 and r.content == DATA
    assert "content-range" not in r.headers


def test_multiple_ranges_multipart(local):
    r = local.get(PATH, headers={"Range": "bytes=0-9,20-29"})
    assert r.status_code == 206
    assert r.headers["content-type"].startswith("multipart/byteranges")


@pytest.mark.parametrize("ext, mime", [(".m4a", "audio/mp4"), (".mp3", "audio/mpeg"), (".webm", "audio/webm"),
                                       (".ogg", "audio/ogg"), (".flac", "audio/flac"),   # S15: flac, wav — локальная музыка
                                       (".wav", "audio/wav"), (".xyz", "audio/mp4")])
def test_mime_by_extension(api, ext, mime):
    add_download(api.profile, ext=ext)
    r = api.get(PATH, headers={"Range": "bytes=0-1"})
    assert r.status_code == 206
    assert r.headers["content-type"] == mime


def test_soundcloud_download_plays_from_disk(api):
    add_download(api.profile, source="sc", track_id="1000000001")
    r = api.get("/api/stream/sc/1000000001", headers={"Range": "bytes=0-"})
    assert r.status_code == 206 and r.content == DATA
    _no_network(api)


def test_warm_for_local_track_is_noop(local):
    assert local.get(PATH, params={"warm": 1}).status_code == 204
    _no_network(local)


def test_deleted_file_falls_back_to_source(api):
    """Файл удалили вручную: запись исчезает из индекса, трек играет из источника, ошибки 500 нет."""
    d = add_download(api.profile)
    (d / "audio.mp3").unlink()
    api.net.add_file(f"/yt/{YT}", DATA)
    r = api.get(PATH, headers={"Range": "bytes=0-"})
    assert r.status_code == 206
    assert api.resolver.total() == 1
    assert api.profile.sql("SELECT COUNT(*) FROM downloads")[0][0] == 0


def test_index_row_outside_downloads_is_not_served(api):
    """Запись индекса с выходом за каталог загрузок (../) не должна отдавать чужой файл."""
    outside = api.profile.root / "outside"
    outside.mkdir()
    (outside / "audio.mp3").write_bytes(b"SECRET" * 10)
    library.add(source="yt", track_id=YT, title="t", artist="a", album=None, duration=1, folder="../outside",
                audio="audio.mp3", cover=None, lyrics=None, size=60, created_at=1.0, root=str(api.profile.downloads))
    r = api.get(PATH)
    assert r.status_code >= 400
    assert b"SECRET" not in r.content


@pytest.mark.parametrize("folder", ["../x", "..", "", ".", "/etc", "a/../../x"])
def test_folder_path_rejects_escape(wave_profile, folder):
    with pytest.raises(ValueError):
        library.folder_path(folder)


def test_folder_path_accepts_nested_name(wave_profile):
    assert library.folder_path("Артист - Песня [yt-x]") == wave_profile.downloads / "Артист - Песня [yt-x]"


@pytest.mark.parametrize("artist, title, track_id, expected", [
    ("AC/DC", 'Back: In "Black"?', "AAAAAAAAAA1", "ACDC - Back In Black [yt-AAAAAAAAAA1]"),
    ("  ..dots..  ", "...", "AAAAAAAAAA1", "dots - track [yt-AAAAAAAAAA1]"),
    ("Кириллица", "Песня", "AAAAAAAAAA1", "Кириллица - Песня [yt-AAAAAAAAAA1]"),
    ("", None, "AAAAAAAAAA1", "track - track [yt-AAAAAAAAAA1]"),
    ("A" * 100, "B" * 100, "AAAAAAAAAA1", f"{'A' * 40} - {'B' * 60} [yt-AAAAAAAAAA1]"),
    ("x<>|*\x01", "y", "AAAAAAAAAA1", "x - y [yt-AAAAAAAAAA1]"),
])
def test_safe_folder_names_are_windows_safe(artist, title, track_id, expected):
    name = library.safe_folder(artist, title, "yt", track_id)
    assert name == expected
    assert not set('<>:"/\\|?*') & set(name)
    assert not name.endswith((" ", "."))


def test_head_request_supported(local):
    """L2 (закрыто в S8): HEAD на скачанный трек — 200, заголовки файла, без тела и без сети."""
    r = local.http.head(PATH)
    assert r.status_code == 200
    assert r.headers.get("accept-ranges") == "bytes"
    assert r.headers["content-length"] == str(SIZE) and r.headers["content-type"] == "audio/mpeg"
    assert r.content == b""
    _no_network(local)


def test_head_on_local_file_ignores_range(local):
    r = local.http.head(PATH, headers={"Range": "bytes=100-199"})
    assert r.status_code == 200 and "content-range" not in r.headers and r.headers["content-length"] == str(SIZE)


def test_valid_range_still_partial_after_l2_fix(local):
    """Контроль: исправление L2 не трогает корректные диапазоны (206) и неудовлетворимые (416)."""
    assert local.get(PATH, headers={"Range": "bytes=10-19"}).status_code == 206
    assert local.get(PATH, headers={"Range": "bytes=9999-"}).status_code == 416


def test_stream_contract_constants():
    """Значения, на которые опирается поведение плеера (меняются только осознанно, вместе с тестами)."""
    assert stream.STREAM_CHUNK_BYTES == 1024 * 1024
    assert stream.STALE_STATUSES == {401, 403, 404, 410}
    assert stream.MAX_PARALLEL_RESOLVES == 4
