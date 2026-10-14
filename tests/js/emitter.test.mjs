// core/emitter.js: порядок вызова, отписка, синхронность — от этого зависит, что интерфейс плеера
// обновляется в том же такте, что и раньше прямыми вызовами.
import { test } from "node:test";
import assert from "node:assert/strict";
import { Emitter } from "../../frontend/js/core/emitter.js";

test("обработчики вызываются синхронно, по порядку подписки, с аргументами", () => {
  const e = new Emitter(), log = [];
  e.on("track", (t, x) => log.push(["a", t, x]));
  e.on("track", (t) => log.push(["b", t]));
  e.on("other", () => log.push(["other"]));
  e.emit("track", 1, 2);
  log.push("после");
  assert.deepEqual(log, [["a", 1, 2], ["b", 1], "после"]);
});

test("отписка, в том числе изнутри обработчика", () => {
  const e = new Emitter(), log = [];
  const off = e.on("x", () => { log.push(1); off(); });
  e.on("x", () => log.push(2));
  e.emit("x"); e.emit("x");
  assert.deepEqual(log, [1, 2, 2]);
  off();                                                    // повторная отписка безопасна
  e.emit("нет подписчиков");
});
