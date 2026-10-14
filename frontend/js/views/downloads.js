// Загрузки и офлайн: скачать, удалить, «Скачанные». Скачивается штатный поток трека — тот же, что играет плеер
// (download_policy.py, S5); прогресс — опросом /api/downloads/jobs.
import { $$ } from "../core/dom.js";
import { api, enc, jsonOpts, trackKey } from "../core/api.js";
import { el, now } from "../core/elements.js";
import { state, current } from "../state/store.js";
import { paintDl, rowTrack, BUSY } from "../components/track-row.js";
import { askConfirm } from "../components/dialogs.js";
import { setBlock } from "../components/skeleton.js";
import { toast } from "../components/toast.js";
import { layout } from "./shell.js";
import { resetList, setTracks, setStatus } from "./list.js";

// Эти коды значат, что поток этого трека получить нельзя или источник отдаёт лишь фрагмент: повтор не поможет,
// кнопка показывает причину. Остальные ошибки (сеть, лимит запросов, проверка на бота, сбой) временные:
// кнопка предлагает повторить.
const DL_FINAL = ["unavailable", "age_restricted", "no_direct_stream", "preview_only"];

function repaintDl() {
  $$(".track__dl", el.tracks).forEach((b) => { const t = rowTrack(b); if (t) paintDl(b, t); });
  const cur = current();
  if (cur) paintDl(now.dl, cur);
}
export async function refreshDownloads() {
  try {
    const d = await api("/api/downloads");
    state.dl = new Map(d.items.map((t) => [trackKey(t), t]));
  } catch { /* сервер недоступен: оставляем прежнее состояние */ }
  repaintDl();
}
let polling = false;
const seenFinished = new Set();
/** Итог задачи — один раз: отказ запоминается для трека, ошибка и готовность показываются тостом. */
function noteFinished(key, j) {
  const id = key + j.finished_at;
  if (seenFinished.has(id)) return;
  seenFinished.add(id);
  const title = state.dlTitles.get(key);
  if (j.state === "done") { if (title) toast(`Готово: «${title}» сохранён на компьютер`); return; }
  if (j.state !== "error") return;
  if (DL_FINAL.includes(j.code)) state.dlDenied.set(key, j.error);
  toast(title ? `«${title}»: ${j.error}` : j.error, 6000);
}
export async function pollJobs() {
  if (polling) return;
  polling = true;
  try {
    for (;;) {
      state.jobs = await api("/api/downloads/jobs");
      for (const [k, j] of Object.entries(state.jobs)) if (j.finished_at) noteFinished(k, j);
      if (Object.values(state.jobs).some((j) => j.state === "done")) await refreshDownloads(); else repaintDl();
      if (!Object.values(state.jobs).some((j) => BUSY.includes(j.state))) break;
      await new Promise((r) => setTimeout(r, 800));
    }
    await refreshDownloads();
    if (state.view === "downloads") { await showDownloads(); }
  } catch { /* сеть пропала: опрос возобновится при следующей загрузке */ } finally { polling = false; }
}
async function startDownload(t) {
  const key = trackKey(t);
  state.dlTitles.set(key, t.title);
  state.dlDenied.delete(key);
  state.jobs[key] = { state: "preparing", progress: 0 };
  repaintDl();
  try {
    const j = await api("/api/downloads", jsonOpts("POST", { source: t.source, id: t.id, title: t.title, artist: t.artist, album: t.album, duration: t.duration, cover: t.cover }));
    if (j.state === "done") { await refreshDownloads(); toast(`«${t.title}» уже скачан`); return; }
    toast("Загрузка началась");
    pollJobs();
  } catch (e) {
    delete state.jobs[key];
    repaintDl();
    toast(`Не удалось начать загрузку: ${e.message}`);
  }
}
async function removeDownload(t) {
  if (!await askConfirm({ title: "Удалить загрузку?", text: `Локальная копия «${t.title}» будет удалена с компьютера. Сам трек останется доступным онлайн.`, ok: "Удалить" })) return;
  try {
    const r = await api(`/api/downloads/${t.source}/${enc(t.id)}`, { method: "DELETE" });
    state.dl.delete(trackKey(t));
    if (r.files_kept) toast("Запись удалена из «Скачанных». Файлы на диске не тронуты: их папку создал не Wave.", 6000);
    else toast("Локальная копия удалена");
    if (state.view === "downloads") await showDownloads(); else repaintDl();
  } catch (e) { toast(e.message); }
}
/** Кнопка загрузки трека: скачать, удалить скачанное, объяснить отказ. */
export function toggleDownload(t) {
  const key = trackKey(t);
  if (state.dl.has(key)) return removeDownload(t);
  const denied = state.dlDenied.get(key);
  if (denied) { toast(`Скачивание недоступно. ${denied}`, 6000); return; }
  if (state.jobs[key] && BUSY.includes(state.jobs[key].state)) return;
  return startDownload(t);
}

export async function showDownloads() {
  state.reopen = () => showDownloads();
  resetList({ view: "downloads" });
  setStatus("");
  layout();
  await refreshDownloads();
  const items = [...state.dl.values()];
  el.resultsTitle.textContent = "Скачанные";
  setTracks(items);
  setBlock(el.empty, items.length ? "" : "Здесь появятся треки, которые ты скачал. Они играют без интернета.");
  const active = Object.values(state.jobs).filter((j) => BUSY.includes(j.state)).length;
  setStatus(items.length || active ? `Скачано: ${items.length}${active ? ` · загружается: ${active}` : ""}` : "");
  layout();
}
