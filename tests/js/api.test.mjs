// core/api.js: ключ трека, снимок трека для backend (контракт Track, S6/M6), разбор ответа рекомендаций (M4),
// текст ошибки из detail. fetch подменяется на время теста.
import { test } from "node:test";
import assert from "node:assert/strict";
import { api, jsonOpts, recsResult, trackKey, trackPayload } from "../../frontend/js/core/api.js";

const full = {
  id: "abc", source: "yt", title: "Т", artist: "А", artists: ["А", "Б"], album: "Альбом", album_id: "MPREb_1", duration: 200,
  cover: "https://lh3.googleusercontent.com/x=w544-h544", explicit: true, stream_url: "/api/stream/yt/abc",
  artist_id: "UC1", artist_ids: ["UC1", "UC2"], plays: 12345, released: "2025-03-01T10:00:00Z",
  url: "https://music.youtube.com/watch?v=abc", reason: "Похоже на …", _i: 7,
};

test("trackKey: источник и id вместе", () => {
  assert.equal(trackKey({ source: "sc", id: "123" }), "sc:123");
  assert.notEqual(trackKey({ source: "sc", id: "1" }), trackKey({ source: "yt", id: "1" }));
});

test("trackPayload: все поля Track, без служебных полей интерфейса", () => {
  const p = trackPayload(full);
  // S10: + album_id (аддитивно)
  assert.deepEqual(Object.keys(p).sort(), ["album", "album_id", "artist", "artist_id", "artist_ids", "artists", "cover", "duration",
    "explicit", "id", "plays", "released", "source", "stream_url", "title", "url"]);
  assert.equal(p.album_id, "MPREb_1");
  assert.equal(p.plays, 12345);
  assert.equal(p.released, "2025-03-01T10:00:00Z");
  assert.equal(p.explicit, true);
});

test("trackPayload: пропуски заменяются значениями по умолчанию контракта", () => {
  const p = trackPayload({ id: "1", source: "sc", title: "x", artist: "y", stream_url: "/api/stream/sc/1", plays: 1.5, released: 2025 });
  assert.deepEqual(p.artists, ["y"]);
  assert.equal(p.album, null); assert.equal(p.album_id, null); assert.equal(p.duration, null); assert.equal(p.cover, null);
  assert.equal(p.explicit, false); assert.equal(p.artist_id, null); assert.deepEqual(p.artist_ids, []);
  assert.equal(p.plays, null);                      // не целое — не прослушивания
  assert.equal(p.released, null);                   // не строка — не дата
  assert.equal(p.url, null);
});

test("recsResult: сбой источника без треков — ошибка, а не «нечего предложить» (M4)", () => {
  assert.throws(() => recsResult({ tracks: [], failed_sources: ["yt"] }), /Источник сейчас недоступен: YouTube Music/);
  assert.deepEqual(recsResult({ tracks: [], failed_sources: [] }), { tracks: [], partial: "" });
  const ok = recsResult({ tracks: [full], failed_sources: ["sc"] });
  assert.equal(ok.tracks.length, 1);
  assert.equal(ok.partial, "SoundCloud");
  assert.deepEqual(recsResult(null), { tracks: [], partial: "" });
});

test("api: ошибка несёт detail из ответа backend", async (t) => {
  const real = globalThis.fetch;
  t.after(() => { globalThis.fetch = real; });
  globalThis.fetch = async () => new Response(JSON.stringify({ detail: "Источник не ответил" }), { status: 503 });
  await assert.rejects(api("/api/x"), /Источник не ответил/);
  globalThis.fetch = async () => new Response("не JSON", { status: 500 });
  await assert.rejects(api("/api/x"), /Ошибка 500/);
  globalThis.fetch = async () => new Response(JSON.stringify({ ok: 1 }), { status: 200 });
  assert.deepEqual(await api("/api/x"), { ok: 1 });
});

test("jsonOpts: JSON-тело и заголовок", () => {
  assert.deepEqual(jsonOpts("POST", { a: 1 }), { method: "POST", headers: { "Content-Type": "application/json" }, body: '{"a":1}' });
  assert.equal(jsonOpts("DELETE").body, undefined);
});
