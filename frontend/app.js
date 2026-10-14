/* ==========================================================================
   Wave UI (Vanilla JS). Один <audio>, одна очередь, одно состояние избранного.
   Экраны (state.view): home | search | artist | album | mix | playlists | playlist |
   collection | liked | downloads | history | settings | now.
   Звук всегда идёт с нашего бэкенда (track.stream_url), YouTube напрямую не трогаем.
   Кнопки управления плеером (play/prev/next/like/...) помечены data-do и обрабатываются
   одним обработчиком на документе: сколько бы таких кнопок ни было на экране, состояние одно.
   Подключается как ES-модуль (строгий режим включён всегда, отдельная директива не нужна).
   ========================================================================== */
import { $, $$, mk, icon, button, setCover, coverEl, initImageFallback } from "./js/core/dom.js";
import { SOURCES, clamp, rand, fmtTime, plural, thumb } from "./js/core/format.js";
import { enc, api, getJSON, jsonOpts, recsResult, trackKey, trackPayload } from "./js/core/api.js";
import { el, views, audio, hero, now, fs, ctl } from "./js/core/elements.js";
import { state, queue, current, PAGE, nextSeq, nav } from "./js/state/store.js";
import { uiSave, initSettingsSync, round3, markLocal, settingsGeneration, flags, applySettings, fetchSettings,
  migrateLocalUi, initThemeFollowSystem } from "./js/state/settings.js";
import { toast, hideToast } from "./js/components/toast.js";
import { setBlock, initEmptyRetry, skelRows, skelCards } from "./js/components/skeleton.js";
import { askText, askConfirm, pickPlaylist, initPickPlaylist } from "./js/components/dialogs.js";

(() => {
  /** Показывает значок источника в узле .src (или прячет, если источник неизвестен). */
  function setSource(node, source) {
    const name = SOURCES[source];
    node.hidden = !name;
    if (!name) return;
    node.title = name;
    node.setAttribute("aria-label", name);
    node.querySelector("use").setAttribute("href", `#i-src-${source}`);
  }

  initImageFallback();

  const heroTrack = () => current() || state.featured;
  initSettingsSync();

  // ---------- Статус и пустые состояния ----------
  function setStatus(text) { state.baseStatus = text; el.status.textContent = text; }
  initEmptyRetry();

  // ==========================================================================
  // Экраны и каркас
  // ==========================================================================
  const NAV_OF = { home: "home", search: "search", playlists: "playlists", playlist: "playlists", collection: "collection",
    liked: "liked", downloads: "downloads", history: "history", settings: "settings" };
  const DETAIL_VIEWS = ["artist", "album", "playlist", "mix"];
  const sectionOf = (v) => (v === "home" || v === "collection" || v === "settings" || v === "now" ? v : "list");
  const isCards = () => state.view === "search" && state.tab !== "tracks";

  /** Показывает нужные блоки для state.view. Мини-плеер скрывает CSS по body[data-view] и body[data-track]. */
  function layout() {
    const v = state.view, sec = sectionOf(v);
    Object.entries(views).forEach(([k, n]) => { n.hidden = k !== sec; });
    el.detail.hidden = !DETAIL_VIEWS.includes(v);
    el.tabs.hidden = !(v === "search" && state.query);
    el.dRename.hidden = el.dDelete.hidden = v !== "playlist";
    el.albumsSec.hidden = !(v === "artist" && state.albums.length);
    el.relatedSec.hidden = !(v === "artist" && state.related.length);
    el.clearHistory.hidden = v !== "history";
    const showCards = isCards() || v === "playlists";
    el.cards.hidden = !showCards;
    el.tracks.hidden = showCards;

    if (v in NAV_OF) state.nav = NAV_OF[v];
    else if (v === "mix") state.nav = null;
    $$(".nav__item", el.nav.closest(".side")).forEach((n) => {
      if (n.dataset.view === state.nav) n.setAttribute("aria-current", "page"); else n.removeAttribute("aria-current");
    });
    document.body.dataset.view = v;
    document.body.dataset.track = current() ? "1" : "0";
    if (v !== "now" && state.fs.open) closeFs();
    updateSort();
  }

  const navTo = (fn) => nav.to(fn);
  const goBack = () => nav.back();
  nav.fallback = () => goHome(false);

  function syncTabs() {
    $$(".tab", el.tabs).forEach((b) => b.setAttribute("aria-selected", String(b.dataset.tab === state.tab)));
    $$(".seg__btn", el.sourceSwitch).forEach((b) => b.setAttribute("aria-selected", String(b.dataset.source === state.source)));
  }

  function showDetail(kind, title, sub, cover, round) {
    el.dKind.textContent = kind;
    el.dTitle.textContent = title;
    el.dSub.textContent = sub || "";
    el.dCoverWrap.classList.toggle("detail__cover--round", !!round);
    setCover(el.dCover, cover, 544);
  }

  // ==========================================================================
  // Список треков (строки) и карточки
  // ==========================================================================
  /** Строка трека из шаблона. */
  function buildRow(t, i) {
    const node = el.tpl.content.cloneNode(true);
    const li = $(".row", node);
    li.dataset.index = i;
    li.dataset.key = trackKey(t);
    $(".track", node).setAttribute("aria-label", `Играть: ${t.artist} — ${t.title}`);
    $(".track__idx", node).textContent = i + 1;
    setCover($(".track__cover img", node), t.cover, 120);
    $(".track__title", node).textContent = t.title;
    renderArtists($(".track__artist", node), t);          // каждый автор отдельной ссылкой на его страницу
    setSource($(".src", node), t.source);
    $(".track__album", node).textContent = t.reason || t.album || "";   // у рекомендаций здесь причина
    $(".track__time", node).textContent = t.duration ? fmtTime(t.duration) : "";
    paintFav($(".track__fav", node), t);
    paintDl($(".track__dl", node), t);
    return node;
  }
  const rowTrack = (node) => state.list[Number(node.closest(".row")?.dataset.index)];

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
  function updateSort() {
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
  function setTracks(tracks) {
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

  /** Имена авторов трека: каждый отдельным элементом; со ссылкой, если известен его id. */
  function renderArtists(node, t) {
    const names = t.artists && t.artists.length ? t.artists : [t.artist];
    const ids = t.artist_ids && t.artist_ids.length ? t.artist_ids : (t.artist_id ? [t.artist_id] : []);
    node.replaceChildren();
    names.forEach((name, i) => {
      if (i) node.append(", ");
      const span = mk("span", "who", name);
      if (ids[i]) { span.classList.add("is-link"); span.dataset.aid = ids[i]; }
      node.append(span);
    });
  }

  // ---------- карточки артистов, альбомов, плейлистов ----------
  const cardKey = (c) => `${c.source}:${c.kind}:${c.id}`;
  /** list: ключ из CARD_LISTS, по нему обработчик находит данные карточки. */
  function buildCard(c, i, list = "cards") {
    const b = mk("button", `card card--${c.kind}`);
    b.type = "button";
    b.dataset.index = i;
    b.dataset.list = list;
    if (c.kind === "new") {
      const plus = mk("span", "card__cv card__cv--plus");
      plus.append(icon("i-plus"));
      b.append(plus, mk("span", "card__title", "Новый плейлист"), mk("span", "card__sub", "Создать"));
      return b;
    }
    const sub = c.kind === "artist" ? (c.subtitle || "Артист")
      : c.kind === "playlist" ? `${c.count} тр.` : [c.artist, c.year].filter(Boolean).join(" · ");
    b.append(coverEl(c.cover, 320, "card__cv"), mk("span", "card__title", c.name || c.title), mk("span", "card__sub", sub));
    return b;
  }
  function renderCards(from = 0, append = false) {
    const nodes = state.cards.slice(from).map((c, i) => buildCard(c, from + i, "cards"));
    if (append) el.cards.append(...nodes); else el.cards.replaceChildren(...nodes);
  }
  const renderRow = (root, items, list) => root.replaceChildren(...items.map((c, i) => buildCard(c, i, list)));
  const CARD_LISTS = { cards: () => state.cards, albums: () => state.albums, related: () => state.related,
    colpl: () => state.colPl, colar: () => state.colAr, simar: () => similar.artists };

  function onCardClick(e) {
    const node = e.target.closest(".card");
    if (!node) return;
    const c = (CARD_LISTS[node.dataset.list] || (() => []))()[Number(node.dataset.index)];
    if (!c) return;
    if (c.kind === "new") { createPlaylistFlow(); return; }
    if (c.kind === "artist") navTo(() => openArtist(c.source, c.id));
    else if (c.kind === "playlist") navTo(() => openPlaylist(c.id));
    else navTo(() => openAlbum(c.source, c.id, c.playlist_id));
  }
  [el.cards, el.albumsRow, el.relatedRow, el.colPlaylists, el.colArtists, now.similarArtists].forEach((r) => r.addEventListener("click", onCardClick));

  // ---------- карточки треков (главная, «похожие») ----------
  function trackCard(t, i) {
    const art = mk("article", "tcard");
    art.dataset.index = i;
    art.dataset.key = trackKey(t);
    const open = mk("button", "tcard__open");
    open.type = "button";
    open.setAttribute("aria-label", `Играть: ${t.artist} — ${t.title}`);
    const cv = coverEl(t.cover, 320, "tcard__cv");
    const ov = mk("span", "tcard__ov");
    ov.append(icon("i-play", "icon--fill ico-play"), icon("i-pause", "icon--fill ico-pause"));
    cv.append(ov);
    open.append(cv, mk("span", "tcard__title", t.title), mk("span", "tcard__sub", t.artist));
    const more = button("tbtn tcard__more", "Ещё", "i-more", true);
    more.setAttribute("aria-haspopup", "menu");
    art.append(open, more);
    return art;
  }
  function miniCard(t, i) {
    const art = mk("article", "minicard");
    art.dataset.index = i;
    art.dataset.key = trackKey(t);
    const open = mk("button", "minicard__open");
    open.type = "button";
    open.setAttribute("aria-label", `Играть: ${t.artist} — ${t.title}`);
    const body = mk("span", "minicard__body");
    const bar = mk("span", "minibar");
    bar.append(mk("i"));
    body.append(mk("span", "minicard__title", t.title), mk("span", "minicard__sub", t.artist), bar);
    const time = mk("span", "minicard__time", t.duration ? fmtTime(t.duration) : "");
    const play = mk("span", "minicard__play");
    play.append(icon("i-play", "icon--fill ico-play"), icon("i-pause", "icon--fill ico-pause"));
    open.append(coverEl(t.cover, 120, "minicard__cv"), body, time, play);
    art.append(open);
    return art;
  }
  /** Один обработчик на ряд карточек: клик = играть (или пауза, если это текущий трек), «⋯» = меню. */
  function bindTrackGrid(root, getArr, getLabel) {
    root.addEventListener("click", (e) => {
      const card = e.target.closest("[data-index]");
      if (!card || !root.contains(card)) return;
      const arr = getArr(), i = Number(card.dataset.index), t = arr[i];
      if (!t) return;
      if (e.target.closest(".tcard__more")) { openMenu(t, e.target.closest("button")); return; }
      if (queue.isFrom(arr) && queue.current === t) togglePlay(); else playTracks(arr, i, getLabel());
    });
  }
  bindTrackGrid(el.newGrid, () => state.feed, () => feedLabel());
  bindTrackGrid(el.continueRow, () => state.hist, () => "Недавно слушали");
  bindTrackGrid(now.similarRow, () => similar.tracks, () => `Похожие на «${current()?.title || "трек"}»`);

  document.addEventListener("click", (e) => {            // стрелки у горизонтальных рядов
    const b = e.target.closest("[data-scroll]");
    if (!b) return;
    const [id, dir] = b.dataset.scroll.split(":");
    const row = document.getElementById(id);
    if (row) row.scrollBy({ left: Number(dir) * row.clientWidth * 0.85, behavior: "smooth" });
  });

  // ==========================================================================
  // Поиск и ленты (поиск, артист, история)
  // ==========================================================================
  function feedUrl(offset) {
    const q = enc(state.query);
    if (state.view === "history") return `/api/history?limit=${PAGE}&offset=${offset}`;
    if (state.view === "artist") return `/api/artist/${state.ctx.source}/${enc(state.ctx.id)}/tracks?limit=${PAGE}&offset=${offset}`;
    if (state.tab === "tracks") return `/api/search?q=${q}&limit=${PAGE}&offset=${offset}&sources=${state.source}`;
    return `/api/search/${state.tab}?q=${q}&limit=${PAGE}&offset=${offset}&source=${state.source}`;
  }
  function emptyText() {
    if (state.view === "history") return "Ты ещё ничего не слушал. Найди что-нибудь для первой сессии.";
    if (state.view === "artist") return "Треков у этого артиста не найдено.";
    return `По запросу «${state.query}» ничего не найдено. Попробуй изменить запрос.`;
  }
  /** Показывает скелетон в том контейнере, который сейчас виден. */
  function showSkeleton() {
    setBlock(el.empty, "");
    if (isCards() || state.view === "playlists") el.cards.replaceChildren(...skelCards());
    else el.tracks.replaceChildren(...skelRows());
  }
  function clearSkeleton() { el.tracks.replaceChildren(); el.cards.replaceChildren(); }

  /**
   * Одна страница ленты (поиск по вкладке, треки артиста, история). append=false: сначала,
   * append=true: бесконечная прокрутка. Возвращает список или null (ошибка/отмена).
   */
  async function fetchPage(append) {
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

  function resetList(extra) {
    state.searchCtl?.abort();
    Object.assign(state, { query: "", offset: 0, more: false, sort: "default", cards: [], albums: [], related: [] }, extra);
    el.more.hidden = true;
  }

  function search(query) {
    query = query.trim();
    if (!query) return Promise.resolve(null);
    resetList({ view: "search", query });
    const snap = { query, tab: state.tab, source: state.source };
    state.reopen = () => { Object.assign(state, { tab: snap.tab, source: snap.source }); syncTabs(); el.input.value = snap.query; search(snap.query); };
    el.resultsTitle.textContent = "Результаты";
    setStatus("Ищем…");
    layout();
    showSkeleton();
    return fetchPage(false);
  }

  function showSearch() {
    state.reopen = () => showSearch();
    if (state.view !== "search" || !state.query) {
      resetList({ view: "search" });
      setTracks([]);
      el.cards.replaceChildren();
      el.resultsTitle.textContent = "Поиск";
      setStatus("");
      setBlock(el.empty, "Введи название трека или исполнителя и нажми Enter.");
    }
    layout();
    el.input.focus();
  }

  // ---------- страницы артиста и альбома ----------
  async function openArtist(source, id) {
    resetList({ view: "artist", ctx: { source, id } });
    const ctl = (state.searchCtl = new AbortController());
    state.reopen = () => openArtist(source, id);
    setTracks([]);
    showDetail("Артист", "Загружаем…", "", null, true);
    el.resultsTitle.textContent = "Популярные треки";
    setStatus("");
    layout();
    showSkeleton();
    try {
      const d = await getJSON(`/api/artist/${source}/${enc(id)}`, ctl.signal);
      showDetail("Артист", d.artist.name, d.artist.subtitle, d.artist.cover, true);
      state.albums = d.albums || [];
      state.related = d.related || [];
      renderRow(el.albumsRow, state.albums, "albums");
      renderRow(el.relatedRow, state.related, "related");
      setTracks(d.tracks);
      state.offset = d.tracks.length;
      state.more = d.has_more && d.tracks.length > 0;
      el.more.hidden = !state.more; el.more.textContent = "";
      setBlock(el.empty, d.tracks.length ? "" : emptyText());
      layout();
      el.main.scrollTo({ top: 0 });
    } catch (err) {
      if (err.name === "AbortError") return;
      showDetail("Артист", "Не удалось открыть артиста", err.message, null, true);
      clearSkeleton();
      setBlock(el.empty, "Источник не ответил. Попробуй ещё раз.", () => openArtist(source, id));
    }
  }

  async function openAlbum(source, id, pl) {
    resetList({ view: "album", ctx: { source, id } });
    const ctl = (state.searchCtl = new AbortController());
    state.reopen = () => openAlbum(source, id, pl);
    setTracks([]);
    showDetail("Альбом", "Загружаем…", "", null, false);
    el.resultsTitle.textContent = "Треки";
    setStatus("");
    layout();
    showSkeleton();
    try {
      const d = await getJSON(`/api/album/${source}/${enc(id)}${pl ? `?pl=${enc(pl)}` : ""}`, ctl.signal);
      const mins = Math.round(d.tracks.reduce((s, t) => s + (t.duration || 0), 0) / 60);
      const info = [d.album.artist, d.album.year, d.tracks.length ? `${d.tracks.length} тр.${mins ? `, ${mins} мин` : ""}` : ""].filter(Boolean).join(" · ");
      showDetail("Альбом", d.album.title, info, d.album.cover, false);
      setTracks(d.tracks);
      setBlock(el.empty, d.tracks.length ? "" : (d.unavailable
        ? "Источник не отдаёт эти треки: они закрыты в твоём регионе или доступны только по подписке (на SoundCloud это часто 30-секундные превью)."
        : "В этом альбоме нет доступных треков."));
      layout();
      el.main.scrollTo({ top: 0 });
    } catch (err) {
      if (err.name === "AbortError") return;
      showDetail("Альбом", "Не удалось открыть альбом", err.message, null, false);
      clearSkeleton();
      setBlock(el.empty, "Попробуй ещё раз или открой другой альбом.", () => openAlbum(source, id, pl));
    }
  }

  // ---------- миксы ----------
  // Мини-«рецепты»: список треков собирается по клику существующими API (поиск, рекомендации).
  // Названия описывают, что подборка делает на самом деле (M13): без фирменных названий чужих сервисов
  // и без обещаний персонализации там, где её нет. id не меняются: по ним хранятся обложки (mix_covers).
  const YEAR = new Date().getFullYear();
  const THEMES = [
    { id: "mood", title: "Для настроения", sub: "Спокойная подборка на вечер", tone: 1, q: "chill" },
    { id: "hiphop", title: "Хип-хоп и электроника", sub: "Тяжёлые биты и атмосфера", tone: 2, q: "hip hop electronic" },
    { id: "discover", title: `Новинки ${YEAR}`, sub: `Поиск по запросу «новинки ${YEAR}»`, tone: 3, q: `новинки ${YEAR}` },
    { id: "shake", title: "Другая подборка", sub: "Новая выборка из рекомендаций", tone: 4, wave: true },
    { id: "indie", title: "Инди без границ", sub: "Альтернатива, которая вдохновляет", tone: 5, q: "indie alternative" },
  ];
  const covers = {};                                         // id микса -> обложка; приходит из настроек (mix_covers)
  const themeMixes = THEMES.map((th) => ({
    id: `theme:${th.id}`, title: th.title, sub: th.sub, tone: th.tone, cover: covers[th.id] || null, fresh: !!th.wave, themeId: th.id,
    async load() {
      if (th.wave) return recsResult(await api(`/api/recommendations?limit=40&page=${rand(3)}&salt=${rand(1e6)}`)).tracks;
      const q = typeof th.q === "function" ? th.q() : th.q;
      return (await api(`/api/search?q=${enc(q)}&limit=40&sources=${state.source}`)).tracks || [];
    },
  }));
  const radioMemo = new Map();
  function radioMix(t) {
    const id = `radio:${trackKey(t)}`;
    if (!radioMemo.has(id)) {
      radioMemo.set(id, { id, title: `Похоже на «${t.title}»`, sub: `Радио · ${t.artist}`, cover: t.cover, fresh: false,
        async load() { return recsResult(await api(`/api/recommendations?limit=40&source=${t.source}&id=${enc(t.id)}&title=${enc(t.title)}`)).tracks; } });
    }
    return radioMemo.get(id);
  }
  const MIX_TTL = 10 * 60 * 1000;
  async function loadMix(mix) {
    if (!mix.fresh && mix._tracks && Date.now() - mix._at < MIX_TTL) return mix._tracks;
    const have = new Set();
    const tracks = (await mix.load()).filter((t) => !have.has(trackKey(t)) && have.add(trackKey(t)));
    mix._tracks = tracks; mix._at = Date.now();
    if (tracks[0] && !mix.cover && mix.themeId) {          // запоминаем обложку микса для боковой панели
      mix.cover = tracks[0].cover;
      covers[mix.themeId] = mix.cover;
      uiSave.put({ mix_covers: { ...covers } }, 0);
      renderThemeMixes();
    }
    return tracks;
  }
  /** Ряд миксов: радио по недавним трекам (если есть история), остальное добираем тематическими подборками. */
  function buildMixes() {
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
  async function playMix(mix) {
    toast("Собираем микс…", 8000);
    try {
      const tracks = await loadMix(mix);
      if (!tracks.length) { toast("Не удалось собрать микс: источник ничего не вернул."); return; }
      hideToast();
      playTracks(tracks, 0, mix.title);
      state.mixId = mix.id; markMix();
    } catch (e) { toast(`Не удалось собрать микс: ${e.message}`); }
  }
  let mixSeq = 0;
  async function openMix(mix) {
    resetList({ view: "mix", ctx: { mix: mix.id } });
    const my = ++mixSeq;
    state.reopen = () => openMix(mix);
    setTracks([]);
    showDetail("Микс", mix.title, mix.sub, mix.cover, false);
    el.resultsTitle.textContent = "Треки";
    setStatus("");
    layout();
    markMix();
    showSkeleton();
    try {
      const tracks = await loadMix(mix);
      if (my !== mixSeq || state.view !== "mix") return;
      showDetail("Микс", mix.title, `${mix.sub} · ${tracks.length} тр.`, mix.cover || tracks[0]?.cover, false);
      setTracks(tracks);
      setBlock(el.empty, tracks.length ? "" : "Источник ничего не вернул для этого микса.", () => openMix(mix));
      el.main.scrollTo({ top: 0 });
    } catch (e) {
      if (my !== mixSeq) return;
      clearSkeleton();
      setBlock(el.empty, `Не удалось собрать микс: ${e.message}`, () => openMix(mix));
    }
  }
  function markMix() {
    $$("[data-mix]").forEach((n) => n.classList.toggle("is-active", n.dataset.mix === state.mixId || (state.view === "mix" && n.dataset.mix === state.ctx?.mix)));
  }

  // ---------- боковая панель: «Твоя музыка» и «Миксы по артистам» ----------
  function sideItem({ cover, title, sub, tone, data }) {
    const li = mk("li");
    const b = mk("button", "mix");
    b.type = "button";
    Object.assign(b.dataset, data);
    const cv = coverEl(cover, 96, "mix__cv");
    if (!cover) { cv.dataset.tone = tone || 0; cv.append(icon("i-wave")); }
    const text = mk("span", "mix__t");
    text.append(mk("span", "mix__title", title), mk("span", "mix__sub", sub));
    b.append(cv, text, icon("i-chev-r", "mix__go"));
    b.title = title;
    li.append(b);
    return li;
  }
  function renderThemeMixes() {
    el.mixes.replaceChildren(...themeMixes.map((m) => sideItem({ cover: m.cover, title: m.title, sub: m.sub, tone: m.tone, data: { mix: m.id } })));
    markMix();
  }
  function renderArtistMixes() {
    const shown = state.artists.slice(0, state.artistsShown);
    el.artistMixes.replaceChildren(...shown.map((a, i) => sideItem({ cover: a.cover, title: a.name, sub: a.id ? "Страница артиста" : "Поиск по имени", tone: 0, data: { artist: i } })));
    el.artistHint.hidden = state.artists.length > 0;
    el.artistMore.hidden = state.artists.length <= 4;
    el.artistMore.textContent = state.artistsShown > 4 ? "Свернуть" : "Показать ещё";
  }
  /** Любимые артисты: избранное весит втрое больше недавних прослушиваний (как в рекомендациях на сервере). */
  function computeArtists() {
    const map = new Map();
    const add = (t, w) => {
      const name = (t.artists && t.artists[0]) || t.artist;
      if (!name) return;
      const k = name.toLowerCase();
      const e = map.get(k) || { name, w: 0, cover: null, source: t.source, id: null };
      e.w += w;
      e.cover = e.cover || t.cover;
      const aid = (t.artist_ids && t.artist_ids[0]) || t.artist_id;
      if (aid && !e.id) { e.id = aid; e.source = t.source; }
      map.set(k, e);
    };
    state.favItems.forEach((t) => add(t, 3));
    state.hist.forEach((t) => add(t, 1));
    state.artists = [...map.values()].sort((a, b) => b.w - a.w).slice(0, 12);
  }
  let artistTimer = 0;
  function scheduleArtists() {
    clearTimeout(artistTimer);
    artistTimer = setTimeout(() => { computeArtists(); renderArtistMixes(); }, 1200);
  }
  function openArtistByName(a) {
    if (a.id) { openArtist(a.source, a.id); return; }
    state.tab = "artists"; syncTabs(); el.input.value = a.name; search(a.name);
  }
  el.mixes.addEventListener("click", (e) => {
    const b = e.target.closest("[data-mix]");
    const m = b && themeMixes.find((x) => x.id === b.dataset.mix);
    if (m) { nav.reset(); openMix(m); }
  });
  el.artistMixes.addEventListener("click", (e) => {
    const b = e.target.closest("[data-artist]");
    const a = b && state.artists[Number(b.dataset.artist)];
    if (a) { nav.reset(); openArtistByName(a); }
  });
  el.artistMore.addEventListener("click", () => {
    state.artistsShown = state.artistsShown > 4 ? 4 : 10;
    renderArtistMixes();
  });

  // ==========================================================================
  // Главная: лента /api/recommendations
  // ==========================================================================
  const home = { seq: 0, loading: false, error: "", partial: "", ready: false, at: 0 };
  const HOME_TTL = 10 * 60 * 1000;

  function goHome(refresh = true) {
    state.reopen = () => goHome(false);
    state.searchCtl?.abort();
    Object.assign(state, { view: "home", query: "", cards: [], albums: [], related: [], sort: "default" });
    state.list = [];
    el.tracks.replaceChildren(); el.cards.replaceChildren();
    el.more.hidden = true;
    layout();
    if (refresh || !home.ready || Date.now() - home.at > HOME_TTL) { loadHome(); }
    else { renderHome(); refreshHistory().then(() => { if (state.view === "home") { renderContinue(); renderMixRow(); } }); }
    el.main.scrollTo({ top: 0 });
  }

  async function refreshHistory() {
    try { state.hist = (await api("/api/history?limit=50")).tracks || []; } catch { /* оставляем прежнее */ }
    computeArtists();
    renderArtistMixes();
  }

  async function loadHome() {
    const my = ++home.seq;
    home.loading = true; home.error = ""; home.partial = "";
    state.salt = rand(1e6);
    renderHome();
    const cur = current();
    const qs = new URLSearchParams({ limit: "30", page: "0", salt: String(state.salt) });
    if (cur) { qs.set("source", cur.source); qs.set("id", cur.id); qs.set("title", cur.title); }
    const [recs] = await Promise.all([api(`/api/recommendations?${qs}`).then((d) => ({ d, r: recsResult(d) })).catch((e) => e), refreshHistory()]);
    if (my !== home.seq) return;
    home.loading = false;
    if (recs instanceof Error) {
      home.error = recs.message || "Не удалось подобрать музыку.";
      Object.assign(state, { feed: [], featured: null, more: false });
    } else {
      home.partial = recs.r.partial;
      Object.assign(state, { feed: recs.r.tracks, featured: recs.r.tracks[0] || null, personalized: !!recs.d.personalized,
        feedRadio: !!recs.d.radio, feedPage: 1 });
      state.more = state.feed.length > 0;
      home.ready = true; home.at = Date.now();
      warmTrack(state.featured);
    }
    if (state.view === "home") renderHome();
  }

  function renderHome() {
    renderHero();
    renderMixRow();
    renderContinue();
    renderFeed();
  }
  function mixCard(m) {
    const art = mk("article", "mixcard");
    art.dataset.mix = m.id;
    const open = mk("button", "mixcard__open");
    open.type = "button";
    open.setAttribute("aria-label", `Открыть микс: ${m.title}`);
    const cv = coverEl(m.cover, 320, "mixcard__cv");
    if (!m.cover) { cv.dataset.tone = m.tone || 0; cv.append(icon("i-wave")); }
    const body = mk("span", "mixcard__body");
    body.append(mk("span", "mixcard__title", m.title), mk("span", "mixcard__sub", m.sub));
    open.append(cv, body);
    const play = button("playbtn mixcard__play", `Слушать: ${m.title}`, "i-play", true);
    art.append(open, play);
    return art;
  }
  function renderMixRow() {
    state.mixes = buildMixes();
    // Заголовок честный: «по вашим трекам» — только когда в ряду есть радио по истории или избранному
    el.hMixes.textContent = state.mixes.some((m) => m.id.startsWith("radio:")) ? "Миксы по вашим трекам" : "Тематические миксы";
    el.mixRow.replaceChildren(...state.mixes.map(mixCard));
    markMix();
  }
  el.mixRow.addEventListener("click", (e) => {
    const card = e.target.closest(".mixcard");
    const m = card && state.mixes.find((x) => x.id === card.dataset.mix);
    if (!m) return;
    if (e.target.closest(".playbtn")) playMix(m); else navTo(() => openMix(m));
  });

  function renderContinue() {
    const items = state.hist.slice(0, 8);
    el.secContinue.hidden = !items.length;
    el.continueRow.replaceChildren(...items.map(miniCard));
    markPlaying();
  }
  function renderFeed() {
    const items = state.feed.slice(1);                    // первый трек волны показан в баннере
    if (home.loading && !items.length) {
      el.newGrid.replaceChildren(...skelCards(5));
      setBlock(el.homeEmpty, "");
      el.homeMore.hidden = true;
      return;
    }
    el.newGrid.replaceChildren(...items.map((t, i) => trackCard(t, i + 1)));
    markPlaying();
    if (home.error) setBlock(el.homeEmpty, `Не удалось подобрать музыку. ${home.error}.`, () => loadHome());
    else if (home.partial && state.feed.length) setBlock(el.homeEmpty, `Подборка неполная: ${home.partial} ответил не на все запросы.`, () => loadHome());
    else setBlock(el.homeEmpty, state.feed.length ? "" : "Пока нечего предложить. Найди что-нибудь через поиск.");
    el.homeMore.hidden = !state.more; el.homeMore.textContent = "";
  }
  function appendFeedCards(from) {
    el.newGrid.append(...state.feed.slice(from).map((t, i) => trackCard(t, from + i)));
    markPlaying();
  }

  /**
   * Лента без конца: каждая следующая страница строится от ПОСЛЕДНЕГО показанного трека («радио»)
   * плюс следующие треки любимых артистов. Лента заканчивается, только если трижды подряд нет ничего нового.
   */
  async function loadMoreHome() {
    const my = home.seq;
    state.loadingMore = true;
    el.homeMore.hidden = false; el.homeMore.textContent = "Подбираем ещё…";
    try {
      let added = false;
      for (let attempt = 0; attempt < 3 && !added; attempt++) {
        const last = state.feed[state.feed.length - 1];
        const qs = new URLSearchParams({ limit: "24", page: String(state.feedPage++), salt: String(state.salt),
          exclude: state.feed.map(trackKey).slice(-200).join(",") });
        if (last) { qs.set("source", last.source); qs.set("id", last.id); qs.set("title", last.title); }
        const d = recsResult(await api(`/api/recommendations?${qs}`));   // сбой источника = ошибка, а не «лента кончилась»
        if (my !== home.seq || state.view !== "home") return;
        const have = new Set(state.feed.map(trackKey));
        const fresh = d.tracks.filter((t) => !have.has(trackKey(t)));
        if (fresh.length) {
          const from = state.feed.length;
          state.feed.push(...fresh);
          appendFeedCards(from);
          added = true;
          if (queue.isFrom(state.feed)) queue.extend();     // очередь играет ленту: продолжается новыми треками
        }
      }
      state.more = added;
    } catch {
      el.homeMore.textContent = "Не удалось загрузить. Нажми, чтобы повторить";
      return;
    } finally { state.loadingMore = false; }
    el.homeMore.hidden = !state.more; el.homeMore.textContent = "";
  }

  /** Название ленты главной по тому, как она реально собрана (поля personalized и radio ответа). */
  function feedLabel() {
    if (state.personalized) return "Подобрано по вашей истории";
    if (state.feedRadio) return "Похоже на текущий трек";
    if (!state.hist.length && !state.favItems.length) return "С чего начать";
    return "Подборка";
  }
  /** «Слушать подборку»: очередь = лента главной, старт с трека из баннера. */
  async function startWave() {
    if (!state.feed.length) {
      if (home.loading) return;
      await loadHome();
    }
    if (!state.feed.length) { toast("Не удалось подобрать музыку. Попробуй позже."); return; }
    playTracks(state.feed, Math.max(0, state.feed.indexOf(state.featured)), feedLabel());
  }

  // ---------- баннер ----------
  function renderHero() {
    const cur = current(), t = heroTrack();
    const loading = home.loading && !cur;
    const playing = !!cur && !audio.paused;
    hero.root.dataset.state = cur ? (playing ? "playing" : "paused") : "idle";
    hero.root.dataset.feed = home.loading ? "loading" : home.error ? "error" : state.feed.length ? "ready" : "empty";
    const label = home.loading || home.error || !state.feed.length ? "Подборка" : feedLabel();
    hero.root.setAttribute("aria-label", label);
    hero.eyebrow.textContent = cur ? "Сейчас играет" : label;
    if (t) {
      hero.title.textContent = t.title;
      hero.sub.textContent = cur ? (queue.label ? `${t.artist} · из «${queue.label}»` : t.artist)
        : (t.reason ? `${t.artist} · ${t.reason}` : t.artist);
      const meta = [t.duration ? fmtTime(t.duration) : "", SOURCES[t.source], t.plays != null ? `${t.plays.toLocaleString("ru-RU")} прослушиваний` : ""].filter(Boolean);
      hero.meta.textContent = meta.join(" · ");
      hero.meta.hidden = !meta.length;
      setCover(hero.img, t.cover, 544);
    } else {
      hero.title.textContent = label;
      hero.sub.textContent = loading ? "Подбираем музыку…" : (home.error || "Пока нечего предложить. Найди что-нибудь через поиск.");
      hero.meta.hidden = true;
      setCover(hero.img, null);
    }
    hero.play.dataset.playing = String(playing);
    hero.playLabel.textContent = playing ? "Пауза" : cur ? "Продолжить" : "Слушать подборку";
    hero.play.disabled = !cur && (loading || !state.feed.length);
    hero.like.hidden = !t;
    if (t) paintFav(hero.like, t);
    hero.ctl.forEach((b) => { b.hidden = !cur; });
    hero.prog.hidden = !cur;
    const nx = cur && !state.shuffle ? queue.peekNext() : null;
    hero.next.hidden = !nx;
    if (nx) hero.nextT.textContent = `Далее: ${nx.title}`;
  }

  // ==========================================================================
  // Воспроизведение
  // ==========================================================================
  /** Играть треки как очередь. Очередь копирует массив (M11); если источник потом дополнится (лента главной,
   *  следующая страница списка), очередь продолжится через continuation — пока пользователь её не правил. */
  function playTracks(tracks, i, label = "") {
    if (!tracks[i]) return;
    const prev = current();                                // играющий трек — до замены очереди
    queue.load(tracks, { label, source: tracks, continuation: async () => tracks });
    state.mixId = null; markMix();
    playIndex(i, prev);
  }
  /** Подпись очереди: откуда играет («Альбом 1», «Поиск: gspd», «Нравится»...). */
  function listLabel() {
    if (DETAIL_VIEWS.includes(state.view)) return el.dTitle.textContent;
    if (state.view === "search") return `Поиск: ${state.query}`;
    return el.resultsTitle.textContent;
  }
  function playFromList(i) {
    const list = state.list;
    if (!list[i]) return;
    const prev = current();
    queue.load(list, { label: listLabel(), source: list, continuation: async () => list });
    state.mixId = state.view === "mix" ? state.ctx?.mix : null; markMix();
    playIndex(i, prev);
  }

  /** prev — трек, который играл до этого. Если очередь только что заменили, current() уже указывает в новую
   *  очередь по старому номеру, поэтому вызывающий передаёт прерванный трек сам. */
  function playIndex(i, prev = current()) {
    const track = queue.at(i);
    if (!track) return;
    if (prev && audio.currentTime > 0 && !audio.ended && audio.currentTime < Math.min(30, (getDuration() || 999) * 0.4)) reportSkip(prev);   // переключили почти сразу = пропуск
    queue.select(i);
    recordPlay(track);

    showNowPlaying(track);
    setLoading("Загрузка…");
    player.recover = 0;
    startTrack(track, 0);
  }

  // ==========================================================================
  // Источник звука трека (S8): обычный файл через <audio src> или HLS через hls.js — в тот же <audio>.
  // ==========================================================================
  // Источники, у которых backend отдаёт только прогрессивный файл (streaming/resolver.py: SOURCES[...].hls=False).
  // Для остальных плеер сначала спрашивает /api/stream/{source}/{id}/playback, как играть трек: SoundCloud
  // часть треков отдаёт только по HLS (N4). Совпадение с backend проверяет tests/unit/test_format_choice.py.
  const PROGRESSIVE_ONLY = new Set(["yt"]);
  const HLS_MAX_RECOVER = 2;                // перезапусков трека после сбоя потока (с того же места), не больше
  const player = { gen: 0, kind: null, hls: null, token: null, pending: false, recover: 0, mediaRecovered: false, ctl: null };
  let hlsLib = null;

  /** hls.js грузится с локального диска при первом HLS-треке (не замедляет старт; без CDN и интернета). */
  function loadHls() {
    if (window.Hls) return Promise.resolve(window.Hls);
    if (!hlsLib) {
      hlsLib = new Promise((resolve, reject) => {
        const s = document.createElement("script");
        s.src = "vendor/hls.light.min.js";
        s.async = true;
        s.onload = () => (window.Hls ? resolve(window.Hls) : reject(new Error("Компонент воспроизведения HLS не загрузился.")));
        s.onerror = () => { hlsLib = null; reject(new Error("Компонент воспроизведения HLS не загрузился.")); };
        document.head.appendChild(s);
      });
    }
    return hlsLib;
  }

  /** Освобождает прошлый HLS-поток: экземпляр hls.js (MSE, таймеры, загрузки) и сеанс на backend. */
  function detachHls() {
    const h = player.hls, token = player.token;
    player.hls = null; player.token = null;
    if (h) { try { h.destroy(); } catch { /* уже уничтожен */ } }
    if (token) fetch(`/api/stream/hls/${token}`, { method: "DELETE", keepalive: true }).catch(() => {});
  }

  /** Запуск трека с позиции at (с): новый «поколение» отменяет всё, что относилось к прошлому треку. */
  function startTrack(track, at) {
    const gen = ++player.gen;
    if (player.ctl) player.ctl.abort();
    player.ctl = null;
    detachHls();
    player.kind = null;
    player.mediaRecovered = false;
    if (PROGRESSIVE_ONLY.has(track.source)) { playDirect(track.stream_url, at, "progressive"); return; }
    player.pending = true;                  // пока ждём ответа, Play не должен запускать что-то другое
    const ctl = new AbortController();
    player.ctl = ctl;
    fetch(`${track.stream_url}/playback`, { signal: ctl.signal, cache: "no-store" })
      .then(async (r) => {
        const body = await r.json().catch(() => ({}));
        if (!r.ok) throw Object.assign(new Error(body.detail || `Ошибка ${r.status}`), { code: r.headers.get("X-Wave-Error") });
        return body;
      })
      .then((info) => {
        if (gen !== player.gen) return undefined;
        player.ctl = null;
        if (info.kind === "hls") return playHls(track, info, at, gen);
        playDirect(info.url || track.stream_url, at, info.kind || "progressive");
        return undefined;
      })
      .catch((err) => {
        if (gen !== player.gen || err.name === "AbortError") return;
        player.pending = false;
        failTrack(track, err.message);
      });
  }

  function playDirect(url, at, kind) {
    player.pending = false;
    player.kind = kind;
    audio.src = url;
    audio.loop = state.repeat;
    if (at > 0) audio.addEventListener("loadedmetadata", () => { audio.currentTime = at; }, { once: true });
    audio.play().catch(onPlayRejected);
  }

  async function playHls(track, info, at, gen) {
    let Hls;
    try {
      Hls = await loadHls();
    } catch (err) {
      if (gen === player.gen) { player.pending = false; failTrack(track, err.message); }
      return;
    }
    if (gen !== player.gen) return;
    if (!Hls.isSupported()) { player.pending = false; failTrack(track, "Встроенный браузер не поддерживает воспроизведение HLS (Media Source Extensions)."); return; }
    const retry = { maxNumRetry: 2, retryDelayMs: 800, maxRetryDelayMs: 4000 };
    const policy = (ttfb, total) => ({ default: { maxTimeToFirstByteMs: ttfb, maxLoadTimeMs: total, timeoutRetry: retry, errorRetry: retry } });
    const h = new Hls({
      enableWorker: false,                 // без blob-воркера: всё в одном потоке, предсказуемо в WebView2
      lowLatencyMode: false,
      startPosition: at > 0 ? at : -1,
      maxBufferLength: 30,
      backBufferLength: 30,
      manifestLoadPolicy: policy(15000, 30000),
      playlistLoadPolicy: policy(15000, 30000),
      fragLoadPolicy: policy(15000, 60000),
    });
    player.hls = h;
    player.token = (info.url.match(/^\/api\/stream\/hls\/([\w-]+)\//) || [])[1] || null;
    player.kind = "hls";
    player.pending = false;
    h.on(Hls.Events.MANIFEST_PARSED, () => {
      if (gen !== player.gen) return;
      audio.loop = state.repeat;
      audio.play().catch(onPlayRejected);
    });
    h.on(Hls.Events.ERROR, (_e, data) => {
      if (gen !== player.gen || !data || !data.fatal) return;   // нефатальные сбои hls.js повторяет сам
      onHlsFatal(track, h, data, Hls);
    });
    h.attachMedia(audio);
    h.loadSource(info.url);
  }

  /** Текст ошибки backend (detail) из ответа, который вернулся hls.js. */
  function hlsDetail(data) {
    try {
      const x = data.networkDetails;
      const text = x && typeof x.responseText === "string" ? x.responseText : "";
      return text ? JSON.parse(text).detail || null : null;
    } catch { return null; }
  }

  function onHlsFatal(track, h, data, Hls) {
    // Ошибка декодера: одна штатная попытка восстановления hls.js, потом — честная ошибка
    if (data.type === Hls.ErrorTypes.MEDIA_ERROR && !player.mediaRecovered) {
      player.mediaRecovered = true;
      h.recoverMediaError();
      return;
    }
    const at = audio.currentTime || 0;
    const code = data.response && data.response.code;
    // Сеть, устаревший сеанс (404) или сменившийся поток (409): перезапуск трека с того же места через
    // /playback (новая ссылка и новый сеанс), не больше HLS_MAX_RECOVER раз подряд
    // Экземпляр hls.js не уничтожается внутри его же обработчика события: перезапуск и ошибка — следующей задачей
    const gen = player.gen;
    if (data.type === Hls.ErrorTypes.NETWORK_ERROR && player.recover < HLS_MAX_RECOVER && navigator.onLine) {
      player.recover++;
      setLoading("Восстановление потока…");
      setTimeout(() => { if (gen === player.gen) startTrack(track, at); }, 0);
      return;
    }
    const fallback = data.type === Hls.ErrorTypes.MEDIA_ERROR ? "Плеер не смог декодировать HLS-поток этого трека."
      : code ? `Сервер потока ответил ошибкой HTTP ${code}.` : "Не удалось загрузить HLS-поток. Проверьте подключение к интернету.";
    const detail = hlsDetail(data);
    const url = data.url || (data.frag && data.frag.url) || (data.context && data.context.url);
    if (detail || !code || !url) { setTimeout(() => { if (gen === player.gen) failTrack(track, detail || fallback); }, 0); return; }
    // Причину (detail) отдаёт backend — тот же адрес отвечает JSON, а hls.js читал тело как бинарные данные
    fetch(url, { cache: "no-store" })
      .then((r) => (r.ok ? null : r.json()))
      .catch(() => null)
      .then((b) => { if (gen === player.gen) failTrack(track, (b && b.detail) || fallback); });
  }

  /** Трек не играет: сообщение с настоящей причиной и переход к следующему (не больше 3 сбоев подряд). */
  function failTrack(track, detail) {
    if (current() !== track) return;        // пользователь уже переключил трек: старая ошибка не мешает новому
    detachHls();
    // Одно сообщение с трек-названием и настоящей причиной (тост показывает одно сообщение: второе затирало первое)
    toast(detail ? `Не удалось воспроизвести «${track.title}»: ${detail}` : `Не удалось воспроизвести: ${track.title}`,
      detail ? 6000 : 3600);
    clearLoading();
    setPlayingUI(false);
    if (state.errors++ < 3 && nextIndex() >= 0) setTimeout(playNext, 800);
  }

  function onPlayRejected(err) {
    // AbortError: быстро переключили трек, пока шёл прошлый play(): это нормально
    if (err.name === "AbortError") return;
    if (err.name === "NotAllowedError") { toast("Нажми Play, чтобы начать воспроизведение"); return; }
    onAudioError();
  }

  function togglePlay() {
    if (player.pending) return undefined;   // трек ещё готовится (/playback): Play не запускает ничего другого
    if (!audio.getAttribute("src")) {
      if (state.view === "home") return startWave();
      if (queue.length) return playIndex(Math.max(0, queue.index));
      if (state.list.length) return playFromList(0);
      el.input.focus();
      return undefined;
    }
    if (audio.paused) audio.play().catch(onPlayRejected); else audio.pause();
    return undefined;
  }

  function nextIndex() {
    return queue.nextIndex({ shuffle: state.shuffle });
  }
  function playNext() {
    const j = nextIndex();
    if (j >= 0) playIndex(j);
  }
  function playPrev() {
    // как в большинстве плееров: после 3 секунд «назад» перематывает в начало трека
    if (audio.currentTime > 3 || queue.index <= 0) { audio.currentTime = 0; return; }
    playIndex(queue.prevIndex());
  }
  function seekBy(delta) {
    const dur = getDuration();
    if (!dur) return;
    audio.currentTime = clamp(audio.currentTime + delta, 0, dur);
  }
  function getDuration() {
    if (Number.isFinite(audio.duration) && audio.duration > 0) return audio.duration;
    return current()?.duration || 0;
  }
  function setShuffle(on) {
    state.shuffle = on;
    ctl.shuffle.forEach((b) => b.setAttribute("aria-pressed", String(on)));
    if (state.view === "home") renderHero();
  }
  function setRepeat(on) {
    state.repeat = on;
    audio.loop = on;
    ctl.repeat.forEach((b) => b.setAttribute("aria-pressed", String(on)));
  }

  /**
   * Прогрев: просим у сервера 1 байт трека, чтобы он заранее получил прямую ссылку
   * через yt-dlp (это занимает секунды). К моменту клика трек стартует почти сразу.
   */
  const warmed = new Map();                  // id -> время прогрева
  function warmTrack(t) {
    if (!t) return;
    const last = warmed.get(t.id);
    if (last && Date.now() - last < 20 * 60 * 1000) return;   // ссылка на сервере живёт часами
    warmed.set(t.id, Date.now());
    // Сервер сразу отвечает 202/204 и в фоне получает ссылку + первую порцию трека
    fetch(`${t.stream_url}?warm=1`, { cache: "no-store" })
      .then((r) => { if (r.status !== 202 && r.status !== 204) warmed.delete(t.id); })  // 503: занят, позже
      .catch(() => warmed.delete(t.id));
  }
  // После поиска готовим первые треки заранее, с паузой, чтобы не нагружать сервер разом
  let warmTimers = [];
  function scheduleWarm(list) {
    warmTimers.forEach(clearTimeout);
    warmTimers = list.map((t, i) => setTimeout(() => warmTrack(t), i * 1500));
  }
  /** Прогреваем следующий трек очереди (при shuffle он заранее неизвестен). */
  function warmNext() {
    if (!state.shuffle) warmTrack(queue.peekNext());
  }

  // Индикатор в плеере: пока идёт резолв/буферизация, вместо исполнителя пишем статус
  function setLoading(text) { el.artist.textContent = text; }
  function clearLoading() { const t = current(); if (t) renderArtists(el.artist, t); }

  // ---------- События <audio> ----------
  audio.addEventListener("play", () => setPlayingUI(true));
  audio.addEventListener("pause", () => setPlayingUI(false));
  audio.addEventListener("waiting", () => setLoading("Буферизация…"));
  audio.addEventListener("playing", () => {
    state.errors = 0;
    clearLoading();
    warmNext();
  });
  audio.addEventListener("loadedmetadata", () => { updateProgress(); updatePositionState(); });
  audio.addEventListener("durationchange", updateProgress);
  audio.addEventListener("timeupdate", () => { if (!state.seeking) updateProgress(); });
  audio.addEventListener("seeked", updatePositionState);
  audio.addEventListener("ratechange", updatePositionState);
  audio.addEventListener("ended", () => {
    const j = nextIndex();
    if (j >= 0) playIndex(j);
    else if (state.settings.autoplay && navigator.onLine) continueWithRecs();
    else stopAtEnd();
  });
  audio.addEventListener("error", onAudioError);

  function onAudioError() {
    if (!audio.getAttribute("src") || player.pending) return;
    const t = current();
    if (player.kind === "hls") return;     // ошибки HLS-потока обрабатывает onHlsFatal (у hls.js точнее причина)
    const at = audio.currentTime || 0;
    const kind = player.kind;
    clearLoading();
    setPlayingUI(false);
    if (!t) return;
    if (kind === "local") { failTrack(t, "Не удалось прочитать скачанный файл."); return; }
    // Причина — с backend (тот же адрес отвечает JSON с detail и X-Wave-Error)
    fetch(t.stream_url, { headers: { Range: "bytes=0-0" } })
      .then(async (r) => (r.ok ? null : { code: r.headers.get("X-Wave-Error"), detail: (await r.json().catch(() => ({}))).detail }))
      .catch(() => null)
      .then((e) => {
        if (current() !== t) return;
        // Прогрессивная ссылка сменилась на HLS (S8): перезапуск через /playback с того же места
        if (e && e.code === "hls_required" && player.recover < HLS_MAX_RECOVER) {
          player.recover++;
          startTrack(t, at);
          return;
        }
        failTrack(t, e && e.detail);
      });
  }
  // Окно закрывают: HLS-сеанс на backend освобождается сразу, не дожидаясь срока жизни
  window.addEventListener("pagehide", detachHls);
  function stopAtEnd() { audio.currentTime = 0; setPlayingUI(false); updateProgress(); }

  // ==========================================================================
  // UI плеера: мини-плеер, баннер и страница трека показывают одно состояние
  // ==========================================================================
  function showNowPlaying(t) {
    el.title.textContent = t.title;
    renderArtists(el.artist, t);
    setSource(el.playerSrc, t.source);
    setCover(el.cover, t.cover, 120);
    document.title = `${t.title} — ${t.artist}`;
    document.body.dataset.track = "1";
    progressBars.forEach((p) => p.bar.style.setProperty("--p", "0%"));   // только полосы прогресса: громкость остаётся как есть
    progressBars.forEach((p) => { if (p.cur) p.cur.textContent = "0:00"; if (p.tot) p.tot.textContent = fmtTime(t.duration); });
    markPlaying();
    updateMediaMetadata(t);
    paintLike();
    renderHero();
    if (state.view === "now") renderNow();
    if (state.fs.open) renderFs();
    renderQueue();
  }

  function markPlaying() {
    const cur = current(), k = cur ? trackKey(cur) : null;
    $$("[data-key]").forEach((n) => n.classList.toggle("is-playing", n.dataset.key === k));
  }

  function setPlayingUI(playing) {
    document.body.dataset.audio = playing ? "playing" : "paused";
    ctl.play.forEach((b) => {
      b.dataset.playing = String(playing);
      if (b !== hero.play) b.setAttribute("aria-label", playing ? "Пауза" : "Играть");
    });
    if (mediaSession) mediaSession.playbackState = playing ? "playing" : "paused";
    renderHero();
    updatePositionState();
  }

  // Полосы прогресса: мини-плеер, баннер, страница трека
  const progressBars = [
    { bar: el.progress, cur: el.tCur, tot: el.tTotal },
    { bar: hero.bar, cur: hero.cur, tot: hero.tot },
    { bar: now.bar, cur: now.cur, tot: now.tot },
    { bar: fs.bar, cur: fs.cur, tot: fs.tot },
  ];
  function updateProgress() {
    const dur = getDuration();
    const cur = audio.currentTime || 0;
    const pct = dur ? clamp((cur / dur) * 100, 0, 100) : 0;
    for (const p of progressBars) {
      p.bar.style.setProperty("--p", `${pct}%`);
      p.bar.setAttribute("aria-valuenow", String(Math.round(pct)));
      p.bar.setAttribute("aria-valuetext", `${fmtTime(cur)} из ${fmtTime(dur)}`);
      p.cur.textContent = fmtTime(cur);
      p.tot.textContent = fmtTime(dur);
    }
    if (state.view === "home") {                          // у текущей карточки «Продолжить слушать» живой прогресс
      const mc = el.continueRow.querySelector(".minicard.is-playing");
      if (mc) {
        mc.querySelector(".minibar").style.setProperty("--p", `${pct}%`);
        mc.querySelector(".minicard__time").textContent = `${fmtTime(cur)} / ${fmtTime(dur)}`;
      }
    }
    if (state.view === "now" || state.fs.open) tickLyrics(cur);
  }

  // ---------- Универсальный ползунок (прогресс и громкость) ----------
  function makeSlider(node, { preview, commit, nudge, cancel = () => {} }) {
    const fraction = (e) => {
      const r = node.getBoundingClientRect();
      return clamp((e.clientX - r.left) / r.width, 0, 1);
    };
    let dragging = false;
    node.addEventListener("pointerdown", (e) => {
      if (e.pointerType === "mouse" && e.button !== 0) return;
      dragging = true;
      node.setPointerCapture(e.pointerId);
      preview(fraction(e));
    });
    node.addEventListener("pointermove", (e) => { if (dragging) preview(fraction(e)); });
    node.addEventListener("pointerup", (e) => {
      if (!dragging) return;
      dragging = false;
      commit(fraction(e));
    });
    node.addEventListener("pointercancel", () => { dragging = false; cancel(); });
    node.addEventListener("keydown", (e) => {
      const dir = { ArrowLeft: -1, ArrowDown: -1, ArrowRight: 1, ArrowUp: 1 }[e.key];
      if (!dir) return;
      e.preventDefault();
      nudge(dir);
    });
  }
  // Прогресс: во время перетаскивания обновляем только вид, а перемотку делаем при отпускании
  for (const p of progressBars) {
    makeSlider(p.bar, {
      preview(f) {
        state.seeking = true;
        p.bar.style.setProperty("--p", `${f * 100}%`);
        p.cur.textContent = fmtTime(f * getDuration());
      },
      commit(f) {
        state.seeking = false;
        const dur = getDuration();
        if (dur) audio.currentTime = f * dur;            // сервер ответит 206 на Range-запрос
        updateProgress();
      },
      cancel() { state.seeking = false; updateProgress(); },
      nudge: (dir) => seekBy(dir * 5),
    });
  }

  // Громкость: ползунки мини-плеера, страницы трека и полноэкранного плеера управляют одним audio.volume.
  // Mute = audio.muted: сама громкость не обнуляется, поэтому после включения звука она возвращается как была.
  // Вид всех ползунков и кнопок перерисовывается по событию volumechange самого <audio>.
  const volBars = [el.volume, now.volume, fs.volume];
  function paintVolume() {
    const eff = audio.muted ? 0 : audio.volume;
    for (const node of volBars) {
      node.style.setProperty("--p", `${eff * 100}%`);
      node.setAttribute("aria-valuenow", String(Math.round(eff * 100)));
      node.setAttribute("aria-valuetext", audio.muted ? "Звук выключен" : `${Math.round(eff * 100)}%`);
    }
    const off = eff === 0;
    ctl.mute.forEach((b) => {
      const label = off ? "Включить звук" : "Выключить звук";
      b.setAttribute("aria-pressed", String(off));
      b.title = label;
      b.setAttribute("aria-label", label);
      b.querySelector("use").setAttribute("href", off ? "#i-volume-off" : "#i-volume");
    });
  }
  /** commit=false: промежуточные значения при перетаскивании. «Последней слышимой» громкостью они не считаются,
   *  поэтому, если ушли ползунком в ноль, mute вернёт то значение, что было до перетаскивания. */
  function setVolume(v, commit = true) {
    if (!flags.uiReady) flags.volTouched = true;
    v = clamp(v, 0, 1);
    audio.volume = v;
    if (v > 0) audio.muted = false;
    if (commit && v >= 0.05) { state.lastVol = v; uiSave.put({ last_volume: round3(v) }); }
    paintVolume();
  }
  function toggleMute() {
    if (!flags.uiReady) flags.volTouched = true;
    if (audio.muted || audio.volume === 0) {
      audio.muted = false;
      if (audio.volume === 0) audio.volume = state.lastVol || 0.6;   // ушли ползунком в ноль: возвращаем последнюю слышимую громкость
    } else {
      audio.muted = true;
    }
    paintVolume();
  }
  audio.addEventListener("volumechange", () => {
    paintVolume();
    if (flags.uiReady) uiSave.put({ volume: round3(audio.volume) });   // перетаскивание: одна запись в конце (см. uiSave)
  });
  for (const node of volBars) {
    makeSlider(node, { preview: (v) => setVolume(v, false), commit: (v) => setVolume(v, true), nudge: (dir) => setVolume((audio.muted ? 0 : audio.volume) + dir * 0.05) });
  }
  paintVolume();                                           // громкость из настроек придёт в applyUiSettings

  // ==========================================================================
  // Media Session (медиаклавиши, экран блокировки, часы/наушники)
  // ==========================================================================
  const mediaSession = "mediaSession" in navigator ? navigator.mediaSession : null;
  function updateMediaMetadata(t) {
    if (!mediaSession || typeof MediaMetadata === "undefined") return;
    mediaSession.metadata = new MediaMetadata({
      title: t.title, artist: t.artist, album: t.album || "",
      artwork: t.cover ? [{ src: t.cover, sizes: "544x544" }] : [],
    });
  }
  function updatePositionState() {
    if (!mediaSession || !mediaSession.setPositionState) return;
    const dur = getDuration();
    if (!dur) return;
    try {
      mediaSession.setPositionState({ duration: dur, playbackRate: audio.playbackRate || 1, position: clamp(audio.currentTime || 0, 0, dur) });
    } catch { /* некорректное состояние при смене трека: игнорируем */ }
  }
  if (mediaSession) {
    const actions = {
      play: () => audio.play().catch(onPlayRejected),
      pause: () => audio.pause(),
      previoustrack: playPrev,
      nexttrack: playNext,
      stop: () => { audio.pause(); audio.currentTime = 0; },
      seekbackward: (d) => seekBy(-(d.seekOffset || 10)),
      seekforward: (d) => seekBy(d.seekOffset || 10),
      seekto: (d) => { if (Number.isFinite(d.seekTime)) audio.currentTime = d.seekTime; },
    };
    for (const [name, handler] of Object.entries(actions)) {
      try { mediaSession.setActionHandler(name, handler); } catch { /* действие не поддерживается */ }
    }
  }

  // ==========================================================================
  // Страница текущего трека
  // ==========================================================================
  function openNow() {
    if (!current()) return;
    state.reopen = () => openNow();
    state.view = "now";
    layout();
    renderNow();
    el.main.scrollTo({ top: 0 });
  }
  const openNowFromUi = () => { if (current() && state.view !== "now") navTo(openNow); };

  function renderNow() {
    const t = current();
    if (!t) return;
    setCover(now.cover, t.cover, 544);
    setCover(now.bg, t.cover, 544);
    now.eyebrow.textContent = queue.label ? `Сейчас играет · из «${queue.label}»` : "Сейчас играет";
    now.title.textContent = t.title;
    renderArtists(now.artist, t);
    const year = t.released ? String(t.released).slice(0, 4) : "";
    now.meta.textContent = [t.album, year, t.duration ? fmtTime(t.duration) : "", SOURCES[t.source],
      t.plays != null ? `${t.plays.toLocaleString("ru-RU")} прослушиваний` : ""].filter(Boolean).join(" · ");
    paintFav(now.like, t);
    paintDl(now.dl, t);
    now.stage.classList.toggle("no-lyrics", !state.lyricsOn);
    now.lyricsBtn.setAttribute("aria-pressed", String(state.lyricsOn));
    if (state.lyricsOn) { loadLyrics(t); ly.idx = -2; tickLyrics(audio.currentTime || 0, true); }
    loadSimilar(t);
    updateProgress();
  }

  // ---------- тексты ----------
  // Данные одни (LRCLIB через /api/lyrics), показываем их в двух местах: панель на странице трека и режим «Текст песни».
  const LY_TEXT = { loading: "Ищем текст…", none: "Текст для этого трека не найден", error: "Текст сейчас недоступен" };
  const ly = { lines: [], plain: "", status: "idle", idx: -1, key: "" };
  const lyBoxes = [now.lyrics, fs.lyrics];
  function paintLyrics() {
    for (const box of lyBoxes) {
      if (ly.status === "ok" && ly.lines.length) box.replaceChildren(...ly.lines.map((l, i) => { const p = mk("p", "ly", l.text); p.dataset.i = i; return p; }));
      else if (ly.status === "ok") box.replaceChildren(mk("p", "ly ly--plain", ly.plain));   // текст без таймкодов: без ложной синхронизации
      else box.replaceChildren(mk("p", "lyrics__empty", LY_TEXT[ly.status] || ""));
    }
    ly.idx = -2;                                           // заставляем tickLyrics заново выставить подсветку
    tickLyrics(audio.currentTime || 0, true);
  }
  async function loadLyrics(t) {
    const key = trackKey(t);
    if (ly.key === key) return;
    Object.assign(ly, { lines: [], plain: "", status: "loading", idx: -1, key });
    paintLyrics();
    try {
      const artist = (t.artists && t.artists[0]) || t.artist;
      const local = t.downloaded || state.dl.has(key) ? `&source=${t.source}&id=${enc(t.id)}` : "";
      const d = await api(`/api/lyrics?artist=${enc(artist)}&title=${enc(t.title)}${t.duration ? `&duration=${t.duration}` : ""}${local}`);
      if (ly.key !== key) return;                          // трек уже сменился
      if (d.synced && d.synced.length) Object.assign(ly, { lines: d.synced, status: "ok" });
      else if (d.plain) Object.assign(ly, { plain: d.plain, status: "ok" });
      else ly.status = "none";
    } catch {
      if (ly.key !== key) return;
      ly.key = ""; ly.status = "error";                    // в следующий раз попробуем снова
    }
    paintLyrics();
  }
  /** Подсветка текущей строки (только при реальных таймкодах). Прокручиваем сам блок текста, а не страницу. */
  function tickLyrics(cur, instant = false) {
    if (ly.status !== "ok" || !ly.lines.length) return;
    let idx = -1;
    for (let i = 0; i < ly.lines.length && ly.lines[i].t <= cur; i++) idx = i;
    if (idx === ly.idx) return;
    ly.idx = idx;
    for (const box of lyBoxes) {
      const kids = box.children;
      for (let i = 0; i < kids.length; i++) { kids[i].classList.toggle("cur", i === idx); kids[i].classList.toggle("past", i < idx); }
      const p = kids[idx];
      if (p && box.offsetParent !== null) box.scrollTo({ top: p.offsetTop - box.clientHeight / 2 + p.clientHeight / 2, behavior: instant ? "auto" : "smooth" });
    }
  }
  for (const box of lyBoxes) {
    box.addEventListener("click", (e) => {                 // клик по строке = перемотка к ней
      const p = e.target.closest(".ly[data-i]");
      if (p && ly.lines[Number(p.dataset.i)]) audio.currentTime = ly.lines[Number(p.dataset.i)].t;
    });
  }
  function toggleLyrics() {
    state.lyricsOn = !state.lyricsOn;
    uiSave.put({ lyrics_visible: state.lyricsOn }, 0);
    now.stage.classList.toggle("no-lyrics", !state.lyricsOn);
    now.lyricsBtn.setAttribute("aria-pressed", String(state.lyricsOn));
    const t = current();
    if (state.lyricsOn && t) { loadLyrics(t); ly.idx = -2; tickLyrics(audio.currentTime || 0, true); }
  }

  // ---------- похожие треки и исполнители (радио текущего трека) ----------
  const similar = { key: "", tracks: [], artists: [], seq: 0 };
  async function loadSimilar(t) {
    const key = trackKey(t);
    if (similar.key === key) return;
    similar.key = key;
    const my = ++similar.seq;
    similar.tracks = []; similar.artists = [];
    now.similarRow.replaceChildren(...skelCards(5).map((c) => { c.classList.add("tcard--skel"); return c; }));
    now.similarArtistsSec.hidden = true;
    setBlock(now.similarEmpty, "");
    try {
      const d = recsResult(await api(`/api/recommendations?limit=30&source=${t.source}&id=${enc(t.id)}&title=${enc(t.title)}`));
      if (my !== similar.seq) return;
      const all = d.tracks;
      const radio = all.filter((x) => (x.reason || "").startsWith("Похоже"));   // только настоящее «радио» трека, без подборок по любимым
      similar.tracks = (radio.length >= 3 ? radio : all).slice(0, 12);
      const own = new Set(((t.artists && t.artists.length) ? t.artists : [t.artist]).map((n) => n.toLowerCase()));
      const seen = new Set();
      similar.artists = similar.tracks.flatMap((x) => {
        const name = (x.artists && x.artists[0]) || x.artist;
        const id = (x.artist_ids && x.artist_ids[0]) || x.artist_id;
        const k = (name || "").toLowerCase();
        if (!id || !name || own.has(k) || seen.has(k)) return [];
        seen.add(k);
        return [{ kind: "artist", source: x.source, id: String(id), name, cover: x.cover, subtitle: "Исполнитель" }];
      }).slice(0, 8);
      now.similarRow.replaceChildren(...similar.tracks.map(trackCard));
      renderRow(now.similarArtists, similar.artists, "simar");
      now.similarArtistsSec.hidden = !similar.artists.length;
      setBlock(now.similarEmpty, similar.tracks.length ? "" : "Источник не нашёл похожих треков.");
      markPlaying();
    } catch (e) {
      if (my !== similar.seq) return;
      similar.key = "";
      now.similarRow.replaceChildren();
      setBlock(now.similarEmpty, `Не удалось загрузить похожие треки: ${e.message}`, () => loadSimilar(t));
    }
  }

  // ==========================================================================
  // Полноэкранный плеер: одно состояние (state.fs), три режима: default | lyrics | queue.
  // Это не отдельный плеер: звук, время, громкость, shuffle/repeat и очередь общие с остальным приложением.
  // Режимы только переключают, что показано рядом с обложкой. Окно Windows и браузерный Fullscreen API не трогаем.
  // ==========================================================================
  const FS_MODES = ["default", "lyrics", "queue"];
  /** Пока открыт полноэкранный плеер, остальное приложение недоступно ни мыши, ни клавиатуре, ни скринридеру. */
  function setInert(on) {
    ["side", "main", "player", "queue"].forEach((id) => document.getElementById(id)?.toggleAttribute("inert", on));
  }
  /** Мягкий фон из цветов обложки: 32x18 пикселей, растянутые браузером. Без blur и без тяжёлых эффектов. */
  function paintFsBg(url) {
    if (fs.bg._url === url) return;
    fs.bg._url = url;
    const g = fs.bg.getContext("2d");
    g.clearRect(0, 0, fs.bg.width, fs.bg.height);
    if (!url) return;
    const img = new Image();
    img.referrerPolicy = "no-referrer";
    img.onload = () => {
      if (fs.bg._url !== url) return;                      // трек уже сменился
      g.imageSmoothingQuality = "high";
      g.drawImage(img, 0, 0, fs.bg.width, fs.bg.height);
    };
    img.src = thumb(url, 120);
  }
  function renderFs() {
    const t = current();
    if (!t) { closeFs(); return; }
    setCover(fs.cover, t.cover, 544);
    paintFsBg(t.cover);
    fs.title.textContent = t.title;
    renderArtists(fs.artist, t);
    const meta = [t.album, t.released ? String(t.released).slice(0, 4) : "", SOURCES[t.source]].filter(Boolean).join(" · ");
    fs.meta.textContent = meta;
    fs.meta.hidden = !meta;
    if (state.fs.mode === "lyrics") loadLyrics(t);
    updateProgress();
  }
  function openFs() {
    if (!current() || state.fs.open) return;
    state.fs.open = true;
    state.fs.opener = document.activeElement;
    el.queue.hidden = true;                                // боковая очередь обычного режима не должна остаться под плеером
    closeMenu();
    document.body.dataset.focus = "1";
    fs.root.hidden = false;
    setInert(true);
    setFsMode("default");
    renderFs();
    fs.play.focus({ preventScroll: true });
  }
  function closeFs() {
    if (!state.fs.open) return;
    state.fs.open = false;
    state.fs.mode = "default";
    closeMenu();
    fs.root.hidden = true;
    document.body.dataset.focus = "0";
    setInert(false);
    const o = state.fs.opener;
    state.fs.opener = null;
    if (o && o.isConnected) o.focus({ preventScroll: true });   // фокус возвращается туда, откуда открыли
  }
  /** Режимы взаимоисключающие. Звук, позиция и громкость при переключении не трогаются. */
  function setFsMode(mode) {
    if (!FS_MODES.includes(mode)) mode = "default";
    const prev = state.fs.mode;
    state.fs.mode = mode;
    fs.root.dataset.mode = mode;
    fs.lyricsPanel.hidden = mode !== "lyrics";
    fs.queuePanel.hidden = mode !== "queue";
    fs.lyricsBtn.setAttribute("aria-checked", String(mode === "lyrics"));
    fs.queueBtn.setAttribute("aria-pressed", String(mode === "queue"));
    closeMenu();
    const t = current();
    if (mode === "lyrics" && t) { loadLyrics(t); ly.idx = -2; tickLyrics(audio.currentTime || 0, true); }
    if (mode === "queue") renderFsQueue();
    // кнопки верхней панели в режиме очереди скрыты: фокус не должен пропасть
    if (mode === "queue" && prev !== "queue") fs.queuePanel.querySelector('[data-fs="qclose"]')?.focus({ preventScroll: true });
    else if (prev === "queue" && mode !== "queue") fs.queueBtn.focus({ preventScroll: true });
  }
  const toggleFsMode = (mode) => setFsMode(state.fs.mode === mode ? "default" : mode);

  document.addEventListener("click", (e) => {
    const b = e.target.closest("[data-fs]");
    if (!b) return;
    switch (b.dataset.fs) {
      case "close": closeFs(); break;
      case "lyrics": toggleFsMode("lyrics"); break;
      case "queue": toggleFsMode("queue"); break;
      case "qclose": setFsMode("default"); break;
      case "qclear": queueClearFlow(); break;
      default: break;
    }
  });

  // ==========================================================================
  // Избранное (одно состояние для всех экранов)
  // ==========================================================================
  function paintFav(btn, t) {
    const on = state.fav.has(trackKey(t));
    const label = on ? "Убрать из избранного" : "В избранное";
    btn.classList.toggle("is-fav", on);
    btn.setAttribute("aria-pressed", String(on));
    btn.title = label;
    btn.setAttribute("aria-label", label);
    const txt = btn.querySelector("span:not(.track__pct)");
    if (txt && btn.classList.contains("abtn")) txt.textContent = on ? "В избранном" : "Нравится";
  }
  function paintLike() {
    const t = current();
    el.btnLike.hidden = !t;
    if (t) { paintFav(el.btnLike, t); paintFav(now.like, t); }
  }
  function repaintFav() {
    $$(".track__fav", el.tracks).forEach((b) => { const t = rowTrack(b); if (t) paintFav(b, t); });
    paintLike();
    const ht = heroTrack();
    if (ht) paintFav(hero.like, ht);
    $$(".q__fav", el.queueList).forEach((b) => { const t = queue.at(Number(b.dataset.index)); if (t) paintFav(b, t); });
  }
  async function toggleFav(t) {
    const k = trackKey(t), was = state.fav.has(k);
    const items = state.favItems;
    if (was) { state.fav.delete(k); state.favItems = items.filter((x) => trackKey(x) !== k); }
    else { state.fav.add(k); state.favItems = [{ ...t }, ...items]; }
    repaintFav();                                          // сразу, без ожидания сервера
    try {
      if (was) await api(`/api/favorites/${t.source}/${enc(t.id)}`, { method: "DELETE" });
      else await api("/api/favorites", jsonOpts("POST", trackPayload(t)));
      scheduleArtists();
    } catch (e) {
      if (was) { state.fav.add(k); state.favItems = items; } else { state.fav.delete(k); state.favItems = items; }
      repaintFav();
      toast(`Не удалось обновить избранное: ${e.message}`);
    }
  }
  function recordPlay(t) { api("/api/history", jsonOpts("POST", trackPayload(t))).catch(() => {}); }
  function reportSkip(t) { api("/api/history/skip", jsonOpts("POST", { source: t.source, id: t.id })).catch(() => {}); }

  async function showLiked() {
    state.reopen = () => showLiked();
    resetList({ view: "liked" });
    setTracks([]);
    el.resultsTitle.textContent = "Нравится";
    setStatus("");
    layout();
    showSkeleton();
    try {
      const d = await api("/api/favorites");
      state.fav = new Set(d.items.map(trackKey));
      state.favItems = d.items;
      setTracks(d.items);
      setBlock(el.empty, d.items.length ? "" : "Нажми на сердечко у трека, и он появится здесь.");
      setStatus(d.items.length ? `Треков: ${d.items.length}` : "");
    } catch (e) {
      clearSkeleton();
      setBlock(el.empty, `Не удалось загрузить избранное: ${e.message}`, () => showLiked());
    }
    layout();
  }

  function showHistory() {
    state.reopen = () => showHistory();
    resetList({ view: "history" });
    setTracks([]);
    el.resultsTitle.textContent = "Недавно слушали";
    setStatus("Загружаем…");
    layout();
    showSkeleton();
    return fetchPage(false);                               // та же бесконечная подгрузка, что и у поиска
  }
  el.clearHistory.addEventListener("click", async () => {
    if (!await askConfirm({ title: "Очистить историю?", text: "Рекомендации станут менее точными. Избранное и загрузки не затрагиваются.", ok: "Очистить" })) return;
    try { await api("/api/history", { method: "DELETE" }); state.hist = []; computeArtists(); renderArtistMixes(); toast("История очищена"); showHistory(); }
    catch (e) { toast(e.message); }
  });

  // ==========================================================================
  // Очередь
  // ==========================================================================
  // Правки очереди — методами модели (player/queue.js); здесь — то, что видит пользователь.
  function queueAdd(t, next) {
    queue.add(t, next);
    toast(next ? "Будет играть следующим" : "Добавлено в очередь");
    renderQueue();
    if (state.view === "home") renderHero();
  }
  function queueRemove(i) {
    if (!queue.remove(i)) return;                          // играющий трек не убираем
    renderQueue();
  }
  function queueMove(i, d) {
    if (!queue.move(i, d)) return;
    renderQueue();
  }
  function queueClear() {
    queue.clear();
    renderQueue();
  }
  /** Очистка необратима: спрашиваем подтверждение. Играющий трек остаётся. */
  async function queueClearFlow() {
    if (queue.length <= 1) return;
    if (!await askConfirm({ title: "Очистить очередь?", text: "Останется только текущий трек. Остальные уберутся из очереди (в избранном, плейлистах и истории они сохранятся).", ok: "Очистить" })) return;
    queueClear();
    toast("Очередь очищена");
  }
  /** Перенос трека на позицию to (индекс вставки до удаления). Играющий трек не прерывается. */
  function queueMoveTo(from, to) {
    if (!queue.moveTo(from, to)) return;
    renderQueue();
    if (state.view === "home") renderHero();
  }
  /** Перерисовывает очередь везде, где она сейчас видна. */
  function renderQueue() {
    if (!el.queue.hidden) renderDrawer();
    if (state.fs.open && state.fs.mode === "queue") renderFsQueue();
  }
  function renderDrawer() {
    el.queueLabel.textContent = queue.label ? `Из «${queue.label}»` : "";
    const rows = queue.tracks.map((t, i) => {
      const li = mk("li", i === queue.index ? "q is-current" : "q");
      const main = mk("button", "q__main");
      main.type = "button";
      main.dataset.index = i;
      main.append(coverEl(t.cover, 96, "q__cv"));
      const text = mk("span", "q__text");
      text.append(mk("span", "q__title", t.title), mk("span", "q__artist", t.artist));
      main.append(text);
      li.append(main);
      const fav = button("q__btn q__fav", "В избранное", "i-heart");
      fav.dataset.index = i;
      paintFav(fav, t);
      li.append(fav);
      if (i !== queue.index) {
        for (const [name, label, ico] of [["up", "Выше", "i-chev-u"], ["down", "Ниже", "i-chev-d"], ["rm", "Убрать из очереди", "i-x"]]) {
          const b = button("q__btn", label, ico);
          b.dataset.q = name; b.dataset.index = i;
          li.append(b);
        }
      }
      return li;
    });
    el.queueList.replaceChildren(...(rows.length ? rows : [mk("li", "lyrics__empty", "Очередь пуста")]));
    el.queueList.querySelector(".is-current")?.scrollIntoView?.({ block: "nearest" });
  }
  // Один обработчик на весь список очереди
  el.queueList.addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    const i = Number(b.dataset.index);
    if (b.classList.contains("q__fav")) { const t = queue.at(i); if (t) toggleFav(t); return; }
    if (b.dataset.q === "up") queueMove(i, -1);
    else if (b.dataset.q === "down") queueMove(i, 1);
    else if (b.dataset.q === "rm") queueRemove(i);
    else if (b.classList.contains("q__main")) playIndex(i);   // «играть сейчас»
  });
  function toggleQueue() { el.queue.hidden = !el.queue.hidden; if (!el.queue.hidden) renderQueue(); }
  $("#queue-close").addEventListener("click", () => { el.queue.hidden = true; });
  $("#queue-clear").addEventListener("click", queueClearFlow);

  // ---------- очередь в полноэкранном плеере: «Сейчас играет» и «Далее» ----------
  const FQ_MAX = 300;                                      // длинную очередь не рисуем целиком
  function fqRow(t, i) {
    const cur = i === queue.index;
    const li = mk("li", cur ? "fq__row is-current" : "fq__row");
    li.dataset.index = i;
    const grip = mk("span", "fq__grip");
    grip.title = "Перетащить";
    grip.append(icon("i-grip", "icon--fill"));
    const main = mk("button", "fq__main");
    main.type = "button";
    main.dataset.index = i;
    main.setAttribute("aria-label", `${cur ? "Пауза или продолжить" : "Играть"}: ${t.artist} — ${t.title}`);
    const text = mk("span", "fq__text");
    text.append(mk("span", "fq__title", t.title), mk("span", "fq__artist", t.artist));
    main.append(coverEl(t.cover, 96, "fq__cv"), text);
    if (cur) {                                             // индикатор текущего воспроизведения
      const eq = mk("span", "eq");
      eq.setAttribute("aria-hidden", "true");
      eq.append(mk("i"), mk("i"), mk("i"));
      main.append(eq);
    }
    main.append(mk("span", "fq__dur", t.duration ? fmtTime(t.duration) : ""));
    const more = button("tbtn fq__more", "Ещё", "i-more", true);
    more.dataset.index = i;
    more.setAttribute("aria-haspopup", "menu");
    li.append(grip, main, more);
    return li;
  }
  function renderFsQueue() {
    const q = queue.tracks, cur = queue.index;
    fs.qCount.textContent = q.length ? plural(q.length, "трек", "трека", "треков") : "";
    fs.qClear.disabled = q.length <= 1;
    const nodes = [];
    if (cur > 0) nodes.push(mk("li", "fq__note", `Уже сыграно: ${cur}`));   // объясняет, почему всего треков больше, чем ниже в списке
    if (q[cur]) nodes.push(mk("li", "fq__h", "Сейчас играет"), fqRow(q[cur], cur));
    const rest = Math.max(0, q.length - cur - 1);
    nodes.push(mk("li", "fq__h", rest ? `Далее · ${rest}` : "Далее"));
    if (state.shuffle && rest) nodes.push(mk("li", "fq__note", "Включено перемешивание: следующий трек выбирается случайно, порядок ниже не гарантирован."));
    if (!rest) nodes.push(mk("li", "fq__note", state.settings.autoplay ? "Дальше ничего нет. Когда очередь закончится, Wave продолжит похожими треками." : "Дальше ничего нет."));
    for (let i = cur + 1; i < Math.min(q.length, cur + 1 + FQ_MAX); i++) nodes.push(fqRow(q[i], i));
    if (rest > FQ_MAX) nodes.push(mk("li", "fq__note", `Показаны первые ${FQ_MAX} из ${rest}.`));
    fs.qList.replaceChildren(...nodes);
  }
  // Один набор обработчиков на весь список: клик, меню «⋯», перетаскивание за «ручку»
  fs.qList.addEventListener("click", (e) => {
    const more = e.target.closest(".fq__more");
    if (more) {
      const i = Number(more.dataset.index), t = queue.at(i);
      if (t) openMenu(t, more, { queueIndex: i });
      return;
    }
    const main = e.target.closest(".fq__main");
    if (!main) return;
    const i = Number(main.dataset.index);
    if (i === queue.index) togglePlay(); else playIndex(i);
  });
  let fqFrom = -1, fqTo = -1;
  const fqClearMarks = () => $$(".drop-before, .drop-after, .is-drag", fs.qList).forEach((n) => n.classList.remove("drop-before", "drop-after", "is-drag"));
  const fqLockRows = () => $$(".fq__row", fs.qList).forEach((r) => { r.draggable = false; });
  fs.qList.addEventListener("pointerdown", (e) => {
    const grip = e.target.closest(".fq__grip");
    if (grip) grip.closest(".fq__row").draggable = true;   // строка тянется только за «ручку», кнопки внутри остаются кликабельными
  });
  document.addEventListener("pointerup", fqLockRows);
  fs.qList.addEventListener("dragstart", (e) => {
    const row = e.target.closest(".fq__row");
    if (!row) return;
    fqFrom = Number(row.dataset.index); fqTo = -1;
    e.dataTransfer.effectAllowed = "move";
    e.dataTransfer.setData("text/plain", String(fqFrom));
    row.classList.add("is-drag");
  });
  fs.qList.addEventListener("dragover", (e) => {
    const row = e.target.closest(".fq__row");
    if (fqFrom < 0 || !row) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    const r = row.getBoundingClientRect(), after = e.clientY > r.top + r.height / 2, i = Number(row.dataset.index);
    fqTo = after ? i + 1 : i;
    $$(".drop-before, .drop-after", fs.qList).forEach((n) => n.classList.remove("drop-before", "drop-after"));
    row.classList.add(after ? "drop-after" : "drop-before");
  });
  fs.qList.addEventListener("drop", (e) => {
    e.preventDefault();
    const from = fqFrom, to = fqTo;
    fqClearMarks(); fqFrom = -1; fqTo = -1;
    if (from >= 0 && to >= 0) queueMoveTo(from, to);
  });
  fs.qList.addEventListener("dragend", () => { fqClearMarks(); fqLockRows(); fqFrom = -1; fqTo = -1; });

  /** Очередь закончилась: дописываем похожие треки и продолжаем (настройка «Продолжать похожими»). */
  async function continueWithRecs() {
    const cur = current();
    if (!cur) return stopAtEnd();
    try {
      const qs = new URLSearchParams({ limit: "12", source: cur.source, id: cur.id, title: cur.title, exclude: queue.tracks.map(trackKey).join(",") });
      const d = recsResult(await api(`/api/recommendations?${qs}`));
      const have = new Set(queue.tracks.map(trackKey));
      const fresh = d.tracks.filter((t) => !have.has(trackKey(t)));
      const from = queue.append(fresh);                    // как и раньше: дописанная очередь — собственная
      if (!fresh.length) return stopAtEnd();
      queue.label = "Похожие треки";
      toast("Очередь закончилась. Продолжаем похожими треками.");
      return playIndex(from);
    } catch { return stopAtEnd(); }
  }

  // ==========================================================================
  // Диалоги (вместо prompt/confirm браузера)
  // ==========================================================================
  initPickPlaylist();

  // ==========================================================================
  // Меню трека
  // ==========================================================================
  let menuTrack = null, menuCtx = null;
  /** ctx = { queueIndex }: меню строки очереди (играть сейчас, выше/ниже, убрать). */
  function openMenu(track, anchor, ctx = null) {
    menuTrack = track;
    menuCtx = ctx;
    const downloaded = state.dl.has(trackKey(track));
    let items;
    if (ctx && ctx.queueIndex != null) {
      const i = ctx.queueIndex, isCur = i === queue.index, fav = state.fav.has(trackKey(track));
      items = [];
      if (!isCur) items.push(["playnow", "Играть сейчас", "i-play"]);
      if (!isCur && i > queue.index + 1) items.push(["movenext", "Играть следующим", "i-next"]);
      if (!isCur && i > queue.index + 1) items.push(["up", "Выше", "i-chev-u"]);
      if (!isCur && i < queue.length - 1) items.push(["down", "Ниже", "i-chev-d"]);
      if (!isCur) items.push(["rm", "Убрать из очереди", "i-trash"]);
      items.push(["fav", fav ? "Убрать из избранного" : "В избранное", "i-heart"], ["playlist", "Добавить в плейлист…", "i-playlists"],
        ["artist", "Перейти к исполнителю", "i-user"], ["share", "Поделиться", "i-share"]);
    } else {
      items = [
        ["next", "Играть следующим", "i-next"],
        ["add", "Добавить в очередь", "i-queue"],
        ["playlist", "Добавить в плейлист…", "i-playlists"],
      ];
      if (state.view === "playlist" && anchor.closest("#tracks")) items.push(["unlist", "Убрать из плейлиста", "i-trash"]);
      items.push(["artist", "Перейти к исполнителю", "i-user"]);
      if (track.album) items.push(["album", "Открыть альбом", "i-disc"]);
      const denied = !downloaded && state.dlDenied.has(trackKey(track));
      items.push(["download", downloaded ? "Удалить загрузку" : denied ? "Скачивание недоступно" : "Скачать", downloaded ? "i-trash" : "i-download"]);
      items.push(["share", "Поделиться", "i-share"]);
    }
    el.menu.replaceChildren(...items.map(([act, label, ico]) => {
      const b = mk("button");
      b.type = "button"; b.setAttribute("role", "menuitem"); b.dataset.act = act;
      b.append(icon(ico), mk("span", null, label));
      return b;
    }));
    el.menu.hidden = false;
    const r = anchor.getBoundingClientRect(), mw = el.menu.offsetWidth, mh = el.menu.offsetHeight;
    let top = r.bottom + 4;
    if (top + mh > window.innerHeight - 8) top = Math.max(8, r.top - mh - 4);
    el.menu.style.top = `${top}px`;
    el.menu.style.left = `${clamp(r.right - mw, 8, Math.max(8, window.innerWidth - mw - 8))}px`;
    el.menu.querySelector("button")?.focus();
  }
  function closeMenu() { el.menu.hidden = true; menuTrack = null; menuCtx = null; }
  el.menu.addEventListener("click", (e) => {
    const b = e.target.closest("[data-act]");
    const t = menuTrack, qi = menuCtx ? menuCtx.queueIndex : null;
    if (!b || !t) return;
    closeMenu();
    if (state.fs.open && (b.dataset.act === "artist" || b.dataset.act === "album")) closeFs();   // переходим на другую страницу: полноэкранный режим закрываем
    switch (b.dataset.act) {
      case "playnow": playIndex(qi); break;
      case "movenext": queueMoveTo(qi, queue.index + 1); break;
      case "up": queueMove(qi, -1); break;
      case "down": queueMove(qi, 1); break;
      case "rm": queueRemove(qi); break;
      case "fav": toggleFav(t); break;
      case "next": queueAdd(t, true); break;
      case "add": queueAdd(t, false); break;
      case "playlist": addToPlaylistFlow(t); break;
      case "unlist": removeFromPlaylist(t); break;
      case "artist": goArtist(t); break;
      case "album": goAlbum(t); break;
      case "download": toggleDownload(t); break;
      case "share": shareTrack(t); break;
      default: break;
    }
  });
  document.addEventListener("click", (e) => {
    if (!el.menu.hidden && !e.target.closest("#menu") && !e.target.closest('.track__more, .tcard__more, .fq__more, [data-do="more"]')) closeMenu();
  });

  function goArtist(t) {
    const id = (t.artist_ids && t.artist_ids[0]) || t.artist_id;
    const name = (t.artists && t.artists[0]) || t.artist;
    navTo(() => openArtistByName({ source: t.source, id, name }));
  }
  async function goAlbum(t) {
    toast("Ищем альбом…");
    try {
      const q = `${(t.artists && t.artists[0]) || t.artist} ${t.album}`;
      const d = await api(`/api/search/albums?q=${enc(q)}&limit=5&source=${t.source}`);
      const c = (d.items || [])[0];
      if (!c) throw new Error("альбом не найден");
      hideToast();
      navTo(() => openAlbum(c.source, c.id, c.playlist_id));
    } catch (e) { toast(`Не удалось открыть альбом: ${e.message}`); }
  }

  // ---------- поделиться ----------
  const trackUrl = (t) => t.url || (t.source === "yt" ? `https://music.youtube.com/watch?v=${t.id}` : null);
  async function copyText(text) {
    try { await navigator.clipboard.writeText(text); return true; } catch { /* пробуем запасной путь */ }
    try {
      const ta = mk("textarea");
      ta.value = text; ta.style.cssText = "position:fixed;opacity:0;top:0;left:0";
      document.body.append(ta); ta.select();
      const ok = document.execCommand("copy");
      ta.remove();
      return ok;
    } catch { return false; }
  }
  async function shareTrack(t) {
    const url = trackUrl(t);
    const ok = await copyText(url || `${t.artist} — ${t.title}`);
    if (!ok) toast("Не удалось скопировать");
    else toast(url ? "Ссылка на трек скопирована" : "У этого источника нет публичной ссылки: скопировано название");
  }

  // ==========================================================================
  // Загрузки и офлайн: скачать, удалить, «Скачанные»
  // ==========================================================================
  const BUSY = ["preparing", "downloading", "processing"];
  // Скачивается штатный поток трека — тот же, что играет плеер (download_policy.py, S5). Эти коды значат, что
  // поток этого трека получить нельзя или источник отдаёт лишь фрагмент: повтор не поможет, кнопка показывает
  // причину. Остальные ошибки (сеть, лимит запросов, проверка на бота, сбой) временные: кнопка предлагает повторить.
  const DL_FINAL = ["unavailable", "age_restricted", "no_direct_stream", "preview_only"];
  const DL_STAGE = { preparing: "Подготовка", downloading: "Скачивание", processing: "Обработка" };
  const mb = (n) => `${(n / 1048576).toLocaleString("ru-RU", { maximumFractionDigits: 1 })} МБ`;

  /** Что сейчас с загрузкой трека: done | busy | denied | error | idle (+ текст для кнопки и подсказки). */
  function dlStatus(t) {
    const key = trackKey(t), job = state.jobs[key];
    if (state.dl.has(key)) return { kind: "done", short: "Скачано", label: "Скачано. Нажми, чтобы удалить локальную копию" };
    if (job && BUSY.includes(job.state)) {
      const stage = DL_STAGE[job.state];
      let pct = "…";
      if (job.state === "downloading") pct = job.total ? `${job.progress || 0}%` : job.received ? mb(job.received) : "…";
      return { kind: "busy", short: stage, pct, label: job.state === "downloading" ? `${stage}: ${pct}` : `${stage}…` };
    }
    const denied = state.dlDenied.get(key);
    if (denied) return { kind: "denied", short: "Недоступно", label: `Скачивание недоступно. ${denied}` };
    if (job && job.state === "error") return { kind: "error", short: "Ошибка", label: `Ошибка: ${job.error || "не удалось скачать"} Нажми, чтобы повторить.` };
    return { kind: "idle", short: "Скачать", label: "Скачать" };
  }

  /** Рисует кнопку загрузки: скачать / этап и прогресс / скачано (клик = удалить) / недоступно / ошибка. */
  function paintDl(btn, t) {
    const st = dlStatus(t);
    btn.classList.toggle("is-done", st.kind === "done");
    btn.classList.toggle("is-busy", st.kind === "busy");
    btn.classList.toggle("is-denied", st.kind === "denied");
    btn.classList.toggle("is-error", st.kind === "error");
    btn.disabled = st.kind === "busy";
    btn.dataset.dl = st.kind;
    btn.title = st.label;
    btn.setAttribute("aria-label", st.label);
    btn.querySelector("use").setAttribute("href", st.kind === "done" ? "#i-check" : "#i-download");
    btn.querySelector(".track__pct").textContent = st.kind === "busy" ? st.pct || "" : st.kind === "error" ? "!" : "";
    const txt = btn.querySelector(".abtn__t");
    if (txt) txt.textContent = st.short;
  }
  function repaintDl() {
    $$(".track__dl", el.tracks).forEach((b) => { const t = rowTrack(b); if (t) paintDl(b, t); });
    const cur = current();
    if (cur) paintDl(now.dl, cur);
  }
  async function refreshDownloads() {
    try {
      const d = await api("/api/downloads");
      state.dl = new Map(d.items.map((t) => [trackKey(t), t]));
    } catch { /* сервер недоступен: оставляем прежнее состояние */ }
    repaintDl();
  }
  let polling = false;
  const seenFinished = new Set();
  /** Итог задачи — один раз: отказ запоминается для трека, ошибка и готовность показываются тостом. */
  function noteFinished(key, j) {
    const id = key + j.finished_at;
    if (seenFinished.has(id)) return;
    seenFinished.add(id);
    const title = state.dlTitles.get(key);
    if (j.state === "done") { if (title) toast(`Готово: «${title}» сохранён на компьютер`); return; }
    if (j.state !== "error") return;
    if (DL_FINAL.includes(j.code)) state.dlDenied.set(key, j.error);
    toast(title ? `«${title}»: ${j.error}` : j.error, 6000);
  }
  async function pollJobs() {
    if (polling) return;
    polling = true;
    try {
      for (;;) {
        state.jobs = await api("/api/downloads/jobs");
        for (const [k, j] of Object.entries(state.jobs)) if (j.finished_at) noteFinished(k, j);
        if (Object.values(state.jobs).some((j) => j.state === "done")) await refreshDownloads(); else repaintDl();
        if (!Object.values(state.jobs).some((j) => BUSY.includes(j.state))) break;
        await new Promise((r) => setTimeout(r, 800));
      }
      await refreshDownloads();
      if (state.view === "downloads") { await showDownloads(); }
    } catch { /* сеть пропала: опрос возобновится при следующей загрузке */ } finally { polling = false; }
  }
  async function startDownload(t) {
    const key = trackKey(t);
    state.dlTitles.set(key, t.title);
    state.dlDenied.delete(key);
    state.jobs[key] = { state: "preparing", progress: 0 };
    repaintDl();
    try {
      const j = await api("/api/downloads", jsonOpts("POST", { source: t.source, id: t.id, title: t.title, artist: t.artist, album: t.album, duration: t.duration, cover: t.cover }));
      if (j.state === "done") { await refreshDownloads(); toast(`«${t.title}» уже скачан`); return; }
      toast("Загрузка началась");
      pollJobs();
    } catch (e) {
      delete state.jobs[key];
      repaintDl();
      toast(`Не удалось начать загрузку: ${e.message}`);
    }
  }
  async function removeDownload(t) {
    if (!await askConfirm({ title: "Удалить загрузку?", text: `Локальная копия «${t.title}» будет удалена с компьютера. Сам трек останется доступным онлайн.`, ok: "Удалить" })) return;
    try {
      const r = await api(`/api/downloads/${t.source}/${enc(t.id)}`, { method: "DELETE" });
      state.dl.delete(trackKey(t));
      if (r.files_kept) toast("Запись удалена из «Скачанных». Файлы на диске не тронуты: их папку создал не Wave.", 6000);
      else toast("Локальная копия удалена");
      if (state.view === "downloads") await showDownloads(); else repaintDl();
    } catch (e) { toast(e.message); }
  }
  function toggleDownload(t) {
    const key = trackKey(t);
    if (state.dl.has(key)) return removeDownload(t);
    const denied = state.dlDenied.get(key);
    if (denied) { toast(`Скачивание недоступно. ${denied}`, 6000); return; }
    if (state.jobs[key] && BUSY.includes(state.jobs[key].state)) return;
    return startDownload(t);
  }

  async function showDownloads() {
    state.reopen = () => showDownloads();
    resetList({ view: "downloads" });
    setStatus("");
    layout();
    await refreshDownloads();
    const items = [...state.dl.values()];
    el.resultsTitle.textContent = "Скачанные";
    setTracks(items);
    setBlock(el.empty, items.length ? "" : "Здесь появятся треки, которые ты скачал. Они играют без интернета.");
    const active = Object.values(state.jobs).filter((j) => BUSY.includes(j.state)).length;
    setStatus(items.length || active ? `Скачано: ${items.length}${active ? ` · загружается: ${active}` : ""}` : "");
    layout();
  }

  // ==========================================================================
  // Плейлисты
  // ==========================================================================
  const createPlaylist = (name) => api("/api/playlists", jsonOpts("POST", { name }));

  async function createPlaylistFlow() {
    const name = await askText("Новый плейлист", "", "Создать");
    if (!name) return;
    try { const p = await createPlaylist(name); navTo(() => openPlaylist(String(p.id))); }
    catch (e) { toast(e.message); }
  }

  async function showPlaylists() {
    state.reopen = () => showPlaylists();
    resetList({ view: "playlists" });
    setTracks([]);
    el.resultsTitle.textContent = "Плейлисты";
    setStatus("");
    layout();
    el.cards.replaceChildren(...skelCards());
    setBlock(el.empty, "");
    try {
      const d = await api("/api/playlists");
      state.cards = [{ kind: "new", id: "new" }, ...d.items.map((p) => ({ kind: "playlist", id: String(p.id), name: p.name, count: p.count, cover: p.cover }))];
      renderCards();
      setStatus(d.items.length ? `Плейлистов: ${d.items.length}` : "Создай первый плейлист.");
    } catch (e) {
      el.cards.replaceChildren();
      setBlock(el.empty, `Не удалось загрузить плейлисты: ${e.message}`, () => showPlaylists());
    }
  }

  async function openPlaylist(id) {
    resetList({ view: "playlist", ctx: { id } });
    state.reopen = () => openPlaylist(id);
    setTracks([]);
    showDetail("Плейлист", "Загружаем…", "", null, false);
    el.resultsTitle.textContent = "Треки";
    setStatus("");
    layout();
    showSkeleton();
    try {
      const d = await api(`/api/playlists/${id}`);
      state.plName = d.playlist.name;
      showDetail("Плейлист", d.playlist.name, `${d.playlist.count} тр.`, d.playlist.cover, false);
      setTracks(d.tracks);
      setBlock(el.empty, d.tracks.length ? "" : "В плейлисте пока пусто. Добавь треки через меню «⋯» у любого трека.");
      layout();
      el.main.scrollTo({ top: 0 });
    } catch (e) {
      showDetail("Плейлист", "Не удалось открыть плейлист", e.message, null, false);
      clearSkeleton();
      setBlock(el.empty, "Попробуй ещё раз.", () => openPlaylist(id));
    }
  }
  el.dRename.addEventListener("click", async () => {
    const name = await askText("Переименовать плейлист", state.plName || "", "Сохранить");
    if (!name) return;
    try { await api(`/api/playlists/${state.ctx.id}`, jsonOpts("PATCH", { name })); toast("Плейлист переименован"); openPlaylist(state.ctx.id); }
    catch (e) { toast(e.message); }
  });
  el.dDelete.addEventListener("click", async () => {
    if (!await askConfirm({ title: "Удалить плейлист?", text: `«${state.plName}» будет удалён. Сами треки не удаляются.`, ok: "Удалить" })) return;
    try { await api(`/api/playlists/${state.ctx.id}`, { method: "DELETE" }); nav.reset(); toast("Плейлист удалён"); showPlaylists(); }
    catch (e) { toast(e.message); }
  });

  async function addToPlaylistFlow(t) {
    const choice = await pickPlaylist();
    if (!choice) return;
    try {
      let pid = choice, name = "";
      if (choice === "new") {
        name = await askText("Новый плейлист", "", "Создать") || "";
        if (!name) return;
        pid = String((await createPlaylist(name)).id);
      }
      const r = await api(`/api/playlists/${pid}/tracks`, jsonOpts("POST", trackPayload(t)));
      toast(r.added ? `Добавлено в плейлист${name ? ` «${name}»` : ""}` : "Этот трек уже есть в плейлисте");
    } catch (e) { toast(`Не удалось добавить в плейлист: ${e.message}`); }
  }
  async function removeFromPlaylist(t) {
    try {
      await api(`/api/playlists/${state.ctx.id}/tracks/${t.source}/${enc(t.id)}`, { method: "DELETE" });
      openPlaylist(state.ctx.id);
    } catch (e) { toast(e.message); }
  }

  // ==========================================================================
  // Коллекция: сводка пользовательской библиотеки
  // ==========================================================================
  async function showCollection() {
    state.reopen = () => showCollection();
    state.searchCtl?.abort();
    state.view = "collection";
    layout();
    const [pl] = await Promise.all([
      api("/api/playlists").catch(() => null),
      api("/api/favorites").then((d) => { state.fav = new Set(d.items.map(trackKey)); state.favItems = d.items; }).catch(() => {}),
      refreshDownloads(),
    ]);
    if (state.view !== "collection") return;
    const tile = (go, ico, title, sub) => {
      const b = mk("button", "tile");
      b.type = "button"; b.dataset.go = go;
      const i = mk("span", "tile__ico"); i.append(icon(ico));
      const t = mk("span", "tile__t"); t.append(mk("span", "tile__title", title), mk("span", "tile__sub", sub));
      b.append(i, t, icon("i-chev-r", "tile__go"));
      return b;
    };
    const n = plural;
    el.colTiles.replaceChildren(
      tile("liked", "i-heart", "Нравится", n(state.favItems.length, "трек", "трека", "треков")),
      tile("downloads", "i-download", "Скачанные", n(state.dl.size, "трек", "трека", "треков")),
      tile("history", "i-clock", "Недавно слушали", "История прослушивания"),
    );
    state.colPl = [{ kind: "new", id: "new" }, ...(pl ? pl.items.map((p) => ({ kind: "playlist", id: String(p.id), name: p.name, count: p.count, cover: p.cover })) : [])];
    renderRow(el.colPlaylists, state.colPl, "colpl");
    const seen = new Set();
    state.colAr = state.favItems.flatMap((t) => {
      const name = (t.artists && t.artists[0]) || t.artist, id = (t.artist_ids && t.artist_ids[0]) || t.artist_id, k = (name || "").toLowerCase();
      if (!id || !name || seen.has(k)) return [];
      seen.add(k);
      return [{ kind: "artist", source: t.source, id: String(id), name, cover: t.cover, subtitle: "Из избранного" }];
    }).slice(0, 20);
    renderRow(el.colArtists, state.colAr, "colar");
    el.colArtistsSec.hidden = !state.colAr.length;
  }
  el.colTiles.addEventListener("click", (e) => {
    const b = e.target.closest("[data-go]");
    if (!b) return;
    navTo(() => ({ liked: showLiked, downloads: showDownloads, history: showHistory })[b.dataset.go]());
  });

  // ==========================================================================
  // Настройки
  // ==========================================================================
  initThemeFollowSystem();

  /** Поле формы настроек: не перезаписываем то, что пользователь сейчас редактирует. */
  function setField(sel, prop, value) {
    const node = $(sel);
    if (node !== document.activeElement) node[prop] = value;
  }
  /** Применяет настройки интерфейса один раз, при старте (ответы на последующие PUT их не трогают). */
  function applyUiSettings(v) {
    if (!flags.volTouched && Number.isFinite(v.volume)) audio.volume = clamp(v.volume, 0, 1);
    if (Number.isFinite(v.last_volume) && v.last_volume >= 0.05) state.lastVol = v.last_volume;
    state.lyricsOn = v.lyrics_visible !== false;
    Object.assign(covers, v.mix_covers || {});
    for (const m of themeMixes) if (covers[m.themeId]) m.cover = covers[m.themeId];
    flags.uiReady = true;
    if (flags.volTouched) uiSave.put({ volume: round3(audio.volume) });   // пока ждали backend, громкость уже поменяли
    paintVolume();
    renderThemeMixes();
    renderProfile();
    document.body.dataset.settings = "ready";              // признак готовности (используют e2e-тесты)
  }
  async function loadSettings() {
    try {
      let d = await fetchSettings();
      if (d.meta && d.meta.local_ui_migrated === false) {
        const since = settingsGeneration();
        try { d = await migrateLocalUi(); applySettings(d.values, since); state.settingsPaths = d.paths; } catch { /* попробуем при следующем запуске */ }
      }
      state.source = d.values.default_source;              // источник по умолчанию
      syncTabs();
      applyUiSettings(d.values);
    } catch { flags.uiReady = true; document.body.dataset.settings = "default"; /* остаются значения по умолчанию */ }
  }
  const setMsg = (t) => { $("#set-msg").textContent = t; };
  async function showSettings() {
    state.reopen = () => showSettings();
    state.searchCtl?.abort();
    state.view = "settings";
    layout();
    try { await fetchSettings(); } catch { setMsg("Не удалось загрузить настройки."); return; }
    const s = state.settings, p = state.settingsPaths;   // уже без устаревших значений (applySettings)
    setField("#set-source", "value", s.default_source);
    setField("#set-autoplay", "checked", !!s.autoplay);
    setField("#set-theme", "value", s.theme);
    setField("#set-dir", "value", p.downloads);
    setField("#set-name", "value", profileName());
    $("#set-dir").disabled = $("#set-dir-save").disabled = !!p.downloads_locked;
    $("#set-paths").textContent = `Данные приложения: ${p.data}`;
    setMsg(p.downloads_locked ? "Папка загрузок задана переменной окружения WAVE_DOWNLOADS_DIR." : "");
  }
  async function saveSetting(body, okText) {
    markLocal(Object.keys(body));
    const since = settingsGeneration();
    try {
      const d = await api("/api/settings", jsonOpts("PUT", body));
      applySettings(d.values, since);                      // свои ключи применяются, более поздние правки — нет
      renderProfile();
      state.settingsPaths = d.paths;
      setField("#set-dir", "value", d.paths.downloads);
      setMsg(okText || "Сохранено");
    } catch (e) { setMsg(e.message); }
  }
  $("#set-source").addEventListener("change", (e) => saveSetting({ default_source: e.target.value }));
  $("#set-autoplay").addEventListener("change", (e) => saveSetting({ autoplay: e.target.checked }));
  $("#set-theme").addEventListener("change", (e) => saveSetting({ theme: e.target.value }));
  $("#set-dir-save").addEventListener("click", () => saveSetting({ downloads_dir: $("#set-dir").value.trim() }, "Папка сохранена. Новые загрузки пойдут в неё."));
  $("#set-clear-history").addEventListener("click", async () => {
    if (!await askConfirm({ title: "Очистить историю?", text: "Рекомендации станут менее точными. Избранное и загрузки не затрагиваются.", ok: "Очистить" })) return;
    try { await api("/api/settings/clear-history", { method: "POST" }); state.hist = []; computeArtists(); renderArtistMixes(); setMsg("История очищена."); } catch (e) { setMsg(e.message); }
  });
  $("#set-clear-cache").addEventListener("click", async () => {
    try { await api("/api/settings/clear-cache", { method: "POST" }); setMsg("Кэш очищен. Загрузки не затронуты."); } catch (e) { setMsg(e.message); }
  });

  // Профиль: локальное имя в боковой панели (хранится в настройках backend, никуда не отправляется)
  const profileName = () => (state.settings.profile_name || "").trim();
  function renderProfile() {
    const n = profileName() || "Слушатель";
    el.profileName.textContent = n;
    el.avatar.textContent = [...n][0].toUpperCase();
  }
  $("#set-name").addEventListener("change", (e) => {
    const name = [...e.target.value.trim()].slice(0, 24).join("");
    state.settings.profile_name = name;                     // панель обновляется сразу, запись — следом
    renderProfile();
    saveSetting({ profile_name: name });
  });

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
  hero.title.addEventListener("click", () => { if (current()) openNowFromUi(); });   // на главной нет мини-плеера: заголовок баннера ведёт на страницу трека
  el.playerNow.addEventListener("keydown", (e) => {
    if ((e.key === "Enter" || e.key === " ") && e.target === el.playerNow) { e.preventDefault(); openNowFromUi(); }
  });

  el.form.addEventListener("submit", (e) => {
    e.preventDefault();
    nav.reset();
    search(el.input.value);
  });

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

  // Сортировка, вкладки поиска, источник
  el.sort.addEventListener("change", () => { state.sort = el.sort.value; sortList(); renderList(); });
  el.tabs.addEventListener("click", (e) => {
    const btn = e.target.closest(".tab");
    if (!btn || btn.dataset.tab === state.tab) return;
    state.tab = btn.dataset.tab;
    syncTabs();
    if (state.query) search(state.query);
  });
  el.sourceSwitch.addEventListener("click", (e) => {
    const btn = e.target.closest(".seg__btn");
    if (!btn || btn.dataset.source === state.source) return;
    state.source = btn.dataset.source;
    syncTabs();
    if (state.query && state.view === "search") search(state.query);
  });

  el.back.addEventListener("click", goBack);
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
    if (e.target.closest(".track__dl")) { toggleDownload(t); return; }
    const more = e.target.closest(".track__more");
    if (more) { openMenu(t, more); return; }
    const who = e.target.closest(".who.is-link");
    if (who) { navTo(() => openArtist(t.source, who.dataset.aid)); return; }   // имя автора: на его страницу, трек не запускаем
    if (queue.isFrom(state.list) && queue.current === t) togglePlay(); else playFromList(i);
  });
  // Имя автора в мини-плеере и на странице трека
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

  // Прогрев при наведении (с задержкой, чтобы не греть всё подряд при движении мыши)
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
    if (navigator.onLine) goHome(true); else showDownloads();
    refreshDownloads();
    pollJobs();                                            // подхватываем загрузки, которые шли до перезагрузки страницы
  }
  boot();
})();