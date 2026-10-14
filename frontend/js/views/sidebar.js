// Боковая панель: «Твоя музыка» (тематические миксы) и «Миксы по артистам» (любимые артисты из избранного и
// истории).
import { el } from "../core/elements.js";
import { state, nav, bus } from "../state/store.js";
import { themeMixes } from "../state/mixes.js";
import { sideItem, markMix } from "../components/cards.js";
import { openMix } from "./detail.js";
import { openArtistByName } from "./search.js";

export function renderThemeMixes() {
  el.mixes.replaceChildren(...themeMixes.map((m) => sideItem({ cover: m.cover, title: m.title, sub: m.sub, tone: m.tone, data: { mix: m.id } })));
  markMix();
}
export function renderArtistMixes() {
  const shown = state.artists.slice(0, state.artistsShown);
  el.artistMixes.replaceChildren(...shown.map((a, i) => sideItem({ cover: a.cover, title: a.name, sub: a.id ? "Страница артиста" : "Поиск по имени", tone: 0, data: { artist: i } })));
  el.artistHint.hidden = state.artists.length > 0;
  el.artistMore.hidden = state.artists.length <= 4;
  el.artistMore.textContent = state.artistsShown > 4 ? "Свернуть" : "Показать ещё";
}
/** Любимые артисты: избранное весит втрое больше недавних прослушиваний (как в рекомендациях на сервере). */
export function computeArtists() {
  const map = new Map();
  const add = (t, w) => {
    const name = (t.artists && t.artists[0]) || t.artist;
    if (!name) return;
    const k = name.toLowerCase();
    const e = map.get(k) || { name, w: 0, cover: null, source: t.source, id: null };
    e.w += w;
    e.cover = e.cover || t.cover;
    const aid = (t.artist_ids && t.artist_ids[0]) || t.artist_id;
    if (aid && !e.id) { e.id = aid; e.source = t.source; }
    map.set(k, e);
  };
  state.favItems.forEach((t) => add(t, 3));
  state.hist.forEach((t) => add(t, 1));
  state.artists = [...map.values()].sort((a, b) => b.w - a.w).slice(0, 12);
}
let artistTimer = 0;
function scheduleArtists() {
  clearTimeout(artistTimer);
  artistTimer = setTimeout(() => { computeArtists(); renderArtistMixes(); }, 1200);
}

export function initSidebar() {
  el.mixes.addEventListener("click", (e) => {
    const b = e.target.closest("[data-mix]");
    const m = b && themeMixes.find((x) => x.id === b.dataset.mix);
    if (m) { nav.reset(); openMix(m); }
  });
  el.artistMixes.addEventListener("click", (e) => {
    const b = e.target.closest("[data-artist]");
    const a = b && state.artists[Number(b.dataset.artist)];
    if (a) { nav.reset(); openArtistByName(a); }
  });
  el.artistMore.addEventListener("click", () => {
    state.artistsShown = state.artistsShown > 4 ? 4 : 10;
    renderArtistMixes();
  });
  bus.on("mix-covers", renderThemeMixes);                  // микс получил обложку
  bus.on("favorites", scheduleArtists);                    // избранное сохранено
}
