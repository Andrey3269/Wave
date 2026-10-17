// Экран настроек и профиль в боковой панели. Значения — в backend (/api/settings, SQLite); логика синхронизации
// и защиты от запоздавших ответов — state/settings.js.
import { $, mk } from "../core/dom.js";
import { clamp } from "../core/format.js";
import { api, jsonOpts, trackKey } from "../core/api.js";
import { el, audio } from "../core/elements.js";
import { state } from "../state/store.js";
import { uiSave, round3, markLocal, settingsGeneration, flags, applySettings, fetchSettings, migrateLocalUi } from "../state/settings.js";
import { covers, themeMixes } from "../state/mixes.js";
import { paintVolume } from "../player/sliders.js";
import { askConfirm } from "../components/dialogs.js";
import { listExclusions, removeExclusion, resetSignals, undoReset } from "../components/recs-control.js";
import { focusSafely, rescueFocus } from "../components/focus.js";
import { layout, syncTabs } from "./shell.js";
import { renderThemeMixes, computeArtists, renderArtistMixes } from "./sidebar.js";
import { repaintFav } from "../components/favorites.js";
import { loadLibrary } from "./library.js";

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
/** Настройки интерфейса после импорта библиотеки (S15): громкость, тексты, обложки миксов, имя профиля. */
function applyImportedUi(v) {
  if (Number.isFinite(v.volume)) audio.volume = clamp(v.volume, 0, 1);
  if (Number.isFinite(v.last_volume) && v.last_volume >= 0.05) state.lastVol = v.last_volume;
  state.lyricsOn = v.lyrics_visible !== false;
  Object.assign(covers, v.mix_covers || {});
  for (const m of themeMixes) if (covers[m.themeId]) m.cover = covers[m.themeId];
  paintVolume();
  renderThemeMixes();
  renderProfile();
}
/** Настройки при запуске: из backend, при первом запуске новой версии — перенос старых из localStorage. */
export async function loadSettings() {
  try {
    let d = await fetchSettings();
    if (d.meta && d.meta.local_ui_migrated === false) {
      const since = settingsGeneration();
      try { d = await migrateLocalUi(); applySettings(d.values, since); state.settingsPaths = d.paths; } catch { /* попробуем при следующем запуске */ }
    }
    if (!state.sourcePicked) state.source = d.values.default_source;   // источник по умолчанию, если пользователь ещё не выбрал
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
  renderRecs();
}

// ---------- рекомендации (S14): сброс сигналов и исключения ----------
const fmtDate = (ts) => new Date(ts * 1000).toLocaleString("ru-RU", { dateStyle: "long", timeStyle: "short" });
let recsSeq = 0;
/** Состояние сброса и список исключений из backend. Возвращает true, если данные получены. */
async function renderRecs() {
  const my = ++recsSeq;
  let d;
  try { d = await listExclusions(); } catch (e) {
    if (my === recsSeq) $("#set-recs-state").textContent = `Не удалось загрузить настройки рекомендаций: ${e.message}`;
    return false;
  }
  if (my !== recsSeq) return false;
  $("#set-recs-state").textContent = d.reset_at
    ? `Персонализация сброшена ${fmtDate(d.reset_at)}: прослушивания, пропуски и избранное до этого момента не влияют на рекомендации. Сами записи сохранены.`
    : "Рекомендации учитывают историю прослушиваний, пропуски и избранное. Сброс их не удаляет и его можно отменить.";
  $("#set-recs-undo").hidden = !d.reset_at;
  $("#set-excl").replaceChildren(...d.items.map((item) => {
    const li = mk("li", "excl__item");
    const text = mk("span", "excl__text");
    text.append(mk("span", "excl__label", item.label), mk("span", "excl__kind", item.kind === "artist" ? "Исполнитель" : "Трек"));
    const back = mk("button", "btn btn--ghost", "Вернуть");
    back.type = "button";
    back.setAttribute("aria-label", `Вернуть в рекомендации: ${item.label}`);
    back.addEventListener("click", () => returnExclusion(item, li));
    li.append(text, back);
    return li;
  }));
  $("#set-excl-empty").hidden = d.items.length > 0;
  return true;
}
async function returnExclusion(item, li) {
  const i = [...li.parentElement.children].indexOf(li);
  const hadFocus = li.contains(document.activeElement);
  try { await removeExclusion(item); } catch (e) { setMsg(e.message); return; }
  setMsg(`«${item.label}» снова может попасть в рекомендации.`);
  await renderRecs();
  const items = $("#set-excl").children;                  // фокус — на строку, вставшую на место возвращённой
  if (hadFocus) focusSafely((items[i] || items[i - 1])?.querySelector("button")) || focusSafely($("#set-recs-reset")) || rescueFocus();
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
  $("#set-recs-reset").addEventListener("click", async () => {
    if (!await askConfirm({ title: "Сбросить персонализацию?", ok: "Сбросить",
      text: "Рекомендации перестанут учитывать прежние прослушивания, пропуски и избранное. История, избранное, плейлисты и загрузки не удаляются, исключения остаются; сброс можно отменить здесь же." })) return;
    try { await resetSignals(); setMsg("Персонализация сброшена. История и избранное сохранены."); } catch (e) { setMsg(e.message); return; }
    renderRecs();
  });
  $("#set-recs-undo").addEventListener("click", async () => {
    try { await undoReset(); setMsg("Прежние сигналы снова учитываются."); } catch (e) { setMsg(e.message); return; }
    await renderRecs();
    focusSafely($("#set-recs-reset"));                    // кнопка «Вернуть» скрылась вместе с фокусом
  });
  $("#set-clear-cache").addEventListener("click", async () => {
    try { await api("/api/settings/clear-cache", { method: "POST" }); setMsg("Кэш очищен. Загрузки не затронуты."); } catch (e) { setMsg(e.message); }
  });
  // S15: резервная копия библиотеки (JSON). Экспорт пишет файл в папку загрузок: окно Wave (WebView2) не сохраняет
  // скачивания браузера. Импорт — содержимое выбранного файла; backend проверяет его целиком и пишет одной транзакцией.
  $("#set-export").addEventListener("click", async () => {
    try { const r = await api("/api/library/export/file", { method: "POST" }); setMsg(`Библиотека сохранена в файл: ${r.path}`); }
    catch (e) { setMsg(`Экспорт не удался: ${e.message}`); }
  });
  $("#set-import").addEventListener("click", () => $("#set-import-file").click());
  $("#set-import-file").addEventListener("change", async (e) => {
    const file = e.target.files[0];
    e.target.value = "";                                   // тот же файл можно выбрать ещё раз
    if (!file) return;
    if (!await askConfirm({ title: "Импортировать библиотеку?", ok: "Импортировать",
      text: `Из файла «${file.name}» добавятся избранное и плейлисты, которых здесь нет, а настройки заменятся сохранёнными в файле. Если в файле есть ошибка, ничего не изменится.` })) return;
    let d;
    try { d = await api("/api/library/import", { method: "POST", headers: { "Content-Type": "application/json" }, body: await file.text() }); }
    catch (err) { setMsg(`Импорт не выполнен: ${err.message}`); return; }
    const im = d.imported, sk = d.skipped;
    setMsg(`Импорт выполнен: треков в избранном ${im.tracks}, альбомов ${im.albums}, исполнителей ${im.artists}, плейлистов ${im.playlists}`
      + ` (треков в них ${im.playlist_tracks}), настроек ${im.settings}.`
      + (sk.existing || sk.duplicate_playlists ? ` Уже было: ${sk.existing + sk.duplicate_playlists}.` : "")
      + (sk.missing_local ? ` Локальных файлов нет на этом компьютере: ${sk.missing_local}.` : ""));
    try {
      const fav = await api("/api/favorites");
      state.fav = new Set(fav.items.map(trackKey));
      state.favItems = fav.items;
      repaintFav(); computeArtists(); renderArtistMixes();
    } catch { /* обновится при следующем открытии экранов */ }
    loadLibrary();
    await showSettings();                                  // настройки из файла — на экран и в интерфейс (тема — там же)
    applyImportedUi(state.settings);
  });
  $("#set-name").addEventListener("change", (e) => {
    const name = [...e.target.value.trim()].slice(0, 24).join("");
    state.settings.profile_name = name;                     // панель обновляется сразу, запись — следом
    renderProfile();
    saveSetting({ profile_name: name });
  });
}
