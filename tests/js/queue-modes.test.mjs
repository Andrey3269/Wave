// player/queue.js, S10: перемешивание без повторов, история «Назад», повтор off | one | all, сохранение.
// Случайность внедряется (детерминированный генератор), поэтому тесты не зависят от случайного результата.
import { test } from "node:test";
import assert from "node:assert/strict";
import { Queue, REPEAT_MODES, nextRepeat, normRepeat } from "../../frontend/js/player/queue.js";

const T = (n) => ({ id: `t${n}`, source: "yt", title: `Трек ${n}`, duration: 100 + n });
const range = (a, b) => Array.from({ length: b - a }, (_, i) => T(a + i));
const ids = (q) => q.tracks.map((t) => t.id);
const SEEDS = [1, 2, 3, 7, 42, 1234, 99991];

/** Детерминированный генератор (LCG) — одна и та же последовательность при одном seed. */
function rng(seed) {
  let x = seed >>> 0;
  return () => { x = (Math.imul(x, 1664525) + 1013904223) >>> 0; return x / 2 ** 32; };
}
function queue(n, { seed = 1, at = 0 } = {}) {
  const q = new Queue({ random: rng(seed) });
  q.load(range(0, n));
  if (at >= 0) q.select(at);
  return q;
}
/** «Следующий» как в контроллере: позиция из модели и выбор её текущей. -1 — очередь закончилась. */
function next(q, modes) {
  const j = q.nextIndex(modes);
  if (j >= 0) q.select(j);
  return j;
}
const cur = (q) => q.current?.id;

// ---------------------------------------------------------------- перемешивание без повторов
test("shuffle: в одном круге нет повторов, после круга при repeat off — конец", () => {
  for (const seed of SEEDS) {
    const q = queue(10, { seed });
    const played = [cur(q)];
    for (let k = 0; k < 9; k++) { assert.ok(next(q, { shuffle: true }) >= 0); played.push(cur(q)); }
    assert.equal(new Set(played).size, 10, `seed ${seed}: каждый трек ровно один раз (${played})`);
    assert.equal(q.nextIndex({ shuffle: true }), -1, "все сыграны — дальше ничего");
    assert.equal(q.nextIndex({ shuffle: true, repeat: "off" }), -1);
  }
});

test("shuffle + repeat all: новый круг без немедленного повтора, снова без повторов", () => {
  for (const seed of SEEDS) {
    const q = queue(6, { seed });
    for (let k = 0; k < 5; k++) next(q, { shuffle: true, repeat: "all" });
    const last = cur(q);
    const round = [];
    for (let k = 0; k < 6; k++) { next(q, { shuffle: true, repeat: "all" }); round.push(cur(q)); }
    assert.notEqual(round[0], last, `seed ${seed}: первый трек нового круга — не только что игравший`);
    assert.equal(new Set(round).size, 6, `seed ${seed}: второй круг тоже без повторов (${round})`);
  }
});

test("shuffle: выбор следующего устойчив — nextIndex и peekNext совпадают до смены трека", () => {
  const q = queue(8, { seed: 5 });
  const a = q.nextIndex({ shuffle: true });
  assert.equal(q.nextIndex({ shuffle: true }), a);
  assert.equal(q.peekNext({ shuffle: true }), q.at(a));
  q.select(a);
  assert.notEqual(q.nextIndex({ shuffle: true }), a, "после перехода выбирается другой трек");
});

test("shuffle: очередь из одного трека и пустая очередь", () => {
  const one = queue(1);
  assert.equal(one.nextIndex({ shuffle: true }), -1);
  assert.equal(one.nextIndex({ shuffle: true, repeat: "all" }), 0, "repeat all: единственный трек по кругу");
  assert.equal(one.prevIndex({ shuffle: true }), -1);
  const empty = new Queue({ random: rng(1) });
  for (const m of [{}, { shuffle: true }, { repeat: "all" }, { repeat: "one", auto: true }, { shuffle: true, repeat: "all" }]) {
    assert.equal(empty.nextIndex(m), -1);
  }
  assert.equal(empty.prevIndex(), -1);
  assert.equal(empty.prevIndex({ shuffle: true }), -1);
  assert.equal(empty.peekNext({ shuffle: true }), null);
});

// ---------------------------------------------------------------- история для «Назад»
test("shuffle: «Назад» идёт по фактическому порядку воспроизведения, а не по номеру в очереди", () => {
  const q = queue(10, { seed: 3 });
  const played = [q.index];
  for (let k = 0; k < 4; k++) played.push(next(q, { shuffle: true }));
  assert.notDeepEqual(played, [0, 1, 2, 3, 4], "порядок действительно перемешан");
  for (let k = played.length - 2; k >= 0; k--) {
    const j = q.prevIndex({ shuffle: true });
    assert.equal(j, played[k]);
    q.select(j);
  }
  assert.equal(q.prevIndex({ shuffle: true }), -1, "дальше назад истории нет");
});

test("shuffle: после «Назад» «Вперёд» повторяет уже сыгранное будущее, потом выбирает новый трек", () => {
  const q = queue(10, { seed: 11 });
  const played = [q.index];
  for (let k = 0; k < 4; k++) played.push(next(q, { shuffle: true }));      // p0..p4
  q.select(q.prevIndex({ shuffle: true }));                                   // p3
  q.select(q.prevIndex({ shuffle: true }));                                   // p2
  assert.equal(q.index, played[2]);
  assert.equal(next(q, { shuffle: true }), played[3], "вперёд — тот же p3");
  assert.equal(next(q, { shuffle: true }), played[4], "вперёд — тот же p4");
  const fresh = next(q, { shuffle: true });
  assert.ok(!played.includes(fresh), "дальше — несыгранный трек круга");
  assert.equal(q.prevIndex({ shuffle: true }), played[4], "и история продолжается");
});

test("shuffle: «Назад», затем «Вперёд» никогда не даёт текущий трек дважды подряд", () => {
  for (const seed of SEEDS) {
    const q = queue(7, { seed });
    let prev = q.index;
    for (let step = 0; step < 60; step++) {
      const back = step % 5 === 3;
      const j = back ? q.prevIndex({ shuffle: true }) : q.nextIndex({ shuffle: true, repeat: "all" });
      if (j < 0) continue;
      assert.notEqual(j, q.index, `seed ${seed}, шаг ${step}`);
      q.select(j);
      assert.notEqual(q.index, prev);
      prev = q.index;
    }
  }
});

test("прямой выбор трека после «Назад» обрезает будущее истории", () => {
  const q = queue(10, { seed: 4 });
  const played = [q.index];
  for (let k = 0; k < 3; k++) played.push(next(q, { shuffle: true }));      // p0..p3
  q.select(q.prevIndex({ shuffle: true }));                                   // p2
  const other = [...Array(10).keys()].find((i) => !played.includes(i));
  q.select(other);                                                            // клик по треку в очереди
  assert.equal(q.prevIndex({ shuffle: true }), played[2], "назад — откуда пришли");
  const j = q.nextIndex({ shuffle: true });
  assert.ok(j !== played[3] && !played.includes(j), "p3 уже не «будущее»: он сыгран в круге и не повторится");
});

test("без перемешивания «Назад» — предыдущий по порядку очереди (как в S9)", () => {
  const q = queue(5, { at: 3 });
  assert.equal(q.prevIndex(), 2);
  q.select(0);
  assert.equal(q.prevIndex(), -1);
});

// ---------------------------------------------------------------- правки очереди
test("удаление: история и круг забывают позицию, «Назад» её пропускает", () => {
  const q = queue(6, { seed: 8 });
  const played = [q.index];
  for (let k = 0; k < 3; k++) played.push(next(q, { shuffle: true }));      // p0..p3
  const victim = q.at(played[1]).id;
  assert.ok(q.remove(played[1]));
  const back = q.prevIndex({ shuffle: true });
  assert.equal(q.at(back).id, q.tracks.find((t) => t.id === q.at(back).id).id);
  assert.notEqual(q.at(back).id, victim);
  q.select(back);
  const back2 = q.prevIndex({ shuffle: true });
  assert.ok(back2 >= 0 && q.at(back2).id !== victim);
  assert.ok(!ids(q).includes(victim));
});

test("удаление: соседние одинаковые записи истории схлопываются (не «назад» на тот же трек)", () => {
  const q = queue(3, { at: 0 });
  q.select(1); q.select(0); q.select(2);                                      // история: 0, 1, 0, 2
  q.remove(1);                                                                // история: 0, 0, 2 → 0, 2
  const j = q.prevIndex({ shuffle: true });
  assert.equal(q.at(j).id, "t0");
  q.select(j);
  assert.equal(q.prevIndex({ shuffle: true }), -1);
});

test("перестановки не портят историю и текущий трек", () => {
  const q = queue(6, { seed: 9 });
  const played = [q.index];
  for (let k = 0; k < 3; k++) played.push(next(q, { shuffle: true }));
  const playedIds = played.map((i) => q.at(i).id);
  const curId = cur(q);
  q.moveTo(5, 0); q.move(2, 1); q.moveTo(0, 6);
  assert.equal(cur(q), curId, "текущий трек остаётся текущим");
  for (let k = playedIds.length - 2; k >= 0; k--) {
    const j = q.prevIndex({ shuffle: true });
    assert.equal(q.at(j).id, playedIds[k]);
    q.select(j);
  }
});

test("добавление в круг, очистка и замена очереди сбрасывают историю корректно", () => {
  const q = queue(3, { seed: 2 });
  next(q, { shuffle: true }); next(q, { shuffle: true });                     // круг из 3 сыгран
  assert.equal(q.nextIndex({ shuffle: true }), -1);
  q.add(T(50));
  assert.equal(q.at(q.nextIndex({ shuffle: true })).id, "t50", "добавленный трек ещё не играл в этом круге");
  q.clear();
  assert.deepEqual(ids(q), [cur(q)]);
  assert.equal(q.prevIndex({ shuffle: true }), -1, "после очистки истории «Назад» нет");
  assert.equal(q.nextIndex({ shuffle: true }), -1);
  q.load(range(10, 13));
  assert.equal(q.prevIndex({ shuffle: true }), -1, "новая очередь — новая история");
  q.select(1);
  assert.notEqual(q.nextIndex({ shuffle: true }), 1);
});

test("включение и выключение перемешивания не теряет текущий трек и не меняет порядок очереди", () => {
  const q = queue(6, { seed: 6, at: 2 });
  const order = ids(q);
  q.restartCycle();                                                           // включили перемешивание
  next(q, { shuffle: true }); next(q, { shuffle: true });
  const curId = cur(q), at = q.index;
  assert.deepEqual(ids(q), order, "порядок очереди не перемешивается");
  assert.equal(q.nextIndex(), at + 1 < 6 ? at + 1 : -1, "выключили: дальше по порядку от текущего");
  assert.equal(cur(q), curId);
  q.restartCycle();                                                           // снова включили: новый круг
  const seen = new Set([cur(q)]);
  for (let k = 0; k < 5; k++) { next(q, { shuffle: true }); seen.add(cur(q)); }
  assert.equal(seen.size, 6, "новый круг без повторов от текущего трека");
});

// ---------------------------------------------------------------- повтор
test("repeat: цикл кнопки off → all → one → off и разбор значений", () => {
  assert.deepEqual([...REPEAT_MODES], ["off", "all", "one"]);
  assert.equal(nextRepeat("off"), "all");
  assert.equal(nextRepeat("all"), "one");
  assert.equal(nextRepeat("one"), "off");
  assert.equal(normRepeat(true), "one", "старое булево true (S9: «повторять трек»)");
  assert.equal(normRepeat(false), "off");
  assert.equal(normRepeat("loop"), "off");
  assert.equal(nextRepeat(undefined), "all");
});

test("repeat off / all / one на конце очереди и в середине", () => {
  const q = queue(3, { at: 2 });
  assert.equal(q.nextIndex({ repeat: "off" }), -1, "off: после последнего — конец");
  assert.equal(q.nextIndex({ repeat: "off", auto: true }), -1);
  assert.equal(q.nextIndex({ repeat: "all" }), 0, "all: после последнего — с начала");
  assert.equal(q.nextIndex({ repeat: "all", auto: true }), 0);
  assert.equal(q.nextIndex({ repeat: "one", auto: true }), 2, "one: доиграл — тот же трек");
  assert.equal(q.nextIndex({ repeat: "one" }), -1, "one: кнопка «Следующий» — как без повтора");
  q.select(0);
  assert.equal(q.nextIndex({ repeat: "one", auto: true }), 0);
  assert.equal(q.nextIndex({ repeat: "one" }), 1);
  assert.equal(q.nextIndex({ repeat: "all" }), 1);
  const one = queue(1);
  assert.equal(one.nextIndex({ repeat: "all" }), 0, "all: единственный трек повторяется");
  assert.equal(one.nextIndex({ repeat: "off" }), -1);
});

test("shuffle × repeat: one повторяет текущий, all — по кругу, off — до конца круга", () => {
  const q = queue(4, { seed: 13 });
  assert.equal(q.nextIndex({ shuffle: true, repeat: "one", auto: true }), q.index);
  const manual = q.nextIndex({ shuffle: true, repeat: "one" });
  assert.ok(manual >= 0 && manual !== q.index, "кнопка «Следующий» при repeat one — случайный следующий");
  for (let k = 0; k < 3; k++) next(q, { shuffle: true, repeat: "off" });
  assert.equal(q.nextIndex({ shuffle: true, repeat: "off" }), -1);
  assert.ok(q.nextIndex({ shuffle: true, repeat: "all" }) >= 0);
  assert.equal(q.nextIndex({ shuffle: true, repeat: "one", auto: true }), q.index);
});

// ---------------------------------------------------------------- сохранение
test("serialize/restore: треки, текущий, подпись, история и круг переживают перезапуск", () => {
  const q = queue(8, { seed: 21 });
  q.label = "Альбом 1";
  const played = [q.index];
  for (let k = 0; k < 3; k++) played.push(next(q, { shuffle: true }));
  const snap = JSON.parse(JSON.stringify(q.serialize()));                    // как через JSON и SQLite
  const r = new Queue({ random: rng(77) });
  assert.equal(r.restore(snap), true);
  assert.deepEqual(ids(r), ids(q));
  assert.equal(r.index, q.index);
  assert.equal(r.label, "Альбом 1");
  assert.equal(r.isFrom(snap.tracks), false, "восстановленная очередь собственная, без продолжения");
  assert.equal(r.prevIndex({ shuffle: true }), played[2], "«Назад» после перезапуска — по истории");
  const rest = new Set(played);
  for (let k = 0; k < 4; k++) { const j = next(r, { shuffle: true }); assert.ok(!rest.has(j), "круг продолжается без повторов"); rest.add(j); }
  assert.equal(r.nextIndex({ shuffle: true }), -1);
});

test("serialize: длинная очередь — окно вокруг текущего трека с пересчётом номеров", () => {
  const q = queue(700, { at: 650 });
  q.select(651); q.select(10);                                                // 10 — вне окна
  q.select(652);
  const snap = q.serialize({ max: 500 });
  assert.equal(snap.tracks.length, 500);
  assert.equal(snap.tracks[snap.index].id, "t652");
  assert.ok(snap.history.every((k) => k >= 0 && k < 500));
  assert.deepEqual(snap.history.map((k) => snap.tracks[k].id), ["t650", "t651", "t652"]);
  assert.equal(snap.tracks[snap.history[snap.cursor]].id, "t652");
});

test("restore: негодный снимок не принимается, неверные номера отбрасываются", () => {
  const q = new Queue();
  for (const bad of [null, {}, { tracks: [] }, { tracks: "x" }]) assert.equal(q.restore(bad), false);
  assert.equal(q.restore({ tracks: range(0, 3), index: 9, history: [0, 7, -1, "1", 2], cursor: 40, cycle: [1, 99] }), true);
  assert.equal(q.index, -1, "номер вне очереди — ничего не выбрано");
  q.select(2);
  assert.equal(q.prevIndex({ shuffle: true }), 0);
  const ok = new Queue();
  ok.restore({ tracks: range(0, 3), index: 1, history: [1, 1, 0], cursor: 0, cycle: [] });
  assert.equal(ok.prevIndex({ shuffle: true }), -1, "курсор не на текущем — ставится на последнее его вхождение");
});

test("свойство: случайные «Назад»/«Вперёд»/правки — нет повтора подряд, «Вперёд» в круге не повторяет треки", () => {
  for (const seed of SEEDS) {
    const r = rng(seed * 31 + 7);
    const q = queue(9, { seed });
    let roundSeen = new Set([cur(q)]), forwardOnly = true;
    for (let step = 0; step < 400; step++) {
      const x = r();
      const prevId = cur(q);
      let j;
      if (x < 0.2) { j = q.prevIndex({ shuffle: true }); forwardOnly = false; }
      else if (x < 0.25 && q.length > 3) {                                    // правка: убрать не текущий трек
        const i = (q.index + 1 + Math.floor(r() * (q.length - 1))) % q.length;
        q.remove(i); forwardOnly = false; continue;
      } else if (x < 0.3) { q.add(T(100 + step)); continue; }
      else j = q.nextIndex({ shuffle: true, repeat: "all" });
      if (j < 0) continue;
      q.select(j);
      assert.notEqual(cur(q), prevId, `seed ${seed}, шаг ${step}: тот же трек дважды подряд`);
      if (x >= 0.3 && forwardOnly) {
        if (roundSeen.size === q.length) roundSeen = new Set();
        assert.ok(!roundSeen.has(cur(q)), `seed ${seed}, шаг ${step}: повтор внутри круга`);
        roundSeen.add(cur(q));
      }
    }
  }
});

test("устаревшее предложение «Назад» не применяется к случайному выбору «Вперёд»", () => {
  // Круг из 3 сыгран; prevIndex() только спросили (подсказка), потом «Вперёд» случайно выбирает трек — в том
  // числе тот, что в истории стоит перед текущим. «Назад» после этого должен вести на фактически
  // предыдущий трек (последний трек круга), а не по сдвинутому устаревшим шагом курсору.
  let hitPrevious = 0;
  for (let seed = 1; seed <= 60; seed++) {
    const q = queue(3, { seed });
    next(q, { shuffle: true, repeat: "all" }); next(q, { shuffle: true, repeat: "all" });
    const last = q.index, beforeLast = q.prevIndex({ shuffle: true });
    next(q, { shuffle: true, repeat: "all" });
    if (q.index === beforeLast) hitPrevious++;
    assert.equal(q.prevIndex({ shuffle: true }), last, `seed ${seed}`);
  }
  assert.ok(hitPrevious > 0, "сценарий «случайный выбор = предыдущий в истории» действительно встретился");
});

test("onChange: вызывается после каждого изменения очереди и текущего трека, не при чтении", async () => {
  const list = range(0, 4);
  const q = new Queue({ random: rng(1) });
  let n = 0;
  q.onChange = () => { n++; };
  const step = (fn, expect, why) => { const before = n; fn(); assert.equal(n - before, expect, why); };
  step(() => q.load(list, { source: list, continuation: async () => [...list, T(9)] }), 1, "load");
  step(() => q.select(0), 1, "select");
  step(() => { q.nextIndex({ shuffle: true }); q.prevIndex({ shuffle: true }); q.peekNext({ shuffle: true }); void q.tracks; }, 0, "чтение");
  step(() => q.restartCycle(), 1, "restartCycle");
  const before = n;
  assert.equal(await q.extend(), 1);
  assert.equal(n - before, 1, "extend с новыми треками");
  step(() => q.add(T(5)), 1, "add");
  step(() => q.move(1, 1), 1, "move");
  step(() => q.moveTo(0, 3), 1, "moveTo");
  step(() => q.remove(q.index === 2 ? 1 : 2), 1, "remove");
  step(() => q.append([T(7)]), 1, "append");
  step(() => q.clear(), 1, "clear");
  step(() => q.restore({ tracks: range(0, 2), index: 0 }), 2, "restore (внутри load)");
  step(() => { q.remove(0); q.move(9, 1); q.moveTo(5, 5); }, 0, "отказ в правке — без уведомления");
});
