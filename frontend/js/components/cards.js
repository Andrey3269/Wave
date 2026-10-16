// Карточки: артисты, альбомы, плейлисты (кнопки), треки на главной и в «похожих», миксы, пункты боковой панели.
import { $$, mk, icon, button, coverEl } from "../core/dom.js";
import { fmtTime } from "../core/format.js";
import { trackKey } from "../core/api.js";
import { state, queue } from "../state/store.js";
import { play, togglePlay } from "../player/controller.js";
import { openMenu } from "./menu.js";

// ---------- карточки артистов, альбомов, плейлистов ----------
export const cardKey = (c) => `${c.source}:${c.kind}:${c.id}`;
/** list: имя списка, по нему обработчик (bindCardClicks) находит данные карточки. */
export function buildCard(c, i, list = "cards") {
  const b = mk("button", `card card--${c.kind}`);
  b.type = "button";
  b.dataset.index = i;
  b.dataset.list = list;
  if (c.kind === "new") {
    const plus = mk("span", "card__cv card__cv--plus");
    plus.append(icon("i-plus"));
    b.append(plus, mk("span", "card__title", "Новый плейлист"), mk("span", "card__sub", "Создать"));
    return b;
  }
  const sub = c.kind === "artist" ? (c.subtitle || "Артист")
    : c.kind === "playlist" ? `${c.count} тр.` : [c.artist, c.year].filter(Boolean).join(" · ");
  b.append(coverEl(c.cover, 320, "card__cv"), mk("span", "card__title", c.name || c.title), mk("span", "card__sub", sub));
  return b;
}
export const renderRow = (root, items, list) => root.replaceChildren(...items.map((c, i) => buildCard(c, i, list)));

/** Один обработчик на контейнер карточек. lists: { имя: () => массив }; onOpen(card) — что открыть. */
export function bindCardClicks(roots, lists, onOpen) {
  const onClick = (e) => {
    const node = e.target.closest(".card");
    if (!node) return;
    const c = (lists[node.dataset.list] || (() => []))()[Number(node.dataset.index)];
    if (c) onOpen(c);
  };
  roots.forEach((r) => r.addEventListener("click", onClick));
}

// ---------- карточки треков (главная, «похожие») ----------
export function trackCard(t, i) {
  const art = mk("article", "tcard");
  art.dataset.index = i;
  art.dataset.key = trackKey(t);
  const open = mk("button", "tcard__open");
  open.type = "button";
  open.setAttribute("aria-label", `Играть: ${t.artist} — ${t.title}`);
  const cv = coverEl(t.cover, 320, "tcard__cv");
  const ov = mk("span", "tcard__ov");
  ov.append(icon("i-play", "icon--fill ico-play"), icon("i-pause", "icon--fill ico-pause"));
  cv.append(ov);
  open.append(cv, mk("span", "tcard__title", t.title), mk("span", "tcard__sub", t.artist));
  const more = button("tbtn tcard__more", "Ещё", "i-more", true);
  more.setAttribute("aria-haspopup", "menu");
  art.append(open, more);
  return art;
}
export function miniCard(t, i) {
  const art = mk("article", "minicard");
  art.dataset.index = i;
  art.dataset.key = trackKey(t);
  const open = mk("button", "minicard__open");
  open.type = "button";
  open.setAttribute("aria-label", `Играть: ${t.artist} — ${t.title}`);
  const body = mk("span", "minicard__body");
  const bar = mk("span", "minibar");
  bar.append(mk("i"));
  body.append(mk("span", "minicard__title", t.title), mk("span", "minicard__sub", t.artist), bar);
  const time = mk("span", "minicard__time", t.duration ? fmtTime(t.duration) : "");
  const playIco = mk("span", "minicard__play");
  playIco.append(icon("i-play", "icon--fill ico-play"), icon("i-pause", "icon--fill ico-pause"));
  open.append(coverEl(t.cover, 120, "minicard__cv"), body, time, playIco);
  art.append(open);
  return art;
}
/** Один обработчик на ряд карточек: клик = играть (или пауза, если это текущий трек), «⋯» = меню. */
export function bindTrackGrid(root, getArr, getLabel) {
  root.addEventListener("click", (e) => {
    const card = e.target.closest("[data-index]");
    if (!card || !root.contains(card)) return;
    const arr = getArr(), i = Number(card.dataset.index), t = arr[i];
    if (!t) return;
    if (e.target.closest(".tcard__more")) { openMenu(t, e.target.closest("button")); return; }
    if (queue.isFrom(arr) && queue.current === t) togglePlay(); else play(arr, i, { label: getLabel() });
  });
}

// ---------- миксы ----------
export function mixCard(m) {
  const art = mk("article", "mixcard");
  art.dataset.mix = m.id;
  const open = mk("button", "mixcard__open");
  open.type = "button";
  open.setAttribute("aria-label", `Открыть микс: ${m.title}`);
  const cv = coverEl(m.cover, 320, "mixcard__cv");
  if (!m.cover) { cv.dataset.tone = m.tone || 0; cv.append(icon("i-wave")); }
  const body = mk("span", "mixcard__body");
  body.append(mk("span", "mixcard__title", m.title), mk("span", "mixcard__sub", m.sub));
  open.append(cv, body);
  const playBtn = button("playbtn mixcard__play", `Слушать: ${m.title}`, "i-play", true);
  art.append(open, playBtn);
  return art;
}
/** Пункт боковой панели («Твоя музыка», «Миксы по артистам»). */
export function sideItem({ cover, title, sub, tone, data }) {
  const li = mk("li");
  const b = mk("button", "mix");
  b.type = "button";
  Object.assign(b.dataset, data);
  const cv = coverEl(cover, 96, "mix__cv");
  if (!cover) { cv.dataset.tone = tone || 0; cv.append(icon("i-wave")); }
  const text = mk("span", "mix__t");
  text.append(mk("span", "mix__title", title), mk("span", "mix__sub", sub));
  b.append(cv, text, icon("i-chev-r", "mix__go"));
  b.title = title;
  li.append(b);
  return li;
}
/** Отметка микса, который играет или открыт, — везде, где показаны миксы. */
export function markMix() {
  $$("[data-mix]").forEach((n) => n.classList.toggle("is-active", n.dataset.mix === state.mixId || (state.view === "mix" && n.dataset.mix === state.ctx?.mix)));
}

/** Стрелки у горизонтальных рядов карточек (data-scroll="id:направление"). */
export function initRowScroll() {
  document.addEventListener("click", (e) => {
    const b = e.target.closest("[data-scroll]");
    if (!b) return;
    const [id, dir] = b.dataset.scroll.split(":");
    const row = document.getElementById(id);
    if (row) row.scrollBy({ left: Number(dir) * row.clientWidth * 0.85, behavior: "smooth" });
  });
  // S12: карточка, на которую фокус пришёл с клавиатуры, видна в ряду целиком. Chromium при обходе Tab не прокручивает
  // частично видимый элемент — часть карточки и её рамка фокуса оставались за краем ряда. Прокручиваем до точки привязки
  // (начало карточки, scroll-snap-align: start), иначе scroll-snap вернул бы ряд обратно.
  document.addEventListener("focusin", (e) => {
    const t = e.target;
    const row = t.closest?.(".scroller");
    if (!row || !t.matches(":focus-visible")) return;
    const cards = [...row.children], card = cards.find((c) => c.contains(t));
    if (!card) return;
    const q = row.getBoundingClientRect(), r = card.getBoundingClientRect();
    const right = r.right > q.right + 0.5, left = r.left < q.left - 0.5;
    if (!right && !left) return;
    const x = (c) => c.getBoundingClientRect().left - q.left + row.scrollLeft;     // начало карточки в координатах ряда
    const max = row.scrollWidth - row.clientWidth;
    let to;
    if (left) to = x(card);
    else {
      const need = row.scrollLeft + (r.right - q.right);                         // меньше нельзя: карточка не влезет
      to = cards.map(x).find((s) => s >= need - 0.5) ?? max;
    }
    row.scrollTo({ left: Math.min(Math.max(0, to), max) });
  });
}
