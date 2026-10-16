// Клавиатурная навигация по группе пунктов (меню трека, вкладки): какой пункт получит фокус. Без DOM (S12).
//
// Правило одно для всех групп:
//  - стрелки ходят по кругу по ВСЕМ пунктам, включая недоступные: их видно, скринридер называет их недоступными,
//    но действие не выполнится (так делает и WAI-ARIA APG);
//  - Home и End — первый и последний ДОСТУПНЫЙ пункт (если доступных нет — первый и последний вообще);
//  - открытие меню ставит фокус на первый доступный пункт (firstEnabled).

const KEYS = {
  vertical: { next: "ArrowDown", prev: "ArrowUp" },
  horizontal: { next: "ArrowRight", prev: "ArrowLeft" },
};

/** Индекс первого доступного пункта; если доступных нет — 0 (или -1 для пустой группы). */
export function firstEnabled(enabled) {
  if (!enabled.length) return -1;
  const i = enabled.indexOf(true);
  return i < 0 ? 0 : i;
}

/** Индекс последнего доступного пункта; если доступных нет — последний (или -1 для пустой группы). */
export function lastEnabled(enabled) {
  if (!enabled.length) return -1;
  const i = enabled.lastIndexOf(true);
  return i < 0 ? enabled.length - 1 : i;
}

/**
 * enabled — флаги «пункт доступен» по порядку; current — индекс пункта в фокусе (-1, если фокус не на пункте);
 * key — KeyboardEvent.key; orientation — "vertical" (меню) или "horizontal" (вкладки).
 * Возвращает индекс пункта, которому передать фокус, или -1, если клавиша к навигации не относится.
 */
export function navTarget(enabled, current, key, orientation = "vertical") {
  const n = enabled.length;
  if (!n) return -1;
  const k = KEYS[orientation] || KEYS.vertical;
  if (key === "Home") return firstEnabled(enabled);
  if (key === "End") return lastEnabled(enabled);
  if (key === k.next) return current < 0 ? 0 : (current + 1) % n;
  if (key === k.prev) return current < 0 ? n - 1 : (current - 1 + n) % n;
  return -1;
}
