// Обращения к backend и общие операции над треком. Без DOM (fetch вызывается только внутри функций).
import { SOURCES } from "./format.js";

export const enc = encodeURIComponent;

/** JSON-запрос к backend; при ошибке — Error с текстом detail из ответа. */
export async function api(url, opts) {
  const res = await fetch(url, opts);
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || `Ошибка ${res.status}`);
  return res.json();
}
export const getJSON = (url, signal) => api(url, { signal });
export const jsonOpts = (method, body) => ({ method, headers: { "Content-Type": "application/json" }, body: body ? JSON.stringify(body) : undefined });

/** Ответ /api/recommendations. Сбой источников не выдаём за «нечего предложить» (находка M4):
 *  пусто + failed_sources = ошибка (её покажут с кнопкой «Повторить»); есть треки + failed_sources = частичный сбой. */
export function recsResult(d) {
  const tracks = (d && d.tracks) || [];
  const names = ((d && d.failed_sources) || []).map((s) => SOURCES[s] || s).join(", ");
  if (!tracks.length && names) throw new Error(`Источник сейчас недоступен: ${names}`);
  return { tracks, partial: names };
}

/** Ключ трека: источник и id всегда вместе. */
export const trackKey = (t) => `${t.source}:${t.id}`;

// Снимок трека для избранного, истории и плейлистов — все поля Track (S6, находка M6: explicit, plays и
// released раньше терялись, и в «Нравится» не было сортировки по прослушиваниям и дате выхода)
export const trackPayload = (t) => ({ id: t.id, source: t.source, title: t.title, artist: t.artist, artists: t.artists || [t.artist],
  album: t.album || null, duration: t.duration || null, cover: t.cover || null, explicit: !!t.explicit,
  stream_url: t.stream_url, artist_id: t.artist_id || null, artist_ids: t.artist_ids || [],
  plays: Number.isInteger(t.plays) ? t.plays : null, released: typeof t.released === "string" ? t.released : null,
  url: t.url || null });
