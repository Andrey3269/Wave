// Полноэкранный плеер: одно состояние (state.fs), три режима: default | lyrics | queue.
// Это не отдельный плеер: звук, время, громкость, shuffle/repeat и очередь общие с остальным приложением.
// Режимы только переключают, что показано рядом с обложкой. Окно Windows и браузерный Fullscreen API не трогаем.
import { setCover } from "../core/dom.js";
import { SOURCES, thumb } from "../core/format.js";
import { el, fs } from "../core/elements.js";
import { state, current } from "../state/store.js";
import { playerEvents } from "../player/controller.js";
import { updateProgress } from "../player/sliders.js";
import { loadLyrics, syncLyrics } from "../components/lyrics.js";
import { closeMenu } from "../components/menu.js";
import { renderArtists } from "../components/track-row.js";
import { renderFsQueue, queueClearFlow } from "./queue-drawer.js";

const FS_MODES = ["default", "lyrics", "queue"];
/** Пока открыт полноэкранный плеер, остальное приложение недоступно ни мыши, ни клавиатуре, ни скринридеру. */
function setInert(on) {
  ["side", "main", "player", "queue"].forEach((id) => document.getElementById(id)?.toggleAttribute("inert", on));
}
/** Мягкий фон из цветов обложки: 32x18 пикселей, растянутые браузером. Без blur и без тяжёлых эффектов. */
function paintFsBg(url) {
  if (fs.bg._url === url) return;
  fs.bg._url = url;
  const g = fs.bg.getContext("2d");
  g.clearRect(0, 0, fs.bg.width, fs.bg.height);
  if (!url) return;
  const img = new Image();
  img.referrerPolicy = "no-referrer";
  img.onload = () => {
    if (fs.bg._url !== url) return;                      // трек уже сменился
    g.imageSmoothingQuality = "high";
    g.drawImage(img, 0, 0, fs.bg.width, fs.bg.height);
  };
  img.src = thumb(url, 120);
}
function renderFs() {
  const t = current();
  if (!t) { closeFs(); return; }
  setCover(fs.cover, t.cover, 544);
  paintFsBg(t.cover);
  fs.title.textContent = t.title;
  renderArtists(fs.artist, t, 3);                         // узкая строка: не больше трёх имён (S12)
  const meta = [t.album, t.released ? String(t.released).slice(0, 4) : "", SOURCES[t.source]].filter(Boolean).join(" · ");
  fs.meta.textContent = meta;
  fs.meta.hidden = !meta;
  if (state.fs.mode === "lyrics") loadLyrics(t);
  updateProgress();
}
export function openFs() {
  if (!current() || state.fs.open) return;
  state.fs.open = true;
  state.fs.opener = document.activeElement;
  el.queue.hidden = true;                                // боковая очередь обычного режима не должна остаться под плеером
  closeMenu();
  document.body.dataset.focus = "1";
  fs.root.hidden = false;
  setInert(true);
  setFsMode("default");
  renderFs();
  fs.play.focus({ preventScroll: true });
}
export function closeFs() {
  if (!state.fs.open) return;
  state.fs.open = false;
  state.fs.mode = "default";
  closeMenu();
  fs.root.hidden = true;
  document.body.dataset.focus = "0";
  setInert(false);
  const o = state.fs.opener;
  state.fs.opener = null;
  if (o && o.isConnected) o.focus({ preventScroll: true });   // фокус возвращается туда, откуда открыли
}
/** Режимы взаимоисключающие. Звук, позиция и громкость при переключении не трогаются. */
export function setFsMode(mode) {
  if (!FS_MODES.includes(mode)) mode = "default";
  const prev = state.fs.mode;
  state.fs.mode = mode;
  fs.root.dataset.mode = mode;
  fs.lyricsPanel.hidden = mode !== "lyrics";
  fs.queuePanel.hidden = mode !== "queue";
  fs.lyricsBtn.setAttribute("aria-checked", String(mode === "lyrics"));
  fs.queueBtn.setAttribute("aria-pressed", String(mode === "queue"));
  closeMenu();
  const t = current();
  if (mode === "lyrics" && t) { loadLyrics(t); syncLyrics(); }
  if (mode === "queue") renderFsQueue();
  // кнопки верхней панели в режиме очереди скрыты: фокус не должен пропасть
  if (mode === "queue" && prev !== "queue") fs.queuePanel.querySelector('[data-fs="qclose"]')?.focus({ preventScroll: true });
  else if (prev === "queue" && mode !== "queue") fs.queueBtn.focus({ preventScroll: true });
}
const toggleFsMode = (mode) => setFsMode(state.fs.mode === mode ? "default" : mode);

export function initFullscreen() {
  playerEvents.on("track", () => { if (state.fs.open) renderFs(); });
  document.addEventListener("click", (e) => {
    const b = e.target.closest("[data-fs]");
    if (!b) return;
    switch (b.dataset.fs) {
      case "close": closeFs(); break;
      case "lyrics": toggleFsMode("lyrics"); break;
      case "queue": toggleFsMode("queue"); break;
      case "qclose": setFsMode("default"); break;
      case "qclear": queueClearFlow(); break;
      default: break;
    }
  });
}
