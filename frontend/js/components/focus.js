// Фокус клавиатуры (S12): можно ли его передать элементу и куда перенести, если элемент, на котором он был,
// скрылся или исчез (смена экрана, перерисовка списка, закрытое меню). Куда — решает приложение (setFocusFallback:
// заголовок текущего экрана), чтобы этот модуль не зависел от экранов.

let fallback = () => null;
let last = null;                                         // последний элемент, получивший фокус
let keyboard = false;                                    // последнее действие пользователя — клавиша, а не мышь/касание

/** fn() → элемент, на который переносить потерянный фокус (обычно заголовок открытого экрана). */
export function setFocusFallback(fn) { fallback = fn; }

/** Элемент на странице, видим, не отключён и не внутри inert — фокус на нём имеет смысл. */
export function canFocus(node) {
  if (!node || !node.isConnected || node.disabled || typeof node.focus !== "function") return false;
  if (node.closest("[inert]")) return false;
  const visible = typeof node.checkVisibility === "function" ? node.checkVisibility() : node.getClientRects().length > 0;
  return visible && getComputedStyle(node).visibility !== "hidden";
}

/** Фокус на node, если на нём фокус имеет смысл. true — фокус теперь там. */
export function focusSafely(node) {
  if (!canFocus(node)) return false;
  node.focus({ preventScroll: true });
  return document.activeElement === node;
}

/** Фокуса нет (он на body) или он на скрытом элементе. */
export function focusLost() {
  const a = document.activeElement;
  return !a || a === document.body || !canFocus(a);
}

/**
 * Пользователь работает с клавиатуры (последнее действие — нажатие клавиши, не мышь и не касание). Перенос фокуса
 * после перерисовки (меню, очередь, «Загрузить ещё») нужен только ему: после клика мышью фокус, поставленный на
 * соседнюю кнопку, перехватил бы пробел (пауза/пуск) — он нажал бы эту кнопку. Мышью — поведение как до S12.
 */
export function keyboardMode() { return keyboard; }

/**
 * Фокус остался на скрытом элементе (кнопку нажали мышью, и она скрылась вместе с меню или панелью) — снимаем его.
 * Иначе до того, как браузер сам уберёт фокус (Chromium на Windows делает это не сразу), пробел достаётся невидимой
 * кнопке, а не переключает паузу.
 */
export function dropLostFocus() {
  const a = document.activeElement;
  if (a && a !== document.body && !canFocus(a)) a.blur();
}

/** Переносит потерянный фокус на заголовок текущего экрана. */
export function rescueFocus() { return focusSafely(fallback()); }

/**
 * После смены экрана (views/shell.js: layout): если элемент с фокусом скрылся или исчез, фокус переходит на заголовок
 * открытого экрана, а не остаётся в скрытом DOM. Если пользователь сам убрал фокус (клик в пустое место) или фокуса
 * ещё не было (запуск), ничего не делаем. Проверка — после синхронных перерисовок того же действия.
 */
export function keepFocus() {
  queueMicrotask(() => {
    if (!focusLost()) return;
    if (!last || canFocus(last)) return;
    rescueFocus();
  });
}

export function initFocusKeeper() {
  document.addEventListener("focusin", (e) => { last = e.target; });
  document.addEventListener("keydown", () => { keyboard = true; }, true);
  document.addEventListener("pointerdown", () => { keyboard = false; }, true);
  // Нажатие без указателя (Enter/пробел на кнопке, «нажатие» из экранного диктора или голосового управления) приходит
  // как click с detail 0 без keydown и pointerdown: это не мышь — фокус после действия переносится, как с клавиатуры
  document.addEventListener("click", (e) => { if (e.detail === 0) keyboard = true; }, true);
}
