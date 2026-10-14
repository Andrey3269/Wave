// Общий экран списка (#view-list): строки треков или карточки, сортировка, «Загрузить ещё», пустые состояния.
// Его используют поиск, артист, альбом, микс, плейлист, «Нравится», история и «Скачанные».
import { SOURCES, rand } from "../core/format.js";
import { getJSON, enc, trackKey } from "../core/api.js";
import { el } from "../core/elements.js";
import { state, queue, PAGE, nextSeq, isCards, sectionOf, DETAIL_VIEWS } from "../state/store.js";
import { play, togglePlay, setShuffle, scheduleWarm, warmTrack } from "../player/controller.js";
import { buildRow, markPlaying } from "../components/track-row.js";
import { buildCard, cardKey } from "../components/cards.js";
import { setBlock, skelRows, skelCards } from "../components/skeleton.js";
import { openMenu } from "../components/menu.js";
import { toggleFav } from "../components/favorites.js";

export function setStatus(text) { state.baseStatus = text; el.status.textContent = text; }

// ---------- сортировка ----------
// Сортируем уже загруженное: источники не умеют сортировать выдачу поиска, и подделывать это мы не станем.
// «Популярные» и «Новые» есть только там, где источник отдаёт прослушивания и даты (SoundCloud).
const SORTS = {
  default: (a, b) => a._i - b._i,
  title: (a, b) => a.title.localeCompare(b.title, "ru"),
  artist: (a, b) => a.artist.localeCompare(b.artist, "ru"),
  dur_asc: (a, b) => (a.duration || 0) - (b.duration || 0),
  dur_desc: (a, b) => (b.duration || 0) - (a.duration || 0),
  plays: (a, b) => (b.plays || 0) - (a.plays || 0),
  new: (a, b) => (b.released || "").localeCompare(a.released || ""),
};
const SORT_LABELS = { default: "По умолчанию", title: "По названию", artist: "По исполнителю",
  dur_asc: "Короткие сначала", dur_desc: "Длинные сначала", plays: "Популярные", new: "Новые" };

/** Сортирует только список на экране: у очереди свой массив (M11), её порядок сортировка не трогает. */
function sortList() {
  state.list.sort(SORTS[state.sort] || SORTS.default);
}
export function updateSort() {
  const opts = ["default", "title", "artist", "dur_asc", "dur_desc"];
  if (state.list.some((t) => t.plays != null)) opts.push("plays");
  if (state.list.some((t) => t.released)) opts.push("new");
  el.sort.replaceChildren(...opts.map((k) => { const o = new Option(SORT_LABELS[k], k); o.selected = k === state.sort; return o; }));
  el.sort.hidden = sectionOf(state.view) !== "list" || isCards() || state.list.length < 2;
}

function renderList() {
  el.tracks.replaceChildren(...state.list.map((t, i) => buildRow(t, i)));
  markPlaying();
  updateSort();
}
/** Новый список треков: первая страница поиска, страница артиста/альбома, загрузки. */
export function setTracks(tracks) {
  tracks.forEach((t) => { t._i = nextSeq(); });
  state.list = tracks;
  if (state.sort !== "default") sortList();
  renderList();
  scheduleWarm(state.list.slice(0, 3));
}
/** Следующая страница: без дублей, в тот же список. Если очередь играет этот список, она продолжается
 *  новыми треками — через свой источник продолжения (queue.extend), а не через общий массив. */
function appendTracks(raw) {
  const have = new Set(state.list.map(trackKey));
  const fresh = raw.filter((t) => !have.has(trackKey(t)));
  fresh.forEach((t) => { t._i = nextSeq(); });
  const from = state.list.length;
  state.list.push(...fresh);
  if (fresh.length && queue.isFrom(state.list)) queue.extend();
  if (state.sort !== "default") { sortList(); renderList(); }
  else { fresh.forEach((t, i) => el.tracks.appendChild(buildRow(t, from + i))); updateSort(); }
}

/** Карточки поиска (артисты, альбомы) и плейлистов в #cards. */
export function renderCards(from = 0, append = false) {
  const nodes = state.cards.slice(from).map((c, i) => buildCard(c, from + i, "cards"));
  if (append) el.cards.append(...nodes); else el.cards.replaceChildren(...nodes);
}

// ---------- ленты с подгрузкой: поиск, треки артиста, история ----------
function feedUrl(offset) {
  const q = enc(state.query);
  if (state.view === "history") return `/api/history?limit=${PAGE}&offset=${offset}`;
  if (state.view === "artist") return `/api/artist/${state.ctx.source}/${enc(state.ctx.id)}/tracks?limit=${PAGE}&offset=${offset}`;
  if (state.tab === "tracks") return `/api/search?q=${q}&limit=${PAGE}&offset=${offset}&sources=${state.source}`;
  return `/api/search/${state.tab}?q=${q}&limit=${PAGE}&offset=${offset}&source=${state.source}`;
}
export function emptyText() {
  if (state.view === "history") return "Ты ещё ничего не слушал. Найди что-нибудь для первой сессии.";
  if (state.view === "artist") return "Треков у этого артиста не найдено.";
  return `По запросу «${state.query}» ничего не найдено. Попробуй изменить запрос.`;
}
/** Показывает скелетон в том контейнере, который сейчас виден. */
export function showSkeleton() {
  setBlock(el.empty, "");
  if (isCards() || state.view === "playlists") el.cards.replaceChildren(...skelCards());
  else el.tracks.replaceChildren(...skelRows());
}
export function clearSkeleton() { el.tracks.replaceChildren(); el.cards.replaceChildren(); }

/**
 * Одна страница ленты (поиск по вкладке, треки артиста, история). append=false: сначала,
 * append=true: бесконечная прокрутка. Возвращает список или null (ошибка/отмена).
 */
export async function fetchPage(append) {
  const q = state.query;
  const ctl = (state.searchCtl = new AbortController());
  state.loadingMore = append;
  if (append) { el.more.hidden = false; el.more.textContent = "Загружаем…"; }
  try {
    const data = await getJSON(feedUrl(append ? state.offset : 0), ctl.signal);
    const got = data.items || data.tracks || [];
    if (isCards()) {
      const have = new Set(state.cards.map(cardKey));
      const fresh = append ? got.filter((c) => !have.has(cardKey(c))) : got;
      const from = append ? state.cards.length : 0;
      state.cards = append ? state.cards.concat(fresh) : fresh;
      renderCards(from, append);
    } else if (append) {
      appendTracks(got);
    } else {
      state.lastQuery = q;
      setTracks(got);
    }
    if (state.view === "search") el.resultsTitle.textContent = `Результаты: ${q}`;
    state.offset = (append ? state.offset : 0) + got.length;
    state.more = !!data.has_more && got.length > 0;
    el.more.hidden = !state.more; el.more.textContent = "";

    const n = isCards() ? state.cards.length : state.list.length;
    setBlock(el.empty, n ? "" : emptyText());
    const down = (data.failed_sources || []).map((s) => SOURCES[s] || s).join(", ");
    setStatus(n ? `Найдено: ${n}${state.more ? "+" : ""}${down ? ` (${down} сейчас недоступен)` : ""}` : (down ? `${down} сейчас недоступен` : ""));
    return isCards() ? state.cards : state.list;
  } catch (err) {
    if (err.name === "AbortError") return null;
    if (append) { el.more.hidden = false; el.more.textContent = "Не удалось загрузить. Нажми, чтобы повторить"; }
    else {
      clearSkeleton();
      setStatus("");
      setBlock(el.empty, navigator.onLine ? `Не удалось загрузить: ${err.message}` : "Нет соединения. Открой «Скачанные»: эти треки играют без интернета.", () => state.reopen?.());
    }
    return null;
  } finally {
    if (ctl === state.searchCtl) state.loadingMore = false;
  }
}

/** Новый экран списка: отменить прошлую загрузку, сбросить поиск, сортировку, карточки. */
export function resetList(extra) {
  state.searchCtl?.abort();
  Object.assign(state, { query: "", offset: 0, more: false, sort: "default", cards: [], albums: [], related: [] }, extra);
  el.more.hidden = true;
}

// ---------- воспроизведение из списка ----------
/** Подпись очереди: откуда играет («Альбом 1», «Поиск: gspd», «Нравится»...). */
function listLabel() {
  if (DETAIL_VIEWS.includes(state.view)) return el.dTitle.textContent;
  if (state.view === "search") return `Поиск: ${state.query}`;
  return el.resultsTitle.textContent;
}
export function playFromList(i) {
  play(state.list, i, { label: listLabel(), mixId: state.view === "mix" ? state.ctx?.mix : null });
}

/**
 * Обработчики списка. Переход на артиста и загрузка — у других экранов, поэтому их передаёт приложение:
 * onArtist(source, artistId), onDownload(track).
 */
export function initList({ onArtist, onDownload }) {
  el.sort.addEventListener("change", () => { state.sort = el.sort.value; sortList(); renderList(); });
  el.dPlay.addEventListener("click", () => playFromList(0));
  el.dShuffle.addEventListener("click", () => {
    if (!state.list.length) return;
    setShuffle(true);
    playFromList(rand(state.list.length));
  });

  // Список треков: один обработчик на всё (игра, избранное, загрузка, меню, ссылка на артиста)
  el.tracks.addEventListener("click", (e) => {
    const row = e.target.closest(".row");
    if (!row || row.classList.contains("row--skel")) return;
    const i = Number(row.dataset.index), t = state.list[i];
    if (!t) return;
    if (e.target.closest(".track__fav")) { toggleFav(t); return; }
    if (e.target.closest(".track__dl")) { onDownload(t); return; }
    const more = e.target.closest(".track__more");
    if (more) { openMenu(t, more); return; }
    const who = e.target.closest(".who.is-link");
    if (who) { onArtist(t.source, who.dataset.aid); return; }   // имя автора: на его страницу, трек не запускаем
    if (queue.isFrom(state.list) && queue.current === t) togglePlay(); else playFromList(i);
  });
}

/** Прогрев при наведении (с задержкой, чтобы не греть всё подряд при движении мыши). */
export function initListWarm() {
  let hoverTimer = 0;
  const warmRow = (e) => {
    const row = e.target.closest(".row");
    if (!row) return;
    clearTimeout(hoverTimer);
    hoverTimer = setTimeout(() => warmTrack(state.list[Number(row.dataset.index)]), 300);
  };
  el.tracks.addEventListener("pointerover", warmRow);
  el.tracks.addEventListener("focusin", warmRow);
  el.tracks.addEventListener("pointerleave", () => clearTimeout(hoverTimer));
}
