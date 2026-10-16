/* ==========================================================================
   Wave UI (Vanilla JS, ES-модули). Один <audio>, одна очередь, одно состояние избранного.
   Экраны (state.view): home | search | artist | album | mix | playlists | playlist |
   collection | liked | downloads | history | settings | now.
   Звук всегда идёт с нашего бэкенда (track.stream_url), YouTube напрямую не трогаем.
   Кнопки управления плеером (play/prev/next/like/...) помечены data-do и обрабатываются
   одним обработчиком на документе: сколько бы таких кнопок ни было на экране, состояние одно.

   Входной модуль (index.html: <script type="module" src="js/main.js">, TARGET_ARCHITECTURE §11): связывает
   модули frontend/js/**, регистрирует общие обработчики (порядок регистрации — как в прежнем app.js)
   и открывает первый экран. Модули выполняются после разбора страницы, до DOMContentLoaded.
   ========================================================================== */
import { initImageFallback } from "./core/dom.js";
import { api, trackKey } from "./core/api.js";
import { el, now, fs } from "./core/elements.js";
import { state, queue, current, heroTrack, nav } from "./state/store.js";
import { initSettingsSync, initThemeFollowSystem } from "./state/settings.js";
import { toast } from "./components/toast.js";
import { initEmptyRetry } from "./components/skeleton.js";
import { initPickPlaylist } from "./components/dialogs.js";
import { initLyrics } from "./components/lyrics.js";
import { bindCardClicks, initRowScroll } from "./components/cards.js";
import { openMenu, closeMenu, initMenu } from "./components/menu.js";
import { repaintFav, toggleFav } from "./components/favorites.js";
import { shareTrack } from "./components/share.js";
import { toggleMute, initSliders } from "./player/sliders.js";
import { setIdleAction, playIndex, togglePlay, playNext, playPrev, setShuffle, setRepeat,
  initPlayer } from "./player/controller.js";
import { fetchPage, playFromList, initList, initListWarm } from "./views/list.js";
import { syncTabs } from "./views/shell.js";
import { openArtist, openAlbum, goAlbum } from "./views/detail.js";
import { showSearch, goArtist, initSearchForm, initSearchTabs } from "./views/search.js";
import { renderThemeMixes, renderArtistMixes, computeArtists, initSidebar } from "./views/sidebar.js";
import { goHome, startWave, loadMoreHome, initHomeGrids, initHomeMixRow, initHomeHero } from "./views/home.js";
import { openNowFromUi, toggleLyrics, similar, initSimilarGrid, initNow } from "./views/now.js";
import { initPlayerBar } from "./views/player-bar.js";
import { openFs, closeFs, setFsMode, initFullscreen } from "./views/fullscreen.js";
import { queueAdd, queueRemove, queueMove, queueMoveTo, toggleQueue, initQueueDrawer } from "./views/queue-drawer.js";
import { showLiked, showHistory, initHistory, createPlaylistFlow, showPlaylists, openPlaylist, initPlaylistActions,
  addToPlaylistFlow, removeFromPlaylist, showCollection, initCollectionTiles } from "./views/collection.js";
import { refreshDownloads, pollJobs, toggleDownload, showDownloads } from "./views/downloads.js";
import { loadSettings, showSettings, renderProfile, initSettingsView } from "./views/settings.js";

initImageFallback();
initSettingsSync();
initEmptyRetry();

// ---------- Навигация «Назад» ----------
const navTo = (fn) => nav.to(fn);
const goBack = () => nav.back();
nav.fallback = () => goHome(false);

// ---------- Карточки ----------
// Карточки артистов, альбомов и плейлистов на всех экранах: по имени списка обработчик находит данные карточки
const CARD_LISTS = { cards: () => state.cards, albums: () => state.albums, related: () => state.related,
  colpl: () => state.colPl, colar: () => state.colAr, simar: () => similar.artists };
bindCardClicks([el.cards, el.albumsRow, el.relatedRow, el.colPlaylists, el.colArtists, now.similarArtists], CARD_LISTS, (c) => {
  if (c.kind === "new") { createPlaylistFlow(); return; }
  if (c.kind === "artist") navTo(() => openArtist(c.source, c.id));
  else if (c.kind === "playlist") navTo(() => openPlaylist(c.id));
  else navTo(() => openAlbum(c.source, c.id, c.playlist_id));
});
initHomeGrids();                                         // карточки треков главной и «Продолжить слушать»
initSimilarGrid();                                       // «Похожие» на странице трека
initRowScroll();                                         // стрелки у горизонтальных рядов

initSidebar();                                           // «Твоя музыка» и «Миксы по артистам»
initHomeMixRow();

// ---------- Воспроизведение ----------
// Play при пустом <audio>: на главной — подборка, иначе очередь или список на экране
setIdleAction(() => {
  if (state.view === "home") return startWave();
  if (queue.length) return playIndex(Math.max(0, queue.index));
  if (state.list.length) return playFromList(0);
  el.input.focus();
  return undefined;
});
// Мини-плеер, баннер главной и страница трека показывают одно состояние (подписки на события плеера)
initPlayerBar();
initHomeHero();
initNow();
initPlayer();
initSliders();
initLyrics();
initFullscreen();

initHistory();                                           // «Очистить историю»
initQueueDrawer();
initPickPlaylist();

// ---------- Меню трека ----------
initMenu({
  playnow: (t, qi) => playIndex(qi),
  movenext: (t, qi) => queueMoveTo(qi, queue.index + 1),
  up: (t, qi) => queueMove(qi, -1),
  down: (t, qi) => queueMove(qi, 1),
  rm: (t, qi) => queueRemove(qi),
  fav: (t) => toggleFav(t),
  next: (t) => queueAdd(t, true),
  add: (t) => queueAdd(t, false),
  playlist: (t) => addToPlaylistFlow(t),
  unlist: (t) => removeFromPlaylist(t),
  // переходим на другую страницу: полноэкранный режим закрываем
  artist: (t) => { if (state.fs.open) closeFs(); goArtist(t); },
  album: (t) => { if (state.fs.open) closeFs(); goAlbum(t); },
  download: (t) => toggleDownload(t),
  share: (t) => shareTrack(t),
});

initPlaylistActions();                                   // «Переименовать», «Удалить» плейлист
initCollectionTiles();
initThemeFollowSystem();
initSettingsView();

// ==========================================================================
// Обработчики интерфейса
// ==========================================================================
/** Все кнопки с data-do (мини-плеер, баннер, страница трека). */
const ACTIONS = {
  play: () => togglePlay(),
  prev: () => playPrev(),
  next: () => playNext(),
  shuffle: () => setShuffle(!state.shuffle),
  repeat: () => setRepeat(!state.repeat),
  like: (b) => { const t = b.closest("#hero") ? heroTrack() : current(); if (t) toggleFav(t); },
  queue: () => toggleQueue(),
  now: () => openNowFromUi(),
  back: () => goBack(),
  playlist: () => { const t = current(); if (t) addToPlaylistFlow(t); },
  download: () => { const t = current(); if (t) toggleDownload(t); },
  lyrics: () => toggleLyrics(),
  share: () => { const t = current(); if (t) shareTrack(t); },
  more: (b) => { const t = b.closest("#hero") ? heroTrack() : current(); if (t) openMenu(t, b); },
  expand: () => openFs(),
  mute: () => toggleMute(),
};
document.addEventListener("click", (e) => {
  const b = e.target.closest("[data-do]");
  if (b && ACTIONS[b.dataset.do]) ACTIONS[b.dataset.do](b, e);
});
el.playerNow.addEventListener("keydown", (e) => {
  if ((e.key === "Enter" || e.key === " ") && e.target === el.playerNow) { e.preventDefault(); openNowFromUi(); }
});

initSearchForm();

// Боковая панель и все кнопки с data-view
document.addEventListener("click", (e) => {
  const item = e.target.closest("button[data-view]");      // не просто [data-view]: этот атрибут есть и у <body>
  if (!item) return;
  const view = item.dataset.view;
  nav.reset();
  el.main.scrollTo({ top: 0 });
  const go = { home: () => goHome(true), search: showSearch, playlists: showPlaylists, collection: showCollection,
    liked: showLiked, downloads: showDownloads, history: showHistory, settings: showSettings }[view];
  if (go) go();
});

initSearchTabs();                                        // вкладки поиска и источник

el.back.addEventListener("click", goBack);
// Список треков: игра, избранное, загрузка, меню, ссылка на артиста; сортировка; «Играть» и «Перемешать»
initList({ onArtist: (source, aid) => navTo(() => openArtist(source, aid)), onDownload: (t) => toggleDownload(t) });

// Имя автора в мини-плеере, на странице трека и в полноэкранном плеере
function artistLinkClick(e) {
  const who = e.target.closest(".who.is-link");
  const t = current();
  if (!who || !t) return;
  e.stopPropagation();
  if (state.fs.open) closeFs();
  navTo(() => openArtist(t.source, who.dataset.aid));
}
el.artist.addEventListener("click", artistLinkClick);
now.artist.addEventListener("click", artistLinkClick);
fs.artist.addEventListener("click", artistLinkClick);

initListWarm();                                          // прогрев трека при наведении

// Бесконечная прокрутка
function loadMore() {
  if (!state.more || state.loadingMore) return;
  if (state.view === "home") loadMoreHome();
  else if (state.query || state.view === "artist" || state.view === "history") fetchPage(true);
}
const io = new IntersectionObserver((e) => { if (e.some((x) => x.isIntersecting)) loadMore(); }, { root: el.main, rootMargin: "400px" });
io.observe(el.more);
io.observe(el.homeMore);
el.more.addEventListener("click", loadMore);
el.homeMore.addEventListener("click", loadMore);

// Горячие клавиши: Пробел: пауза/пуск, "/": фокус на поиск, Esc: закрыть верхнее
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    if (el.dlg.open || el.dlgConfirm.open || el.dlgPick.open) return;     // диалог закроется сам
    if (!el.menu.hidden) { closeMenu(); return; }
    if (state.fs.open) { if (state.fs.mode !== "default") setFsMode("default"); else closeFs(); return; }   // сначала закрываем панель, потом сам режим
    if (!el.queue.hidden) { el.queue.hidden = true; return; }
    if (state.view === "now") goBack();
    return;
  }
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  const t = e.target;
  const typing = t.matches && t.matches("input, textarea, select, [contenteditable]");
  if (e.key === "/" && !typing && !state.fs.open) { e.preventDefault(); el.input.focus(); return; }
  if (e.code === "KeyM" && !typing) { toggleMute(); return; }
  // В полноэкранном плеере пробел всегда пауза/пуск (кроме полей ввода и меню), а не нажатие на кнопку в фокусе
  const spaceOk = state.fs.open ? !(t.closest && t.closest("#menu")) : !(t.closest && t.closest("button, [role=slider], [role=button]"));
  if (e.code === "Space" && !typing && spaceOk) {
    e.preventDefault();
    togglePlay();
  }
});
document.addEventListener("keyup", (e) => {              // кнопка в фокусе нажимается по keyup: не даём пробелу сработать второй раз
  if (e.code === "Space" && state.fs.open && !(e.target.closest && e.target.closest("input, textarea, select, #menu"))) e.preventDefault();
});

// Нет интернета: сразу показываем скачанное
window.addEventListener("offline", () => { showDownloads().then(() => toast("Нет соединения. Доступны скачанные треки.")); });

// ==========================================================================
// Старт
// ==========================================================================
async function boot() {
  renderProfile();
  renderThemeMixes();
  renderArtistMixes();
  syncTabs();
  await Promise.allSettled([
    api("/api/favorites").then((d) => { state.fav = new Set(d.items.map(trackKey)); state.favItems = d.items; computeArtists(); renderArtistMixes(); repaintFav(); }),
    loadSettings(),
  ]);
  // Первый экран — только если пользователь, пока шли запросы выше, сам ничего не открыл (каждый экран
  // выставляет state.reopen). Иначе поиск, начатый в первые секунды, уводило на главную.
  if (!state.reopen) { if (navigator.onLine) goHome(true); else showDownloads(); }
  refreshDownloads();
  pollJobs();                                            // подхватываем загрузки, которые шли до перезагрузки страницы
}
boot();
