// Контроллер воспроизведения (TARGET_ARCHITECTURE §11): единственный <audio>, источник звука трека
// (обычный файл или HLS через hls.js — в тот же <audio>, S8), очередь, события <audio>, прогрев, Media Session,
// сигналы истории. Экраны к <audio> не обращаются — только к функциям этого модуля.
//
// Интерфейс узнаёт о состоянии плеера из событий playerEvents (вызываются синхронно, по порядку подписки):
//   "track"   (track)       — начался новый трек: мини-плеер, баннер, страница трека, очередь
//   "loading" (text|null)   — вместо исполнителя показать статус («Загрузка…», «Буферизация…») или вернуть имя
//   "playing" (bool)        — играет / пауза
//   "modes"   ("shuffle"|"repeat") — сменился режим перемешивания или повтора (state.shuffle, state.repeat)
//   "mix"     ()            — сменился микс, из которого играет очередь (state.mixId)
// Что делать, если Play нажали, когда ничего не загружено, решает приложение: setIdleAction().
import { clamp } from "../core/format.js";
import { api, jsonOpts, recsResult, trackKey, trackPayload } from "../core/api.js";
import { Emitter } from "../core/emitter.js";
import { audio } from "../core/elements.js";
import { state, queue, current } from "../state/store.js";
import { nextRepeat, normRepeat } from "./queue.js";
import { toast } from "../components/toast.js";
import { getDuration, seekBy, seekTo, resetProgress, updateProgress } from "./sliders.js";
import { currentPosition, saveSoon, withoutSaving } from "./persist.js";
import { ListenTracker } from "./listen.js";

export const playerEvents = new Emitter();

let idleAction = () => {};
/** Play при пустом <audio>: что запустить (подборку главной, очередь, список на экране). */
export function setIdleAction(fn) { idleAction = fn; }

// ---------- Очередь ----------
/**
 * Играть треки как очередь. Очередь копирует массив (M11); если источник потом дополнится (лента главной,
 * следующая страница списка), очередь продолжится через continuation — пока пользователь её не правил.
 * mixId — микс, из которого играет очередь (отмечается в боковой панели).
 */
export function play(tracks, i, { label = "", mixId = null } = {}) {
  if (!tracks[i]) return;
  // Прерванный трек для сигнала «пропуск» помнит сам запуск (listenTrack), а не очередь: её сейчас заменят (S9)
  queue.load(tracks, { label, source: tracks, continuation: async () => tracks });
  state.mixId = mixId;
  playerEvents.emit("mix");
  playIndex(i);
}

// ---------- История прослушивания (S10) ----------
// Прослушивание записывается, когда фактически проиграно min(30 с, 50 % длительности) — player/listen.js;
// один запуск трека — не больше одной записи. Пропуск — запуск, прерванный пользователем после начала звука,
// но до порога: сигнал уходит для прерванного трека (S9: не для трека нового списка) один раз. Естественный
// конец трека и ошибка потока пропуском не считаются.
const listen = new ListenTracker();
let listenTrack = null;                  // трек текущего запуска (его прослушивание или пропуск записываются)

function startListen(track) {
  listenTrack = track;
  listen.start(trackKey(track));
}
/** Запуск прерван: пользователь переключил трек (user) — пропуск, если порог не достигнут. */
function endListen(user) {
  if (user && listenTrack && listen.isSkip()) reportSkip(listenTrack);
  listen.stop();
  listenTrack = null;
}
/** Длительность для порога: реальная из <audio>, пока её нет — из метаданных трека; 0 — неизвестна. */
function listenDuration() {
  if (Number.isFinite(audio.duration) && audio.duration > 0) return audio.duration;
  return listenTrack?.duration || 0;
}
function onListenProgress() {
  if (!audio.getAttribute("src") || audio.paused && !audio.ended) return;
  if (listen.progress(audio.currentTime, performance.now(), { rate: audio.playbackRate || 1, duration: listenDuration() }) && listenTrack) {
    recordPlay(listenTrack);
  }
}

/** Играть трек i очереди. at — с какой секунды начать (восстановленная позиция после перезапуска, S10).
 *  auto — переход без участия пользователя (трек доиграл, ошибка потока): прерванный запуск — не пропуск.
 *  Прерванный трек (для сигнала «пропуск») берётся из текущего запуска, а не из очереди: при play() очередь уже
 *  заменена новой (исправление S9 теперь следует из устройства, а не из переданного параметра). */
export function playIndex(i, { at = 0, auto = false } = {}) {
  const track = queue.at(i);
  if (!track) return;
  state.resumeAt = null;
  endListen(!auto && !audio.ended);
  queue.select(i);
  startListen(track);

  resetProgress(track.duration);
  updateMediaMetadata(track);
  playerEvents.emit("track", track);
  setLoading("Загрузка…");
  player.recover = 0;
  startTrack(track, at > 0 ? at : 0);
}

/**
 * Восстановление после перезапуска (S10): очередь, текущий трек, позиция и режимы из сохранённого состояния.
 * Решение продукта: звук сам не включается. Трек показывается на паузе с сохранённой позицией (мини-плеер,
 * баннер «Продолжить», системный оверлей); Play продолжает с этой позиции. Если пользователь уже запустил
 * что-то сам, пока шёл запуск, сохранённое не применяется.
 */
export function restorePlayback(saved) {
  if (!saved || queue.length || audio.getAttribute("src")) return false;
  return withoutSaving(() => applySaved(saved));
}
function applySaved(saved) {
  if (!queue.restore(saved)) return false;
  state.shuffle = saved.shuffle === true;
  state.repeat = normRepeat(saved.repeat);
  playerEvents.emit("modes", "shuffle");
  playerEvents.emit("modes", "repeat");
  const t = current();
  if (!t) return true;
  const dur = t.duration || 0;
  state.resumeAt = clamp(Number(saved.position) || 0, 0, dur > 1 ? dur - 1 : Number.MAX_SAFE_INTEGER);
  resetProgress(t.duration);
  updateProgress();
  updateMediaMetadata(t);
  playerEvents.emit("track", t);
  setPlaying(false);
  return true;
}

function recordPlay(t) { api("/api/history", jsonOpts("POST", trackPayload(t))).catch(() => {}); }
// С S10 — со снимком трека: пропуск до порога — отдельная запись истории (backend/userdata.py)
function reportSkip(t) { api("/api/history/skip", jsonOpts("POST", { source: t.source, id: t.id, track: trackPayload(t) })).catch(() => {}); }

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
  audio.loop = false;                     // повтор трека — в обработчике ended (одинаково для файла и HLS)
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
    audio.loop = false;
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
  endListen(false);                       // ошибка потока — не пропуск и не прослушивание
  // Одно сообщение с трек-названием и настоящей причиной (тост показывает одно сообщение: второе затирало первое)
  toast(detail ? `Не удалось воспроизвести «${track.title}»: ${detail}` : `Не удалось воспроизвести: ${track.title}`,
    detail ? 6000 : 3600);
  clearLoading();
  setPlaying(false);
  if (state.errors++ < 3 && nextIndex() >= 0) setTimeout(() => playNext({ auto: true }), 800);
}

function onPlayRejected(err) {
  // AbortError: быстро переключили трек, пока шёл прошлый play(): это нормально
  if (err.name === "AbortError") return;
  if (err.name === "NotAllowedError") { toast("Нажми Play, чтобы начать воспроизведение"); return; }
  onAudioError();
}

// ---------- Управление ----------
export function togglePlay() {
  if (player.pending) return undefined;   // трек ещё готовится (/playback): Play не запускает ничего другого
  if (!audio.getAttribute("src")) {
    // восстановленный после перезапуска трек — с сохранённой позиции (S10); иначе решает приложение
    if (state.resumeAt != null && current()) return playIndex(queue.index, { at: state.resumeAt });
    return idleAction();
  }
  if (audio.paused) audio.play().catch(onPlayRejected); else audio.pause();
  return undefined;
}

/** Следующая позиция очереди при текущих режимах; auto — трек доиграл сам (только тогда repeat "one" — тот же трек). */
export function nextIndex(auto = false) {
  return queue.nextIndex({ shuffle: state.shuffle, repeat: state.repeat, auto });
}
/** «Следующий» (кнопка, медиаклавиша; auto — переход после ошибки потока). */
export function playNext({ auto = false } = {}) {
  const j = nextIndex();
  if (j < 0) return;
  if (j === queue.index) replayCurrent(!auto);           // repeat "all" с одним треком: тот же трек сначала
  else playIndex(j, { auto });
}
export function playPrev() {
  // как в большинстве плееров: после 3 секунд «назад» перематывает в начало трека;
  // при перемешивании «назад» идёт по истории воспроизведения (player/queue.js)
  if (currentPosition() > 3) { seekTo(0); return; }
  const j = queue.prevIndex({ shuffle: state.shuffle });
  if (j < 0) { seekTo(0); return; }
  playIndex(j);
}
export function setShuffle(on) {
  state.shuffle = !!on;
  if (state.shuffle) queue.restartCycle();               // новый круг без повторов — с текущего трека
  playerEvents.emit("modes", "shuffle");
  saveSoon();
}
/** Повтор: "off" | "all" | "one". audio.loop не используется: с ним не приходит ended, и повтор не работал бы
 *  одинаково для обычного файла и HLS. */
export function setRepeat(mode) {
  state.repeat = normRepeat(mode);
  audio.loop = false;
  playerEvents.emit("modes", "repeat");
  saveSoon();
}
/** Кнопка повтора: выкл → вся очередь → текущий трек → выкл. */
export function cycleRepeat() { setRepeat(nextRepeat(state.repeat)); }

/** Тот же трек сначала (repeat "one" или единственный трек при repeat "all"): без нового резолва потока.
 *  Новый запуск для истории: свой порог, своя запись. user — по кнопке (прерванный проигрыш может быть пропуском). */
function replayCurrent(user = false) {
  const t = current();
  if (!t) return;
  endListen(user && !audio.ended);
  startListen(t);
  playerEvents.emit("replay", t);
  audio.currentTime = 0;
  audio.play().catch((err) => {
    if (err.name === "AbortError" || current() !== t) return;
    startTrack(t, 0);                                    // поток не продолжился с начала — запускаем трек заново
  });
}

// ---------- Прогрев ----------
/**
 * Прогрев: просим у сервера 1 байт трека, чтобы он заранее получил прямую ссылку
 * через yt-dlp (это занимает секунды). К моменту клика трек стартует почти сразу.
 */
const warmed = new Map();                  // id -> время прогрева
const WARM_FRESH = 20 * 60 * 1000, WARMED_MAX = 200;
export function warmTrack(t) {
  if (!t) return;
  const last = warmed.get(t.id);
  if (last && Date.now() - last < WARM_FRESH) return;       // ссылка на сервере живёт часами
  warmed.set(t.id, Date.now());
  if (warmed.size > WARMED_MAX) {                            // S16: отметки старше 20 мин больше не нужны
    const old = Date.now() - WARM_FRESH;
    for (const [id, at] of warmed) if (at < old) warmed.delete(id);
    while (warmed.size > WARMED_MAX) warmed.delete(warmed.keys().next().value);
  }
  // Сервер сразу отвечает 202/204 и в фоне получает ссылку + первую порцию трека
  fetch(`${t.stream_url}?warm=1`, { cache: "no-store" })
    .then((r) => { if (r.status !== 202 && r.status !== 204) warmed.delete(t.id); })  // 503: занят, позже
    .catch(() => warmed.delete(t.id));
}
// После поиска готовим первые треки заранее, с паузой, чтобы не нагружать сервер разом
let warmTimers = [];
export function scheduleWarm(list) {
  warmTimers.forEach(clearTimeout);
  warmTimers = list.map((t, i) => setTimeout(() => warmTrack(t), i * 1500));
}
/** Прогреваем следующий трек очереди (при перемешивании модель заранее выбирает его и запоминает выбор). */
function warmNext() {
  const t = queue.peekNext({ shuffle: state.shuffle, repeat: state.repeat });
  if (t) warmTrack(t);
}

// Индикатор в плеере: пока идёт резолв/буферизация, вместо исполнителя пишем статус
function setLoading(text) { playerEvents.emit("loading", text); }
function clearLoading() { playerEvents.emit("loading", null); }

function setPlaying(playing) {
  playerEvents.emit("playing", playing);
  if (mediaSession) mediaSession.playbackState = playing ? "playing" : "paused";
  updatePositionState();
}

function onAudioError() {
  if (!audio.getAttribute("src") || player.pending) return;
  const t = current();
  if (player.kind === "hls") return;     // ошибки HLS-потока обрабатывает onHlsFatal (у hls.js точнее причина)
  const at = audio.currentTime || 0;
  const kind = player.kind;
  clearLoading();
  setPlaying(false);
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
function stopAtEnd() { audio.currentTime = 0; setPlaying(false); updateProgress(); }

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
    return playIndex(from, { auto: true });          // очередь доиграла сама: не пропуск
  } catch { return stopAtEnd(); }
}

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
    mediaSession.setPositionState({ duration: dur, playbackRate: audio.playbackRate || 1, position: clamp(currentPosition(), 0, dur) });
  } catch { /* некорректное состояние при смене трека: игнорируем */ }
}

/** События <audio>, освобождение HLS-сеанса при закрытии окна, медиаклавиши. */
export function initPlayer() {
  audio.addEventListener("play", () => setPlaying(true));
  audio.addEventListener("pause", () => setPlaying(false));
  audio.addEventListener("waiting", () => setLoading("Буферизация…"));
  audio.addEventListener("playing", () => {
    state.errors = 0;
    clearLoading();
    warmNext();
  });
  audio.addEventListener("loadedmetadata", () => { updateProgress(); updatePositionState(); });
  audio.addEventListener("durationchange", updateProgress);
  audio.addEventListener("timeupdate", () => { if (!state.seeking) updateProgress(); onListenProgress(); });
  audio.addEventListener("seeking", () => listen.seeking());      // перемотка не засчитывается в прослушивание
  audio.addEventListener("seeked", updatePositionState);
  audio.addEventListener("ratechange", updatePositionState);
  audio.addEventListener("ended", () => {
    const j = nextIndex(true);
    if (j >= 0 && j === queue.index) replayCurrent();
    else if (j >= 0) playIndex(j, { auto: true });
    else if (state.settings.autoplay && navigator.onLine) continueWithRecs();
    else stopAtEnd();
  });
  audio.addEventListener("error", onAudioError);
  // Окно закрывают: HLS-сеанс на backend освобождается сразу, не дожидаясь срока жизни
  window.addEventListener("pagehide", detachHls);

  if (mediaSession) {
    const actions = {
      play: () => (audio.getAttribute("src") ? audio.play().catch(onPlayRejected) : togglePlay()),
      pause: () => audio.pause(),
      previoustrack: () => playPrev(),
      nexttrack: () => playNext(),
      stop: () => { audio.pause(); seekTo(0); },
      seekbackward: (d) => seekBy(-(d.seekOffset || 10)),
      seekforward: (d) => seekBy(d.seekOffset || 10),
      seekto: (d) => { if (Number.isFinite(d.seekTime)) seekTo(d.seekTime); },
    };
    for (const [name, handler] of Object.entries(actions)) {
      try { mediaSession.setActionHandler(name, handler); } catch { /* действие не поддерживается */ }
    }
  }
}
