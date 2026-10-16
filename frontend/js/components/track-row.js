// Строка трека в списке и то, что рисуется на любых кнопках трека: исполнители-ссылки, значок источника,
// «в избранном», состояние загрузки, подсветка играющего трека.
import { $, $$, mk, setCover } from "../core/dom.js";
import { SOURCES, fmtTime } from "../core/format.js";
import { trackKey } from "../core/api.js";
import { el } from "../core/elements.js";
import { state, current } from "../state/store.js";

/** Показывает значок источника в узле .src (или прячет, если источник неизвестен). */
export function setSource(node, source) {
  const name = SOURCES[source];
  node.hidden = !name;
  if (!name) return;
  node.title = name;
  node.setAttribute("aria-label", name);
  node.querySelector("use").setAttribute("href", `#i-src-${source}`);
}

/**
 * Имена авторов трека: каждый отдельным элементом. Если источник дал id автора — это кнопка-ссылка на его страницу
 * (S12: доступна с клавиатуры, Enter и Space открывают страницу, трек не трогают); без id — обычный текст, ложной
 * ссылки нет. Узел node не должен лежать внутри другой кнопки: вложенные интерактивные элементы запрещены.
 */
export function renderArtists(node, t) {
  const names = t.artists && t.artists.length ? t.artists : [t.artist];
  const ids = t.artist_ids && t.artist_ids.length ? t.artist_ids : (t.artist_id ? [t.artist_id] : []);
  node.replaceChildren();
  names.forEach((name, i) => {
    if (i) node.append(", ");
    if (!ids[i]) { node.append(mk("span", "who", name)); return; }
    const b = mk("button", "who is-link", name);
    b.type = "button";
    b.dataset.aid = ids[i];
    const label = `Исполнитель: ${name}`;
    b.setAttribute("aria-label", label);
    b.title = label;
    node.append(b);
  });
}

/** Кнопка «в избранное» любого вида (строка, очередь, мини-плеер, страница трека, баннер). */
export function paintFav(btn, t) {
  const on = state.fav.has(trackKey(t));
  const label = on ? "Убрать из избранного" : "В избранное";
  btn.classList.toggle("is-fav", on);
  btn.setAttribute("aria-pressed", String(on));
  btn.title = label;
  btn.setAttribute("aria-label", label);
  const txt = btn.querySelector("span:not(.track__pct)");
  if (txt && btn.classList.contains("abtn")) txt.textContent = on ? "В избранном" : "Нравится";
}

// ---------- Состояние загрузки трека ----------
// S11: queued — ждёт свободного места (backend выполняет не больше двух загрузок сразу); cancelled — отменена, это
// не ошибка: кнопка снова «Скачать». Пока загрузка идёт или ждёт, нажатие на кнопку её отменяет.
export const BUSY = ["queued", "preparing", "downloading", "processing"];
const DL_STAGE = { queued: "В очереди", preparing: "Подготовка", downloading: "Скачивание", processing: "Обработка" };
const CANCEL_HINT = "Нажми, чтобы отменить загрузку";
const mb = (n) => `${(n / 1048576).toLocaleString("ru-RU", { maximumFractionDigits: 1 })} МБ`;

/** Что сейчас с загрузкой трека: done | busy | denied | error | idle (+ текст для кнопки и подсказки). */
function dlStatus(t) {
  const key = trackKey(t), job = state.jobs[key];
  if (state.dl.has(key)) return { kind: "done", short: "Скачано", label: "Скачано. Нажми, чтобы удалить локальную копию" };
  // Запуск ещё не подтверждён backend: задачи в /api/downloads/jobs может ещё не быть, а отменять нечего
  if (state.dlStarting.has(key)) return { kind: "busy", short: DL_STAGE.preparing, pct: "…", label: "Подготовка… Загрузка запускается", cancelling: true };
  if (job && BUSY.includes(job.state)) {
    if (state.dlCancelling.has(key)) return { kind: "busy", short: "Отмена…", pct: "", label: "Загрузка отменяется…", cancelling: true };
    const stage = DL_STAGE[job.state];
    if (job.state === "queued") {
      return { kind: "busy", queued: true, short: stage, pct: "", label: `${stage}: начнётся, когда закончится одна из текущих загрузок. ${CANCEL_HINT}` };
    }
    let pct = "…";
    if (job.state === "downloading") pct = job.total ? `${job.progress || 0}%` : job.received ? mb(job.received) : "…";
    return { kind: "busy", short: stage, pct, label: `${job.state === "downloading" ? `${stage}: ${pct}` : `${stage}…`} ${CANCEL_HINT}` };
  }
  const denied = state.dlDenied.get(key);
  if (denied) return { kind: "denied", short: "Недоступно", label: `Скачивание недоступно. ${denied}` };
  if (job && job.state === "error") return { kind: "error", short: "Ошибка", label: `Ошибка: ${job.error || "не удалось скачать"} Нажми, чтобы повторить.` };
  return { kind: "idle", short: "Скачать", label: "Скачать" };
}

/** Рисует кнопку загрузки: скачать / этап и прогресс (клик = отменить) / скачано (клик = удалить) / недоступно / ошибка. */
export function paintDl(btn, t) {
  const st = dlStatus(t);
  btn.classList.toggle("is-done", st.kind === "done");
  btn.classList.toggle("is-busy", st.kind === "busy");
  btn.classList.toggle("is-queued", !!st.queued);
  btn.classList.toggle("is-denied", st.kind === "denied");
  btn.classList.toggle("is-error", st.kind === "error");
  // S11: кнопка идущей загрузки не отключается (disabled убрал бы её из обхода клавиатурой) — она отменяет загрузку.
  // Пока запрос отмены в пути, повторное нажатие ничего не делает: aria-disabled, фокус остаётся на кнопке.
  btn.disabled = false;
  if (st.cancelling) btn.setAttribute("aria-disabled", "true"); else btn.removeAttribute("aria-disabled");
  btn.dataset.dl = st.kind;
  btn.title = st.label;
  btn.setAttribute("aria-label", st.label);
  btn.querySelector("use").setAttribute("href", st.kind === "done" ? "#i-check" : st.kind === "busy" ? "#i-x" : "#i-download");
  btn.querySelector(".track__pct").textContent = st.kind === "busy" ? st.pct || "" : st.kind === "error" ? "!" : "";
  const txt = btn.querySelector(".abtn__t");
  if (txt) txt.textContent = st.short;
}

/** Строка трека из шаблона #track-template. i — позиция в state.list. */
export function buildRow(t, i) {
  const node = el.tpl.content.cloneNode(true);
  const li = $(".row", node);
  li.dataset.index = i;
  li.dataset.key = trackKey(t);
  $(".track__play", node).setAttribute("aria-label", `Играть: ${t.artist} — ${t.title}`);
  $(".track__idx", node).textContent = i + 1;
  setCover($(".track__cover img", node), t.cover, 120);
  $(".track__title", node).textContent = t.title;
  renderArtists($(".track__artist", node), t);          // каждый автор отдельной ссылкой на его страницу (вне .track__play)
  setSource($(".src", node), t.source);
  $(".track__album", node).textContent = t.reason || t.album || "";   // у рекомендаций здесь причина
  $(".track__time", node).textContent = t.duration ? fmtTime(t.duration) : "";
  paintFav($(".track__fav", node), t);
  paintDl($(".track__dl", node), t);
  return node;
}
/** Трек строки списка, в которой находится узел. */
export const rowTrack = (node) => state.list[Number(node.closest(".row")?.dataset.index)];

/** Подсветка играющего трека везде, где он показан (строки, карточки, «Продолжить слушать»). */
export function markPlaying() {
  const cur = current(), k = cur ? trackKey(cur) : null;
  $$("[data-key]").forEach((n) => n.classList.toggle("is-playing", n.dataset.key === k));
}
