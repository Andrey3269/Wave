// Чистые функции форматирования и мелкие утилиты. Без DOM: тестируются через node --test (tests/js/format.test.mjs).

// Источники: ключ совпадает с track.source с бэкенда; иконка берётся из спрайта #i-src-<ключ>
export const SOURCES = { yt: "YouTube Music", sc: "SoundCloud" };

export const clamp = (v, min, max) => Math.min(max, Math.max(min, v));
export const rand = (n) => Math.floor(Math.random() * n);

/** Секунды -> «m:ss» или «h:mm:ss»; мусор и отрицательные значения -> «0:00». */
export function fmtTime(sec) {
  if (!Number.isFinite(sec) || sec < 0) return "0:00";
  const s = Math.floor(sec % 60);
  const m = Math.floor(sec / 60) % 60;
  const h = Math.floor(sec / 3600);
  const ss = String(s).padStart(2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${ss}` : `${m}:${ss}`;
}

/** «1 трек», «2 трека», «5 треков». */
export const plural = (n, one, few, many) => `${n} ${n % 10 === 1 && n % 100 !== 11 ? one : [2, 3, 4].includes(n % 10) && ![12, 13, 14].includes(n % 100) ? few : many}`;

// ---------- Обложки нужного размера ----------
const SIZE_RE = /=w\d+-h\d+/;
/** Маленькому превью не нужна обложка 544x544: просим у CDN размер поменьше. */
export function thumb(url, px) {
  if (!url || !px) return url;
  if (SIZE_RE.test(url)) return url.replace(SIZE_RE, `=w${px}-h${px}`);
  if (px <= 300 && url.includes("sndcdn.com") && url.includes("-t500x500.")) return url.replace("-t500x500.", "-t300x300.");
  return url;
}
