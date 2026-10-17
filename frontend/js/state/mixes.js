// Миксы — мини-«рецепты»: список треков собирается по клику существующими API (поиск, рекомендации).
// Названия описывают, что подборка делает на самом деле (M13): без фирменных названий чужих сервисов
// и без обещаний персонализации там, где её нет. id не меняются: по ним хранятся обложки (mix_covers).
import { rand } from "../core/format.js";
import { api, enc, recsResult, trackKey } from "../core/api.js";
import { state, bus } from "./store.js";
import { uiSave } from "./settings.js";

const YEAR = new Date().getFullYear();
const THEMES = [
  { id: "mood", title: "Для настроения", sub: "Спокойная подборка на вечер", tone: 1, q: "chill" },
  { id: "hiphop", title: "Хип-хоп и электроника", sub: "Тяжёлые биты и атмосфера", tone: 2, q: "hip hop electronic" },
  { id: "discover", title: `Новинки ${YEAR}`, sub: `Поиск по запросу «новинки ${YEAR}»`, tone: 3, q: `новинки ${YEAR}` },
  { id: "shake", title: "Другая подборка", sub: "Новая выборка из рекомендаций", tone: 4, wave: true },
  { id: "indie", title: "Инди без границ", sub: "Альтернатива, которая вдохновляет", tone: 5, q: "indie alternative" },
];
/** id микса -> обложка; приходит из настроек (mix_covers). */
export const covers = {};
export const themeMixes = THEMES.map((th) => ({
  id: `theme:${th.id}`, title: th.title, sub: th.sub, tone: th.tone, cover: covers[th.id] || null, fresh: !!th.wave, themeId: th.id,
  async load() {
    if (th.wave) return recsResult(await api(`/api/recommendations?limit=40&page=${rand(3)}&salt=${rand(1e6)}`)).tracks;
    const q = typeof th.q === "function" ? th.q() : th.q;
    return (await api(`/api/search?q=${enc(q)}&limit=40&sources=${state.source}`)).tracks || [];
  },
}));
// S16: радио-миксы запоминаются вместе со своими треками (до 40 на микс), поэтому их не больше RADIO_MEMO_MAX:
// давно не открытый вытесняется (Map хранит порядок вставки — обращение переносит микс в конец)
const radioMemo = new Map();
const RADIO_MEMO_MAX = 32;
function radioMix(t) {
  const id = `radio:${trackKey(t)}`;
  let mix = radioMemo.get(id);
  if (mix) radioMemo.delete(id);
  else mix = { id, title: `Похоже на «${t.title}»`, sub: `Радио · ${t.artist}`, cover: t.cover, fresh: false,
    async load() { return recsResult(await api(`/api/recommendations?limit=40&source=${t.source}&id=${enc(t.id)}&title=${enc(t.title)}`)).tracks; } };
  radioMemo.set(id, mix);
  while (radioMemo.size > RADIO_MEMO_MAX) radioMemo.delete(radioMemo.keys().next().value);
  return mix;
}
const MIX_TTL = 10 * 60 * 1000;
/** Треки микса (без дублей). Тематический микс, впервые получив треки, запоминает обложку (bus "mix-covers"). */
export async function loadMix(mix) {
  if (!mix.fresh && mix._tracks && Date.now() - mix._at < MIX_TTL) return mix._tracks;
  const have = new Set();
  const tracks = (await mix.load()).filter((t) => !have.has(trackKey(t)) && have.add(trackKey(t)));
  mix._tracks = tracks; mix._at = Date.now();
  if (tracks[0] && !mix.cover && mix.themeId) {          // запоминаем обложку микса для боковой панели
    mix.cover = tracks[0].cover;
    covers[mix.themeId] = mix.cover;
    uiSave.put({ mix_covers: { ...covers } }, 0);
    bus.emit("mix-covers");
  }
  return tracks;
}
/** Ряд миксов: радио по недавним трекам (если есть история), остальное добираем тематическими подборками. */
export function buildMixes() {
  const seeds = [], seen = new Set();
  const push = (t) => {
    if (!t) return;
    const a = ((t.artists && t.artists[0]) || t.artist || "").toLowerCase();
    if (seen.has(a)) return;
    seen.add(a); seeds.push(t);
  };
  push(state.hist[0]); push(state.favItems[0]);
  state.hist.slice(1).forEach(push); state.favItems.slice(1).forEach(push);
  const radios = seeds.slice(0, 3).map(radioMix);
  return [...radios, ...themeMixes.slice(0, 6 - radios.length)];
}
