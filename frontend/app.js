/* ==========================================================================
   Wave UI (Vanilla JS). Один <audio>, одна очередь, одно состояние избранного.
   Экраны (state.view): home | search | artist | album | mix | playlists | playlist |
   collection | liked | downloads | history | settings | now.
   Звук всегда идёт с нашего бэкенда (track.stream_url), YouTube напрямую не трогаем.
   Кнопки управления плеером (play/prev/next/like/...) помечены data-do и обрабатываются
   одним обработчиком на документе: сколько бы таких кнопок ни было на экране, состояние одно.
   Подключается как ES-модуль (строгий режим включён всегда, отдельная директива не нужна).
   ========================================================================== */
import { $, $$, mk, icon, button, setCover, initImageFallback } from "./js/core/dom.js";
import { SOURCES, clamp, rand, fmtTime, plural } from "./js/core/format.js";
import { enc, api, jsonOpts, recsResult, trackKey, trackPayload } from "./js/core/api.js";
import { el, views, audio, hero, now, fs } from "./js/core/elements.js";
import { state, queue, current, heroTrack, DETAIL_VIEWS, bus, nav } from "./js/state/store.js";
import { uiSave, initSettingsSync, round3, markLocal, settingsGeneration, flags, applySettings, fetchSettings,
  migrateLocalUi, initThemeFollowSystem } from "./js/state/settings.js";
import { toast, hideToast } from "./js/components/toast.js";
import { setBlock, initEmptyRetry, skelCards } from "./js/components/skeleton.js";
import { askText, askConfirm, pickPlaylist, initPickPlaylist } from "./js/components/dialogs.js";
import { loadLyrics, syncLyrics, initLyrics } from "./js/components/lyrics.js";
import { renderArtists, paintFav, paintDl, BUSY, rowTrack, markPlaying } from "./js/components/track-row.js";
import { renderRow, bindCardClicks, trackCard, miniCard, bindTrackGrid, mixCard, markMix,
  initRowScroll } from "./js/components/cards.js";
import { openMenu, closeMenu, initMenu } from "./js/components/menu.js";
import { repaintFav, toggleFav } from "./js/components/favorites.js";
import { shareTrack } from "./js/components/share.js";
import { updateProgress, paintVolume, toggleMute, initSliders } from "./js/player/sliders.js";
import { loadMix, buildMixes, covers, themeMixes } from "./js/state/mixes.js";
import { layout, syncTabs } from "./js/views/shell.js";
import { setStatus, setTracks, renderCards, showSkeleton, clearSkeleton, fetchPage, resetList, playFromList, initList,
  initListWarm } from "./js/views/list.js";
import { showDetail, openArtist, openAlbum, openMix, goAlbum } from "./js/views/detail.js";
import { search, showSearch, goArtist, initSearchForm, initSearchTabs } from "./js/views/search.js";
import { renderThemeMixes, renderArtistMixes, computeArtists, initSidebar } from "./js/views/sidebar.js";
import { initPlayerBar } from "./js/views/player-bar.js";
import { openFs, closeFs, setFsMode, initFullscreen } from "./js/views/fullscreen.js";
import { queueAdd, queueRemove, queueMove, queueMoveTo, toggleQueue, initQueueDrawer } from "./js/views/queue-drawer.js";
import { playerEvents, setIdleAction, play, playIndex, togglePlay, playNext, playPrev, setShuffle, setRepeat,
  warmTrack, initPlayer } from "./js/player/controller.js";

(() => {
  initImageFallback();

  initSettingsSync();

  initEmptyRetry();

  // ==========================================================================
  // Экраны и каркас
  // ==========================================================================
  const navTo = (fn) => nav.to(fn);
  const goBack = () => nav.back();
  nav.fallback = () => goHome(false);

  // ==========================================================================
  // Список треков (строки) и карточки
  // ==========================================================================
  const CARD_LISTS = { cards: () => state.cards, albums: () => state.albums, related: () => state.related,
    colpl: () => state.colPl, colar: () => state.colAr, simar: () => similar.artists };
  bindCardClicks([el.cards, el.albumsRow, el.relatedRow, el.colPlaylists, el.colArtists, now.similarArtists], CARD_LISTS, (c) => {
    if (c.kind === "new") { createPlaylistFlow(); return; }
    if (c.kind === "artist") navTo(() => openArtist(c.source, c.id));
    else if (c.kind === "playlist") navTo(() => openPlaylist(c.id));
    else navTo(() => openAlbum(c.source, c.id, c.playlist_id));
  });

  // ---------- карточки треков (главная, «похожие») ----------
  bindTrackGrid(el.newGrid, () => state.feed, () => feedLabel());
  bindTrackGrid(el.continueRow, () => state.hist, () => "Недавно слушали");
  bindTrackGrid(now.similarRow, () => similar.tracks, () => `Похожие на «${current()?.title || "трек"}»`);

  initRowScroll();                                         // стрелки у горизонтальных рядов

  // ---------- миксы ----------
  // Мини-«рецепты»: список треков собирается по клику существующими API (поиск, рекомендации).
  // Названия описывают, что подборка делает на самом деле (M13): без фирменных названий чужих сервисов
  // и без обещаний персонализации там, где её нет. id не меняются: по ним хранятся обложки (mix_covers).
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
  initSidebar();                                           // «Твоя музыка» и «Миксы по артистам»

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
  /** Играть треки как очередь (player/controller.js: очередь копирует массив, источник продолжает её). */
  const playTracks = (tracks, i, label = "") => play(tracks, i, { label });
  /** Подпись очереди: откуда играет («Альбом 1», «Поиск: gspd», «Нравится»...). */
  function listLabel() {
    if (DETAIL_VIEWS.includes(state.view)) return el.dTitle.textContent;
    if (state.view === "search") return `Поиск: ${state.query}`;
    return el.resultsTitle.textContent;
  }
  function playFromList(i) {
    play(state.list, i, { label: listLabel(), mixId: state.view === "mix" ? state.ctx?.mix : null });
  }

  // Play при пустом <audio>: на главной — подборка, иначе очередь или список на экране
  setIdleAction(() => {
    if (state.view === "home") return startWave();
    if (queue.length) return playIndex(Math.max(0, queue.index));
    if (state.list.length) return playFromList(0);
    el.input.focus();
    return undefined;
  });

  // ==========================================================================
  // UI плеера: мини-плеер, баннер и страница трека показывают одно состояние
  // ==========================================================================
  initPlayerBar();                                         // мини-плеер и кнопки управления (views/player-bar.js)
  // Баннер главной и страница трека показывают тот же трек
  playerEvents.on("track", () => { renderHero(); if (state.view === "now") renderNow(); });
  playerEvents.on("playing", () => renderHero());
  playerEvents.on("modes", (changed) => { if (changed === "shuffle" && state.view === "home") renderHero(); });
  bus.on("hero", () => { if (state.view === "home") renderHero(); });
  initPlayer();

  initSliders();

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
    if (state.lyricsOn) { loadLyrics(t); syncLyrics(); }
    loadSimilar(t);
    updateProgress();
  }

  // ---------- тексты (components/lyrics.js) ----------
  initLyrics();
  function toggleLyrics() {
    state.lyricsOn = !state.lyricsOn;
    uiSave.put({ lyrics_visible: state.lyricsOn }, 0);
    now.stage.classList.toggle("no-lyrics", !state.lyricsOn);
    now.lyricsBtn.setAttribute("aria-pressed", String(state.lyricsOn));
    const t = current();
    if (state.lyricsOn && t) { loadLyrics(t); syncLyrics(); }
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
  initFullscreen();

  // ==========================================================================
  // Избранное (одно состояние для всех экранов)
  // ==========================================================================
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
  initQueueDrawer();

  // ==========================================================================
  // Диалоги (вместо prompt/confirm браузера)
  // ==========================================================================
  initPickPlaylist();

  // ==========================================================================
  // Меню трека
  // ==========================================================================
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

  // ==========================================================================
  // Загрузки и офлайн: скачать, удалить, «Скачанные»
  // ==========================================================================
  // Скачивается штатный поток трека — тот же, что играет плеер (download_policy.py, S5). Эти коды значат, что
  // поток этого трека получить нельзя или источник отдаёт лишь фрагмент: повтор не поможет, кнопка показывает
  // причину. Остальные ошибки (сеть, лимит запросов, проверка на бота, сбой) временные: кнопка предлагает повторить.
  const DL_FINAL = ["unavailable", "age_restricted", "no_direct_stream", "preview_only"];

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
    if (navigator.onLine) goHome(true); else showDownloads();
    refreshDownloads();
    pollJobs();                                            // подхватываем загрузки, которые шли до перезагрузки страницы
  }
  boot();
})();