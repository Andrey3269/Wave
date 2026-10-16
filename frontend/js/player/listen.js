// Учёт прослушивания трека (S10): когда запуск считается прослушиванием (запись в историю) и когда — пропуском.
// Без DOM: tests/js/listen.test.mjs.
//
// Прослушивание — накопленное ФАКТИЧЕСКИ проигранное время, а не позиция: перемотка вперёд порог не обходит,
// перемотка назад и пауза не дают второй записи. Приращение позиции засчитывается, только если оно не больше
// прошедшего времени (с учётом скорости воспроизведения) плюс 1 с; после события seeking отсчёт начинается
// заново с новой позиции.
//
// Порог: min(30 с, 50 % длительности). Длительность — реальная из <audio>, пока её нет — из метаданных трека.
// Если неизвестны обе (поток без конечной длительности), порог — 30 с прослушивания.
// Один запуск (start) даёт не больше одной записи. Повтор трека (repeat one) — новый запуск со своим порогом.
// Пропуск — запуск прерван пользователем после начала звука, но до порога.

export const THRESHOLD_MAX = 30;            // с
export const THRESHOLD_SHARE = 0.5;

/** Порог прослушивания для трека длительностью duration (с); неизвестная или нулевая — 30 с. */
export function playThreshold(duration) {
  return Number.isFinite(duration) && duration > 0 ? Math.min(THRESHOLD_MAX, duration * THRESHOLD_SHARE) : THRESHOLD_MAX;
}

export class ListenTracker {
  #key = null;
  #listened = 0;
  #pos = null;                               // последняя учтённая позиция
  #at = 0;                                   // и момент (мс), когда она получена
  #recorded = false;

  /** Новый запуск трека key. */
  start(key) {
    this.#key = key;
    this.#listened = 0;
    this.#pos = null;
    this.#recorded = false;
  }

  /** Запуск закончился (доиграл, ошибка, заменён): дальше ничего не засчитывается до нового start(). */
  stop() { this.#key = null; this.#pos = null; }

  get key() { return this.#key; }
  get listened() { return this.#listened; }
  get recorded() { return this.#recorded; }

  /** Перемотка: следующая позиция — новая точка отсчёта. */
  seeking() { this.#pos = null; }

  /**
   * Позиция воспроизведения pos (с) в момент now (мс). Возвращает true ровно один раз за запуск — когда
   * прослушанное впервые достигло порога: пора записать прослушивание.
   */
  progress(pos, now, { rate = 1, duration = 0 } = {}) {
    if (this.#key == null || !Number.isFinite(pos)) return false;
    if (this.#pos != null) {
      const delta = pos - this.#pos;
      const wall = Math.max(0, now - this.#at) / 1000;
      if (delta > 0 && delta <= wall * Math.max(rate, 0.1) + 1) this.#listened += delta;
    }
    this.#pos = pos;
    this.#at = now;
    if (!this.#recorded && this.#listened >= playThreshold(duration)) {
      this.#recorded = true;
      return true;
    }
    return false;
  }

  /** Прервать запуск сейчас — это пропуск? (звук начался, порог не достигнут). */
  isSkip() { return this.#key != null && this.#listened > 0 && !this.#recorded; }
}
