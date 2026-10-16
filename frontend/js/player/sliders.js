// Полосы прогресса и громкости: мини-плеер, баннер главной, страница трека и полноэкранный плеер показывают
// одно состояние одного <audio>.
import { clamp, fmtTime } from "../core/format.js";
import { el, audio, hero, now, fs, ctl } from "../core/elements.js";
import { state, current } from "../state/store.js";
import { uiSave, round3, flags } from "../state/settings.js";
import { tickLyrics } from "../components/lyrics.js";
import { currentPosition, savePosition } from "./persist.js";

/** Длительность текущего трека: из <audio>, пока она неизвестна — из данных трека. */
export function getDuration() {
  if (Number.isFinite(audio.duration) && audio.duration > 0) return audio.duration;
  return current()?.duration || 0;
}
/** Перемотка на t с. Трек восстановлен после перезапуска, но ещё не запущен (S10): меняется позиция, с которой
 *  он начнёт играть, — <audio> без источника не трогаем. */
export function seekTo(t) {
  const dur = getDuration();
  t = clamp(t, 0, dur || Math.max(0, t));
  if (!audio.getAttribute("src")) {
    if (state.resumeAt == null) return;
    state.resumeAt = t;
    updateProgress();
    savePosition();
    return;
  }
  audio.currentTime = t;
}
export function seekBy(delta) {
  const dur = getDuration();
  if (!dur) return;
  seekTo(currentPosition() + delta);
}

// Полосы прогресса: мини-плеер, баннер, страница трека, полноэкранный плеер
const progressBars = [
  { bar: el.progress, cur: el.tCur, tot: el.tTotal },
  { bar: hero.bar, cur: hero.cur, tot: hero.tot },
  { bar: now.bar, cur: now.cur, tot: now.tot },
  { bar: fs.bar, cur: fs.cur, tot: fs.tot },
];
/** Новый трек: полосы прогресса с нуля (громкость остаётся как есть). */
export function resetProgress(duration) {
  progressBars.forEach((p) => p.bar.style.setProperty("--p", "0%"));
  progressBars.forEach((p) => { if (p.cur) p.cur.textContent = "0:00"; if (p.tot) p.tot.textContent = fmtTime(duration); });
}
export function updateProgress() {
  const dur = getDuration();
  const cur = currentPosition();
  const pct = dur ? clamp((cur / dur) * 100, 0, 100) : 0;
  for (const p of progressBars) {
    p.bar.style.setProperty("--p", `${pct}%`);
    p.bar.setAttribute("aria-valuenow", String(Math.round(pct)));
    p.bar.setAttribute("aria-valuetext", `${fmtTime(cur)} из ${fmtTime(dur)}`);
    p.cur.textContent = fmtTime(cur);
    p.tot.textContent = fmtTime(dur);
  }
  if (state.view === "home") {                          // у текущей карточки «Продолжить слушать» живой прогресс
    const mc = el.continueRow.querySelector(".minicard.is-playing");
    if (mc) {
      mc.querySelector(".minibar").style.setProperty("--p", `${pct}%`);
      mc.querySelector(".minicard__time").textContent = `${fmtTime(cur)} / ${fmtTime(dur)}`;
    }
  }
  if (state.view === "now" || state.fs.open) tickLyrics(cur);
}

// ---------- Универсальный ползунок (прогресс и громкость) ----------
function makeSlider(node, { preview, commit, nudge, cancel = () => {} }) {
  const fraction = (e) => {
    const r = node.getBoundingClientRect();
    return clamp((e.clientX - r.left) / r.width, 0, 1);
  };
  let dragging = false;
  node.addEventListener("pointerdown", (e) => {
    if (e.pointerType === "mouse" && e.button !== 0) return;
    dragging = true;
    node.setPointerCapture(e.pointerId);
    preview(fraction(e));
  });
  node.addEventListener("pointermove", (e) => { if (dragging) preview(fraction(e)); });
  node.addEventListener("pointerup", (e) => {
    if (!dragging) return;
    dragging = false;
    commit(fraction(e));
  });
  node.addEventListener("pointercancel", () => { dragging = false; cancel(); });
  node.addEventListener("keydown", (e) => {
    const dir = { ArrowLeft: -1, ArrowDown: -1, ArrowRight: 1, ArrowUp: 1 }[e.key];
    if (!dir) return;
    e.preventDefault();
    nudge(dir);
  });
}

// Громкость: ползунки мини-плеера, страницы трека и полноэкранного плеера управляют одним audio.volume.
// Mute = audio.muted: сама громкость не обнуляется, поэтому после включения звука она возвращается как была.
// Вид всех ползунков и кнопок перерисовывается по событию volumechange самого <audio>.
const volBars = [el.volume, now.volume, fs.volume];
export function paintVolume() {
  const eff = audio.muted ? 0 : audio.volume;
  for (const node of volBars) {
    node.style.setProperty("--p", `${eff * 100}%`);
    node.setAttribute("aria-valuenow", String(Math.round(eff * 100)));
    node.setAttribute("aria-valuetext", audio.muted ? "Звук выключен" : `${Math.round(eff * 100)}%`);
  }
  const off = eff === 0;
  ctl.mute.forEach((b) => {
    const label = off ? "Включить звук" : "Выключить звук";
    b.setAttribute("aria-pressed", String(off));
    b.title = label;
    b.setAttribute("aria-label", label);
    b.querySelector("use").setAttribute("href", off ? "#i-volume-off" : "#i-volume");
  });
}
/** commit=false: промежуточные значения при перетаскивании. «Последней слышимой» громкостью они не считаются,
 *  поэтому, если ушли ползунком в ноль, mute вернёт то значение, что было до перетаскивания. */
function setVolume(v, commit = true) {
  if (!flags.uiReady) flags.volTouched = true;
  v = clamp(v, 0, 1);
  audio.volume = v;
  if (v > 0) audio.muted = false;
  if (commit && v >= 0.05) { state.lastVol = v; uiSave.put({ last_volume: round3(v) }); }
  paintVolume();
}
export function toggleMute() {
  if (!flags.uiReady) flags.volTouched = true;
  if (audio.muted || audio.volume === 0) {
    audio.muted = false;
    if (audio.volume === 0) audio.volume = state.lastVol || 0.6;   // ушли ползунком в ноль: возвращаем последнюю слышимую громкость
  } else {
    audio.muted = true;
  }
  paintVolume();
}

/** Ползунки прогресса и громкости, запись громкости в настройки. */
export function initSliders() {
  // Прогресс: во время перетаскивания обновляем только вид, а перемотку делаем при отпускании
  for (const p of progressBars) {
    makeSlider(p.bar, {
      preview(f) {
        state.seeking = true;
        p.bar.style.setProperty("--p", `${f * 100}%`);
        p.cur.textContent = fmtTime(f * getDuration());
      },
      commit(f) {
        state.seeking = false;
        const dur = getDuration();
        if (dur) seekTo(f * dur);                        // сервер ответит 206 на Range-запрос
        updateProgress();
      },
      cancel() { state.seeking = false; updateProgress(); },
      nudge: (dir) => seekBy(dir * 5),
    });
  }
  audio.addEventListener("volumechange", () => {
    paintVolume();
    if (flags.uiReady) uiSave.put({ volume: round3(audio.volume) });   // перетаскивание: одна запись в конце (см. uiSave)
  });
  for (const node of volBars) {
    makeSlider(node, { preview: (v) => setVolume(v, false), commit: (v) => setVolume(v, true), nudge: (dir) => setVolume((audio.muted ? 0 : audio.volume) + dir * 0.05) });
  }
  paintVolume();                                           // громкость из настроек придёт в applyUiSettings
}
