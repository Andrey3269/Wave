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
