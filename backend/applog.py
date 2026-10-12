"""
Локальные логи: %LOCALAPPDATA%\\Wave\\logs\\wave.log (ротация: 1 МБ x 3 файла).

Секреты (cookies, токены, подписи ссылок, client_id) маскируются перед записью.
Каждый запрос поиска не логируется: access-лог uvicorn выключен.
"""
from __future__ import annotations

import logging
import re
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

import paths

_SECRET = re.compile(
    r"(?i)\b(cookie|set-cookie|authorization|access_token|token|client_id|password|lsig|sig|signature)\b(\s*[=:]\s*)[^&\s;,\"']+"
)


class RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        masked = _SECRET.sub(r"\1\2***", msg)
        if masked != msg:
            record.msg, record.args = masked, ()
        return True


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
