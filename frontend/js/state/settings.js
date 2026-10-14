// Серверные настройки (/api/settings, SQLite) — TARGET_ARCHITECTURE §3, находка H3.
// localStorage теперь только для темы до первой отрисовки (запасной путь к /api/boot.js) и для одноразового
// переноса старых значений в backend (migrateLocalUi). Старые ключи не удаляются: откат на прежнюю версию их найдёт.
import { clamp } from "../core/format.js";
import { api, jsonOpts } from "../core/api.js";
import { state } from "./store.js";

export const LS = {
  get(k, d = null) { try { const v = localStorage.getItem(k); return v == null ? d : v; } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch { /* приватный режим */ } },
};

export const round3 = (v) => Math.round(v * 1000) / 1000;

// Ответы /api/settings могут прийти позже, чем пользователь что-то поменял (экран настроек открыт, его GET
// ещё в пути; два PUT подряд вернулись не по порядку). Каждая локальная правка получает номер; значение
// из ответа применяется, только если ключ не меняли после отправки этого запроса. Иначе запоздавший ответ
// возвращал бы старые значения в интерфейс и в поля формы (сбой CI 38348939401, см. reports/S2.md).
let settingsGen = 0;
const localAt = {};                                      // ключ настройки -> номер последней локальной правки
export function markLocal(keys) { settingsGen++; for (const k of keys) localAt[k] = settingsGen; }
/** Номер последней локальной правки: запоминается перед запросом и передаётся в applySettings. */
export const settingsGeneration = () => settingsGen;

/** uiReady — настройки из backend уже применены; volTouched — громкость меняли до ответа backend: не перетираем. */
export const flags = { uiReady: false, volTouched: false };

// ---------- Сохранение настроек интерфейса в backend ----------
// Изменения копятся и уходят одним PUT /api/settings через короткую паузу (перетаскивание громкости —
// одна запись, а не сотня). При закрытии окна несохранённое отправляется с keepalive.
export const uiSave = (() => {
  let pending = {}, timer = 0;
  function flush(keepalive = false) {
    clearTimeout(timer);
    const body = pending;
    pending = {};
    const changed = Object.keys(body).filter((k) => JSON.stringify(body[k]) !== JSON.stringify(state.settings[k]));
    if (!changed.length) return;
    const send = Object.fromEntries(changed.map((k) => [k, body[k]]));
    Object.assign(state.settings, send);
    fetch("/api/settings", { ...jsonOpts("PUT", send), keepalive }).then((r) => {
      if (!r.ok) throw new Error(String(r.status));
    }).catch(() => {                                     // не записалось: вернём в очередь, уйдёт со следующим изменением
      for (const k of changed) { if (!(k in pending)) { pending[k] = send[k]; state.settings[k] = undefined; } }
    });
  }
  return {
    put(obj, delay = 400) { markLocal(Object.keys(obj)); Object.assign(pending, obj); clearTimeout(timer); timer = setTimeout(flush, delay); },
    flush,
  };
})();
/** При закрытии окна несохранённые настройки уходят сразу. */
export function initSettingsSync() {
  window.addEventListener("pagehide", () => uiSave.flush(true));
}

// Тема: «Авто» следует за системой (в том числе при её смене во время работы)
const systemLight = window.matchMedia ? window.matchMedia("(prefers-color-scheme: light)") : null;
let themeMode = "auto";
export function applyTheme(mode) {
  themeMode = mode || "auto";
  const real = themeMode === "auto" ? (systemLight && systemLight.matches ? "light" : "dark") : themeMode;
  document.documentElement.dataset.theme = real;
  LS.set("wave-theme", themeMode);
}
export function initThemeFollowSystem() {
  if (systemLight && systemLight.addEventListener) systemLight.addEventListener("change", () => { if (themeMode === "auto") applyTheme("auto"); });
}

/** Значения из ответа backend. since — номер правки на момент отправки запроса: ключи, изменённые
 *  локально позже, не трогаем (ответ про них устарел). */
export function applySettings(v, since = settingsGen) {
  const fresh = Object.fromEntries(Object.entries(v).filter(([k]) => !((localAt[k] || 0) > since)));
  state.settings = { ...state.settings, ...fresh };
  applyTheme(state.settings.theme);
}
export async function fetchSettings() {
  const since = settingsGen;
  const d = await api("/api/settings");
  applySettings(d.values, since);
  state.settingsPaths = d.paths;
  return d;
}
/** Однократный перенос старых значений из localStorage этого окна в backend (до S2a они жили только там).
 *  Backend сам следит, чтобы перенос был один раз на профиль и не затирал уже сохранённое. */
export function migrateLocalUi() {
  const body = {};
  const num = (k) => { const v = parseFloat(LS.get(k)); return Number.isFinite(v) ? v : undefined; };
  const vol = num("volume"), last = num("wave-last-volume");
  if (vol !== undefined) body.volume = clamp(vol, 0, 1);
  if (last !== undefined && last >= 0.05) body.last_volume = clamp(last, 0, 1);
  const lyr = LS.get("wave-lyrics");
  if (lyr === "0" || lyr === "1") body.lyrics_visible = lyr === "1";
  const name = LS.get("wave-profile-name");
  if (name != null) body.profile_name = [...name.trim()].slice(0, 24).join("");
  try {
    const c = JSON.parse(LS.get("wave-mix-covers", "null"));
    if (c && typeof c === "object" && !Array.isArray(c)) body.mix_covers = c;
  } catch { /* испорченное значение не переносим */ }
  return api("/api/settings/migrate-local", jsonOpts("POST", body));
}
