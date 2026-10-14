"""
Настройки окружения стриминга. Читаются в момент вызова (config.YTDLP_PROXY), поэтому тесты и диагностика
могут менять их на лету, а резолвер, тексты ошибок и HTTP-клиент видят одно и то же значение.
"""
from __future__ import annotations

import os

# Опционально: куки (если YouTube просит подтвердить, что вы не бот) и прокси.
# ВАЖНО: если резолвим через прокси, то и качать надо через него же.
YTDLP_COOKIES = os.getenv("YTDLP_COOKIES")   # путь к cookies.txt
YTDLP_PROXY = os.getenv("YTDLP_PROXY")       # например socks5://user:pass@host:1080
