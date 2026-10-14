// Библиотека пользователя: «Нравится», история, плейлисты и сводная страница «Коллекция».
import { mk, icon } from "../core/dom.js";
import { plural } from "../core/format.js";
import { api, enc, jsonOpts, trackKey, trackPayload } from "../core/api.js";
import { el } from "../core/elements.js";
import { state, nav } from "../state/store.js";
import { askText, askConfirm, pickPlaylist } from "../components/dialogs.js";
import { renderRow } from "../components/cards.js";
import { setBlock, skelCards } from "../components/skeleton.js";
import { toast } from "../components/toast.js";
import { layout } from "./shell.js";
import { resetList, setTracks, setStatus, showSkeleton, clearSkeleton, fetchPage, renderCards } from "./list.js";
import { showDetail } from "./detail.js";
import { refreshDownloads, showDownloads } from "./downloads.js";
import { computeArtists, renderArtistMixes } from "./sidebar.js";

// ---------- «Нравится» и история ----------
export async function showLiked() {
  state.reopen = () => showLiked();
  resetList({ view: "liked" });
  setTracks([]);
  el.resultsTitle.textContent = "Нравится";
  setStatus("");
  layout();
  showSkeleton();
  try {
    const d = await api("/api/favorites");
    state.fav = new Set(d.items.map(trackKey));
    state.favItems = d.items;
    setTracks(d.items);
    setBlock(el.empty, d.items.length ? "" : "Нажми на сердечко у трека, и он появится здесь.");
    setStatus(d.items.length ? `Треков: ${d.items.length}` : "");
  } catch (e) {
    clearSkeleton();
    setBlock(el.empty, `Не удалось загрузить избранное: ${e.message}`, () => showLiked());
  }
  layout();
}

export function showHistory() {
  state.reopen = () => showHistory();
  resetList({ view: "history" });
  setTracks([]);
  el.resultsTitle.textContent = "Недавно слушали";
  setStatus("Загружаем…");
  layout();
  showSkeleton();
  return fetchPage(false);                               // та же бесконечная подгрузка, что и у поиска
}
export function initHistory() {
  el.clearHistory.addEventListener("click", async () => {
    if (!await askConfirm({ title: "Очистить историю?", text: "Рекомендации станут менее точными. Избранное и загрузки не затрагиваются.", ok: "Очистить" })) return;
    try { await api("/api/history", { method: "DELETE" }); state.hist = []; computeArtists(); renderArtistMixes(); toast("История очищена"); showHistory(); }
    catch (e) { toast(e.message); }
  });
}

// ---------- плейлисты ----------
const createPlaylist = (name) => api("/api/playlists", jsonOpts("POST", { name }));

export async function createPlaylistFlow() {
  const name = await askText("Новый плейлист", "", "Создать");
  if (!name) return;
  try { const p = await createPlaylist(name); nav.to(() => openPlaylist(String(p.id))); }
  catch (e) { toast(e.message); }
}

export async function showPlaylists() {
  state.reopen = () => showPlaylists();
  resetList({ view: "playlists" });
  setTracks([]);
  el.resultsTitle.textContent = "Плейлисты";
  setStatus("");
  layout();
  el.cards.replaceChildren(...skelCards());
  setBlock(el.empty, "");
  try {
    const d = await api("/api/playlists");
    state.cards = [{ kind: "new", id: "new" }, ...d.items.map((p) => ({ kind: "playlist", id: String(p.id), name: p.name, count: p.count, cover: p.cover }))];
    renderCards();
    setStatus(d.items.length ? `Плейлистов: ${d.items.length}` : "Создай первый плейлист.");
  } catch (e) {
    el.cards.replaceChildren();
    setBlock(el.empty, `Не удалось загрузить плейлисты: ${e.message}`, () => showPlaylists());
  }
}

export async function openPlaylist(id) {
  resetList({ view: "playlist", ctx: { id } });
  state.reopen = () => openPlaylist(id);
  setTracks([]);
  showDetail("Плейлист", "Загружаем…", "", null, false);
  el.resultsTitle.textContent = "Треки";
  setStatus("");
  layout();
  showSkeleton();
  try {
    const d = await api(`/api/playlists/${id}`);
    state.plName = d.playlist.name;
    showDetail("Плейлист", d.playlist.name, `${d.playlist.count} тр.`, d.playlist.cover, false);
    setTracks(d.tracks);
    setBlock(el.empty, d.tracks.length ? "" : "В плейлисте пока пусто. Добавь треки через меню «⋯» у любого трека.");
    layout();
    el.main.scrollTo({ top: 0 });
  } catch (e) {
    showDetail("Плейлист", "Не удалось открыть плейлист", e.message, null, false);
    clearSkeleton();
    setBlock(el.empty, "Попробуй ещё раз.", () => openPlaylist(id));
  }
}
/** «Переименовать» и «Удалить» в шапке плейлиста. */
export function initPlaylistActions() {
  el.dRename.addEventListener("click", async () => {
    const name = await askText("Переименовать плейлист", state.plName || "", "Сохранить");
    if (!name) return;
    try { await api(`/api/playlists/${state.ctx.id}`, jsonOpts("PATCH", { name })); toast("Плейлист переименован"); openPlaylist(state.ctx.id); }
    catch (e) { toast(e.message); }
  });
  el.dDelete.addEventListener("click", async () => {
    if (!await askConfirm({ title: "Удалить плейлист?", text: `«${state.plName}» будет удалён. Сами треки не удаляются.`, ok: "Удалить" })) return;
    try { await api(`/api/playlists/${state.ctx.id}`, { method: "DELETE" }); nav.reset(); toast("Плейлист удалён"); showPlaylists(); }
    catch (e) { toast(e.message); }
  });
}

export async function addToPlaylistFlow(t) {
  const choice = await pickPlaylist();
  if (!choice) return;
  try {
    let pid = choice, name = "";
    if (choice === "new") {
      name = await askText("Новый плейлист", "", "Создать") || "";
      if (!name) return;
      pid = String((await createPlaylist(name)).id);
    }
    const r = await api(`/api/playlists/${pid}/tracks`, jsonOpts("POST", trackPayload(t)));
    toast(r.added ? `Добавлено в плейлист${name ? ` «${name}»` : ""}` : "Этот трек уже есть в плейлисте");
  } catch (e) { toast(`Не удалось добавить в плейлист: ${e.message}`); }
}
export async function removeFromPlaylist(t) {
  try {
    await api(`/api/playlists/${state.ctx.id}/tracks/${t.source}/${enc(t.id)}`, { method: "DELETE" });
    openPlaylist(state.ctx.id);
  } catch (e) { toast(e.message); }
}

// ---------- «Коллекция»: сводка пользовательской библиотеки ----------
export async function showCollection() {
  state.reopen = () => showCollection();
  state.searchCtl?.abort();
  state.view = "collection";
  layout();
  const [pl] = await Promise.all([
    api("/api/playlists").catch(() => null),
    api("/api/favorites").then((d) => { state.fav = new Set(d.items.map(trackKey)); state.favItems = d.items; }).catch(() => {}),
    refreshDownloads(),
  ]);
  if (state.view !== "collection") return;
  const tile = (go, ico, title, sub) => {
    const b = mk("button", "tile");
    b.type = "button"; b.dataset.go = go;
    const i = mk("span", "tile__ico"); i.append(icon(ico));
    const t = mk("span", "tile__t"); t.append(mk("span", "tile__title", title), mk("span", "tile__sub", sub));
    b.append(i, t, icon("i-chev-r", "tile__go"));
    return b;
  };
  const n = plural;
  el.colTiles.replaceChildren(
    tile("liked", "i-heart", "Нравится", n(state.favItems.length, "трек", "трека", "треков")),
    tile("downloads", "i-download", "Скачанные", n(state.dl.size, "трек", "трека", "треков")),
    tile("history", "i-clock", "Недавно слушали", "История прослушивания"),
  );
  state.colPl = [{ kind: "new", id: "new" }, ...(pl ? pl.items.map((p) => ({ kind: "playlist", id: String(p.id), name: p.name, count: p.count, cover: p.cover })) : [])];
  renderRow(el.colPlaylists, state.colPl, "colpl");
  const seen = new Set();
  state.colAr = state.favItems.flatMap((t) => {
    const name = (t.artists && t.artists[0]) || t.artist, id = (t.artist_ids && t.artist_ids[0]) || t.artist_id, k = (name || "").toLowerCase();
    if (!id || !name || seen.has(k)) return [];
    seen.add(k);
    return [{ kind: "artist", source: t.source, id: String(id), name, cover: t.cover, subtitle: "Из избранного" }];
  }).slice(0, 20);
  renderRow(el.colArtists, state.colAr, "colar");
  el.colArtistsSec.hidden = !state.colAr.length;
}
export function initCollectionTiles() {
  el.colTiles.addEventListener("click", (e) => {
    const b = e.target.closest("[data-go]");
    if (!b) return;
    nav.to(() => ({ liked: showLiked, downloads: showDownloads, history: showHistory })[b.dataset.go]());
  });
}
