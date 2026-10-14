// Минимальные синхронные события: обработчики вызываются по порядку подписки, в том же такте.
// Плеер сообщает о своём состоянии событиями, а не вызывает экраны напрямую — так модули не зависят друг
// от друга по кругу (TARGET_ARCHITECTURE §11).

export class Emitter {
  #handlers = new Map();

  /** Подписка; возвращает функцию отписки. */
  on(type, fn) {
    if (!this.#handlers.has(type)) this.#handlers.set(type, []);
    this.#handlers.get(type).push(fn);
    return () => {
      const list = this.#handlers.get(type) || [];
      const i = list.indexOf(fn);
      if (i >= 0) list.splice(i, 1);
    };
  }

  /** Вызывает обработчики type по порядку подписки. Список копируется: отписка внутри обработчика безопасна. */
  emit(type, ...args) {
    for (const fn of [...(this.#handlers.get(type) || [])]) fn(...args);
  }
}
