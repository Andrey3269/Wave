// «О программе / Диагностика» (S16): версия Wave, система, время запуска и память, данные, кэши, проверка компонентов
// воспроизведения YouTube. Данные — GET /api/about (быстро, без сети) и GET /api/diagnostics (запускает только
// `deno --version` с таймаутом, сети не использует). Секретов и полных путей backend в эти ответы не кладёт.
import { $, mk } from "../core/dom.js";
import { api } from "../core/api.js";
import { state } from "../state/store.js";
import { layout } from "./shell.js";

const SEC = (n) => (n == null ? "—" : n < 1000 ? `${Math.round(n)} мс` : `${(n / 1000).toLocaleString("ru-RU", { maximumFractionDigits: 1 })} с`);
const MB = (n) => (n == null ? "—" : `${n.toLocaleString("ru-RU", { maximumFractionDigits: 1 })} МБ`);
const AGE = (s) => (s == null ? "—" : s < 120 ? `${s} с` : s < 7200 ? `${Math.round(s / 60)} мин` : `${Math.round(s / 3600)} ч`);
const NUM = (n) => (n == null ? "—" : n.toLocaleString("ru-RU"));
const CACHE_LABELS = { search: "Поиск", lyrics: "Тексты песен", artists: "Страницы исполнителей", stream_links: "Ссылки на аудио",
  stream_heads: "Заголовки аудио", hls_sessions: "Сеансы HLS" };
const COUNT_LABELS = { favorites: "Треков в «Нравится»", favorite_items: "Альбомов и исполнителей", history: "Записей истории",
  playlists: "Плейлистов", playlist_tracks: "Треков в плейлистах", downloads: "Скачанных треков", local_tracks: "Локальных треков" };

let report = { about: null, diagnostics: null };
let seq = 0;

/** Заполняет <dl> парами [название, значение]. */
function fill(sel, pairs) {
  $(sel).replaceChildren(...pairs.flatMap(([k, v]) => [mk("dt", null, k), mk("dd", null, v)]));
}
const setMsg = (t) => { $("#about-msg").textContent = t; };

/** Время до первого экрана в этом окне: метка, которую ставит main.js после первого экрана. */
function firstScreenMs() {
  const m = performance.getEntriesByName("wave:first-screen")[0];
  return m ? m.startTime : null;
}
/** Движок окна без лишнего: Edge WebView2 (Edg/…) или Chromium с номером версии. */
function engine() {
  const ua = navigator.userAgent;
  const edg = ua.match(/Edg\/([\d.]+)/), chr = ua.match(/Chrome\/([\d.]+)/);
  return edg ? `Microsoft Edge WebView2 ${edg[1]}` : chr ? `Chromium ${chr[1]}` : "—";
}

function renderAbout(a) {
  fill("#about-app", [
    ["Версия", a.app.version],
    ["Сборка", a.app.frozen ? "установленное приложение" : "запуск из исходников"],
    ["Система", `${a.app.os} (${a.app.machine}), процессоров: ${NUM(a.app.cpus)}`],
    ["Python", a.app.python],
    ["Окно интерфейса", engine()],
  ]);
  const heap = performance.memory ? performance.memory.usedJSHeapSize / 1048576 : null;
  fill("#about-perf", [
    ["Запуск backend", a.startup.backend_ready_ms == null ? "—" : `${SEC(a.startup.backend_ready_ms)} от старта процесса`],
    ["Первый экран интерфейса", SEC(firstScreenMs())],
    ["Перезапусков backend", NUM(a.startup.restarts)],
    ["Работает", AGE(a.process.uptime_s)],
    ["Память backend", MB(a.process.rss_mb)],
    ...(heap != null ? [["Память интерфейса (JS)", MB(heap)]] : []),
  ]);
  const st = a.storage;
  fill("#about-data", [
    ["Каталог данных", st.data_dir],
    ["Папка загрузок", st.downloads_dir],
    ["База данных", `${MB(st.db_bytes == null ? null : st.db_bytes / 1048576)}, схема v${st.schema_version ?? "—"}`],
    ...Object.entries(COUNT_LABELS).map(([k, label]) => [label, NUM(st.counts[k])]),
  ]);
  fill("#about-caches", Object.entries(a.caches).map(([k, c]) =>
    [CACHE_LABELS[k] || k, `${NUM(c.items)} из ${NUM(c.maxsize)}, хранится до ${AGE(c.ttl)}`]));
}

function renderDiagnostics(d) {
  const ok = d.youtube_playback === "configured";
  const rt = d.js_runtime;
  $("#about-diag-state").textContent = `${ok ? "Компоненты в порядке" : "Есть проблемы"}: yt-dlp ${d.yt_dlp.version}, `
    + `Deno ${rt.ok ? rt.version : "не готов"}. Проверка заняла ${SEC(d.duration_ms)}. ${d.note}`;
  $("#about-issues").replaceChildren(...d.issues.map((i) => {
    const li = mk("li", `diag__item diag__item--${i.severity}`, `${i.severity === "critical" ? "Критично" : "Внимание"}: ${i.message}`);
    if (i.hint) li.append(mk("span", "set__hint set__hint--text", ` ${i.hint}`));
    return li;
  }));
}

async function loadAbout() {
  const my = ++seq;
  try {
    const a = await api("/api/about");
    if (my !== seq || state.view !== "about") return;
    report.about = a;
    renderAbout(a);
  } catch (e) { if (my === seq) setMsg(`Не удалось получить сведения: ${e.message}`); }
}

let checking = null;
async function runCheck() {
  if (checking) return checking;                         // повторное нажатие ждёт текущую проверку
  const btn = $("#about-check");
  btn.disabled = true;
  btn.setAttribute("aria-busy", "true");
  $("#about-diag-state").textContent = "Проверяем компоненты…";
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), 30000);    // backend ограничен таймаутом запуска Deno; это страховка окна
  checking = (async () => {
    try {
      const d = await api("/api/diagnostics", { signal: ctl.signal });
      report.diagnostics = d;
      renderDiagnostics(d);
    } catch (e) {
      $("#about-diag-state").textContent = e.name === "AbortError" ? "Проверка не ответила за 30 с. Попробуйте ещё раз." : `Проверка не выполнена: ${e.message}`;
    } finally {
      clearTimeout(timer);
      btn.disabled = false;
      btn.removeAttribute("aria-busy");
      checking = null;
    }
  })();
  return checking;
}

export function showAbout() {
  state.reopen = () => showAbout();
  state.searchCtl?.abort();
  state.view = "about";
  setMsg("");
  layout();
  loadAbout();
  if (!report.diagnostics) runCheck(); else renderDiagnostics(report.diagnostics);
}

export function initAboutView() {
  $("#about-check").addEventListener("click", () => runCheck());
  $("#about-copy").addEventListener("click", async () => {
    const text = JSON.stringify({ ...report, ui: { first_screen_ms: firstScreenMs(), engine: engine() } }, null, 2);
    try { await navigator.clipboard.writeText(text); setMsg("Отчёт скопирован в буфер обмена."); }
    catch { setMsg("Не удалось скопировать отчёт: буфер обмена недоступен."); }
  });
}
