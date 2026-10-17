// Очередь, которую видит пользователь: боковая панель очереди и панель «Сейчас играет / Далее» полноэкранного
// плеера (перетаскивание за «ручку», меню «⋯»). Правки делает модель (player/queue.js); здесь — показ, тосты
// и подтверждение очистки.
import { $, $$, mk, icon, button, coverEl } from "../core/dom.js";
import { fmtTime, plural } from "../core/format.js";
import { el, fs } from "../core/elements.js";
import { state, queue, bus } from "../state/store.js";
import { playIndex, togglePlay, playerEvents } from "../player/controller.js";
import { askConfirm } from "../components/dialogs.js";
import { toast } from "../components/toast.js";
import { openMenu } from "../components/menu.js";
import { paintFav } from "../components/track-row.js";
import { toggleFav } from "../components/favorites.js";
import { focusSafely, focusLost, rescueFocus, keyboardMode, dropLostFocus } from "../components/focus.js";

// Правки очереди — методами модели (player/queue.js); здесь — то, что видит пользователь.
// Баннер главной показывает «Далее»: после добавления и переноса он перерисовывается (bus "hero").
export function queueAdd(t, next) {
  queue.add(t, next);
  toast(next ? "Будет играть следующим" : "Добавлено в очередь");
  renderQueue();
  bus.emit("hero");
}
export function queueRemove(i) {
  if (!queue.remove(i)) return;                          // играющий трек не убираем
  renderQueue();
}
export function queueMove(i, d) {
  if (!queue.move(i, d)) return;
  renderQueue();
}
function queueClear() {
  queue.clear();
  renderQueue();
}
/** Очистка необратима: спрашиваем подтверждение. Играющий трек остаётся. */
export async function queueClearFlow() {
  if (queue.length <= 1) return;
  if (!await askConfirm({ title: "Очистить очередь?", text: "Останется только текущий трек. Остальные уберутся из очереди (в избранном, плейлистах и истории они сохранятся).", ok: "Очистить" })) return;
  queueClear();
  toast("Очередь очищена");
}
/** Перенос трека на позицию to (индекс вставки до удаления). Играющий трек не прерывается. */
export function queueMoveTo(from, to) {
  if (!queue.moveTo(from, to)) return;
  renderQueue();
  bus.emit("hero");
}
// S12: строки очереди перерисовываются целиком — и по действию пользователя, и сами (трек доиграл, «следующий» с
// медиаклавиш). Если фокус клавиатуры был в списке, он возвращается на ту же кнопку того же трека на новом месте;
// если трек убран или такой кнопки у него больше нет (стал текущим, дошёл до края) — на строку, вставшую на его
// место, иначе на последнюю строку, иначе на запасную кнопку. После клика мышью фокус не переносится (focus.js).
const rowTrack = new WeakMap();                          // строка → её трек (номер строки после правки уже другой)
function keepListFocus(list, render, o) {
  const a = document.activeElement;
  const li = a && a !== list && list.contains(a) ? a.closest("li") : null;
  const t = li ? rowTrack.get(li) : null;
  const kind = li ? o.kind(a) : null, was = li ? Number(a.dataset.index) : -1;
  render();
  if (!li || !focusLost()) return;
  if (!keyboardMode()) return;                          // мышь: кнопка отсоединена, браузер уже перевёл фокус на body
  const j = t ? queue.tracks.indexOf(t) : -1;
  const at = (k, sel) => list.querySelector(`${sel}[data-index="${k}"]`);
  focusSafely(j >= 0 ? at(j, kind) : null) || focusSafely(at(j >= 0 ? j : was, j >= 0 ? o.main : o.vanished))
    || focusSafely([...list.querySelectorAll(o.main)].pop()) || focusSafely(o.fallback());
}
const DRAWER = {
  kind: (b) => (b.dataset.q ? `[data-q="${b.dataset.q}"]` : b.classList.contains("q__fav") ? ".q__fav" : ".q__main"),
  main: ".q__main", vanished: ".q__main", fallback: () => $("#queue-close"),
};
const FSQ = {
  kind: (b) => (b.classList.contains("fq__more") ? ".fq__more" : ".fq__main"),
  main: ".fq__main", vanished: ".fq__more", fallback: () => fs.play,
};

/** Перерисовывает очередь везде, где она сейчас видна. */
export function renderQueue() {
  if (!el.queue.hidden) renderDrawer();
  if (state.fs.open && state.fs.mode === "queue") renderFsQueue();
}
function renderDrawer() { keepListFocus(el.queueList, drawRows, DRAWER); }
function drawRows() {
  el.queueLabel.textContent = queue.label ? `Из «${queue.label}»` : "";
  const rows = queue.tracks.map((t, i) => {
    const li = mk("li", i === queue.index ? "q is-current" : "q");
    rowTrack.set(li, t);
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
// S12: панель очереди помнит, откуда её открыли. Если при закрытии с клавиатуры (Esc, Enter на «Закрыть») фокус был
// в панели, он возвращается на открывшую кнопку, а не пропадает вместе со скрытой панелью.
let queueOpener = null;
export function toggleQueue() {
  if (!el.queue.hidden) { closeQueue(); return; }
  queueOpener = document.activeElement;
  el.queue.hidden = false;
  renderQueue();
}
export function closeQueue() {
  if (el.queue.hidden) return;
  const inside = el.queue.contains(document.activeElement);
  el.queue.hidden = true;
  const opener = queueOpener;
  queueOpener = null;
  if (!inside) return;
  if (!keyboardMode()) { dropLostFocus(); return; }      // закрыли мышью — фокус не переносим, но и не держим в панели
  if (!focusSafely(opener)) rescueFocus();
}
/** Куда вернуть фокус после действия меню строки полноэкранной очереди: «⋯» того же трека, иначе строки на его
 *  месте (трек убран), иначе кнопка Play/Pause. Строки перерисовываются целиком, прежний узел «⋯» уже не годится. */
function fqRefocus(t, i) {
  const j = queue.tracks.indexOf(t);
  return fs.qList.querySelector(`.fq__more[data-index="${j >= 0 ? j : i}"]`) || [...fs.qList.querySelectorAll(".fq__more")].pop() || fs.play;
}

// ---------- очередь в полноэкранном плеере: «Сейчас играет» и «Далее» ----------
const FQ_MAX = 300;                                      // длинную очередь не рисуем целиком
function fqRow(t, i) {
  const cur = i === queue.index;
  const li = mk("li", cur ? "fq__row is-current" : "fq__row");
  li.dataset.index = i;
  rowTrack.set(li, t);
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
export function renderFsQueue() { keepListFocus(fs.qList, drawFsRows, FSQ); }
function drawFsRows() {
  const q = queue.tracks, cur = queue.index;
  fs.qCount.textContent = q.length ? plural(q.length, "трек", "трека", "треков") : "";
  fs.qClear.disabled = q.length <= 1;
  const nodes = [];
  if (cur > 0) nodes.push(mk("li", "fq__note", `Уже сыграно: ${cur}`));   // объясняет, почему всего треков больше, чем ниже в списке
  if (q[cur]) nodes.push(mk("li", "fq__h", "Сейчас играет"), fqRow(q[cur], cur));
  const rest = Math.max(0, q.length - cur - 1);
  nodes.push(mk("li", "fq__h", rest ? `Далее · ${rest}` : "Далее"));
  if (state.shuffle && rest) nodes.push(mk("li", "fq__note", "Включено перемешивание: следующий трек выбирается случайно, порядок ниже не гарантирован."));
  if (!rest) {
    const note = state.repeat === "all" ? "Это последний трек: дальше очередь начнётся сначала (повтор очереди)."
      : state.settings.autoplay ? "Дальше ничего нет. Когда очередь закончится, Wave продолжит похожими треками." : "Дальше ничего нет.";
    nodes.push(mk("li", "fq__note", note));
  }
  for (let i = cur + 1; i < Math.min(q.length, cur + 1 + FQ_MAX); i++) nodes.push(fqRow(q[i], i));
  if (rest > FQ_MAX) nodes.push(mk("li", "fq__note", `Показаны первые ${FQ_MAX} из ${rest}.`));
  fs.qList.replaceChildren(...nodes);
}

export function initQueueDrawer() {
  playerEvents.on("track", () => renderQueue());
  playerEvents.on("modes", () => renderQueue());          // подсказки о перемешивании и повторе
  // Один обработчик на весь список очереди
  el.queueList.addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    const i = Number(b.dataset.index), t = queue.at(i);
    if (b.classList.contains("q__fav")) { if (t) toggleFav(t); return; }
    if (b.dataset.q === "up") queueMove(i, -1);
    else if (b.dataset.q === "down") queueMove(i, 1);
    else if (b.dataset.q === "rm") queueRemove(i);
    else if (b.classList.contains("q__main")) playIndex(i);   // «играть сейчас»; фокус сохраняет renderDrawer
  });
  $("#queue-close").addEventListener("click", closeQueue);
  $("#queue-clear").addEventListener("click", queueClearFlow);

  // Один набор обработчиков на весь список полноэкранной очереди: клик, меню «⋯», перетаскивание за «ручку»
  fs.qList.addEventListener("click", (e) => {
    const more = e.target.closest(".fq__more");
    if (more) {
      const i = Number(more.dataset.index), t = queue.at(i);
      if (t) openMenu(t, more, { queueIndex: i, refocus: () => fqRefocus(t, i) });
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
}
