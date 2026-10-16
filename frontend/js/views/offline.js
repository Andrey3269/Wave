// Нет соединения (S10). Раньше событие offline открывало «Скачанные» и пользователь терял текущий экран.
// Теперь — сообщение в верхней панели (role=status: скринридер его прочтёт) с кнопкой «Скачанные»; переход —
// только по этой кнопке (общий обработчик data-view в main.js). Плеер, очередь и экран не трогаются.
// Сообщение скрывается, только когда браузер прислал online (или пользователь закрыл его сам); повторные
// offline/online переключают один и тот же элемент — новых узлов и обработчиков не появляется.
import { $ } from "../core/dom.js";

export function initOffline() {
  const banner = $("#offline");
  const show = () => { banner.hidden = false; };
  const hide = () => { banner.hidden = true; };
  window.addEventListener("offline", show);
  window.addEventListener("online", hide);
  $("#offline-close").addEventListener("click", hide);
  if (!navigator.onLine) show();                         // запуск без сети
}
