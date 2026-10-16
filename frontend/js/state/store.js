// Единое состояние интерфейса, очередь воспроизведения и навигация «Назад». Без DOM.
// Кто чем владеет: экраны пишут в state поля своего экрана (list, cards, view...), плеер — поля воспроизведения.
import { Emitter } from "../core/emitter.js";
import { Queue } from "../player/queue.js";

export const PAGE = 20;                                   // размер страницы ленты, поиска, истории

export const state = {
  list: [],          // то, что показано на экране (список треков текущей страницы); очередь — отдельно (queue)
  lastQuery: "",
  source: "yt",      // активный источник поиска: yt | sc
  sourcePicked: false, // источник выбрал пользователь (источник по умолчанию из настроек его уже не меняет)
  view: "home",
  nav: "home",       // какой пункт меню подсвечен
  tab: "tracks",     // вкладка поиска: tracks | artists | albums
  sort: "default",
  cards: [], albums: [], related: [], colPl: [], colAr: [],
  ctx: null, reopen: null,
  fav: new Set(),    // ключи избранных треков (единственный источник правды)
  favItems: [],      // сами избранные треки (для рекомендаций и коллекции)
  // Настройки из /api/settings (SQLite). Интерфейсные (громкость, тексты, имя, обложки миксов) раньше жили в
  // localStorage, но он привязан к порту окна, а порт меняется при каждом запуске (H3).
  settings: { autoplay: true, theme: "auto", volume: 1, last_volume: 1, lyrics_visible: true, profile_name: "", mix_covers: {} },
  dl: new Map(),     // скачанные треки: "source:id" -> трек
  jobs: {},          // активные загрузки с сервера
  dlDenied: new Map(),   // "source:id" -> причина окончательного отказа источника (на время сессии)
  dlTitles: new Map(),   // "source:id" -> название (для сообщений о готовности и ошибке)
  query: "", offset: 0, more: false, loadingMore: false,
  shuffle: false, seeking: false, errors: 0,
  repeat: "off",     // повтор: "off" | "all" | "one" (player/queue.js; до S10 — булево «повторять трек»)
  resumeAt: null,    // трек восстановлен после перезапуска и ещё не запущен: с какой секунды он начнёт (S10)
  searchCtl: null, baseStatus: "",
  // главная
  feed: [], feedPage: 1, salt: 0, featured: null, personalized: false, feedRadio: false,
  hist: [], mixes: [], mixId: null, artists: [], artistsShown: 4,
  lyricsOn: true,
  lastVol: 1,        // последняя слышимая громкость (для возврата из mute и из нуля)
  fs: { open: false, mode: "default", opener: null },   // полноэкранный плеер
};

/** Одна очередь на всё приложение: собственный массив, список экрана — только источник (M11). */
export const queue = new Queue();

/** Текущий трек очереди или null. */
export const current = () => queue.current;
/** Трек баннера главной: играющий, а если ничего не играет — первый трек подборки. */
export const heroTrack = () => current() || state.featured;

// ---------- Экраны: чистые функции от state ----------
/** Экраны со «шапкой» (обложка, название, кнопки): артист, альбом, плейлист, микс. */
export const DETAIL_VIEWS = ["artist", "album", "playlist", "mix"];
/** Какой раздел главной области показывает экран v (остальные экраны — общий раздел списка). */
export const sectionOf = (v) => (v === "home" || v === "collection" || v === "settings" || v === "now" ? v : "list");
/** Поиск по артистам или альбомам: вместо строк треков — карточки. */
export const isCards = () => state.view === "search" && state.tab !== "tracks";

/** События приложения, когда один экран должен обновить другой, не завися от него напрямую:
 *  "favorites" — избранное сохранено; "mix-covers" — микс получил обложку; "hero" — изменилась очередь,
 *  которую показывает баннер главной. */
export const bus = new Emitter();

let seq = 0;
/** Порядковый номер прихода трека в список (по нему сортировка «по умолчанию»). */
export const nextSeq = () => seq++;

/** История для кнопки «Назад»: каждый экран кладёт в state.reopen функцию, которая его откроет снова. */
export const nav = {
  stack: [],
  /** Что делает «Назад», когда возвращаться некуда (задаёт запуск приложения: главная). */
  fallback: () => {},
  /** Переход «вглубь»: текущая страница запоминается для кнопки «Назад». */
  to(fn) {
    if (state.reopen) this.stack.push(state.reopen);
    if (this.stack.length > 40) this.stack.shift();
    fn();
  },
  back() {
    const back = this.stack.pop();
    if (back) back(); else this.fallback();
  },
  /** Переход из меню и боковой панели: «Назад» начинается заново. */
  reset() { this.stack.length = 0; },
};
