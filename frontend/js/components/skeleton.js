// Состояния загрузки (скелетоны) и блок «пусто / ошибка» с кнопкой «Повторить».
import { $, mk } from "../core/dom.js";

/** Блок «пусто / ошибка» с необязательной кнопкой «Повторить». */
export function setBlock(root, text, retry) {
  $(".empty__t", root).textContent = text || "";
  $(".empty__retry", root).hidden = !retry;
  root._retry = retry || null;
  root.hidden = !text;
}
/** «Повторить» в любом блоке .empty вызывает то, что передали в setBlock. */
export function initEmptyRetry() {
  document.addEventListener("click", (e) => {
    const b = e.target.closest(".empty__retry");
    if (b) b.closest(".empty")?._retry?.();
  });
}

// ---------- «Загрузить ещё» (S12: #more и #home-more — настоящие <button>) ----------
// Кнопка — и цель IntersectionObserver (подгрузка при прокрутке), и ручная подгрузка или повтор после ошибки.
// idle — есть ещё; loading — запрос идёт: aria-busy и aria-disabled, а не disabled, чтобы фокус не пропадал (второго
// запроса не будет: его не пустит loadMore); error — не удалось, нажатие повторяет; none — больше нечего, кнопки нет.
const MORE_TEXT = { idle: "Загрузить ещё", error: "Не удалось загрузить. Нажми, чтобы повторить" };
export function setMore(btn, mode, loadingText = "Загружаем…") {
  const busy = mode === "loading";
  btn.hidden = mode === "none";
  btn.dataset.state = mode;
  btn.textContent = busy ? loadingText : (MORE_TEXT[mode] || MORE_TEXT.idle);
  if (busy) { btn.setAttribute("aria-busy", "true"); btn.setAttribute("aria-disabled", "true"); }
  else { btn.removeAttribute("aria-busy"); btn.removeAttribute("aria-disabled"); }
}

// ---------- Скелетоны (состояние загрузки) ----------
const skelRow = () => {
  const li = mk("li", "row row--skel");
  const lines = mk("span", "skel__lines");
  lines.append(mk("span", "skel skel--l1"), mk("span", "skel skel--l2"));
  li.append(mk("span", "skel skel--cv"), lines);
  return li;
};
const skelCard = () => {
  const d = mk("div", "card card--skel");
  d.append(mk("span", "skel skel--sq"), mk("span", "skel skel--l1"), mk("span", "skel skel--l2"));
  return d;
};
export const skelRows = (n = 8) => Array.from({ length: n }, skelRow);
export const skelCards = (n = 6) => Array.from({ length: n }, skelCard);
