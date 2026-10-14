"""
Понятные ошибки стриминга (находка L6, тексты S2d). Пользователь видит короткий текст без технических
подробностей: ни текста исключения, ни URL, ни путей и значений из настроек. Подробности — только в журнале.
Тип ошибки уходит в заголовке X-Wave-Error (для тестов и интерфейса).
"""
from __future__ import annotations

from fastapi import HTTPException
from yt_dlp.utils import DownloadError

import jsruntime
from streaming import config

ERROR_HEADER = "X-Wave-Error"


class StreamUnavailable(Exception):
    """У трека нет подходящего прямого потока."""


_AGE = ("confirm your age", "age-restricted", "age restricted", "inappropriate for some users")
_BOT = ("not a bot", "not a robot", "sign in to confirm", "--cookies", "cookies-from-browser")
_RATE = ("http error 429", "too many requests")
_PROXY = ("proxy", "socks")
_NETWORK = (
    "urlopen error", "name resolution", "name or service not known", "nodename nor servname", "getaddrinfo",
    "failed to resolve", "errno 11001", "errno 11002", "network is unreachable", "no route to host",
    "connection refused", "connection reset", "connection aborted", "remote end closed", "timed out", "timeout",
    "errno 10051", "errno 10054", "errno 10060", "errno 10061", "errno 10065", "ssl:", "eof occurred",
)
_FORMAT = ("requested format is not available", "no video formats", "nsig", "signature extraction",
           "unable to extract", "javascript runtime", "js runtime", "player response")
_GONE = ("video unavailable", "this video is unavailable", "private video", "is private", "not available",
         "removed", "has been terminated", "does not exist", "unavailable", "private", "404")


def _label(source: str) -> str:
    from streaming import resolver            # отложенный импорт: resolver сам импортирует этот модуль
    return resolver.SOURCES[source].label


def _err(status: int, kind: str, detail: str) -> HTTPException:
    return HTTPException(status, detail, headers={ERROR_HEADER: kind})


def _resolve_error(source: str, exc: DownloadError) -> HTTPException:
    """Сообщение yt-dlp -> понятная ошибка. Про cookies говорим только когда YouTube действительно просит вход."""
    label = _label(source)
    msg = str(exc).lower()
    if any(s in msg for s in _AGE):
        return _err(403, "age_restricted", "Трек ограничен YouTube по возрасту и недоступен без входа в аккаунт.")
    if any(s in msg for s in _BOT):
        hint = ("Возможно, файл cookies из YTDLP_COOKIES устарел." if config.YTDLP_COOKIES
                else "Если повтор не помогает, можно указать файл cookies в переменной YTDLP_COOKIES.")
        return _err(503, "bot_check", f"{label} просит подтвердить, что запрос не от бота. Повторите позже. {hint}")
    if any(s in msg for s in _RATE):
        return _err(503, "rate_limited", f"{label} временно ограничил число запросов. Повторите через несколько минут.")
    if config.YTDLP_PROXY and any(s in msg for s in _PROXY):
        return _err(502, "proxy", "Не удалось подключиться через прокси из настройки YTDLP_PROXY. Проверьте прокси.")
    if any(s in msg for s in _NETWORK):
        return _err(503, "network", f"Нет связи с {label}. Проверьте подключение к интернету.")
    if any(s in msg for s in _FORMAT):
        return _err(502, "extractor", f"Не удалось получить аудиопоток: {label} изменил формат ответа или компонент "
                                      "yt-dlp устарел. Обычно помогает обновление приложения.")
    if any(s in msg for s in _GONE):
        return _err(404, "unavailable", "Трек недоступен")
    return _err(502, "resolve_failed", f"Не удалось получить аудиопоток от {label}. Попробуйте позже.")


# Ошибки, за которыми может стоять JS-runtime. Возраст, проверка на бота, лимит запросов, сеть и
# «трек недоступен» — настоящие причины, их не подменяем (S3, требование 13).
_RUNTIME_KINDS = {"extractor", "resolve_failed", "internal"}


def _runtime_error(source: str, err: HTTPException, js_issue: str | None) -> HTTPException:
    """
    YouTube без JS-runtime работает в урезанном режиме и падает с общей ошибкой формата. Если runtime
    недоступен — говорим об этом (а не «трек недоступен»); если доступен, но yt-dlp сообщил о сбое
    решения JS-задачи — тоже отдельный текст. Подробности — в журнале (jsruntime.YtDlpLogger).
    """
    if source != "yt" or err.headers.get(ERROR_HEADER) not in _RUNTIME_KINDS:
        return err
    st = jsruntime.status()
    if not st.ok:
        return _err(503, "js_runtime", jsruntime.user_message(st))
    if js_issue:
        return _err(502, "js_runtime_failed", "Компонент воспроизведения YouTube не смог обработать ответ YouTube. "
                                              "Обычно помогает обновление приложения; подробности — в журнале.")
    return err
