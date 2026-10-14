"""
HLS через backend (S8): /api/stream/{source}/{id}/playback, HLS-прокси /api/stream/hls/{token}/..., защита от
SSRF, сеансы с ограниченным сроком жизни, протухшие ссылки, обрыв сегмента (N2) и запрет отдавать m3u8 через
progressive-адрес. CDN — wavetest.fakes.FakeNet (https://cdn.test), резолвер — FakeResolver с hls=...;
публичного интернета нет, DNS подменён (harness.dns).
"""
from __future__ import annotations

import asyncio
import logging
import re

import httpx
import pytest

import library
import stream
from streaming import hls_proxy, netguard, resolver
from wavetest.hlsfix import fmp4_init, fmp4_segment, master_playlist, media_playlist, mp3_frames

SC = "1000000001"
KEY = f"sc:{SC}"
ROOT = f"/sc/{SC}/main.m3u8"
SEGS = [mp3_frames(30 + i) for i in range(3)]          # сегменты разной длины: подмену легко заметить
SIG = "Policy=SECRETPOLICY&Signature=SECRETSIG&Key-Pair-Id=SECRETKID"


def mp3_hls(api, segs=SEGS, sig=SIG):
    for i, data in enumerate(segs):
        api.net.add_file(f"/sc/{SC}/seg{i}.mp3", data, "audio/mpeg")
    text = media_playlist([f"https://cdn.test/sc/{SC}/seg{i}.mp3?{sig}" for i in range(len(segs))],
                          [round(len(d) / 417 * 0.026122, 3) for d in segs])
    api.net.add_file(ROOT, text.encode(), "application/vnd.apple.mpegurl")
    api.resolver.hls[KEY] = "mp3"
    return text


def play(api):
    r = api.get(f"/api/stream/sc/{SC}/playback")
    assert r.status_code == 200, r.text
    return r.json()


def playlist_lines(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if ln and not ln.startswith("#")]


# ---------------------------------------------------------------------------
# /playback
# ---------------------------------------------------------------------------
def test_playback_progressive_track(api):
    body = api.get("/api/stream/yt/AAAAAAAAAA1/playback").json()
    assert body == {"kind": "progressive", "url": "/api/stream/yt/AAAAAAAAAA1", "preview": False}
    body = api.get(f"/api/stream/sc/{SC}/playback").json()
    assert body["kind"] == "progressive" and body["url"] == f"/api/stream/sc/{SC}"
    assert not api.net.requests                                         # для progressive в сеть не ходили


def add_download(profile, source="sc", track_id=SC, data=b"ID3local-file"):
    folder = library.safe_folder("Артист", "Песня", source, track_id)
    d = profile.downloads / folder
    d.mkdir(parents=True, exist_ok=True)
    (d / "audio.mp3").write_bytes(data)
    library.add(source=source, track_id=track_id, title="Песня", artist="Артист", album=None, duration=60,
                folder=folder, audio="audio.mp3", cover=None, lyrics=None, size=len(data), created_at=1.0,
                root=str(profile.downloads))


def test_playback_local_track_needs_no_network(api):
    add_download(api.profile)
    assert api.get(f"/api/stream/sc/{SC}/playback").json() == {"kind": "local", "url": f"/api/stream/sc/{SC}",
                                                                "preview": False}
    assert api.resolver.total() == 0 and not api.net.requests


@pytest.mark.parametrize("path, status", [("/api/stream/spotify/1/playback", 404), ("/api/stream/sc/12ab/playback", 400)])
def test_playback_validation(api, path, status):
    assert api.get(path).status_code == status and api.resolver.total() == 0


def test_playback_resolver_error_is_passed_through(api):
    from yt_dlp.utils import DownloadError
    api.resolver.fail("sc", SC, DownloadError("ERROR: Unable to download JSON metadata: <urlopen error timed out>"))
    r = api.get(f"/api/stream/sc/{SC}/playback")
    assert r.status_code == 503 and r.headers[stream.ERROR_HEADER] == "network"
    assert r.json()["detail"] == "Нет связи с SoundCloud. Проверьте подключение к интернету."


# ---------------------------------------------------------------------------
# Полный путь mp3-HLS
# ---------------------------------------------------------------------------
def test_mp3_hls_end_to_end(api):
    mp3_hls(api)
    body = play(api)
    assert body["kind"] == "hls" and body["codec"] == "mp3" and body["preview"] is False
    m = re.fullmatch(r"/api/stream/hls/([A-Za-z0-9_-]{16,64})/main\.m3u8", body["url"])
    assert m
    token = m.group(1)

    r = api.get(body["url"])
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/vnd.apple.mpegurl")
    assert r.headers["cache-control"] == "no-store"
    assert playlist_lines(r.text) == [f"/api/stream/hls/{token}/main/s{i}" for i in range(3)]
    assert "cdn.test" not in r.text and "SECRET" not in r.text            # адреса и подписи источника не видны
    assert "#EXT-X-ENDLIST" in r.text

    for i, data in enumerate(SEGS):
        seg = api.get(f"/api/stream/hls/{token}/main/s{i}")
        assert seg.status_code == 200 and seg.content == data
        assert seg.headers["content-type"] == "audio/mpeg" and seg.headers["content-length"] == str(len(data))
    up = [q for q in api.net.cdn_requests() if q.url.path.endswith(".mp3")]
    assert [q.url.params["Signature"] for q in up] == ["SECRETSIG"] * 3    # подпись сегмента дошла до CDN как есть
    assert "range" not in up[0].headers and up[0].headers["user-agent"] == "test-agent"
    assert len(api.net.cdn_requests(ROOT)) == 1          # /playback проверил плейлист — плеер получил его без повтора
    assert api.resolver.calls[KEY] == 1
    assert all(s.closed for s in api.net.streams)


def test_playback_replaces_session_for_same_track(api):
    """Повторный /playback того же трека: новый токен, старый сразу недействителен — на трек один сеанс, и
    запоздавший DELETE старого токена (плеер освобождает его при перезапуске) не задевает новый."""
    mp3_hls(api)
    first, second = play(api)["url"], play(api)["url"]
    assert first != second and hls_proxy.stats()["sessions"] == 1
    assert api.get(first).status_code == 404
    api.delete(first.rsplit("/", 1)[0])
    assert api.get(second).status_code == 200


# ---------------------------------------------------------------------------
# fMP4 (AAC-HLS SoundCloud), master playlist, BYTERANGE
# ---------------------------------------------------------------------------
def test_fmp4_hls_with_init_section(api):
    init, segs = fmp4_init(), [fmp4_segment(i) for i in range(2)]
    api.net.add_file(f"/sc/{SC}/init.mp4", init, "audio/mp4")
    for i, d in enumerate(segs):
        api.net.add_file(f"/sc/{SC}/data{i:03d}.m4s", d, "audio/mp4")
    api.net.add_file(ROOT, media_playlist([f"data{i:03d}.m4s?{SIG}" for i in range(2)], map_uri=f"init.mp4?{SIG}",
                                          version=7).encode(), "application/vnd.apple.mpegurl")
    api.resolver.hls[KEY] = "aac"
    body = play(api)
    token = body["url"].split("/")[4]
    text = api.get(body["url"]).text
    assert f'#EXT-X-MAP:URI="/api/stream/hls/{token}/main/m0"' in text
    assert api.get(f"/api/stream/hls/{token}/main/m0").content == init
    seg = api.get(f"/api/stream/hls/{token}/main/s1")
    assert seg.content == segs[1] and seg.headers["content-type"] == "audio/mp4"


def test_master_playlist_variants(api):
    api.net.add_file(ROOT, master_playlist([("v/0.m3u8", 'BANDWIDTH=160000,CODECS="mp4a.40.2"'),
                                            ("v/1.m3u8", 'BANDWIDTH=96000,CODECS="mp4a.40.2"')]).encode(),
                     "application/vnd.apple.mpegurl")
    api.net.add_file(f"/sc/{SC}/v/0.m3u8", media_playlist(["a.mp3", "b.mp3"]).encode(), "application/vnd.apple.mpegurl")
    api.net.add_file(f"/sc/{SC}/v/a.mp3", SEGS[0], "audio/mpeg")
    api.net.add_file(f"/sc/{SC}/v/b.mp3", SEGS[1], "audio/mpeg")
    api.resolver.hls[KEY] = "mp3"
    body = play(api)
    token = body["url"].split("/")[4]
    master = api.get(body["url"]).text
    assert playlist_lines(master) == [f"/api/stream/hls/{token}/v0.m3u8", f"/api/stream/hls/{token}/v1.m3u8"]
    variant = api.get(f"/api/stream/hls/{token}/v0.m3u8").text
    assert playlist_lines(variant) == [f"/api/stream/hls/{token}/v0/s0", f"/api/stream/hls/{token}/v0/s1"]
    assert api.get(f"/api/stream/hls/{token}/v0/s1").content == SEGS[1]
    assert api.get(f"/api/stream/hls/{token}/v9.m3u8").status_code == 404    # варианта нет


def test_byterange_segments_are_fetched_with_range(api):
    whole = b"".join(SEGS)
    api.net.add_file(f"/sc/{SC}/all.mp3", whole, "audio/mpeg")
    a, b = len(SEGS[0]), len(SEGS[1])
    api.net.add_file(ROOT, media_playlist(["all.mp3", "all.mp3", "all.mp3"],
                                          byteranges=[f"{a}@0", str(b), f"{len(SEGS[2])}@{a + b}"]).encode(),
                     "application/vnd.apple.mpegurl")
    api.resolver.hls[KEY] = "mp3"
    token = play(api)["url"].split("/")[4]
    assert "BYTERANGE" not in api.get(f"/api/stream/hls/{token}/main.m3u8").text
    for i in range(3):
        r = api.get(f"/api/stream/hls/{token}/main/s{i}")
        assert r.status_code == 200 and r.content == SEGS[i] and r.headers["content-length"] == str(len(SEGS[i]))
    assert [q.headers["range"] for q in api.net.cdn_requests(f"/sc/{SC}/all.mp3")] == [
        f"bytes=0-{a - 1}", f"bytes={a}-{a + b - 1}", f"bytes={a + b}-{len(whole) - 1}"]


# ---------------------------------------------------------------------------
# HLS никогда не отдаётся progressive-адресом
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_direct_stream_url_refuses_hls_track(api, method):
    mp3_hls(api)
    r = api.http.request(method, f"/api/stream/sc/{SC}", headers={"Range": "bytes=0-"})
    assert r.status_code == 409 and r.headers[stream.ERROR_HEADER] == "hls_required"
    assert b"#EXTM3U" not in r.content
    assert not api.net.cdn_requests(ROOT)                    # плейлист даже не запрашивался как «аудиофайл»


def test_warm_for_hls_track_resolves_without_head(harness):
    mp3_hls(harness)

    async def go(c):
        r = await c.get(f"/api/stream/sc/{SC}", params={"warm": 1})
        await asyncio.gather(*list(harness.app.state.resolver.background))
        return r
    r = harness.run(go)
    assert r.status_code == 202
    assert harness.resolver.calls[KEY] == 1 and not stream._heads and not harness.net.cdn_requests()


# ---------------------------------------------------------------------------
# Сеансы: токены, освобождение, лимиты
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("path", [
    "/api/stream/hls/AAAAAAAAAAAAAAAAAAAAAAAA/main.m3u8",          # неизвестный токен
    "/api/stream/hls/short/main.m3u8",                              # не похож на токен
    "/api/stream/hls/AAAAAAAAAAAAAAAAAAAAAAAA/main/s0",
])
def test_unknown_token_is_404(api, path):
    r = api.get(path)
    assert r.status_code == 404 and r.headers[stream.ERROR_HEADER] == "hls_session_expired"
    assert api.resolver.total() == 0 and not api.net.requests


@pytest.mark.parametrize("suffix", ["x9.m3u8", "main/x0", "main/s", "main/s-1", "main/s1e3", "main/m99", "main/s99"])
def test_bad_resource_names_are_404(api, suffix):
    mp3_hls(api)
    token = play(api)["url"].split("/")[4]
    assert api.get(f"/api/stream/hls/{token}/{suffix}").status_code == 404


def test_release_frees_session(api):
    mp3_hls(api)
    url = play(api)["url"]
    token = url.split("/")[4]
    assert api.delete(f"/api/stream/hls/{token}").status_code == 204
    assert api.get(url).status_code == 404 and hls_proxy.stats()["sessions"] == 0
    assert api.delete(f"/api/stream/hls/{token}").status_code == 204     # повторно — без ошибки


def test_session_count_is_bounded(api, monkeypatch):
    monkeypatch.setattr(hls_proxy, "MAX_SESSIONS", 3)
    tokens = [hls_proxy.open_session("sc", str(1000 + i)).token for i in range(5)]
    assert hls_proxy.stats()["sessions"] == 3
    assert hls_proxy.get_session(tokens[0]) is None and hls_proxy.get_session(tokens[-1]) is not None


def test_idle_sessions_expire(api, monkeypatch):
    s = hls_proxy.open_session("sc", SC)
    monkeypatch.setattr(hls_proxy, "IDLE_TTL", -1)
    assert hls_proxy.get_session(s.token) is None and hls_proxy.stats()["sessions"] == 0


def test_old_sessions_expire_even_if_used(api, monkeypatch):
    s = hls_proxy.open_session("sc", SC)
    monkeypatch.setattr(hls_proxy, "MAX_AGE", -1)
    assert hls_proxy.get_session(s.token) is None


def test_clear_cache_drops_sessions(api):
    mp3_hls(api)
    url = play(api)["url"]
    assert api.post("/api/settings/clear-cache").status_code == 200
    assert api.get(url).status_code == 404


# ---------------------------------------------------------------------------
# SSRF: адреса из плейлиста и редиректы
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("host, ips", [("evil.test", ["127.0.0.1"]), ("meta.test", ["169.254.169.254"]),
                                       ("lan.test", ["192.168.0.10"]), ("mixed.test", ["93.184.216.34", "10.0.0.1"])])
def test_segment_on_internal_address_is_blocked(api, host, ips):
    api.dns[host] = ips
    api.net.add_file(ROOT, media_playlist([f"https://{host}/x.mp3"]).encode(), "application/vnd.apple.mpegurl")
    api.resolver.hls[KEY] = "mp3"
    token = play(api)["url"].split("/")[4]
    r = api.get(f"/api/stream/hls/{token}/main/s0")
    assert r.status_code == 502 and r.headers[stream.ERROR_HEADER] == "upstream_blocked"
    assert host not in r.text
    assert not [q for q in api.net.requests if q.url.host == host]           # запроса к внутреннему адресу не было


@pytest.mark.parametrize("uri", ["https://127.0.0.1:8765/api/settings", "https://[::1]/x", "http://cdn.test/x.mp3",
                                 "https://localhost/x"])
def test_segment_with_forbidden_literal_or_scheme_is_blocked(api, uri):
    api.net.add_file(ROOT, media_playlist([uri]).encode(), "application/vnd.apple.mpegurl")
    api.resolver.hls[KEY] = "mp3"
    token = play(api)["url"].split("/")[4]
    assert api.get(f"/api/stream/hls/{token}/main/s0").headers[stream.ERROR_HEADER] == "upstream_blocked"
    assert len(api.net.requests) == 1                                         # только сам плейлист


def test_redirect_to_internal_address_is_blocked(api):
    mp3_hls(api)
    api.net.queue(f"/sc/{SC}/seg0.mp3", lambda req: httpx.Response(302, headers={
        "Location": "https://169.254.169.254/latest/meta-data/iam"}))
    token = play(api)["url"].split("/")[4]
    r = api.get(f"/api/stream/hls/{token}/main/s0")
    assert r.status_code == 502 and r.headers[stream.ERROR_HEADER] == "upstream_blocked"
    assert not [q for q in api.net.requests if q.url.host == "169.254.169.254"]


def test_root_playlist_redirect_to_internal_is_blocked(api):
    mp3_hls(api)
    api.net.queue(ROOT, lambda req: httpx.Response(307, headers={"Location": "https://10.0.0.1/p.m3u8"}))
    r = api.get(f"/api/stream/sc/{SC}/playback")
    assert r.status_code == 502 and r.headers[stream.ERROR_HEADER] == "upstream_blocked"


def test_safe_redirect_is_followed_and_relative_uris_use_final_url(api):
    api.net.queue(ROOT, lambda req: httpx.Response(302, headers={"Location": f"/sc/{SC}/moved/index.m3u8?t=1"}))
    api.net.add_file(f"/sc/{SC}/moved/index.m3u8", media_playlist(["s.mp3"]).encode(), "application/vnd.apple.mpegurl")
    api.net.add_file(f"/sc/{SC}/moved/s.mp3", SEGS[0], "audio/mpeg")
    api.resolver.hls[KEY] = "mp3"
    token = play(api)["url"].split("/")[4]
    assert api.get(f"/api/stream/hls/{token}/main/s0").content == SEGS[0]


def test_hls_responses_are_not_readable_cross_origin(api):
    """Чужая страница в браузере пользователя не может прочитать плейлист Wave (CORS только для своих адресов)."""
    mp3_hls(api)
    url = play(api)["url"]
    r = api.get(url, headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in r.headers
    r = api.get(f"/api/stream/sc/{SC}/playback", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in r.headers


# ---------------------------------------------------------------------------
# Протухшие ссылки, смена формата, ошибки источника
# ---------------------------------------------------------------------------
def test_stale_segment_link_is_refreshed_once(api):
    mp3_hls(api)
    token = play(api)["url"].split("/")[4]
    api.get(f"/api/stream/hls/{token}/main.m3u8")
    api.net.queue(f"/sc/{SC}/seg1.mp3", 403)
    r = api.get(f"/api/stream/hls/{token}/main/s1")
    assert r.status_code == 200 and r.content == SEGS[1]
    assert api.resolver.calls[KEY] == 2                                     # перерезолв
    assert [q.url.params["v"] for q in api.net.cdn_requests(ROOT)] == ["1", "2"]   # плейлист по новой ссылке


def test_stale_twice_is_link_unavailable(api):
    mp3_hls(api)
    token = play(api)["url"].split("/")[4]
    api.net.queue(f"/sc/{SC}/seg0.mp3", 404, 404)
    r = api.get(f"/api/stream/hls/{token}/main/s0")
    assert r.status_code == 502 and r.headers[stream.ERROR_HEADER] == "link_unavailable" and "HTTP 404" in r.json()["detail"]


def test_stale_root_playlist_is_resolved_again(api):
    mp3_hls(api)
    api.net.queue(ROOT, 403)
    assert play(api)["kind"] == "hls"
    assert api.resolver.calls[KEY] == 2


def test_changed_stream_after_refresh_is_reported(api):
    """После перерезолва другой плейлист (другое число сегментов): нумерация не совпадает — честное 409."""
    mp3_hls(api)
    token = play(api)["url"].split("/")[4]
    api.net.queue(f"/sc/{SC}/seg0.mp3", 403)
    api.net.add_file(ROOT, media_playlist(["seg0.mp3"]).encode(), "application/vnd.apple.mpegurl")
    r = api.get(f"/api/stream/hls/{token}/main/s0")
    assert r.status_code == 409 and r.headers[stream.ERROR_HEADER] == "hls_changed"


def test_resolver_switching_to_progressive_is_reported(api):
    mp3_hls(api)
    token = play(api)["url"].split("/")[4]
    del api.resolver.hls[KEY]
    resolver._invalidate("sc", SC)
    hls_proxy.get_session(token).root_fetched = 0                          # корневой плейлист пора обновить
    r = api.get(f"/api/stream/hls/{token}/main.m3u8")
    assert r.status_code == 409 and r.headers[stream.ERROR_HEADER] == "hls_changed"


def test_encrypted_playlist_is_refused_honestly(api):
    api.net.add_file(ROOT, media_playlist(["s.ts"], extra_header='#EXT-X-KEY:METHOD=AES-128,URI="https://cdn.test/k"').encode(),
                     "application/vnd.apple.mpegurl")
    api.resolver.hls[KEY] = "aac"
    r = api.get(f"/api/stream/sc/{SC}/playback")
    assert r.status_code == 403 and r.headers[stream.ERROR_HEADER] == "hls_encrypted"
    assert "зашифрованном" in r.json()["detail"] and "yt-dlp" not in r.json()["detail"]
    assert not api.net.cdn_requests("/k")                                    # ключ не запрашивался


@pytest.mark.parametrize("body", [b"<html>not a playlist</html>", b"#EXTM3U\n#EXT-X-ENDLIST\n", b"\xff\xfe\x00bad"])
def test_bad_playlist_is_reported(api, body):
    api.net.add_file(ROOT, body, "application/vnd.apple.mpegurl")
    api.resolver.hls[KEY] = "mp3"
    r = api.get(f"/api/stream/sc/{SC}/playback")
    assert r.status_code == 502 and r.headers[stream.ERROR_HEADER] == "hls_bad_playlist"


def test_oversized_playlist_is_refused(api, monkeypatch):
    monkeypatch.setattr(hls_proxy, "MAX_PLAYLIST_BYTES", 300)
    mp3_hls(api)
    r = api.get(f"/api/stream/sc/{SC}/playback")
    assert r.status_code == 502 and r.headers[stream.ERROR_HEADER] == "hls_bad_playlist"


def test_playlist_read_timeout_is_a_network_error(api, monkeypatch):
    monkeypatch.setattr(hls_proxy, "PLAYLIST_TIMEOUT", 0.3)
    mp3_hls(api)
    api.net.hang_streams = True                                              # тело плейлиста «зависает»
    r = api.get(f"/api/stream/sc/{SC}/playback")
    assert r.status_code == 503 and r.headers[stream.ERROR_HEADER] == "network"
    assert all(s.closed for s in api.net.streams)


def test_cdn_unreachable_is_network(api):
    mp3_hls(api)
    api.net.queue(ROOT, httpx.ConnectError("unreachable"))
    r = api.get(f"/api/stream/sc/{SC}/playback")
    assert r.status_code == 503 and r.headers[stream.ERROR_HEADER] == "network"


def test_segment_server_error(api):
    mp3_hls(api)
    token = play(api)["url"].split("/")[4]
    api.net.queue(f"/sc/{SC}/seg2.mp3", 500)
    r = api.get(f"/api/stream/hls/{token}/main/s2")
    assert r.status_code == 502 and r.headers[stream.ERROR_HEADER] == "upstream_status"
    assert all(s.closed for s in api.net.streams)


def test_segment_break_keeps_received_bytes(harness):
    """N2 и для сегментов: обрыв посередине — полученные байты отданы, ответ не завершён как полный."""
    mp3_hls(harness)
    harness.net.chunk = 2000
    sent: list[dict] = []

    async def go(c):
        url = (await c.get(f"/api/stream/sc/{SC}/playback")).json()["url"]
        token = url.split("/")[4]
        harness.net.fail_after = 2
        path = f"/api/stream/hls/{token}/main/s0"
        scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"}, "http_version": "1.1",
                 "method": "GET", "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": b"",
                 "root_path": "", "headers": [(b"host", b"127.0.0.1")], "client": ("127.0.0.1", 1),
                 "server": ("127.0.0.1", 80)}

        async def receive():
            await asyncio.sleep(5)
            return {"type": "http.disconnect"}

        async def send(msg):
            sent.append(msg)
        await asyncio.wait_for(harness.app(scope, receive, send), 5)
    harness.run(go)
    bodies = [m for m in sent if m["type"] == "http.response.body"]
    assert sent[0]["status"] == 200
    assert b"".join(m["body"] for m in bodies) == SEGS[0][:4000]
    assert all(m.get("more_body") for m in bodies)


def test_upstream_connections_are_bounded(harness, monkeypatch):
    """Не больше HLS_PARALLEL одновременных соединений прокси с источником."""
    segs = [mp3_frames(5) for _ in range(12)]
    mp3_hls(harness, segs)
    state = {"active": 0, "peak": 0}
    real = netguard.send

    async def slow_send(client, url, headers):
        state["active"] += 1
        state["peak"] = max(state["peak"], state["active"])
        try:
            await asyncio.sleep(0.05)
            return await real(client, url, headers)
        finally:
            state["active"] -= 1
    monkeypatch.setattr(netguard, "send", slow_send)

    async def go(c):
        token = (await c.get(f"/api/stream/sc/{SC}/playback")).json()["url"].split("/")[4]
        return await asyncio.gather(*(c.get(f"/api/stream/hls/{token}/main/s{i}") for i in range(12)))
    rs = harness.run(go)
    assert [r.status_code for r in rs] == [200] * 12
    assert 1 < state["peak"] <= resolver.HLS_PARALLEL


def test_unresolvable_host_with_user_proxy_is_refused(api, monkeypatch):
    """Замечание аудита S8: настроенный YTDLP_PROXY больше не делает непроверенное имя допустимым."""
    from streaming import config
    monkeypatch.setattr(config, "YTDLP_PROXY", "http://127.0.0.1:3128")
    api.net.add_file(ROOT, media_playlist(["https://nxdomain.test/a.mp3"]).encode(), "application/vnd.apple.mpegurl")
    api.resolver.hls[KEY] = "mp3"
    token = play(api)["url"].split("/")[4]
    r = api.get(f"/api/stream/hls/{token}/main/s0")
    assert r.status_code == 502 and r.headers[stream.ERROR_HEADER] == "upstream_unverifiable"
    assert not [q for q in api.net.requests if q.url.host == "nxdomain.test"]


def test_logs_do_not_contain_signed_urls(api, caplog):
    caplog.set_level(logging.DEBUG)
    mp3_hls(api)
    token = play(api)["url"].split("/")[4]
    api.net.queue(f"/sc/{SC}/seg0.mp3", 403)
    api.dns["evil.test"] = ["127.0.0.1"]
    api.get(f"/api/stream/hls/{token}/main/s0")
    api.net.add_file(ROOT, media_playlist([f"https://evil.test/x.mp3?{SIG}"]).encode(), "application/vnd.apple.mpegurl")
    hls_proxy.clear()
    token = play(api)["url"].split("/")[4]
    api.get(f"/api/stream/hls/{token}/main/s0")
    assert "SECRET" not in caplog.text
