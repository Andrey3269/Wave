// Избранное: одно состояние (state.fav, state.favItems) для всех экранов. Кнопки обновляются сразу,
// запрос к backend — следом; при ошибке всё возвращается как было.
import { $$ } from "../core/dom.js";
import { api, enc, jsonOpts, trackKey, trackPayload } from "../core/api.js";
import { el, hero, now } from "../core/elements.js";
import { state, queue, current, heroTrack, bus } from "../state/store.js";
import { paintFav, rowTrack } from "./track-row.js";
import { toast } from "./toast.js";

/** «В избранное» у играющего трека: мини-плеер и страница трека. */
export function paintLike() {
  const t = current();
  el.btnLike.hidden = !t;
  if (t) { paintFav(el.btnLike, t); paintFav(now.like, t); }
}
export function repaintFav() {
  $$(".track__fav", el.tracks).forEach((b) => { const t = rowTrack(b); if (t) paintFav(b, t); });
  paintLike();
  const ht = heroTrack();
  if (ht) paintFav(hero.like, ht);
  $$(".q__fav", el.queueList).forEach((b) => { const t = queue.at(Number(b.dataset.index)); if (t) paintFav(b, t); });
}
export async function toggleFav(t) {
  const k = trackKey(t), was = state.fav.has(k);
  const items = state.favItems;
  if (was) { state.fav.delete(k); state.favItems = items.filter((x) => trackKey(x) !== k); }
  else { state.fav.add(k); state.favItems = [{ ...t }, ...items]; }
  repaintFav();                                          // сразу, без ожидания сервера
  try {
    if (was) await api(`/api/favorites/${t.source}/${enc(t.id)}`, { method: "DELETE" });
    else await api("/api/favorites", jsonOpts("POST", trackPayload(t)));
    bus.emit("favorites");                               // «Миксы по артистам» пересчитываются
  } catch (e) {
    if (was) { state.fav.add(k); state.favItems = items; } else { state.fav.delete(k); state.favItems = items; }
    repaintFav();
    toast(`Не удалось обновить избранное: ${e.message}`);
  }
}
