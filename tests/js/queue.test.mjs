// player/queue.js: модель очереди без DOM. Главное — M11: у очереди собственный массив, список экрана —
// только источник, продолжение — явный continuation.
import { test } from "node:test";
import assert from "node:assert/strict";
import { Queue } from "../../frontend/js/player/queue.js";

const T = (n) => ({ id: `t${n}`, source: "yt", title: `Трек ${String.fromCharCode(1040 + ((n * 7) % 26))}${n}`, duration: 100 + n });
const ids = (q) => q.tracks.map((t) => t.id);
const range = (a, b) => Array.from({ length: b - a }, (_, i) => T(a + i));

/** Как экран: список, который можно сортировать и дополнять, и очередь, запущенная из него. */
function fromScreen(n = 5, at = 0) {
  const list = range(0, n);
  const q = new Queue();
  q.load(list, { label: "Поиск: m11", source: list, continuation: async () => list });
  q.select(at);
  return { list, q };
}

test("M11: сортировка списка на экране не меняет очередь", () => {
  const { list, q } = fromScreen(5, 1);
  const before = ids(q), cur = q.current;
  list.sort((a, b) => b.duration - a.duration);              // «Длинные сначала»
  assert.notDeepEqual(list.map((t) => t.id), before, "список действительно переставлен");
  assert.deepEqual(ids(q), before);
  assert.equal(q.current, cur);
  assert.equal(q.index, 1);
  assert.equal(q.at(q.nextIndex()).id, before[2], "следующим играет следующий трек очереди, а не списка");
});

test("M11: подгрузка в список не дописывает очередь сама; дописывает только extend()", async () => {
  const { list, q } = fromScreen(3, 0);
  list.push(T(3), T(4));
  assert.equal(q.length, 3, "массив источника очередь не меняет");
  assert.equal(await q.extend(), 2);
  assert.deepEqual(ids(q), ["t0", "t1", "t2", "t3", "t4"]);
  assert.equal(await q.extend(), 0, "уже взятые треки повторно не добавляются");
});

test("M11: продолжение после сортировки дописывает новые треки в конец, не меняя порядок очереди", async () => {
  const { list, q } = fromScreen(4, 2);
  list.push(T(9), T(8));
  list.sort((a, b) => a.title.localeCompare(b.title, "ru"));
  assert.equal(await q.extend(), 2);
  assert.deepEqual(ids(q).slice(0, 4), ["t0", "t1", "t2", "t3"]);
  assert.deepEqual(new Set(ids(q).slice(4)), new Set(["t9", "t8"]));
  assert.equal(q.current.id, "t2");
});

test("M11: правки очереди не меняют список на экране", () => {
  const { list, q } = fromScreen(5, 0);
  const snapshot = list.map((t) => t.id);
  q.add(T(50), true); q.remove(3); q.move(1, 1); q.moveTo(4, 1); q.append([T(60)]); q.clear();
  assert.deepEqual(list.map((t) => t.id), snapshot);
  assert.equal(list.length, 5);
});

test("load копирует треки; источник узнаётся до первой правки", () => {
  const list = range(0, 3);
  const q = new Queue();
  q.load(list, { source: list });
  assert.ok(q.isFrom(list) && !q.isFrom(range(0, 3)) && !q.isFrom(null));
  assert.equal(q.index, -1);
  assert.equal(q.current, null);
  assert.equal(q.select(1).id, "t1");
  assert.equal(q.current, list[1], "те же объекты треков: экран находит играющую строку");
  assert.equal(q.select(7), null);
  assert.equal(q.index, 1);
  q.tracks.push(T(99));                                     // копия: снаружи очередь не изменить
  assert.equal(q.length, 3);
});

test("правка пользователя делает очередь собственной: источник и продолжение снимаются", async () => {
  for (const edit of [(q) => q.add(T(9)), (q) => q.remove(2), (q) => q.move(1, 1), (q) => q.moveTo(2, 0),
    (q) => q.clear(), (q) => q.append([T(9)])]) {
    const { list, q } = fromScreen(4, 0);
    edit(q);
    assert.ok(!q.isFrom(list));
    list.push(T(20));
    assert.equal(await q.extend(), 0);
  }
});

test("extend: очередь заменили, пока шёл запрос продолжения — ничего не дописывается", async () => {
  const list = range(0, 2);
  let release;
  const q = new Queue();
  q.load(list, { source: list, continuation: () => new Promise((r) => { release = () => r([T(5)]); }) });
  q.select(0);
  const pending = q.extend();
  q.load(range(10, 12));
  release();
  assert.equal(await pending, 0);
  assert.deepEqual(ids(q), ["t10", "t11"]);
});

test("extend без продолжения — 0", async () => {
  const q = new Queue();
  q.load(range(0, 2));
  assert.equal(await q.extend(), 0);
});

test("add: копия трека, следующим или в конец", () => {
  const { q } = fromScreen(3, 0);
  const src = T(7);
  const c = q.add(src, true);
  assert.notEqual(c, src);
  assert.deepEqual(ids(q), ["t0", "t7", "t1", "t2"]);
  q.add(src);
  assert.deepEqual(ids(q), ["t0", "t7", "t1", "t2", "t7"], "один трек может стоять дважды");
  const empty = new Queue();
  empty.add(T(1), true);
  assert.deepEqual(ids(empty), ["t1"], "в пустую очередь «следующим» — в конец");
});

test("remove: играющий трек не убирается, номер текущего сдвигается", () => {
  const { q } = fromScreen(5, 2);
  assert.equal(q.remove(2), false);
  assert.equal(q.remove(0), true);
  assert.equal(q.index, 1);
  assert.equal(q.current.id, "t2");
  assert.equal(q.remove(3), true);
  assert.deepEqual(ids(q), ["t1", "t2", "t3"]);
  assert.equal(q.remove(9), false);
});

test("move и moveTo: текущий трек остаётся текущим", () => {
  const { q } = fromScreen(5, 1);
  assert.equal(q.move(0, -1), false);
  assert.equal(q.move(4, 1), false);
  q.move(1, 1);
  assert.deepEqual(ids(q), ["t0", "t2", "t1", "t3", "t4"]);
  assert.equal(q.current.id, "t1");
  assert.equal(q.index, 2);
  q.moveTo(4, 3);                                            // «играть следующим»
  assert.deepEqual(ids(q), ["t0", "t2", "t1", "t4", "t3"]);
  assert.equal(q.moveTo(3, 3), false);
  assert.equal(q.moveTo(3, 4), false, "на своё же место");
  q.moveTo(0, 5);
  assert.deepEqual(ids(q), ["t2", "t1", "t4", "t3", "t0"]);
  assert.equal(q.current.id, "t1");
});

test("clear оставляет только играющий трек", () => {
  const { q } = fromScreen(4, 2);
  q.clear();
  assert.deepEqual(ids(q), ["t2"]);
  assert.equal(q.index, 0);
  const idle = new Queue();
  idle.load(range(0, 3));
  idle.clear();
  assert.equal(idle.length, 0);
  assert.equal(idle.index, -1);
});

test("next и prev по порядку", () => {
  const { q } = fromScreen(3, 0);
  assert.equal(q.nextIndex(), 1);
  assert.equal(q.prevIndex(), -1);
  q.select(2);
  assert.equal(q.nextIndex(), -1, "дальше ничего нет");
  assert.equal(q.prevIndex(), 1);
  assert.equal(q.peekNext(), null);
  q.select(1);
  assert.equal(q.peekNext().id, "t2");
  assert.equal(new Queue().nextIndex(), -1);
});

test("перемешивание: следующий — случайный, но не текущий", () => {
  // S9 проверял конкретное число по механизму «тянуть, пока не выпадет не текущий». В S10 выбор идёт из
  // несыгранных в круге (тест «shuffle без повторов» ниже), свойство «случайный, не текущий» сохранено.
  const seq = [0.5];                                         // несыгранные t1, t2, t3: 0.5*3 → t2
  const q = new Queue({ random: () => seq.shift() });
  q.load(range(0, 4));
  q.select(0);
  assert.equal(q.nextIndex({ shuffle: true }), 2);
  const one = new Queue({ random: () => 0 });
  one.load(range(0, 1));
  one.select(0);
  assert.equal(one.nextIndex({ shuffle: true }), -1, "единственный трек: дальше по порядку ничего нет");
  for (let k = 0; k < 200; k++) {
    const r = new Queue();
    r.load(range(0, 5));
    r.select(k % 5);
    const j = r.nextIndex({ shuffle: true });
    assert.ok(j >= 0 && j < 5 && j !== k % 5);
  }
});

test("append дописывает в конец и возвращает позицию первого", () => {
  const { q } = fromScreen(2, 1);
  assert.equal(q.append([T(5), T(6)]), 2);
  assert.deepEqual(ids(q), ["t0", "t1", "t5", "t6"]);
});
