// core/exclusions.js (S14): какие треки ленты убрать сразу после «Не рекомендовать» — так же, как сопоставляет backend.
import { test } from "node:test";
import assert from "node:assert/strict";
import { artistNorm, matchesExclusion } from "../../frontend/js/core/exclusions.js";

test("artistNorm: регистр, пробелы и знаки препинания не различаются (как recs_store.norm)", () => {
  assert.equal(artistNorm("  ШУМ  волн! "), "шум волн");
  assert.equal(artistNorm("AC/DC & Co"), "ac dc co");
  assert.equal(artistNorm("Beyoncé"), "beyoncé");
  assert.equal(artistNorm("dj_snake"), "dj_snake");
  assert.equal(artistNorm(null), "");
});

test("исключение трека — по источнику и id вместе", () => {
  const item = { kind: "track", value: "yt:AAAAAAAAAA1" };
  assert.equal(matchesExclusion({ source: "yt", id: "AAAAAAAAAA1", artist: "x" }, item), true);
  assert.equal(matchesExclusion({ source: "sc", id: "AAAAAAAAAA1", artist: "x" }, item), false);
});

test("исключение исполнителя — основной исполнитель или соавтор, любое написание", () => {
  const item = { kind: "artist", value: "шум волн" };
  assert.equal(matchesExclusion({ source: "yt", id: "1", artist: "Шум Волн" }, item), true);
  assert.equal(matchesExclusion({ source: "sc", id: "2", artist: "Гость", artists: ["Гость", "шум, волн"] }, item), true);
  assert.equal(matchesExclusion({ source: "yt", id: "3", artist: "Шум" }, item), false);
});

test("неизвестный вид или пустые данные — не совпадение", () => {
  assert.equal(matchesExclusion({ source: "yt", id: "1", artist: "a" }, { kind: "album", value: "a" }), false);
  assert.equal(matchesExclusion(null, { kind: "artist", value: "a" }), false);
  assert.equal(matchesExclusion({ source: "yt", id: "1" }, null), false);
});
