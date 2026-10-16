// Меню трека «⋯»: обычное (в очередь, плейлист, исполнитель, альбом, загрузка, поделиться) и меню строки
// очереди (играть сейчас, выше/ниже, убрать). Что делает каждый пункт, задаёт приложение (initMenu).
import { mk, icon } from "../core/dom.js";
import { clamp } from "../core/format.js";
import { trackKey } from "../core/api.js";
import { el } from "../core/elements.js";
import { state, queue } from "../state/store.js";
import { BUSY } from "./track-row.js";

let menuTrack = null, menuCtx = null;

/** ctx = { queueIndex }: меню строки очереди (играть сейчас, выше/ниже, убрать). */
export function openMenu(track, anchor, ctx = null) {
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
    if (track.album || track.album_id) items.push(["album", "Открыть альбом", "i-disc"]);
    const denied = !downloaded && state.dlDenied.has(trackKey(track));
    const job = state.jobs[trackKey(track)];
    const busy = !downloaded && job && BUSY.includes(job.state);      // S11: идущую или ждущую загрузку можно отменить
    if (!downloaded && state.dlStarting.has(trackKey(track))) {
      items.push(["download", "Загрузка запускается…", "i-download", true]);   // отменять ещё нечего
    } else {
      items.push(["download", downloaded ? "Удалить загрузку" : busy ? "Отменить загрузку" : denied ? "Скачивание недоступно" : "Скачать",
        downloaded ? "i-trash" : busy ? "i-x" : "i-download"]);
    }
    items.push(["share", "Поделиться", "i-share"]);
  }
  el.menu.replaceChildren(...items.map(([act, label, ico, off]) => {
    const b = mk("button");
    b.type = "button"; b.setAttribute("role", "menuitem"); b.dataset.act = act;
    if (off) b.setAttribute("aria-disabled", "true");
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
export function closeMenu() { el.menu.hidden = true; menuTrack = null; menuCtx = null; }

/**
 * actions: { [пункт]: (track, queueIndex) => void } — что делает каждый пункт меню (data-act).
 * Меню закрывается до действия. Клик вне меню и вне кнопок «⋯» тоже закрывает его.
 */
export function initMenu(actions) {
  el.menu.addEventListener("click", (e) => {
    const b = e.target.closest("[data-act]");
    const t = menuTrack, qi = menuCtx ? menuCtx.queueIndex : null;
    if (!b || !t) return;
    closeMenu();
    const act = actions[b.dataset.act];
    if (act) act(t, qi);
  });
  document.addEventListener("click", (e) => {
    if (!el.menu.hidden && !e.target.closest("#menu") && !e.target.closest('.track__more, .tcard__more, .fq__more, [data-do="more"]')) closeMenu();
  });
}
