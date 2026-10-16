// Сохранение очереди и позиции между запусками (S10): backend /api/player/state, тот же SQLite-профиль ВОЛНЫ,
// что и настройки (не localStorage: он привязан к порту окна, а порт меняется при каждом запуске — H3).
//
// Что сохраняется: треки очереди (снимок Track; адрес потока backend вычисляет сам), текущая позиция в очереди,
// позиция в треке, перемешивание и повтор, подпись очереди, история «Назад» и круг перемешивания.
// Как часто (без записи на каждый timeupdate):
//   * очередь, текущий трек, режимы — одним PUT через FULL_DELAY после последнего изменения;
//   * позиция — маленьким PATCH: раз в POS_EVERY во время игры, на паузе, после перемотки;
//   * при закрытии окна — несохранённое сразу через navigator.sendBeacon (POST): браузер доставляет его и после
//     выгрузки страницы; обычный fetch при выгрузке обрывается. Если beacon недоступен — fetch с keepalive.
// Пока сохранённое состояние не прочитано при запуске (ready = false), ничего не пишется: иначе ранняя запись
// затёрла бы то, что ещё предстоит восстановить.
import { jsonOpts, trackKey, trackPayload } from "../core/api.js";
import { audio } from "../core/elements.js";
import { state, queue } from "../state/store.js";

const FULL_DELAY = 800;                  // мс тишины после изменения очереди до записи
const FULL_MAX_WAIT = 3000;              // но не дольше этого после первого несохранённого изменения
const POS_EVERY = 10000;                 // мс между записями позиции во время игры
const KEEPALIVE_MAX = 60000;             // байт: keepalive-запрос браузер ограничивает 64 КБ на все такие запросы

let ready = false;
let fullTimer = 0;
let fullPending = false;
let pendingSince = 0;                    // когда появилось первое несохранённое изменение
let lastPosAt = 0;

/** Позиция в текущем треке, с; пока восстановленный трек не запущен — сохранённая (state.resumeAt). */
export function currentPosition() {
  if (audio.getAttribute("src")) return audio.currentTime || 0;
  return state.resumeAt || 0;
}

/** Снимок для PUT /api/player/state или null (очередь пуста — сохранять нечего). */
export function snapshot() {
  const s = queue.serialize({ max: 500 });
  if (!s.tracks.length) return null;
  return {
    tracks: s.tracks.map(trackPayload), index: s.index, label: s.label, history: s.history, cursor: s.cursor,
    cycle: s.cycle, position: Math.max(0, Math.round(currentPosition() * 100) / 100), shuffle: !!state.shuffle,
    repeat: state.repeat,
  };
}

/** Записать полное состояние сейчас (пустая очередь — забыть сохранённое). */
export function saveNow() {
  clearTimeout(fullTimer);
  fullPending = false;
  if (!ready) return Promise.resolve();
  const snap = snapshot();
  if (!snap) return fetch("/api/player/state", { method: "DELETE" }).catch(() => {});
  return fetch("/api/player/state", jsonOpts("PUT", snap)).catch(() => {});
}

let quiet = false;                       // восстановление при запуске: то же, что уже сохранено, — не записываем

/** Выполнить fn без записи (восстановление очереди из сохранённого: перезаписывать его тем же незачем, а поздний
 *  запрос закрывшейся страницы такая запись затёрла бы). */
export function withoutSaving(fn) {
  quiet = true;
  try { return fn(); } finally { quiet = false; }
}

/** Отправка при закрытии окна: beacon, а если браузер его не принял — fetch с keepalive. */
function beacon(url, data, method) {
  const json = JSON.stringify(data);
  const sent = navigator.sendBeacon && json.length < KEEPALIVE_MAX
    && navigator.sendBeacon(url, new Blob([json], { type: "application/json" }));
  if (!sent) fetch("/api/player/state", { ...jsonOpts(method, data), keepalive: json.length < KEEPALIVE_MAX }).catch(() => {});
}

/** Несохранённое при закрытии окна: полное состояние, если ждёт запись, иначе только позиция. */
function saveOnUnload() {
  if (!ready) return;
  if (fullPending) {
    clearTimeout(fullTimer);
    fullPending = false;
    const snap = snapshot();
    if (!snap) { fetch("/api/player/state", { method: "DELETE", keepalive: true }).catch(() => {}); return; }
    beacon("/api/player/state", snap, "PUT");
    return;
  }
  const t = queue.current;
  if (t) beacon("/api/player/state/position", { key: trackKey(t), index: queue.index, position: currentPosition() }, "PATCH");
}

/** Очередь или режимы изменились: одна запись после паузы в изменениях. Если изменения идут чаще паузы
 *  (трек за треком «Следующим»), запись всё равно уходит не позже FULL_MAX_WAIT после первого из них. */
export function saveSoon() {
  if (!ready || quiet) return;
  const now = Date.now();
  if (!fullPending) pendingSince = now;
  fullPending = true;
  clearTimeout(fullTimer);
  fullTimer = setTimeout(() => saveNow(), Math.max(0, Math.min(FULL_DELAY, pendingSince + FULL_MAX_WAIT - now)));
}

/** Только позиция текущего трека (если ждёт полная запись — позиция уйдёт с ней). */
export function savePosition() {
  if (!ready || fullPending) return;
  const t = queue.current;
  if (!t) return;
  lastPosAt = Date.now();
  fetch("/api/player/state", jsonOpts("PATCH", { key: trackKey(t), index: queue.index, position: currentPosition() }))
    .catch(() => {});                                    // 409 — сохранённая очередь уже другая: запишет следующий PUT
}

/** Прочитать сохранённое при запуске. После этого запись разрешена. */
export async function loadSaved() {
  try {
    const r = await fetch("/api/player/state", { cache: "no-store" });
    return r.ok ? (await r.json()).state || null : null;
  } catch {
    return null;
  } finally {
    ready = true;
  }
}

/** Подписки: изменения очереди, режимы (через saveSoon из контроллера), позиция по событиям <audio>, закрытие окна. */
export function initPersist() {
  queue.onChange = saveSoon;
  audio.addEventListener("pause", () => savePosition());
  audio.addEventListener("seeked", () => savePosition());
  audio.addEventListener("timeupdate", () => { if (!audio.paused && Date.now() - lastPosAt >= POS_EVERY) savePosition(); });
  window.addEventListener("pagehide", saveOnUnload);
}
