// «Поделиться»: ссылка на трек у источника (или название, если публичной ссылки нет) — в буфер обмена.
import { mk } from "../core/dom.js";
import { toast } from "./toast.js";

const trackUrl = (t) => t.url || (t.source === "yt" ? `https://music.youtube.com/watch?v=${t.id}` : null);
async function copyText(text) {
  try { await navigator.clipboard.writeText(text); return true; } catch { /* пробуем запасной путь */ }
  try {
    const ta = mk("textarea");
    ta.value = text; ta.style.cssText = "position:fixed;opacity:0;top:0;left:0";
    document.body.append(ta); ta.select();
    const ok = document.execCommand("copy");
    ta.remove();
    return ok;
  } catch { return false; }
}
export async function shareTrack(t) {
  const url = trackUrl(t);
  const ok = await copyText(url || `${t.artist} — ${t.title}`);
  if (!ok) toast("Не удалось скопировать");
  else toast(url ? "Ссылка на трек скопирована" : "У этого источника нет публичной ссылки: скопировано название");
}
