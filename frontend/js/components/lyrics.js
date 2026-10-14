// Тексты песен. Данные одни (LRCLIB через /api/lyrics), показываем их в двух местах: панель на странице трека
// и режим «Текст песни» полноэкранного плеера.
import { mk } from "../core/dom.js";
import { api, enc, trackKey } from "../core/api.js";
import { audio, now, fs } from "../core/elements.js";
import { state } from "../state/store.js";

const LY_TEXT = { loading: "Ищем текст…", none: "Текст для этого трека не найден", error: "Текст сейчас недоступен" };
const ly = { lines: [], plain: "", status: "idle", idx: -1, key: "" };
const lyBoxes = [now.lyrics, fs.lyrics];

function paintLyrics() {
  for (const box of lyBoxes) {
    if (ly.status === "ok" && ly.lines.length) box.replaceChildren(...ly.lines.map((l, i) => { const p = mk("p", "ly", l.text); p.dataset.i = i; return p; }));
    else if (ly.status === "ok") box.replaceChildren(mk("p", "ly ly--plain", ly.plain));   // текст без таймкодов: без ложной синхронизации
    else box.replaceChildren(mk("p", "lyrics__empty", LY_TEXT[ly.status] || ""));
  }
  syncLyrics();
}
export async function loadLyrics(t) {
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
export function tickLyrics(cur, instant = false) {
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
/** Заново выставить подсветку и прокрутку по текущей позиции (панель только что показали или перерисовали). */
export function syncLyrics() {
  ly.idx = -2;
  tickLyrics(audio.currentTime || 0, true);
}
/** Клик по строке = перемотка к ней. */
export function initLyrics() {
  for (const box of lyBoxes) {
    box.addEventListener("click", (e) => {
      const p = e.target.closest(".ly[data-i]");
      if (p && ly.lines[Number(p.dataset.i)]) audio.currentTime = ly.lines[Number(p.dataset.i)].t;
    });
  }
}
