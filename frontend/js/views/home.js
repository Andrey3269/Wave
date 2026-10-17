// Главная: баннер (текущий трек или первый трек подборки), ряд миксов, «Продолжить слушать» и бесконечная
// лента /api/recommendations.
import { setCover } from "../core/dom.js";
import { SOURCES, fmtTime, rand } from "../core/format.js";
import { api, recsResult, trackKey } from "../core/api.js";
import { el, hero, audio } from "../core/elements.js";
import { state, queue, current, heroTrack, nav, bus } from "../state/store.js";
import { loadMix, buildMixes } from "../state/mixes.js";
import { play, warmTrack, playerEvents } from "../player/controller.js";
import { mixCard, miniCard, trackCard, markMix, bindTrackGrid } from "../components/cards.js";
import { paintFav, markPlaying } from "../components/track-row.js";
import { setBlock, skelCards, setMore } from "../components/skeleton.js";
import { focusLost, focusSafely, rescueFocus, keyboardMode, dropLostFocus } from "../components/focus.js";
import { toast, hideToast } from "../components/toast.js";
import { layout } from "./shell.js";
import { openMix } from "./detail.js";
import { computeArtists, renderArtistMixes } from "./sidebar.js";
import { openNowFromUi } from "./now.js";

const home = { seq: 0, loading: false, error: "", partial: "", ready: false, at: 0, pending: null };   // pending — идущая подгрузка ленты (S12)
const HOME_TTL = 10 * 60 * 1000;

export function goHome(refresh = true) {
  state.reopen = () => goHome(false);
  state.searchCtl?.abort();
  const reload = refresh || !home.ready || Date.now() - home.at > HOME_TTL;
  // S12: подгрузка ленты, начатая до ухода с главной, ещё идёт, а лента не перезагружается — флаг подгрузки снова её:
  // второй запрос не уйдёт, её ответ допишет карточки. При перезагрузке ленты прежняя подгрузка устаревает (loadHome).
  Object.assign(state, { view: "home", query: "", cards: [], albums: [], related: [], sort: "default", loadingMore: (!reload && home.pending) || false });
  state.list = [];
  el.tracks.replaceChildren(); el.cards.replaceChildren();
  setMore(el.more, "none");
  layout();
  if (reload) { loadHome(); }
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
  home.pending = null;                                   // подгрузка прежней ленты устарела (её ответ отбросит проверка seq)
  if (state.view === "home") state.loadingMore = false;
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
async function playMix(mix) {
  toast("Собираем микс…", 8000);
  try {
    const tracks = await loadMix(mix);
    if (!tracks.length) { toast("Не удалось собрать микс: источник ничего не вернул."); return; }
    hideToast();
    play(tracks, 0, { label: mix.title });
    state.mixId = mix.id; markMix();
  } catch (e) { toast(`Не удалось собрать микс: ${e.message}`); }
}

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
    setMore(el.homeMore, "none");
    return;
  }
  el.newGrid.replaceChildren(...items.map((t, i) => trackCard(t, i + 1)));
  markPlaying();
  if (home.error) setBlock(el.homeEmpty, `Не удалось подобрать музыку. ${home.error}.`, () => loadHome());
  else if (home.partial && state.feed.length) setBlock(el.homeEmpty, `Подборка неполная: ${home.partial} ответил не на все запросы.`, () => loadHome());
  else setBlock(el.homeEmpty, state.feed.length ? "" : "Пока нечего предложить. Найди что-нибудь через поиск.");
  setMore(el.homeMore, home.pending && state.loadingMore === home.pending ? "loading" : state.more ? "idle" : "none", "Подбираем ещё…");
}
function appendFeedCards(from) {
  el.newGrid.append(...state.feed.slice(from).map((t, i) => trackCard(t, from + i)));
  markPlaying();
}

/**
 * Лента без конца: каждая следующая страница строится от ПОСЛЕДНЕГО показанного трека («радио»)
 * плюс следующие треки любимых артистов. Лента заканчивается, только если трижды подряд нет ничего нового.
 */
export async function loadMoreHome() {
  const my = home.seq;
  const stale = () => my !== home.seq || state.view !== "home";
  const mine = {};                                       // флаг подгрузки принадлежит этому запуску (S12)
  state.loadingMore = mine;
  home.pending = mine;
  const moreFocused = document.activeElement === el.homeMore;
  setMore(el.homeMore, "loading", "Подбираем ещё…");
  let firstNew = -1;                                     // индекс первой подгруженной карточки
  try {
    let added = false;
    for (let attempt = 0; attempt < 3 && !added; attempt++) {
      const last = state.feed[state.feed.length - 1];
      const qs = new URLSearchParams({ limit: "24", page: String(state.feedPage++), salt: String(state.salt),
        exclude: state.feed.map(trackKey).slice(-200).join(",") });
      if (last) { qs.set("source", last.source); qs.set("id", last.id); qs.set("title", last.title); }
      const d = recsResult(await api(`/api/recommendations?${qs}`));   // сбой источника = ошибка, а не «лента кончилась»
      if (stale()) return;                               // лента перезагружена или открыт другой экран — ответ не нужен
      const have = new Set(state.feed.map(trackKey));
      const fresh = d.tracks.filter((t) => !have.has(trackKey(t)));
      if (fresh.length) {
        const from = state.feed.length;
        state.feed.push(...fresh);
        appendFeedCards(from);
        if (firstNew < 0) firstNew = from;
        added = true;
        if (queue.isFrom(state.feed)) queue.extend();     // очередь играет ленту: продолжается новыми треками
      }
    }
    state.more = added;
  } catch {
    if (!stale()) setMore(el.homeMore, "error");
    return;
  } finally {
    if (state.loadingMore === mine) state.loadingMore = false;
    if (home.pending === mine) home.pending = null;
  }
  setMore(el.homeMore, state.more ? "idle" : "none");
  // Подгрузка кнопкой, а ленте больше нечего добавить: кнопка скрылась вместе с фокусом — на первую новую карточку;
  // новых нет — на последнюю карточку ленты, иначе на заголовок экрана
  if (moreFocused && focusLost()) {
    const first = firstNew >= 0 ? el.newGrid.querySelector(`.tcard[data-index="${firstNew}"] .tcard__open`) : null;
    if (!keyboardMode()) dropLostFocus();                    // нажали мышью — фокус никуда не переносим
    else focusSafely(first) || focusSafely(el.newGrid.querySelector(".tcard:last-child .tcard__open")) || rescueFocus();
  }
}

/** Название ленты главной по тому, как она реально собрана (поля personalized и radio ответа). */
function feedLabel() {
  if (state.personalized) return "Подобрано по вашей истории";
  if (state.feedRadio) return "Похоже на текущий трек";
  if (!state.hist.length && !state.favItems.length) return "С чего начать";
  return "Подборка";
}
/** «Слушать подборку»: очередь = лента главной, старт с трека из баннера. */
export async function startWave() {
  if (!state.feed.length) {
    if (home.loading) return;
    await loadHome();
  }
  if (!state.feed.length) { toast("Не удалось подобрать музыку. Попробуй позже."); return; }
  play(state.feed, Math.max(0, state.feed.indexOf(state.featured)), { label: feedLabel() });
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

/** Карточки ленты и «Продолжить слушать», ряд миксов. */
export function initHomeGrids() {
  bindTrackGrid(el.newGrid, () => state.feed, () => feedLabel());
  bindTrackGrid(el.continueRow, () => state.hist, () => "Недавно слушали");
}
export function initHomeMixRow() {
  el.mixRow.addEventListener("click", (e) => {
    const card = e.target.closest(".mixcard");
    const m = card && state.mixes.find((x) => x.id === card.dataset.mix);
    if (!m) return;
    if (e.target.closest(".playbtn")) playMix(m); else nav.to(() => openMix(m));
  });
}
/** Баннер показывает тот же трек и то же состояние, что и плеер. */
export function initHomeHero() {
  playerEvents.on("track", () => renderHero());
  playerEvents.on("playing", () => renderHero());
  playerEvents.on("modes", (changed) => { if (changed === "shuffle" && state.view === "home") renderHero(); });
  bus.on("hero", () => { if (state.view === "home") renderHero(); });
  // на главной нет мини-плеера: заголовок баннера ведёт на страницу трека
  hero.title.addEventListener("click", () => { if (current()) openNowFromUi(); });
}
