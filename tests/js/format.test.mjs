// core/format.js: форматирование времени, склонения, размер обложек.
import { test } from "node:test";
import assert from "node:assert/strict";
import { fmtTime, plural, thumb, clamp, rand, SOURCES } from "../../frontend/js/core/format.js";

test("fmtTime: минуты и секунды", () => {
  assert.equal(fmtTime(0), "0:00");
  assert.equal(fmtTime(5), "0:05");
  assert.equal(fmtTime(59.99), "0:59");
  assert.equal(fmtTime(60), "1:00");
  assert.equal(fmtTime(185), "3:05");
  assert.equal(fmtTime(3599), "59:59");
});

test("fmtTime: часы с двузначными минутами", () => {
  assert.equal(fmtTime(3600), "1:00:00");
  assert.equal(fmtTime(3725), "1:02:05");
  assert.equal(fmtTime(36000 + 59 * 60 + 9), "10:59:09");
});

test("fmtTime: мусор и отрицательные значения не ломают вёрстку", () => {
  for (const v of [NaN, Infinity, -Infinity, -1, undefined, null, "12"]) assert.equal(fmtTime(v), "0:00");
});

test("plural: русские склонения", () => {
  const t = (n) => plural(n, "трек", "трека", "треков");
  assert.equal(t(1), "1 трек");
  assert.equal(t(2), "2 трека");
  assert.equal(t(4), "4 трека");
  assert.equal(t(5), "5 треков");
  assert.equal(t(0), "0 треков");
  assert.equal(t(11), "11 треков");
  assert.equal(t(12), "12 треков");
  assert.equal(t(14), "14 треков");
  assert.equal(t(21), "21 трек");
  assert.equal(t(22), "22 трека");
  assert.equal(t(111), "111 треков");
  assert.equal(t(112), "112 треков");
  assert.equal(t(101), "101 трек");
});

test("thumb: обложки YouTube Music подставляют нужный размер", () => {
  assert.equal(thumb("https://lh3.googleusercontent.com/abc=w544-h544", 120), "https://lh3.googleusercontent.com/abc=w120-h120");
  assert.equal(thumb("https://lh3.googleusercontent.com/abc=w60-h60-l90-rj", 320), "https://lh3.googleusercontent.com/abc=w320-h320-l90-rj");
});

test("thumb: SoundCloud уменьшается только для маленьких превью", () => {
  const big = "https://i1.sndcdn.com/artworks-xyz-t500x500.jpg";
  assert.equal(thumb(big, 120), "https://i1.sndcdn.com/artworks-xyz-t300x300.jpg");
  assert.equal(thumb(big, 300), "https://i1.sndcdn.com/artworks-xyz-t300x300.jpg");
  assert.equal(thumb(big, 544), big);
});

test("thumb: без адреса или размера — как есть", () => {
  assert.equal(thumb("", 120), "");
  assert.equal(thumb(null, 120), null);
  assert.equal(thumb("https://example.org/x.jpg", 0), "https://example.org/x.jpg");
  assert.equal(thumb("https://example.org/x.jpg", 120), "https://example.org/x.jpg");
});

test("clamp и rand", () => {
  assert.equal(clamp(5, 0, 1), 1);
  assert.equal(clamp(-5, 0, 1), 0);
  assert.equal(clamp(0.4, 0, 1), 0.4);
  for (let i = 0; i < 200; i++) { const r = rand(3); assert.ok(Number.isInteger(r) && r >= 0 && r < 3); }
});

test("названия источников совпадают с Track.source", () => {
  assert.deepEqual(Object.keys(SOURCES).sort(), ["sc", "yt"]);
});
