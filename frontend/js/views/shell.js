// Каркас: какой раздел главной области виден, активный пункт меню, вкладки поиска и переключатель источника.
import { $$ } from "../core/dom.js";
import { el, views, hero, now } from "../core/elements.js";
import { state, current, DETAIL_VIEWS, sectionOf, isCards } from "../state/store.js";
import { updateSort } from "./list.js";
import { closeFs } from "./fullscreen.js";
import { setFocusFallback, keepFocus } from "../components/focus.js";

const NAV_OF = { home: "home", search: "search", playlists: "playlists", playlist: "playlists", collection: "collection",
  liked: "liked", downloads: "downloads", history: "history", settings: "settings" };

/** Показывает нужные блоки для state.view. Мини-плеер скрывает CSS по body[data-view] и body[data-track]. */
export function layout() {
  const v = state.view, sec = sectionOf(v);
  Object.entries(views).forEach(([k, n]) => { n.hidden = k !== sec; });
  el.detail.hidden = !DETAIL_VIEWS.includes(v);
  el.tabs.hidden = !(v === "search" && state.query);
  el.dRename.hidden = el.dDelete.hidden = v !== "playlist";
  el.albumsSec.hidden = !(v === "artist" && state.albums.length);
  el.relatedSec.hidden = !(v === "artist" && state.related.length);
  el.clearHistory.hidden = v !== "history";
  const showCards = isCards() || v === "playlists";
  el.cards.hidden = !showCards;
  el.tracks.hidden = showCards;

  if (v in NAV_OF) state.nav = NAV_OF[v];
  else if (v === "mix") state.nav = null;
  $$(".nav__item", el.nav.closest(".side")).forEach((n) => {
    if (n.dataset.view === state.nav) n.setAttribute("aria-current", "page"); else n.removeAttribute("aria-current");
  });
  document.body.dataset.view = v;
  document.body.dataset.track = current() ? "1" : "0";
  if (v !== "now" && state.fs.open) closeFs();
  updateSort();
  keepFocus();                                           // S12: фокус из скрытого или удалённого элемента — на заголовок
}

/** Заголовок открытого экрана (tabindex="-1" в index.html): сюда переходит фокус, если его элемент скрылся (S12). */
function viewHeading() {
  const v = state.view, sec = sectionOf(v);
  if (sec === "home") return hero.title;
  if (sec === "now") return now.title;
  if (sec === "collection" || sec === "settings") return views[sec].querySelector(".page-h");
  return DETAIL_VIEWS.includes(v) ? el.dTitle : el.resultsTitle;
}
setFocusFallback(viewHeading);

export function syncTabs() {
  $$(".tab", el.tabs).forEach((b) => b.setAttribute("aria-selected", String(b.dataset.tab === state.tab)));
  $$(".seg__btn", el.sourceSwitch).forEach((b) => b.setAttribute("aria-selected", String(b.dataset.source === state.source)));
}
