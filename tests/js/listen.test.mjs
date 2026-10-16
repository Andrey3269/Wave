// player/listen.js (S10): порог записи в историю min(30 с, 50 %), пропуск, перемотка, пауза, повтор.
import { test } from "node:test";
import assert from "node:assert/strict";
import { ListenTracker, playThreshold, THRESHOLD_MAX } from "../../frontend/js/player/listen.js";

/** Проигрывание: позиция растёт вместе со временем, timeupdate каждые 250 мс. */
function play(t, from, seconds, clock, opts = {}) {
  let hit = 0;
  const step = 0.25 * (opts.rate || 1);
  for (let p = from; p <= from + seconds + 1e-9; p += step) {
    if (t.progress(p, clock.now, opts)) hit++;
    clock.now += 250;
  }
  return hit;
}
const clock = () => ({ now: 1_000_000 });

test("порог: min(30 с, 50 % длительности); неизвестная или нулевая длительность — 30 с", () => {
  assert.equal(playThreshold(240), 30);
  assert.equal(playThreshold(60), 30);
  assert.equal(playThreshold(40), 20);
  assert.equal(playThreshold(6), 3);
  assert.equal(playThreshold(0), THRESHOLD_MAX);
  assert.equal(playThreshold(undefined), 30);
  assert.equal(playThreshold(NaN), 30);
  assert.equal(playThreshold(Infinity), 30, "поток без конечной длительности");
  assert.equal(playThreshold(-5), 30);
});

test("запись ровно один раз, когда прослушано столько, сколько требует порог", () => {
  const t = new ListenTracker(), c = clock();
  t.start("yt:a");
  assert.equal(play(t, 0, 2.5, c, { duration: 6 }), 0, "2,5 с из 6 — ещё не прослушивание");
  assert.equal(t.isSkip(), true, "прервать сейчас — пропуск");
  assert.equal(play(t, 2.75, 3, c, { duration: 6 }), 1, "порог 3 с достигнут — одна запись");
  assert.equal(t.recorded, true);
  assert.equal(t.isSkip(), false, "после порога переключение — не пропуск");
  assert.equal(play(t, 5.75, 0.25, c, { duration: 6 }), 0, "дальше записей нет");
});

test("длинный трек: порог 30 с, а не половина", () => {
  const t = new ListenTracker(), c = clock();
  t.start("sc:1");
  assert.equal(play(t, 0, 29.5, c, { duration: 300 }), 0);
  assert.equal(play(t, 29.75, 1, c, { duration: 300 }), 1);
});

test("неизвестная длительность: 30 с прослушивания", () => {
  const t = new ListenTracker(), c = clock();
  t.start("yt:b");
  assert.equal(play(t, 0, 29.5, c, { duration: 0 }), 0);
  assert.equal(play(t, 29.75, 1, c, { duration: NaN }), 1);
});

test("перемотка вперёд не обходит порог: засчитывается только проигранное", () => {
  const t = new ListenTracker(), c = clock();
  t.start("yt:c");
  play(t, 0, 1, c, { duration: 60 });
  t.seeking();
  assert.equal(play(t, 50, 9, c, { duration: 60 }), 0, "1 + 9 с проигрыша < 30 с, хотя позиция дошла до конца");
  assert.ok(t.listened < 11);
  assert.equal(t.isSkip(), true);
});

test("скачок позиции без события seeking (например, восстановление потока) тоже не засчитывается", () => {
  const t = new ListenTracker(), c = clock();
  t.start("yt:d");
  t.progress(0, c.now, { duration: 100 }); c.now += 250;
  t.progress(40, c.now, { duration: 100 }); c.now += 250;
  assert.ok(t.listened < 1);
});

test("перемотка назад и пауза не дают повторной записи", () => {
  const t = new ListenTracker(), c = clock();
  t.start("yt:e");
  assert.equal(play(t, 0, 4, c, { duration: 6 }), 1);
  t.seeking();
  assert.equal(play(t, 0, 4, c, { duration: 6 }), 0, "прослушали ещё раз после перемотки назад — та же запись");
  c.now += 60_000;                                            // пауза минуту
  assert.equal(play(t, 4.25, 1, c, { duration: 6 }), 0);
});

test("пауза до порога: время на паузе не засчитывается, после продолжения отсчёт идёт дальше", () => {
  const t = new ListenTracker(), c = clock();
  t.start("yt:f");
  play(t, 0, 1.5, c, { duration: 6 });
  c.now += 120_000;
  assert.ok(t.listened < 2);
  assert.equal(play(t, 1.75, 1.5, c, { duration: 6 }), 1);
});

test("ускоренное воспроизведение засчитывает проигранное время трека", () => {
  const t = new ListenTracker(), c = clock();
  t.start("yt:g");
  assert.equal(play(t, 0, 31, c, { duration: 300, rate: 2 }), 1);
});

test("повтор трека — новый запуск: свой порог и не больше одной записи за проигрыш", () => {
  const t = new ListenTracker(), c = clock();
  t.start("yt:h");
  assert.equal(play(t, 0, 6, c, { duration: 6 }), 1);
  t.start("yt:h");                                            // repeat one: тот же трек сначала
  assert.equal(t.recorded, false);
  assert.equal(play(t, 0, 2, c, { duration: 6 }), 0, "второй проигрыш ещё не дошёл до порога");
  assert.equal(play(t, 2.25, 4, c, { duration: 6 }), 1, "второй проигрыш — вторая запись");
});

test("пропуск: только после начала звука и до порога; без запуска и после stop — нет", () => {
  const t = new ListenTracker(), c = clock();
  assert.equal(t.isSkip(), false, "ничего не запущено");
  t.start("yt:i");
  assert.equal(t.isSkip(), false, "звук ещё не пошёл (загрузка)");
  t.progress(0, c.now, { duration: 100 }); c.now += 250;
  assert.equal(t.isSkip(), false, "позиция 0 — звук ещё не пошёл");
  t.progress(0.25, c.now, { duration: 100 });
  assert.equal(t.isSkip(), true);
  t.stop();
  assert.equal(t.isSkip(), false, "запуск закончился (ошибка или доиграл)");
  assert.equal(t.progress(1, c.now + 250, { duration: 100 }), false);
});

test("длительность уточнилась во время игры (метаданные → реальная из <audio>)", () => {
  const t = new ListenTracker(), c = clock();
  t.start("yt:j");
  assert.equal(play(t, 0, 4, c, { duration: 200 }), 0, "по метаданным порог 30 с");
  assert.equal(t.progress(4.25, c.now, { duration: 8 }), true, "реальная длительность 8 с: порог 4 с уже пройден");
});
