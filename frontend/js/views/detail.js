// Страницы со «шапкой»: артист, альбом, микс (плейлист — в collection.js). Шапка общая: showDetail.
import { setCover } from "../core/dom.js";
import { api, getJSON, enc } from "../core/api.js";
import { el } from "../core/elements.js";
import { state, nav } from "../state/store.js";
import { loadMix } from "../state/mixes.js";
import { renderRow, markMix } from "../components/cards.js";
import { setBlock, setMore } from "../components/skeleton.js";
import { toast, hideToast } from "../components/toast.js";
import { layout } from "./shell.js";
import { resetList, setTracks, setStatus, showSkeleton, clearSkeleton, emptyText } from "./list.js";

export function showDetail(kind, title, sub, cover, round) {
  el.dKind.textContent = kind;
  el.dTitle.textContent = title;
  el.dSub.textContent = sub || "";
  el.dCoverWrap.classList.toggle("detail__cover--round", !!round);
  setCover(el.dCover, cover, 544);
}

// ---------- страницы артиста и альбома ----------
export async function openArtist(source, id) {
  resetList({ view: "artist", ctx: { source, id } });
  const ctl = (state.searchCtl = new AbortController());
  state.reopen = () => openArtist(source, id);
  setTracks([]);
  showDetail("Артист", "Загружаем…", "", null, true);
  el.resultsTitle.textContent = "Популярные треки";
  setStatus("");
  layout();
  showSkeleton();
  try {
    const d = await getJSON(`/api/artist/${source}/${enc(id)}`, ctl.signal);
    showDetail("Артист", d.artist.name, d.artist.subtitle, d.artist.cover, true);
    state.albums = d.albums || [];
    state.related = d.related || [];
    renderRow(el.albumsRow, state.albums, "albums");
    renderRow(el.relatedRow, state.related, "related");
    setTracks(d.tracks);
    state.offset = d.tracks.length;
    state.more = d.has_more && d.tracks.length > 0;
    setMore(el.more, state.more ? "idle" : "none");
    setBlock(el.empty, d.tracks.length ? "" : emptyText());
    layout();
    el.main.scrollTo({ top: 0 });
  } catch (err) {
    if (err.name === "AbortError") return;
    showDetail("Артист", "Не удалось открыть артиста", err.message, null, true);
    clearSkeleton();
    setBlock(el.empty, "Источник не ответил. Попробуй ещё раз.", () => openArtist(source, id));
  }
}

export async function openAlbum(source, id, pl) {
  resetList({ view: "album", ctx: { source, id } });
  const ctl = (state.searchCtl = new AbortController());
  state.reopen = () => openAlbum(source, id, pl);
  setTracks([]);
  showDetail("Альбом", "Загружаем…", "", null, false);
  el.resultsTitle.textContent = "Треки";
  setStatus("");
  layout();
  showSkeleton();
  try {
    const d = await getJSON(`/api/album/${source}/${enc(id)}${pl ? `?pl=${enc(pl)}` : ""}`, ctl.signal);
    const mins = Math.round(d.tracks.reduce((s, t) => s + (t.duration || 0), 0) / 60);
    const info = [d.album.artist, d.album.year, d.tracks.length ? `${d.tracks.length} тр.${mins ? `, ${mins} мин` : ""}` : ""].filter(Boolean).join(" · ");
    showDetail("Альбом", d.album.title, info, d.album.cover, false);
    setTracks(d.tracks);
    setBlock(el.empty, d.tracks.length ? "" : (d.unavailable
      ? "Источник не отдаёт эти треки: они закрыты в твоём регионе или доступны только по подписке (на SoundCloud это часто 30-секундные превью)."
      : "В этом альбоме нет доступных треков."));
    layout();
    el.main.scrollTo({ top: 0 });
  } catch (err) {
    if (err.name === "AbortError") return;
    showDetail("Альбом", "Не удалось открыть альбом", err.message, null, false);
    clearSkeleton();
    setBlock(el.empty, "Попробуй ещё раз или открой другой альбом.", () => openAlbum(source, id, pl));
  }
}

/** «Открыть альбом» из меню трека. Если источник дал id альбома (S10: YouTube Music, поле album_id) — открываем
 *  альбом по id. Иначе (SoundCloud, снимки треков до S10) — как раньше: ищем по исполнителю и названию альбома. */
export async function goAlbum(t) {
  if (t.album_id) { nav.to(() => openAlbum(t.source, t.album_id)); return; }
  toast("Ищем альбом…");
  try {
    const q = `${(t.artists && t.artists[0]) || t.artist} ${t.album}`;
    const d = await api(`/api/search/albums?q=${enc(q)}&limit=5&source=${t.source}`);
    const c = (d.items || [])[0];
    if (!c) throw new Error("альбом не найден");
    hideToast();
    nav.to(() => openAlbum(c.source, c.id, c.playlist_id));
  } catch (e) { toast(`Не удалось открыть альбом: ${e.message}`); }
}

// ---------- миксы ----------
let mixSeq = 0;
export async function openMix(mix) {
  resetList({ view: "mix", ctx: { mix: mix.id } });
  const my = ++mixSeq;
  state.reopen = () => openMix(mix);
  setTracks([]);
  showDetail("Микс", mix.title, mix.sub, mix.cover, false);
  el.resultsTitle.textContent = "Треки";
  setStatus("");
  layout();
  markMix();
  showSkeleton();
  try {
    const tracks = await loadMix(mix);
    if (my !== mixSeq || state.view !== "mix") return;
    showDetail("Микс", mix.title, `${mix.sub} · ${tracks.length} тр.`, mix.cover || tracks[0]?.cover, false);
    setTracks(tracks);
    setBlock(el.empty, tracks.length ? "" : "Источник ничего не вернул для этого микса.", () => openMix(mix));
    el.main.scrollTo({ top: 0 });
  } catch (e) {
    if (my !== mixSeq) return;
    clearSkeleton();
    setBlock(el.empty, `Не удалось собрать микс: ${e.message}`, () => openMix(mix));
  }
}
