// Мини-плеер внизу экрана и общее состояние кнопок управления (data-do play/shuffle/repeat на всех экранах).
// Обновляется по событиям плеера (player/controller.js).
import { setCover } from "../core/dom.js";
import { el, hero, ctl } from "../core/elements.js";
import { state, current } from "../state/store.js";
import { playerEvents } from "../player/controller.js";
import { renderArtists, setSource, markPlaying } from "../components/track-row.js";
import { markMix } from "../components/cards.js";
import { paintLike } from "../components/favorites.js";

/** Новый трек: мини-плеер, заголовок окна, подсветка трека во всех списках, «в избранном». */
function showNowPlaying(t) {
  el.title.textContent = t.title;
  renderArtists(el.artist, t);
  setSource(el.playerSrc, t.source);
  setCover(el.cover, t.cover, 120);
  document.title = `${t.title} — ${t.artist}`;
  document.body.dataset.track = "1";
  markPlaying();
  paintLike();
}
function setPlayingUI(playing) {
  document.body.dataset.audio = playing ? "playing" : "paused";
  ctl.play.forEach((b) => {
    b.dataset.playing = String(playing);
    if (b !== hero.play) b.setAttribute("aria-label", playing ? "Пауза" : "Играть");
  });
}
// Индикатор в плеере: пока идёт резолв/буферизация, вместо исполнителя пишем статус
function setLoading(text) {
  if (text != null) { el.artist.textContent = text; return; }
  const t = current();
  if (t) renderArtists(el.artist, t);
}
/** Режимы перемешивания и повтора: кнопки на всех экранах. */
function paintModes() {
  ctl.shuffle.forEach((b) => b.setAttribute("aria-pressed", String(state.shuffle)));
  ctl.repeat.forEach((b) => b.setAttribute("aria-pressed", String(state.repeat)));
}

export function initPlayerBar() {
  playerEvents.on("track", showNowPlaying);
  playerEvents.on("loading", setLoading);
  playerEvents.on("playing", setPlayingUI);
  playerEvents.on("modes", paintModes);
  playerEvents.on("mix", () => markMix());
}
