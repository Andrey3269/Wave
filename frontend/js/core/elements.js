// Постоянные узлы разметки index.html — один раз на всё приложение. ID и классы не меняются до этапа
// редизайна: на них опираются tests/e2e_smoke.py и tests/e2e_settings.py (TARGET_ARCHITECTURE §11).
// Модуль выполняется после разбора страницы (модули отложены, как defer), поэтому все узлы уже есть.
import { $, $$ } from "./dom.js";

export const el = {
  main: $("#main"), nav: $("#nav"),
  form: $("#search-form"), input: $("#search-input"), sourceSwitch: $("#source-switch"),
  mixes: $("#mixes"), artistMixes: $("#artist-mixes"), artistMore: $("#artist-more"), artistHint: $("#artist-hint"),
  avatar: $("#avatar"), profileName: $("#profile-name"),
  // главная
  mixRow: $("#mix-row"), hMixes: $("#h-mixes"), secContinue: $("#sec-continue"), continueRow: $("#continue-row"),
  newGrid: $("#new-grid"), homeEmpty: $("#home-empty"), homeMore: $("#home-more"),
  // списки
  detail: $("#detail"), back: $("#back"), dCover: $("#d-cover"), dCoverWrap: $("#d-cover-wrap"), dKind: $("#d-kind"),
  dTitle: $("#d-title"), dSub: $("#d-sub"), dPlay: $("#d-play"), dShuffle: $("#d-shuffle"),
  dRename: $("#d-rename"), dDelete: $("#d-delete"),
  tabs: $("#tabs"), albumsSec: $("#albums-sec"), albumsRow: $("#albums-row"), relatedSec: $("#related-sec"), relatedRow: $("#related-row"),
  results: $("#results"), resultsTitle: $("#results-title"), status: $("#status"), sort: $("#sort"), clearHistory: $("#clear-history"),
  cards: $("#cards"), empty: $("#empty"), tracks: $("#tracks"), tpl: $("#track-template"), more: $("#more"),
  // коллекция
  colTiles: $("#col-tiles"), colPlaylists: $("#col-playlists"), colArtists: $("#col-artists"), colArtistsSec: $("#col-artists-sec"),
  // очередь, меню, диалоги
  queue: $("#queue"), queueList: $("#queue-list"), queueLabel: $("#queue-label"), menu: $("#menu"), toast: $("#toast"),
  dlg: $("#dlg"), dlgTitle: $("#dlg-title"), dlgInput: $("#dlg-input"), dlgOk: $("#dlg-ok"),
  dlgConfirm: $("#dlg-confirm"), dcTitle: $("#dc-title"), dcText: $("#dc-text"), dcOk: $("#dc-ok"),
  dlgPick: $("#dlg-pick"), pickList: $("#pick-list"),
  // мини-плеер
  audio: $("#audio"), playerNow: $("#player-now"), playerOpen: $("#player-open"), cover: $("#player-cover"), title: $("#player-title"),
  artist: $("#player-artist"), playerSrc: $("#player-src"), btnLike: $("#btn-like"),
  progress: $("#progress"), volume: $("#volume"), tCur: $("#time-current"), tTotal: $("#time-total"),
};
/** Разделы главной области: какой из них виден, решает layout() по state.view. */
export const views = { home: $("#view-home"), list: $("#view-list"), collection: $("#view-collection"), settings: $("#view-settings"), now: $("#view-now") };
/** Единственный <audio> приложения. Воспроизведением управляет только плеер. */
export const audio = el.audio;

export const hero = {
  root: $("#hero"), img: $("#hero-img"), eyebrow: $("#hero-eyebrow-text"), title: $("#hero-title"), sub: $("#hero-sub"),
  meta: $("#hero-meta"), play: $("#hero-play"), playLabel: $("#hero-play-label"), like: $("#hero-like"),
  prog: $("#hero-prog"), bar: $("#hero-bar"), cur: $("#hero-cur"), tot: $("#hero-tot"),
  next: $("#hero-next"), nextT: $("#hero-next-t"), ctl: $$(".hero__ctl"),
};
export const now = {
  root: views.now, bg: $("#now-bg"), cover: $("#now-cover"), eyebrow: $("#now-eyebrow"), title: $("#now-title"),
  artist: $("#now-artist"), meta: $("#now-meta"), bar: $("#now-bar"), cur: $("#now-cur"), tot: $("#now-tot"),
  volume: $("#now-volume"), like: $("#now-like"), dl: $("#now-dl"), lyricsBtn: $("#now-lyrics-btn"),
  stage: $("#now-stage"), lyricsPanel: $("#now-lyrics-panel"), lyrics: $("#np-lyrics"),
  similarRow: $("#similar-row"), similarEmpty: $("#similar-empty"), similarArtistsSec: $("#similar-artists-sec"),
  similarArtists: $("#similar-artists"),
};
// Полноэкранный плеер: один компонент, режимы state.fs.mode = default | lyrics | queue
export const fs = {
  root: $("#fs"), bg: $("#fs-bg"), cover: $("#fs-cover"), title: $("#fs-title"), artist: $("#fs-artist"), meta: $("#fs-meta"),
  lyricsBtn: $("#fs-lyrics-btn"), queueBtn: $("#fs-queue-btn"), lyricsPanel: $("#fs-lyrics-panel"), lyrics: $("#fs-lyrics"),
  queuePanel: $("#fs-queue"), qList: $("#fs-q-list"), qScroll: $("#fs-q-scroll"), qCount: $("#fs-q-count"), qClear: $("#fs-q-clear"),
  bar: $("#fs-bar"), cur: $("#fs-cur"), tot: $("#fs-tot"), volume: $("#fs-volume"), play: $("#fs-play"),
};
// Кнопки управления, продублированные на разных экранах: состояние красим на всех сразу
export const ctl = { play: $$('[data-do="play"]'), shuffle: $$('[data-do="shuffle"]'), repeat: $$('[data-do="repeat"]'), mute: $$('[data-do="mute"]') };
