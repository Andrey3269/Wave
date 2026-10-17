// S14: управление рекомендациями. «Не рекомендовать» трек или исполнителя (меню трека), список исключений и сброс
// сигналов персонализации (настройки). Источник правды — backend (/api/recommendations/exclusions, /reset, SQLite):
// здесь только запросы и событие bus "recs-excluded", по которому лента главной сразу убирает исключённое.
import { api, jsonOpts } from "../core/api.js";
import { bus } from "../state/store.js";
import { toast } from "./toast.js";

async function addExclusion(body, okText) {
  try {
    const d = await api("/api/recommendations/exclusions", jsonOpts("POST", body));
    bus.emit("recs-excluded", d.item);
    toast(okText);
    return d.item;
  } catch (e) {
    toast(`Не удалось исключить из рекомендаций: ${e.message}`);
    return null;
  }
}
export function excludeTrack(t) {
  return addExclusion({ kind: "track", source: t.source, id: t.id, title: t.title, artist: t.artist },
    `«${t.title}» больше не будет в рекомендациях. Вернуть можно в настройках.`);
}
export function excludeArtist(t) {
  return addExclusion({ kind: "artist", artist: t.artist },
    `${t.artist}: больше не будет в рекомендациях. Вернуть можно в настройках.`);
}

export const listExclusions = () => api("/api/recommendations/exclusions");
export const removeExclusion = (item) =>
  api(`/api/recommendations/exclusions/${item.kind}/${encodeURIComponent(item.value)}`, { method: "DELETE" });
export const resetSignals = () => api("/api/recommendations/reset", { method: "POST" });
export const undoReset = () => api("/api/recommendations/reset", { method: "DELETE" });
