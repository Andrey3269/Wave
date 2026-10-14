// Короткие уведомления внизу экрана: одно сообщение за раз, новое заменяет прежнее.
import { el } from "../core/elements.js";

let toastTimer = 0;
export function toast(text, ms = 3600) {
  el.toast.textContent = text;
  el.toast.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(hideToast, ms);
}
export function hideToast() { clearTimeout(toastTimer); el.toast.hidden = true; }
