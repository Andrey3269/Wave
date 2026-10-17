// S14: сопоставление трека с исключением из рекомендаций (чистые функции, без DOM — проверяются tests/js).
import { trackKey } from "./api.js";

/** Имя исполнителя так же, как нормализует backend (recs_store.norm): регистр и знаки препинания не различаются. */
export const artistNorm = (s) => (s || "").toLowerCase().replace(/[^\p{L}\p{N}\p{M}_]+/gu, " ").trim();

/** Трек подпадает под исключение (запись из ответа backend: { kind: "track" | "artist", value }). */
export function matchesExclusion(t, item) {
  if (!t || !item) return false;
  if (item.kind === "track") return trackKey(t) === item.value;
  if (item.kind === "artist") return [t.artist, ...(t.artists || [])].some((a) => artistNorm(a) === item.value);
  return false;
}
