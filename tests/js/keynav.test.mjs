// Навигация с клавиатуры по меню и вкладкам (S12): frontend/js/core/keynav.js, без DOM.
//   node --test "tests/js/*.test.mjs"
import { test } from "node:test";
import assert from "node:assert/strict";
import { navTarget, firstEnabled, lastEnabled } from "../../frontend/js/core/keynav.js";

const all = [true, true, true, true];

test("стрелки вниз и вверх ходят по кругу", () => {
  assert.equal(navTarget(all, 0, "ArrowDown"), 1);
  assert.equal(navTarget(all, 3, "ArrowDown"), 0);
  assert.equal(navTarget(all, 0, "ArrowUp"), 3);
  assert.equal(navTarget(all, 2, "ArrowUp"), 1);
});

test("фокус вне группы: вниз — первый пункт, вверх — последний", () => {
  assert.equal(navTarget(all, -1, "ArrowDown"), 0);
  assert.equal(navTarget(all, -1, "ArrowUp"), 3);
});

test("Home и End — крайние доступные пункты", () => {
  assert.equal(navTarget(all, 2, "Home"), 0);
  assert.equal(navTarget(all, 1, "End"), 3);
  const mid = [false, true, true, false];
  assert.equal(navTarget(mid, 2, "Home"), 1);
  assert.equal(navTarget(mid, 1, "End"), 2);
});

test("стрелки заходят и на недоступные пункты (их озвучит скринридер), Home/End — нет", () => {
  const off = [true, false, true];
  assert.equal(navTarget(off, 0, "ArrowDown"), 1);
  assert.equal(navTarget(off, 1, "ArrowDown"), 2);
  assert.equal(navTarget(off, 2, "ArrowUp"), 1);
  assert.equal(navTarget([false, true, false], 1, "End"), 1);
});

test("первый и последний доступный; все недоступны и пустая группа", () => {
  assert.equal(firstEnabled([false, false, true, true]), 2);
  assert.equal(lastEnabled([true, true, false]), 1);
  assert.equal(firstEnabled([false, false]), 0);
  assert.equal(lastEnabled([false, false]), 1);
  assert.equal(firstEnabled([]), -1);
  assert.equal(navTarget([], -1, "ArrowDown"), -1);
  assert.equal(navTarget([], -1, "Home"), -1);
});

test("горизонтальная группа (вкладки): влево и вправо; вертикальные стрелки к ней не относятся", () => {
  assert.equal(navTarget(all, 0, "ArrowRight", "horizontal"), 1);
  assert.equal(navTarget(all, 0, "ArrowLeft", "horizontal"), 3);
  assert.equal(navTarget(all, 0, "ArrowDown", "horizontal"), -1);
  assert.equal(navTarget(all, 0, "ArrowRight"), -1);
});

test("остальные клавиши не обрабатываются", () => {
  for (const key of ["Enter", " ", "Tab", "Escape", "a", "PageDown"]) assert.equal(navTarget(all, 1, key), -1);
});
