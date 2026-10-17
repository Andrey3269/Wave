// Страница текущего трека: обложка, тексты, похожие треки и исполнители (радио трека).
import { setCover } from "../core/dom.js";
import { SOURCES, fmtTime } from "../core/format.js";
import { api, enc, recsResult, trackKey } from "../core/api.js";
import { el, now } from "../core/elements.js";
import { state, queue, current, nav } from "../state/store.js";
import { uiSave } from "../state/settings.js";
import { playerEvents } from "../player/controller.js";
import { updateProgress } from "../player/sliders.js";
import { loadLyrics, syncLyrics } from "../components/lyrics.js";
import { renderArtists, paintFav, paintDl, markPlaying } from "../components/track-row.js";
import { trackCard, renderRow, bindTrackGrid } from "../components/cards.js";
import { focusSafely, keyboardMode, rescueFocus } from "../components/focus.js";
import { setBlock, skelCards } from "../components/skeleton.js";
import { layout } from "./shell.js";

function openNow() {
  if (!current()) return;
  state.reopen = () => openNow();
  state.view = "now";
  layout();
  renderNow();
  el.main.scrollTo({ top: 0 });
}
export const openNowFromUi = () => { if (current() && state.view !== "now") nav.to(openNow); };

function renderNow() {
  const t = current();
  if (!t) return;
  setCover(now.cover, t.cover, 544);
  setCover(now.bg, t.cover, 544);
  now.eyebrow.textContent = queue.label ? `Сейчас играет · из «${queue.label}»` : "Сейчас играет";
  now.title.textContent = t.title;
  renderArtists(now.artist, t);
  const year = t.released ? String(t.released).slice(0, 4) : "";
  now.meta.textContent = [t.album, year, t.duration ? fmtTime(t.duration) : "", SOURCES[t.source],
    t.plays != null ? `${t.plays.toLocaleString("ru-RU")} прослушиваний` : ""].filter(Boolean).join(" · ");
  paintFav(now.like, t);
  paintDl(now.dl, t);
  now.stage.classList.toggle("no-lyrics", !state.lyricsOn);
  now.lyricsBtn.setAttribute("aria-pressed", String(state.lyricsOn));
  if (state.lyricsOn) { loadLyrics(t); syncLyrics(); }
  loadSimilar(t);
  updateProgress();
}

export function toggleLyrics() {
  state.lyricsOn = !state.lyricsOn;
  uiSave.put({ lyrics_visible: state.lyricsOn }, 0);
  now.stage.classList.toggle("no-lyrics", !state.lyricsOn);
  now.lyricsBtn.setAttribute("aria-pressed", String(state.lyricsOn));
  const t = current();
  if (state.lyricsOn && t) { loadLyrics(t); syncLyrics(); }
}

// ---------- похожие треки и исполнители (радио текущего трека) ----------
/** Данные блока «Похожие»: tracks — карточки треков, artists — карточки исполнителей (список "simar"). */
export const similar = { key: "", tracks: [], artists: [], seq: 0, refocus: false };
async function loadSimilar(t) {
  const key = trackKey(t);
  if (similar.key === key) return;
  similar.key = key;
  const my = ++similar.seq;
  similar.tracks = []; similar.artists = [];
  // S12: Enter на карточке «Похожие» запускает трек, и ряд перестраивается под него — фокус клавиатуры, бывший в
  // ряду, переходит на первую карточку нового ряда (или на заголовок экрана, если похожих нет)
  const a = document.activeElement;
  if (a && now.similarRow.contains(a)) similar.refocus = keyboardMode();
  now.similarRow.replaceChildren(...skelCards(5).map((c) => { c.classList.add("tcard--skel"); return c; }));
  now.similarArtistsSec.hidden = true;
  setBlock(now.similarEmpty, "");
  try {
    const d = recsResult(await api(`/api/recommendations?limit=30&source=${t.source}&id=${enc(t.id)}&title=${enc(t.title)}`));
    if (my !== similar.seq) return;
    const all = d.tracks;
    // только настоящее «радио» трека, без подборок по любимым (reason_kind — с S14; текст — для прежнего ответа)
    const radio = all.filter((x) => (x.reason_kind ? x.reason_kind === "radio" : (x.reason || "").startsWith("Похоже")));
    similar.tracks = (radio.length >= 3 ? radio : all).slice(0, 12);
    const own = new Set(((t.artists && t.artists.length) ? t.artists : [t.artist]).map((n) => n.toLowerCase()));
    const seen = new Set();
    similar.artists = similar.tracks.flatMap((x) => {
      const name = (x.artists && x.artists[0]) || x.artist;
      const id = (x.artist_ids && x.artist_ids[0]) || x.artist_id;
      const k = (name || "").toLowerCase();
      if (!id || !name || own.has(k) || seen.has(k)) return [];
      seen.add(k);
      return [{ kind: "artist", source: x.source, id: String(id), name, cover: x.cover, subtitle: "Исполнитель" }];
    }).slice(0, 8);
    now.similarRow.replaceChildren(...similar.tracks.map(trackCard));
    renderRow(now.similarArtists, similar.artists, "simar");
    now.similarArtistsSec.hidden = !similar.artists.length;
    setBlock(now.similarEmpty, similar.tracks.length ? "" : "Источник не нашёл похожих треков.");
    markPlaying();
  } catch (e) {
    if (my !== similar.seq) return;
    similar.key = "";
    now.similarRow.replaceChildren();
    setBlock(now.similarEmpty, `Не удалось загрузить похожие треки: ${e.message}`, () => loadSimilar(t));
  }
  if (similar.refocus) {
    similar.refocus = false;
    const focusNow = document.activeElement;
    if ((!focusNow || focusNow === document.body) && state.view === "now") focusSafely(now.similarRow.querySelector(".tcard__open")) || rescueFocus();
  }
}

export function initSimilarGrid() {
  bindTrackGrid(now.similarRow, () => similar.tracks, () => `Похожие на «${current()?.title || "трек"}»`);
}
/** Новый трек — страница трека показывает его, если открыта. */
export function initNow() {
  playerEvents.on("track", () => { if (state.view === "now") renderNow(); });
}
