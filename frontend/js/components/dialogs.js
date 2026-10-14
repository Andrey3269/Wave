// Диалоги (вместо prompt/confirm браузера): <dialog> из index.html, результат — Promise.
import { mk, icon } from "../core/dom.js";
import { api } from "../core/api.js";
import { el } from "../core/elements.js";

/** Диалог ввода текста: Promise со строкой или null. */
export function askText(title, value = "", okText = "Готово") {
  return new Promise((resolve) => {
    el.dlgTitle.textContent = title;
    el.dlgInput.value = value;
    el.dlgOk.textContent = okText;
    const done = () => {
      el.dlg.removeEventListener("close", done);
      resolve(el.dlg.returnValue === "ok" ? (el.dlgInput.value.trim() || null) : null);
    };
    el.dlg.addEventListener("close", done);
    el.dlg.returnValue = "cancel";
    el.dlg.showModal();
    el.dlgInput.select();
  });
}
/** Подтверждение: Promise<boolean>. */
export function askConfirm({ title, text = "", ok = "Удалить" }) {
  return new Promise((resolve) => {
    el.dcTitle.textContent = title;
    el.dcText.textContent = text;
    el.dcOk.textContent = ok;
    const done = () => { el.dlgConfirm.removeEventListener("close", done); resolve(el.dlgConfirm.returnValue === "ok"); };
    el.dlgConfirm.addEventListener("close", done);
    el.dlgConfirm.returnValue = "cancel";
    el.dlgConfirm.showModal();
  });
}
/** Выбор плейлиста: Promise с id плейлиста, "new" или null. */
export function pickPlaylist() {
  return new Promise((resolve) => {
    const dlg = el.dlgPick;
    el.pickList.replaceChildren(mk("p", "pick__hint", "Загружаем…"));
    const done = () => { dlg.removeEventListener("close", done); resolve(dlg.returnValue && dlg.returnValue !== "cancel" ? dlg.returnValue : null); };
    dlg.addEventListener("close", done);
    dlg.returnValue = "cancel";
    dlg.showModal();
    api("/api/playlists").then((d) => d.items).catch(() => []).then((items) => {
      const item = (text, id, ico) => {
        const b = mk("button", "pick__item");
        b.type = "button"; b.dataset.pl = id;
        b.append(icon(ico), mk("span", null, text));
        return b;
      };
      el.pickList.replaceChildren(item("Новый плейлист…", "new", "i-plus"), ...items.map((p) => item(`${p.name} (${p.count})`, String(p.id), "i-playlists")));
    });
  });
}
/** Клик по плейлисту в списке выбора закрывает диалог с его id. */
export function initPickPlaylist() {
  el.pickList.addEventListener("click", (e) => {
    const b = e.target.closest("[data-pl]");
    if (b) el.dlgPick.close(b.dataset.pl);
  });
}
