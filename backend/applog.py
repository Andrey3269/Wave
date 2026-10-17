"""
Локальные логи: %LOCALAPPDATA%\\Wave\\logs\\wave.log (ротация: 1 МБ x 3 файла).

Секреты (cookies, токены, подписи ссылок, client_id), ссылки и IP из googlevideo, IP-адреса и логины в ссылках
маскируются перед записью — в сообщениях и в трейсбеках (S13, находка L1; функция redact).
Каждый запрос поиска не логируется: access-лог uvicorn выключен.
"""
from __future__ import annotations

import ipaddress
import logging
import re
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

import paths

# Секреты вида «ключ=значение» / «ключ: значение» / "ключ": "значение" вне ссылок. Группы 1 и 2 — ключ и
# разделитель (их оставляем); diagnostics._sanitize пользуется тем же выражением
_SECRET = re.compile(
    r"(?i)\b(cookie|set-cookie|authorization|proxy-authorization|access_token|refresh_token|id_token|token|client_id"
    r"|client_secret|secret|password|passwd|api_?key|lsig|sig|signature|ip|pot|po_token|visitor_data|session_?id|sid)"
    r"\b(\"?\s*[=:]\s*\"?)[^&\s;,\"']+"
)
# Заголовок Cookie / Set-Cookie — до конца строки: в нём несколько пар имя=значение (SID=…; HSID=…)
_COOKIE_LINE = re.compile(r"(?i)\b((?:set-)?cookie)(\"?\s*[=:]\s*)[^\r\n]+")
_AUTH_SCHEME = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]+")
# Ссылки googlevideo целиком: в имени узла CDN (rr3---sn-…) — расположение провайдера пользователя, в пути и
# параметрах — его IP (ip=…), подписи (sig, lsig, n) и срок действия
_GOOGLEVIDEO_URL = re.compile(r"(?i)\bhttps?://[^\s/'\"<>]*googlevideo\.com[^\s'\"<>]*")
_GOOGLEVIDEO_HOST = re.compile(r"(?i)\b[\w.-]*\.googlevideo\.com\b")
_URL = re.compile(r"(?i)\b[a-z][a-z0-9+.-]{1,15}://[^\s'\"<>]+")
_USERINFO = re.compile(r"^([a-z][a-z0-9+.-]*://)[^/@\s]+@", re.IGNORECASE)
_QUERY_VALUE = re.compile(r"([?&;][^=&#\s]+=)[^&#\s]*")
_OCTET = r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)"
_IPV4 = re.compile(rf"(?<![\d.]){_OCTET}(?:\.{_OCTET}){{3}}(?![\d.])")
_IPV6_CANDIDATE = re.compile(r"(?<![\w:.])[0-9A-Fa-f:]{2,39}(?![\w:])")


def _mask_url(m: re.Match) -> str:
    url = _USERINFO.sub(r"\1***@", m.group(0))           # логин и пароль прокси (YTDLP_PROXY=http://user:pass@…)
    return _QUERY_VALUE.sub(r"\1***", url)               # значения параметров: подписи, токены, ip, expire


def _mask_ipv4(m: re.Match) -> str:
    ip = m.group(0)
    return ip if ip.startswith("127.") or ip == "0.0.0.0" else "<ip>"


def _mask_ipv6(m: re.Match) -> str:
    text = m.group(0)
    if text.count(":") < 2:
        return text
    try:
        addr = ipaddress.IPv6Address(text)
    except ValueError:                                  # «14:27:33» и прочее — не адрес
        return text
    return text if addr.is_loopback or addr.is_unspecified else "<ip>"


def redact(text: str) -> str:
    """
    Маскирует секреты в строке журнала (S13, находка L1): ссылки googlevideo целиком, логин и пароль в ссылках,
    значения параметров любых ссылок (имена параметров остаются — по ним видно, что было в запросе), секреты
    «ключ=значение», заголовки Authorization (Bearer/Basic), IP-адреса (кроме 127.x, ::1 и 0.0.0.0).
    """
    if not text:
        return text
    text = _GOOGLEVIDEO_URL.sub("https://***.googlevideo.com/***", text)
    text = _GOOGLEVIDEO_HOST.sub("***.googlevideo.com", text)
    text = _URL.sub(_mask_url, text)
    text = _COOKIE_LINE.sub(r"\1\2***", text)
    text = _AUTH_SCHEME.sub(r"\1 ***", text)
    text = _SECRET.sub(r"\1\2***", text)
    text = _IPV4.sub(_mask_ipv4, text)
    return _IPV6_CANDIDATE.sub(_mask_ipv6, text)


class RedactFilter(logging.Filter):
    """
    Фильтр обработчика журнала: маскирует сообщение, текст исключения (трейсбек, находка L1) и stack_info.
    Трейсбек форматируется здесь один раз и кладётся в record.exc_text — его берёт любой Formatter вместо
    повторного форматирования исключения без маскирования.
    """

    _formatter = logging.Formatter()

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            msg = None
        if msg is not None:
            masked = redact(msg)
            if masked != msg:
                record.msg, record.args = masked, ()
        try:
            if record.exc_info and not record.exc_text:
                record.exc_text = self._formatter.formatException(record.exc_info)
            if record.exc_text:
                record.exc_text = redact(record.exc_text)
            if record.stack_info:
                record.stack_info = redact(record.stack_info)
        except Exception:                               # журнал не должен падать из-за маскирования
            record.exc_text = "(трейсбек не записан: ошибка маскирования)"
        return True


def protect(handlers) -> None:
    """Добавляет RedactFilter обработчикам, у которых его нет (консоль при запуске `uvicorn main:app`)."""
    for h in handlers:
        if not any(isinstance(f, RedactFilter) for f in h.filters):
            h.addFilter(RedactFilter())


def setup(level: int = logging.INFO) -> Path:
    """Включает запись в файл (вызывается лаунчером один раз). Возвращает путь к логу."""
    log_dir = paths.data_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "wave.log"

    handler = RotatingFileHandler(log_file, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(RedactFilter())

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers[:] = [handler]
    for noisy in ("httpx", "httpcore", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    log = logging.getLogger("wave")
    sys.excepthook = lambda t, v, tb: log.critical("Необработанная ошибка", exc_info=(t, v, tb))
    threading.excepthook = lambda a: log.critical("Ошибка в потоке %s", a.thread.name if a.thread else "?",
                                                  exc_info=(a.exc_type, a.exc_value, a.exc_traceback))
    return log_file
