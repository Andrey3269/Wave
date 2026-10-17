// Меню трека «⋯»: обычное (в очередь, плейлист, исполнитель, альбом, загрузка, поделиться) и меню строки
// очереди (играть сейчас, выше/ниже, убрать). Что делает каждый пункт, задаёт приложение (initMenu).
//
// Клавиатура (S12, модель WAI-ARIA «menu button»): открытие ставит фокус на первый доступный пункт; стрелки вверх и
// вниз — по кругу (core/keynav.js), Home/End — крайние доступные; Escape закрывает и возвращает фокус на «⋯»; Tab
// закрывает и продолжает обход от «⋯». Недоступный пункт (aria-disabled) получает фокус, но ничего не делает.
// У открывшей кнопки aria-expanded="true", пока меню открыто. После закрытия меню не хранит ссылок на элементы.
import { mk, icon } from "../core/dom.js";
import { clamp } from "../core/format.js";
import { trackKey } from "../core/api.js";
import { el } from "../core/elements.js";
import { navTarget, firstEnabled } from "../core/keynav.js";
import { state, queue } from "../state/store.js";
import { BUSY } from "./track-row.js";
import { focusSafely, focusLost, rescueFocus, keyboardMode } from "./focus.js";

let menuTrack = null, menuCtx = null, menuAnchor = null;

const items = () => [...el.menu.querySelectorAll('[role="menuitem"]')];
const enabledFlags = (list) => list.map((b) => b.getAttribute("aria-disabled") !== "true");

/**
 * ctx = { queueIndex, refocus }: меню строки очереди (играть сейчас, выше/ниже, убрать); refocus() → куда вернуть
 * фокус, если действие перерисовало строку вместе с «⋯». Повторное нажатие на ту же «⋯» закрывает меню.
 */
export function openMenu(track, anchor, ctx = null) {
  if (!el.menu.hidden) {
    const same = anchor === menuAnchor;
    closeMenu({ restore: same });
    if (same) return;
  }
  menuTrack = track;
  menuCtx = ctx;
  menuAnchor = anchor;
  const downloaded = state.dl.has(trackKey(track));
  let entries;
  if (ctx && ctx.queueIndex != null) {
    const i = ctx.queueIndex, isCur = i === queue.index, fav = state.fav.has(trackKey(track));
    entries = [];
    if (!isCur) entries.push(["playnow", "Играть сейчас", "i-play"]);
    if (!isCur && i > queue.index + 1) entries.push(["movenext", "Играть следующим", "i-next"]);
    if (!isCur && i > queue.index + 1) entries.push(["up", "Выше", "i-chev-u"]);
    if (!isCur && i < queue.length - 1) entries.push(["down", "Ниже", "i-chev-d"]);
    if (!isCur) entries.push(["rm", "Убрать из очереди", "i-trash"]);
    entries.push(["fav", fav ? "Убрать из избранного" : "В избранное", "i-heart"], ["playlist", "Добавить в плейлист…", "i-playlists"],
      ["artist", "Перейти к исполнителю", "i-user"], ["share", "Поделиться", "i-share"]);
  } else {
    entries = [
      ["next", "Играть следующим", "i-next"],
      ["add", "Добавить в очередь", "i-queue"],
      ["playlist", "Добавить в плейлист…", "i-playlists"],
    ];
    if (state.view === "playlist" && anchor.closest("#tracks")) entries.push(["unlist", "Убрать из плейлиста", "i-trash"]);
    entries.push(["artist", "Перейти к исполнителю", "i-user"]);
    if (track.album || track.album_id) entries.push(["album", "Открыть альбом", "i-disc"]);
    const denied = !downloaded && state.dlDenied.has(trackKey(track));
    const job = state.jobs[trackKey(track)];
    const busy = !downloaded && job && BUSY.includes(job.state);      // S11: идущую или ждущую загрузку можно отменить
    if (!downloaded && state.dlStarting.has(trackKey(track))) {
      entries.push(["download", "Загрузка запускается…", "i-download", true]);   // отменять ещё нечего
    } else {
      entries.push(["download", downloaded ? "Удалить загрузку" : busy ? "Отменить загрузку" : denied ? "Скачивание недоступно" : "Скачать",
        downloaded ? "i-trash" : busy ? "i-x" : "i-download"]);
    }
    entries.push(["share", "Поделиться", "i-share"]);
  }
  el.menu.replaceChildren(...entries.map(([act, label, ico, off]) => {
    const b = mk("button");
    b.type = "button"; b.setAttribute("role", "menuitem"); b.dataset.act = act;
    b.tabIndex = -1;                                     // по пунктам ходят стрелками, Tab закрывает меню
    if (off) b.setAttribute("aria-disabled", "true");
    b.append(icon(ico), mk("span", null, label));
    return b;
  }));
  el.menu.setAttribute("aria-label", `Действия с треком: ${track.title}`);
  anchor.setAttribute("aria-expanded", "true");
  el.menu.hidden = false;
  const r = anchor.getBoundingClientRect(), mw = el.menu.offsetWidth, mh = el.menu.offsetHeight;
  let top = r.bottom + 4;
  if (top + mh > window.innerHeight - 8) top = Math.max(8, r.top - mh - 4);
  el.menu.style.top = `${top}px`;
  el.menu.style.left = `${clamp(r.right - mw, 8, Math.max(8, window.innerWidth - mw - 8))}px`;
  const list = items();
  list[firstEnabled(enabledFlags(list))]?.focus();
}

/**
 * Закрывает меню. restore: вернуть фокус на открывшую кнопку — только если фокус был в меню (клик в другое место
 * фокус не отнимает) и кнопка ещё на странице, видима и доступна; иначе — на заголовок экрана. Возвращает кнопку.
 */
export function closeMenu({ restore = false } = {}) {
  const anchor = menuAnchor, ctx = menuCtx;
  const inMenu = el.menu.contains(document.activeElement);
  el.menu.hidden = true;
  menuTrack = null; menuCtx = null; menuAnchor = null;
  anchor?.removeAttribute("aria-expanded");
  if (restore && inMenu && !focusSafely(anchor) && !focusSafely(ctx?.refocus?.())) rescueFocus();
  return anchor;
}

/**
 * actions: { [пункт]: (track, queueIndex) => void } — что делает каждый пункт меню (data-act).
 * Меню закрывается до действия; если пункт выбран с клавиатуры, фокус уже на «⋯»: диалог, открытый действием,
 * вернёт фокус туда. Если действие
 * перерисовало строку вместе с «⋯», фокус — туда, куда укажет ctx.refocus; при смене экрана его переносит
 * views/shell.js (заголовок экрана). Клик вне меню и вне кнопок «⋯» тоже закрывает его.
 */
export function initMenu(actions) {
  el.menu.addEventListener("click", (e) => {
    const b = e.target.closest("[data-act]");
    const t = menuTrack, ctx = menuCtx, qi = ctx ? ctx.queueIndex : null;
    if (!b || !t) return;
    if (b.getAttribute("aria-disabled") === "true") return;   // недоступный пункт: действия нет, меню остаётся
    const kb = keyboardMode();                           // пункт выбран мышью — фокус не возвращаем (см. focus.js)
    closeMenu({ restore: kb });
    const act = actions[b.dataset.act];
    if (act) act(t, qi);
    // Действия меню очереди перерисовывают её синхронно (см. views/queue-drawer.js), поэтому проверка — сразу
    if (kb && ctx?.refocus && focusLost()) focusSafely(ctx.refocus());
  });
  el.menu.addEventListener("keydown", (e) => {
    if (el.menu.hidden) return;
    if (e.key === "Escape") {
      e.preventDefault();
      e.stopPropagation();                               // Esc закрывает только меню, не полноэкранный режим под ним
      closeMenu({ restore: true });
      return;
    }
    if (e.key === "Tab") { closeMenu({ restore: true }); return; }   // обход продолжится от «⋯»
    const list = items();
    const to = navTarget(enabledFlags(list), list.indexOf(document.activeElement), e.key);
    if (to < 0) return;
    e.preventDefault();
    list[to].focus();
  });
  document.addEventListener("click", (e) => {
    if (!el.menu.hidden && !e.target.closest("#menu") && !e.target.closest('.track__more, .tcard__more, .fq__more, [data-do="more"]')) closeMenu();
  });
}
