// DOM-утилиты: поиск узлов, создание элементов и иконок, обложки с запасным вариантом.
import { thumb } from "./format.js";

const SVG_NS = "http://www.w3.org/2000/svg";

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

/** Элемент с классом и текстом (textContent: безопасно от XSS). */
export const mk = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
};

/** Иконка из SVG-спрайта в index.html. */
export function icon(id, cls = "") {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("class", `icon ${cls}`.trim());
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS(SVG_NS, "use");
  use.setAttribute("href", `#${id}`);
  svg.append(use);
  return svg;
}

export function button(cls, label, ico, fill = false) {
  const b = mk("button", cls);
  b.type = "button";
  b.title = label;
  b.setAttribute("aria-label", label);
  if (ico) b.append(icon(ico, fill ? "icon--fill" : ""));
  return b;
}

// ---------- Обложки: нужного размера, с запасным вариантом ----------
export function setCover(img, url, px) {
  if (!img) return;
  const src = url ? thumb(url, px) : "";
  if (img._src === src) return;                       // тот же адрес: не перезагружаем картинку
  img._src = src;
  if (!src) { img.hidden = true; img.removeAttribute("src"); return; }
  img.hidden = false;
  img.referrerPolicy = "no-referrer";
  img.src = src;
}

/** <span class="cv"><img></span>: если обложки нет или она не загрузилась, виден фон-заглушка. */
export function coverEl(url, px, cls = "") {
  const box = mk("span", `cv ${cls}`.trim());
  const img = document.createElement("img");
  img.alt = ""; img.loading = "lazy"; img.decoding = "async";
  setCover(img, url, px);
  box.append(img);
  return box;
}

/** Картинка не загрузилась: обложку прячем (виден фон), вместо логотипа — текстовый вариант.
 *  Ошибка загрузки картинки не всплывает, поэтому слушаем в фазе перехвата. */
export function initImageFallback() {
  document.addEventListener("error", (e) => {
    const img = e.target;
    if (!(img instanceof HTMLImageElement)) return;
    if (img.closest(".brand")) {                          // логотип не найден: текстовый вариант
      const fb = mk("span", `brand__fallback ${img.className}`);
      fb.append(icon("i-wave"), mk("span", null, "ВОЛНА"));
      img.replaceWith(fb);
      return;
    }
    img.hidden = true;
  }, true);
}
