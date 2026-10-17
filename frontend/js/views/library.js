// S15: библиотека — локальная музыка (источник local), альбомы и исполнители в коллекции, порядок треков плейлиста,
// удаление одной записи истории. Данные — в backend (/api/library/*, /api/playlists, /api/history).
import { plural } from "../core/format.js";
import { api, enc, jsonOpts, trackKey } from "../core/api.js";
import { el } from "../core/elements.js";
import { state } from "../state/store.js";
import { askConfirm } from "../components/dialogs.js";
import { setBlock } from "../components/skeleton.js";
import { toast } from "../components/toast.js";
import { repaintFav } from "../components/favorites.js";
import { focusSafely, rescueFocus, keyboardMode } from "../components/focus.js";
import { layout } from "./shell.js";
import { resetList, setTracks, setStatus, showSkeleton, clearSkeleton, emptyText } from "./list.js";

/** Альбомы и исполнители в коллекции и локальные треки — в state (кнопка «В коллекцию», страница «Коллекция»). */
export async function loadLibrary() {
  const [al, ar, lo] = await Promise.all(["/api/library/albums", "/api/library/artists", "/api/library/local"]
    .map((u) => api(u).catch(() => null)));
  if (al) state.libAlbums = al.items;
  if (ar) state.libArtists = ar.items;
  if (lo) state.localItems = lo.items;
}

// ---------- «В коллекцию» на странице артиста и альбома ----------
const listName = (c) => (c.kind === "album" ? "libAlbums" : "libArtists");
const sameCard = (a, b) => a.source === b.source && String(a.id) === String(b.id);
const inLibrary = (c) => state[listName(c)].some((x) => sameCard(x, c));

/** Карточка открытого артиста или альбома (после загрузки страницы): кнопка «В коллекцию» показывает её состояние. */
export function setDetailCard(card) {
  state.detailCard = card;
  paintDetailFav();
  layout();
}
export function paintDetailFav() {
  const c = state.detailCard;
  if (!c) return;
  const on = inLibrary(c);
  el.dFav.setAttribute("aria-pressed", String(on));
  el.dFavT.textContent = on ? "В коллекции" : "В коллекцию";
  el.dFav.title = on ? "Убрать из коллекции" : "Добавить в коллекцию";
}
/** Тело запроса: только поля карточки каталога (год — четыре цифры или ничего). */
function cardBody(c) {
  if (c.kind === "artist") return { id: String(c.id), source: c.source, name: c.name, cover: c.cover || null, subtitle: c.subtitle || null };
  return { id: String(c.id), source: c.source, title: c.title, artist: c.artist || null,
    year: /^\d{4}$/.test(c.year || "") ? c.year : null, cover: c.cover || null,
    track_count: Number.isInteger(c.track_count) ? c.track_count : null, playlist_id: c.playlist_id || null };
}
export async function toggleDetailFav() {
  const c = state.detailCard;
  if (!c) return;
  const name = listName(c), was = inLibrary(c), before = state[name];
  const path = c.kind === "album" ? "albums" : "artists";
  state[name] = was ? before.filter((x) => !sameCard(x, c)) : [{ ...cardBody(c), kind: c.kind }, ...before];
  paintDetailFav();                                      // сразу, запрос — следом; при ошибке — как было
  try {
    if (was) await api(`/api/library/${path}/${c.source}/${enc(String(c.id))}`, { method: "DELETE" });
    else await api(`/api/library/${path}`, jsonOpts("POST", cardBody(c)));
    toast(was ? "Убрано из коллекции" : `${c.kind === "album" ? "Альбом" : "Исполнитель"} в коллекции`);
  } catch (e) {
    state[name] = before;
    paintDetailFav();
    toast(`Не удалось обновить коллекцию: ${e.message}`);
  }
}

// ---------- локальная музыка ----------
const LOCAL_EMPTY = "Здесь будут аудиофайлы с компьютера. Нажми «Добавить файлы»: поддерживаются MP3, M4A, OGG, Opus, FLAC, WAV и WebM.";

export async function showLocal() {
  state.reopen = () => showLocal();
  resetList({ view: "local" });
  setTracks([]);
  el.resultsTitle.textContent = "Локальная музыка";
  setStatus("");
  layout();
  showSkeleton();
  try {
    const d = await api("/api/library/local");
    state.localItems = d.items;
    if (state.view !== "local") return;
    setTracks(d.items.slice());
    setBlock(el.empty, d.items.length ? "" : LOCAL_EMPTY);
    setStatus(d.items.length ? `Треков: ${d.items.length}` : "");
  } catch (e) {
    if (state.view !== "local") return;
    clearSkeleton();
    setBlock(el.empty, `Не удалось загрузить локальную музыку: ${e.message}`, () => showLocal());
  }
  layout();
}

/** Длительность файла по метаданным (браузер читает заголовок файла сам); не определилась — null. */
function probeDuration(file) {
  return new Promise((resolve) => {
    const url = URL.createObjectURL(file), a = new Audio();
    let done = false;
    const finish = (v) => { if (done) return; done = true; clearTimeout(timer); a.removeAttribute("src"); URL.revokeObjectURL(url); resolve(v); };
    const timer = setTimeout(() => finish(null), 4000);
    a.preload = "metadata";
    a.addEventListener("loadedmetadata", () => finish(Number.isFinite(a.duration) && a.duration >= 1 ? Math.round(a.duration) : null), { once: true });
    a.addEventListener("error", () => finish(null), { once: true });
    a.src = url;
  });
}

/** Импорт выбранных файлов по одному: копия файла уходит в backend, путь на компьютере никуда не передаётся. */
export async function importLocalFiles(files) {
  const list = [...files];
  if (!list.length) return;
  let added = 0, same = 0;
  const failed = [];
  for (const [i, f] of list.entries()) {
    if (state.view === "local") setStatus(`Импорт ${i + 1} из ${list.length}: ${f.name}`);
    try {
      const dur = await probeDuration(f);
      const qs = new URLSearchParams({ name: f.name });
      if (dur) qs.set("duration", String(dur));
      const r = await api(`/api/library/local?${qs}`, { method: "POST", headers: { "Content-Type": "application/octet-stream" }, body: f });
      if (r.added) added += 1; else same += 1;
    } catch (e) { failed.push(`${f.name}: ${e.message}`); }
  }
  const parts = [];
  if (added) parts.push(`Добавлено: ${plural(added, "трек", "трека", "треков")}`);
  if (same) parts.push(`уже были в Wave: ${same}`);
  if (failed.length) parts.push(`не удалось: ${failed[0]}${failed.length > 1 ? ` и ещё ${failed.length - 1}` : ""}`);
  toast(parts.join("; ") || "Файлы не выбраны", failed.length ? 9000 : 4000);
  if (state.view === "local") await showLocal(); else await loadLibrary();
}

export async function removeLocal(t) {
  if (!await askConfirm({ title: "Удалить трек из Wave?", ok: "Удалить",
    text: `«${t.title}» исчезнет из локальной музыки, избранного и плейлистов. Исходный файл на компьютере не удаляется: Wave хранил свою копию.` })) return;
  try { await api(`/api/library/local/${enc(t.id)}`, { method: "DELETE" }); } catch (e) { toast(e.message); return; }
  const k = trackKey(t);
  state.fav.delete(k);
  state.favItems = state.favItems.filter((x) => trackKey(x) !== k);
  repaintFav();
  toast("Трек удалён из Wave");
  if (state.view === "local") showLocal(); else loadLibrary();
}

// ---------- порядок треков плейлиста ----------
/** Перестановка на одну позицию (dir = -1 выше, +1 ниже) — только при сортировке «по умолчанию» (это и есть порядок). */
export async function movePlaylistTrack(t, dir) {
  if (state.view !== "playlist" || state.sort !== "default") return;
  const i = state.list.indexOf(t), j = i + dir;
  if (i < 0 || j < 0 || j >= state.list.length) return;
  const next = state.list.slice();
  [next[i], next[j]] = [next[j], next[i]];
  const id = state.ctx.id, kb = keyboardMode();
  try { await api(`/api/playlists/${id}/tracks/order`, jsonOpts("PATCH", { keys: next.map(trackKey) })); }
  catch (e) { toast(`Не удалось изменить порядок: ${e.message}`); return; }
  if (state.view !== "playlist" || state.ctx?.id !== id) return;
  setTracks(next);
  setStatus(`«${t.title}»: позиция ${j + 1} из ${next.length}`);   // объявляется экранным диктором (aria-live)
  const more = el.tracks.querySelector(`.row[data-key="${CSS.escape(trackKey(t))}"] .track__more`);
  if (kb) focusSafely(more) || rescueFocus();
}

// ---------- история: одна запись ----------
export async function removeFromHistory(t) {
  try { await api(`/api/history/${t.source}/${enc(t.id)}`, { method: "DELETE" }); }
  catch (e) { toast(`Не удалось убрать из истории: ${e.message}`); return; }
  toast("Убрано из истории");
  if (state.view !== "history") return;
  const i = state.list.indexOf(t), kb = keyboardMode();
  const rest = state.list.filter((x) => x !== t);
  state.offset = Math.max(0, state.offset - 1);           // следующая страница не пропустит запись
  state.hist = state.hist.filter((x) => trackKey(x) !== trackKey(t));
  setTracks(rest);
  setBlock(el.empty, rest.length ? "" : emptyText());
  setStatus(rest.length ? `Найдено: ${rest.length}${state.more ? "+" : ""}` : "");
  const row = el.tracks.querySelectorAll(".row")[Math.min(Math.max(i, 0), rest.length - 1)];
  if (kb) focusSafely(row?.querySelector(".track__play")) || rescueFocus();
}
