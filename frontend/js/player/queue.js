// Модель очереди воспроизведения (TARGET_ARCHITECTURE §11, находки M11, M14). Без DOM: tests/js/queue.test.mjs.
//
// У очереди СОБСТВЕННЫЙ массив. Раньше очередь была тем же массивом, что и список на экране: сортировка
// списка переставляла очередь, а подгрузка следующей страницы дописывала её. Теперь список экрана только
// источник: load() копирует треки, а продолжение из того же источника приходит явным вызовом extend()
// через continuation: () => Promise<Track[]>. Никакие изменения массива источника очередь не меняют.
//
// Происхождение (source) — сам массив, из которого загружена очередь; по нему экран узнаёт «очередь играет
// мой список» (клик по играющей строке = пауза). Любая правка пользователя (добавить, убрать, переставить,
// очистить) или дописанные похожие треки делают очередь собственной: связь с источником и продолжение
// снимаются. Так было и раньше (первая правка делала копию массива), это поведение сохранено.
//
// S10: перемешивание без повторов, история для «Назад», повтор off | one | all.
// - У каждой позиции очереди свой внутренний номер (uid): один трек может стоять дважды, а перестановки
//   не ломают историю и цикл перемешивания.
// - История — uid в фактическом порядке воспроизведения и курсор на текущем. «Назад» при перемешивании идёт
//   по истории; «Вперёд» после «Назад» сначала проходит то, что уже играло дальше («будущее» истории), и только
//   потом выбирает новый трек. Прямой выбор трека обрезает «будущее».
// - Цикл перемешивания — uid, уже сыгранные в текущем круге. Следующий трек выбирается из несыгранных; когда
//   несыгранных нет: repeat "all" начинает новый круг (без только что игравшего трека, если есть другие),
//   иначе очередь закончилась (-1).
// - Случайный выбор запоминается, пока он остаётся допустимым: nextIndex() и peekNext() (прогрев, «Далее»)
//   отвечают одинаково, пока очередь не сменила текущий трек.
// - repeat "one" — повтор текущего трека только при естественном окончании (auto); кнопка «Следующий»
//   переключает трек как обычно.
import { trackKey } from "../core/api.js";

/** Режимы повтора в порядке переключения кнопкой: выкл → вся очередь → текущий трек → выкл. */
export const REPEAT_MODES = Object.freeze(["off", "all", "one"]);
/** Следующий режим повтора по кнопке. */
export const nextRepeat = (mode) => REPEAT_MODES[(REPEAT_MODES.indexOf(normRepeat(mode)) + 1) % REPEAT_MODES.length];
/** Режим повтора из любого значения: неизвестное — "off" (старое булево true — повтор трека, как кнопка S9). */
export function normRepeat(v) {
  if (v === true) return "one";
  return REPEAT_MODES.includes(v) ? v : "off";
}

const HISTORY_MAX = 500;                 // сколько позиций помнит история «Назад»

export class Queue {
  #items = [];
  #uids = [];                            // uid позиции, параллельно #items
  #seq = 0;
  #index = -1;
  #source = null;
  #continuation = null;
  #fromSource = new Set();               // ключи треков, уже взятых из источника (продолжение их не повторит)
  #history = [];                         // uid в порядке воспроизведения
  #cursor = -1;                          // место текущего трека в #history
  #cycle = new Set();                    // uid, сыгранные в текущем круге перемешивания
  #planned = null;                       // { from, uid } — запомненный случайный выбор после трека from
  #step = null;                          // { uid, c } — шаг по истории, который предложили nextIndex/prevIndex
  #random;

  /** Откуда играет очередь («Подобрано по вашей истории», название альбома...). */
  label = "";

  /** Вызывается после любого изменения очереди или текущего трека (сохранение между запусками, S10). */
  onChange = null;

  /** random — источник случайности для перемешивания (в тестах — предсказуемый). */
  constructor({ random = Math.random } = {}) { this.#random = random; }

  /** Копия треков очереди: изменить очередь можно только её методами. */
  get tracks() { return this.#items.slice(); }
  get length() { return this.#items.length; }
  get index() { return this.#index; }
  get current() { return this.#items[this.#index] || null; }
  at(i) { return this.#items[i] || null; }

  /** Очередь загружена из этого источника и пользователь её ещё не правил. */
  isFrom(source) { return source != null && this.#source === source; }

  #uid() { return ++this.#seq; }
  #changed() { if (this.onChange) this.onChange(); }
  #pos(uid) { return this.#uids.indexOf(uid); }
  get #curUid() { return this.#uids[this.#index]; }

  /** Новая очередь из треков источника (массив копируется). Текущий трек выбирает select(). */
  load(tracks, { label = "", source = null, continuation = null } = {}) {
    this.#items = tracks.slice();
    this.#uids = this.#items.map(() => this.#uid());
    this.#index = -1;
    this.label = label;
    this.#source = source;
    this.#continuation = continuation;
    this.#fromSource = new Set(this.#items.map(trackKey));
    this.#history = [];
    this.#cursor = -1;
    this.#cycle = new Set();
    this.#planned = null;
    this.#step = null;
    this.#changed();
  }

  /**
   * Сделать трек i текущим. Возвращает его или null, если такого нет.
   * История: если это шаг по истории, который только что предложили nextIndex()/prevIndex() («Вперёд» по уже
   * сыгранному или «Назад»), курсор переходит на эту запись. Любой другой выбор — прямой: «будущее» истории
   * обрезается, трек дописывается. Трек отмечается сыгранным в круге перемешивания; если круг был уже
   * сыгран целиком, этот трек открывает новый.
   */
  select(i) {
    const t = this.#items[i];
    if (!t) return null;
    const uid = this.#uids[i], step = this.#step;
    this.#step = null;
    if (step && step.uid === uid && this.#history[step.c] === uid) this.#cursor = step.c;
    else if (this.#history[this.#cursor] !== uid) {
      this.#history.length = this.#cursor + 1;
      this.#history.push(uid);
      if (this.#history.length > HISTORY_MAX) this.#history.shift();
      this.#cursor = this.#history.length - 1;
    }
    if (this.#cycleFull()) this.#cycle.clear();          // круг уже сыгран целиком: этот трек открывает новый
    this.#cycle.add(uid);
    this.#index = i;
    this.#changed();
    return t;
  }

  /** Новый круг перемешивания с текущего трека (включили перемешивание): уже сыгранным считается только он. */
  restartCycle() {
    this.#cycle = new Set(this.#curUid != null ? [this.#curUid] : []);
    this.#planned = null;
    this.#changed();
  }

  #cycleFull() { return this.#uids.length > 0 && this.#uids.every((u) => this.#cycle.has(u)); }

  /** Ближайшая позиция истории в направлении dir (+1 вперёд, -1 назад) от курсора, которая ещё в очереди.
   *  Запоминается как предложенный шаг: select() этой позиции сдвинет курсор, а не допишет историю. */
  #historyStep(dir) {
    for (let c = this.#cursor + dir; c >= 0 && c < this.#history.length; c += dir) {
      const i = this.#pos(this.#history[c]);
      if (i >= 0 && i !== this.#index) { this.#step = { uid: this.#history[c], c }; return i; }
    }
    return -1;
  }

  /** Случайный следующий трек при перемешивании: из несыгранных в круге; круг сыгран — новый (repeat "all"). */
  #shuffleNext(repeat) {
    const cur = this.#curUid;
    let pool = this.#uids.filter((u) => u !== cur && !this.#cycle.has(u));
    if (!pool.length) {
      if (repeat !== "all") return -1;
      pool = this.#uids.filter((u) => u !== cur);
      if (!pool.length) return this.#index;                          // единственный трек по кругу
    }
    const p = this.#planned;
    if (p && p.from === cur && pool.includes(p.uid)) return this.#pos(p.uid);
    const uid = pool[Math.min(pool.length - 1, Math.floor(this.#random() * pool.length))];
    this.#planned = { from: cur, uid };
    return this.#pos(uid);
  }

  /**
   * Следующая позиция или -1 (дальше ничего нет).
   * shuffle — перемешивание; repeat — "off" | "one" | "all"; auto — трек доиграл сам (только тогда repeat
   * "one" повторяет текущий).
   */
  nextIndex({ shuffle = false, repeat = "off", auto = false } = {}) {
    this.#step = null;                                   // шаг по истории — только из последнего предложения
    const n = this.#items.length;
    if (!n) return -1;
    repeat = normRepeat(repeat);
    if (auto && repeat === "one" && this.#index >= 0) return this.#index;
    if (shuffle && this.#index >= 0) {
      const fwd = this.#historyStep(1);
      return fwd >= 0 ? fwd : this.#shuffleNext(repeat);
    }
    if (this.#index + 1 < n) return this.#index + 1;
    return repeat === "all" ? 0 : -1;
  }

  /** Предыдущая позиция или -1. При перемешивании — по истории воспроизведения, иначе по порядку очереди. */
  prevIndex({ shuffle = false } = {}) {
    this.#step = null;
    if (shuffle) return this.#historyStep(-1);
    return this.#index > 0 ? this.#index - 1 : -1;
  }

  /** Трек, который будет следующим при этих режимах (без смены текущего), или null. Без аргументов — следующий по порядку. */
  peekNext(modes = {}) {
    const j = this.nextIndex(modes);
    return j >= 0 && j !== this.#index ? this.#items[j] : null;
  }

  // ---------- правки пользователя: очередь становится собственной ----------
  #own() { this.#source = null; this.#continuation = null; }

  /** Добавить копию трека (один трек может стоять дважды): следующим или в конец. */
  add(track, next = false) {
    this.#own();
    const copy = { ...track };
    const at = next && this.#index >= 0 ? this.#index + 1 : this.#items.length;
    this.#items.splice(at, 0, copy);
    this.#uids.splice(at, 0, this.#uid());
    this.#changed();
    return copy;
  }

  /** Убрать трек i. Играющий трек не убирается. История и круг перемешивания забывают эту позицию. */
  remove(i) {
    if (i === this.#index || i < 0 || i >= this.#items.length) return false;
    this.#own();
    const [uid] = this.#uids.splice(i, 1);
    this.#items.splice(i, 1);
    if (i < this.#index) this.#index--;
    this.#forget(new Set([uid]));
    this.#changed();
    return true;
  }

  /** Убрать из истории uid, которых больше нет; соседние одинаковые записи схлопнуть; курсор — на текущем. */
  #forget(gone) {
    for (const u of gone) this.#cycle.delete(u);
    if (this.#planned && gone.has(this.#planned.uid)) this.#planned = null;
    const cur = this.#history[this.#cursor];
    const kept = [];
    let cursor = -1;
    this.#history.forEach((u, c) => {
      if (gone.has(u)) return;
      if (kept[kept.length - 1] !== u) kept.push(u);
      if (c === this.#cursor && u === cur) cursor = kept.length - 1;
    });
    this.#history = kept;
    this.#cursor = cursor >= 0 ? cursor : kept.lastIndexOf(this.#curUid);
    this.#step = null;
  }

  #reorder(fn) {
    const cur = this.#curUid;
    const pairs = this.#items.map((t, k) => [t, this.#uids[k]]);
    fn(pairs);
    this.#items = pairs.map((p) => p[0]);
    this.#uids = pairs.map((p) => p[1]);
    if (cur != null) this.#index = this.#pos(cur);
    this.#changed();
  }

  /** Поменять трек i с соседом (d = -1 выше, +1 ниже). Текущий трек остаётся текущим. */
  move(i, d) {
    const j = i + d;
    if (i < 0 || i >= this.#items.length || j < 0 || j >= this.#items.length) return false;
    this.#own();
    this.#reorder((a) => { [a[i], a[j]] = [a[j], a[i]]; });
    return true;
  }

  /** Перенести трек from на позицию to (индекс вставки до удаления). Играющий трек не прерывается. */
  moveTo(from, to) {
    const n = this.#items.length;
    if (from < 0 || from >= n || to < 0 || to > n || to === from || to === from + 1) return false;
    this.#own();
    this.#reorder((a) => { const [item] = a.splice(from, 1); a.splice(to > from ? to - 1 : to, 0, item); });
    return true;
  }

  /** Оставить только играющий трек. */
  clear() {
    this.#own();
    const cur = this.current, uid = this.#curUid;
    this.#items = cur ? [cur] : [];
    this.#uids = cur ? [uid] : [];
    this.#index = cur ? 0 : -1;
    this.#history = cur ? [uid] : [];
    this.#cursor = cur ? 0 : -1;
    this.#cycle = new Set(cur ? [uid] : []);
    this.#planned = null;
    this.#step = null;
    this.#changed();
  }

  /** Дописать треки в конец (похожие треки после конца очереди). Возвращает позицию первого. */
  append(tracks) {
    this.#own();
    const from = this.#items.length;
    this.#items.push(...tracks);
    this.#uids.push(...tracks.map(() => this.#uid()));
    this.#changed();
    return from;
  }

  /**
   * Продолжение из источника: спрашивает continuation() и дописывает треки, которых из этого источника
   * ещё не брали. Возвращает число добавленных. Если очередь за время запроса заменили или поправили, ничего
   * не добавляет.
   */
  async extend() {
    const ask = this.#continuation;
    if (!ask) return 0;
    const more = (await ask()) || [];
    if (this.#continuation !== ask) return 0;
    const fresh = more.filter((t) => !this.#fromSource.has(trackKey(t)));
    for (const t of fresh) this.#fromSource.add(trackKey(t));
    this.#items.push(...fresh);
    this.#uids.push(...fresh.map(() => this.#uid()));
    if (fresh.length) this.#changed();
    return fresh.length;
  }

  // ---------- сохранение между запусками (S10) ----------
  /**
   * Снимок для сохранения: треки, текущая позиция, подпись, история «Назад» и круг перемешивания — номерами
   * позиций. Длинная очередь сохраняется окном из max треков вокруг текущего.
   */
  serialize({ max = 500 } = {}) {
    const n = this.#items.length;
    const start = n > max ? Math.max(0, Math.min(this.#index - 100, n - max)) : 0;
    const end = Math.min(n, start + max);
    const win = new Map(this.#uids.slice(start, end).map((u, k) => [u, k]));
    const curUid = this.#curUid;
    const history = [], hist = [];
    this.#history.forEach((u, c) => { if (win.has(u)) { history.push(win.get(u)); hist.push(c); } });
    return {
      tracks: this.#items.slice(start, end),
      index: this.#index >= start && this.#index < end ? this.#index - start : -1,
      label: this.label,
      history,
      cursor: hist.indexOf(this.#cursor) >= 0 ? hist.indexOf(this.#cursor) : history.lastIndexOf(win.get(curUid) ?? -1),
      cycle: [...this.#cycle].filter((u) => win.has(u)).map((u) => win.get(u)),
    };
  }

  /**
   * Восстановление из снимка serialize() (данные уже проверены backend; здесь — защита от несогласованных
   * номеров). Очередь собственная: источника и продолжения у неё нет. Возвращает false, если снимок негоден.
   */
  restore(snap) {
    const tracks = snap && Array.isArray(snap.tracks) ? snap.tracks : null;
    if (!tracks || !tracks.length) return false;
    const n = tracks.length;
    const ok = (k) => Number.isInteger(k) && k >= 0 && k < n;
    this.load(tracks, { label: typeof snap.label === "string" ? snap.label : "" });
    this.#index = ok(snap.index) ? snap.index : -1;
    const history = (Array.isArray(snap.history) ? snap.history : []).filter(ok).map((k) => this.#uids[k]);
    this.#history = history.filter((u, c) => history[c - 1] !== u).slice(-HISTORY_MAX);
    const cur = this.#curUid;
    const c = Number.isInteger(snap.cursor) && this.#history[snap.cursor] === cur ? snap.cursor : this.#history.lastIndexOf(cur);
    if (cur != null && c < 0) { this.#history.push(cur); this.#cursor = this.#history.length - 1; } else this.#cursor = c;
    this.#cycle = new Set((Array.isArray(snap.cycle) ? snap.cycle : []).filter(ok).map((k) => this.#uids[k]));
    if (cur != null) this.#cycle.add(cur);
    this.#changed();
    return true;
  }
}
