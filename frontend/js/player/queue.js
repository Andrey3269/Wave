// Модель очереди воспроизведения (TARGET_ARCHITECTURE §11, находка M11). Без DOM: tests/js/queue.test.mjs.
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
import { trackKey } from "../core/api.js";

export class Queue {
  #items = [];
  #index = -1;
  #source = null;
  #continuation = null;
  #fromSource = new Set();          // ключи треков, уже взятых из источника (продолжение их не повторит)
  #random;

  /** Откуда играет очередь («Подобрано по вашей истории», название альбома...). */
  label = "";

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

  /** Новая очередь из треков источника (массив копируется). Текущий трек выбирает select(). */
  load(tracks, { label = "", source = null, continuation = null } = {}) {
    this.#items = tracks.slice();
    this.#index = -1;
    this.label = label;
    this.#source = source;
    this.#continuation = continuation;
    this.#fromSource = new Set(this.#items.map(trackKey));
  }

  /** Сделать трек i текущим. Возвращает его или null, если такого нет. */
  select(i) {
    const t = this.#items[i];
    if (!t) return null;
    this.#index = i;
    return t;
  }

  /** Следующая позиция: по порядку или случайная (кроме текущей); -1 — дальше ничего нет. */
  nextIndex({ shuffle = false } = {}) {
    const n = this.#items.length;
    if (!n) return -1;
    if (shuffle && n > 1) {
      let j;
      do { j = Math.floor(this.#random() * n); } while (j === this.#index);
      return j;
    }
    return this.#index + 1 < n ? this.#index + 1 : -1;
  }

  /** Предыдущая позиция или -1 (в начале очереди). */
  prevIndex() { return this.#index > 0 ? this.#index - 1 : -1; }

  /** Следующий по порядку трек (без учёта перемешивания) или null. */
  peekNext() { return this.#items[this.#index + 1] || null; }

  // ---------- правки пользователя: очередь становится собственной ----------
  #own() { this.#source = null; this.#continuation = null; }

  /** Добавить копию трека (один трек может стоять дважды): следующим или в конец. */
  add(track, next = false) {
    this.#own();
    const copy = { ...track };
    if (next && this.#index >= 0) this.#items.splice(this.#index + 1, 0, copy); else this.#items.push(copy);
    return copy;
  }

  /** Убрать трек i. Играющий трек не убирается. */
  remove(i) {
    if (i === this.#index || i < 0 || i >= this.#items.length) return false;
    this.#own();
    this.#items.splice(i, 1);
    if (i < this.#index) this.#index--;
    return true;
  }

  /** Поменять трек i с соседом (d = -1 выше, +1 ниже). Текущий трек остаётся текущим. */
  move(i, d) {
    const j = i + d;
    if (j < 0 || j >= this.#items.length) return false;
    this.#own();
    const cur = this.current;
    [this.#items[i], this.#items[j]] = [this.#items[j], this.#items[i]];
    if (cur) this.#index = this.#items.indexOf(cur);
    return true;
  }

  /** Перенести трек from на позицию to (индекс вставки до удаления). Играющий трек не прерывается. */
  moveTo(from, to) {
    const n = this.#items.length;
    if (from < 0 || from >= n || to < 0 || to > n || to === from || to === from + 1) return false;
    this.#own();
    const cur = this.current;
    const [item] = this.#items.splice(from, 1);
    this.#items.splice(to > from ? to - 1 : to, 0, item);
    if (cur) this.#index = this.#items.indexOf(cur);
    return true;
  }

  /** Оставить только играющий трек. */
  clear() {
    this.#own();
    const cur = this.current;
    this.#items = cur ? [cur] : [];
    this.#index = cur ? 0 : -1;
  }

  /** Дописать треки в конец (похожие треки после конца очереди). Возвращает позицию первого. */
  append(tracks) {
    this.#own();
    const from = this.#items.length;
    this.#items.push(...tracks);
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
    return fresh.length;
  }
}
