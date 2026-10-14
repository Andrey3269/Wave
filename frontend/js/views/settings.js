// Экран настроек и профиль в боковой панели. Значения — в backend (/api/settings, SQLite); логика синхронизации
// и защиты от запоздавших ответов — state/settings.js.
import { $ } from "../core/dom.js";
import { clamp } from "../core/format.js";
import { api, jsonOpts } from "../core/api.js";
import { el, audio } from "../core/elements.js";
import { state } from "../state/store.js";
import { uiSave, round3, markLocal, settingsGeneration, flags, applySettings, fetchSettings, migrateLocalUi } from "../state/settings.js";
import { covers, themeMixes } from "../state/mixes.js";
import { paintVolume } from "../player/sliders.js";
import { askConfirm } from "../components/dialogs.js";
import { layout, syncTabs } from "./shell.js";
import { renderThemeMixes, computeArtists, renderArtistMixes } from "./sidebar.js";

/** Поле формы настроек: не перезаписываем то, что пользователь сейчас редактирует. */
function setField(sel, prop, value) {
  const node = $(sel);
  if (node !== document.activeElement) node[prop] = value;
}
/** Применяет настройки интерфейса один раз, при старте (ответы на последующие PUT их не трогают). */
function applyUiSettings(v) {
  if (!flags.volTouched && Number.isFinite(v.volume)) audio.volume = clamp(v.volume, 0, 1);
  if (Number.isFinite(v.last_volume) && v.last_volume >= 0.05) state.lastVol = v.last_volume;
  state.lyricsOn = v.lyrics_visible !== false;
  Object.assign(covers, v.mix_covers || {});
  for (const m of themeMixes) if (covers[m.themeId]) m.cover = covers[m.themeId];
  flags.uiReady = true;
  if (flags.volTouched) uiSave.put({ volume: round3(audio.volume) });   // пока ждали backend, громкость уже поменяли
  paintVolume();
  renderThemeMixes();
  renderProfile();
  document.body.dataset.settings = "ready";              // признак готовности (используют e2e-тесты)
}
/** Настройки при запуске: из backend, при первом запуске новой версии — перенос старых из localStorage. */
export async function loadSettings() {
  try {
    let d = await fetchSettings();
    if (d.meta && d.meta.local_ui_migrated === false) {
      const since = settingsGeneration();
      try { d = await migrateLocalUi(); applySettings(d.values, since); state.settingsPaths = d.paths; } catch { /* попробуем при следующем запуске */ }
    }
    state.source = d.values.default_source;              // источник по умолчанию
    syncTabs();
    applyUiSettings(d.values);
  } catch { flags.uiReady = true; document.body.dataset.settings = "default"; /* остаются значения по умолчанию */ }
}
const setMsg = (t) => { $("#set-msg").textContent = t; };
export async function showSettings() {
  state.reopen = () => showSettings();
  state.searchCtl?.abort();
  state.view = "settings";
  layout();
  try { await fetchSettings(); } catch { setMsg("Не удалось загрузить настройки."); return; }
  const s = state.settings, p = state.settingsPaths;   // уже без устаревших значений (applySettings)
  setField("#set-source", "value", s.default_source);
  setField("#set-autoplay", "checked", !!s.autoplay);
  setField("#set-theme", "value", s.theme);
  setField("#set-dir", "value", p.downloads);
  setField("#set-name", "value", profileName());
  $("#set-dir").disabled = $("#set-dir-save").disabled = !!p.downloads_locked;
  $("#set-paths").textContent = `Данные приложения: ${p.data}`;
  setMsg(p.downloads_locked ? "Папка загрузок задана переменной окружения WAVE_DOWNLOADS_DIR." : "");
}
async function saveSetting(body, okText) {
  markLocal(Object.keys(body));
  const since = settingsGeneration();
  try {
    const d = await api("/api/settings", jsonOpts("PUT", body));
    applySettings(d.values, since);                      // свои ключи применяются, более поздние правки — нет
    renderProfile();
    state.settingsPaths = d.paths;
    setField("#set-dir", "value", d.paths.downloads);
    setMsg(okText || "Сохранено");
  } catch (e) { setMsg(e.message); }
}

// Профиль: локальное имя в боковой панели (хранится в настройках backend, никуда не отправляется)
const profileName = () => (state.settings.profile_name || "").trim();
export function renderProfile() {
  const n = profileName() || "Слушатель";
  el.profileName.textContent = n;
  el.avatar.textContent = [...n][0].toUpperCase();
}

export function initSettingsView() {
  $("#set-source").addEventListener("change", (e) => saveSetting({ default_source: e.target.value }));
  $("#set-autoplay").addEventListener("change", (e) => saveSetting({ autoplay: e.target.checked }));
  $("#set-theme").addEventListener("change", (e) => saveSetting({ theme: e.target.value }));
  $("#set-dir-save").addEventListener("click", () => saveSetting({ downloads_dir: $("#set-dir").value.trim() }, "Папка сохранена. Новые загрузки пойдут в неё."));
  $("#set-clear-history").addEventListener("click", async () => {
    if (!await askConfirm({ title: "Очистить историю?", text: "Рекомендации станут менее точными. Избранное и загрузки не затрагиваются.", ok: "Очистить" })) return;
    try { await api("/api/settings/clear-history", { method: "POST" }); state.hist = []; computeArtists(); renderArtistMixes(); setMsg("История очищена."); } catch (e) { setMsg(e.message); }
  });
  $("#set-clear-cache").addEventListener("click", async () => {
    try { await api("/api/settings/clear-cache", { method: "POST" }); setMsg("Кэш очищен. Загрузки не затронуты."); } catch (e) { setMsg(e.message); }
  });
  $("#set-name").addEventListener("change", (e) => {
    const name = [...e.target.value.trim()].slice(0, 24).join("");
    state.settings.profile_name = name;                     // панель обновляется сразу, запись — следом
    renderProfile();
    saveSetting({ profile_name: name });
  });
}
