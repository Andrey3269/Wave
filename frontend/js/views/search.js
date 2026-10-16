// Поиск: треки, артисты, альбомы выбранного источника; переход к артисту по имени.
import { $$ } from "../core/dom.js";
import { el } from "../core/elements.js";
import { navTarget } from "../core/keynav.js";
import { state, nav } from "../state/store.js";
import { setBlock } from "../components/skeleton.js";
import { layout, syncTabs } from "./shell.js";
import { resetList, setTracks, setStatus, showSkeleton, fetchPage } from "./list.js";
import { openArtist } from "./detail.js";

export function search(query) {
  query = query.trim();
  if (!query) return Promise.resolve(null);
  resetList({ view: "search", query });
  const snap = { query, tab: state.tab, source: state.source };
  state.reopen = () => { Object.assign(state, { tab: snap.tab, source: snap.source }); syncTabs(); el.input.value = snap.query; search(snap.query); };
  el.resultsTitle.textContent = "Результаты";
  setStatus("Ищем…");
  layout();
  showSkeleton();
  return fetchPage(false);
}

export function showSearch() {
  state.reopen = () => showSearch();
  if (state.view !== "search" || !state.query) {
    resetList({ view: "search" });
    setTracks([]);
    el.cards.replaceChildren();
    el.resultsTitle.textContent = "Поиск";
    setStatus("");
    setBlock(el.empty, "Введи название трека или исполнителя и нажми Enter.");
  }
  layout();
  el.input.focus();
}

/** Артист из боковой панели или меню: страница артиста, если известен id, иначе поиск по имени. */
export function openArtistByName(a) {
  if (a.id) { openArtist(a.source, a.id); return; }
  state.tab = "artists"; syncTabs(); el.input.value = a.name; search(a.name);
}
/** «Перейти к исполнителю» из меню трека. */
export function goArtist(t) {
  const id = (t.artist_ids && t.artist_ids[0]) || t.artist_id;
  const name = (t.artists && t.artists[0]) || t.artist;
  nav.to(() => openArtistByName({ source: t.source, id, name }));
}

/** Строка поиска, вкладки «Треки / Артисты / Альбомы», переключатель источника. */
export function initSearchForm() {
  el.form.addEventListener("submit", (e) => {
    e.preventDefault();
    nav.reset();
    search(el.input.value);
  });
}
/**
 * S12, модель WAI-ARIA «tabs» с ручной активацией (смена вкладки запускает запрос к источнику): стрелки влево/вправо
 * и Home/End переводят фокус между вкладками, Enter и пробел выбирают (обычный click кнопки).
 */
function tabKeys(list, selector) {
  list.addEventListener("keydown", (e) => {
    const tabs = $$(selector, list);
    const to = navTarget(tabs.map(() => true), tabs.indexOf(document.activeElement), e.key, "horizontal");
    if (to < 0) return;
    e.preventDefault();
    tabs[to].focus();
  });
}
export function initSearchTabs() {
  tabKeys(el.tabs, ".tab");
  tabKeys(el.sourceSwitch, ".seg__btn");
  el.tabs.addEventListener("click", (e) => {
    const btn = e.target.closest(".tab");
    if (!btn || btn.dataset.tab === state.tab) return;
    state.tab = btn.dataset.tab;
    syncTabs();
    if (state.query) search(state.query);
  });
  el.sourceSwitch.addEventListener("click", (e) => {
    const btn = e.target.closest(".seg__btn");
    if (!btn) return;
    state.sourcePicked = true;                           // выбор пользователя: настройки, пришедшие позже, его не меняют
    if (btn.dataset.source === state.source) return;
    state.source = btn.dataset.source;
    syncTabs();
    if (state.query && state.view === "search") search(state.query);
  });
}
