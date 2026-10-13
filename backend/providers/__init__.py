"""
Музыкальные источники Wave (S7, TARGET_ARCHITECTURE §1 и §5).

  base.py           — общий слой: ProviderError, Capabilities, cooldown, ограниченный пул на источник;
  cache.py          — ограниченный кэш LRU+TTL;
  youtube_music.py  — адаптер YouTube Music (ytmusicapi);
  soundcloud.py     — адаптер SoundCloud (API v2 через yt-dlp; находка M10 изолирована, не устранена).

Остальной backend обращается к источникам только через адаптеры из REGISTRY. Аудиопоток (yt-dlp и прокси)
остаётся в stream.py до этапа S8. Импорты статические — так их находит PyInstaller (packaging/wave.spec).
"""
from __future__ import annotations

from providers import soundcloud, youtube_music
from providers.base import Provider, ProviderError

yt: youtube_music.YouTubeMusicProvider = youtube_music.PROVIDER
sc: soundcloud.SoundCloudProvider = soundcloud.PROVIDER

# Порядок важен: в нём источники чередуются в выдаче поиска (YT, SC, YT, SC …)
REGISTRY: dict[str, Provider] = {"yt": yt, "sc": sc}

__all__ = ["REGISTRY", "Provider", "ProviderError", "get", "reset", "sc", "shutdown_pending", "yt"]


def get(source: str) -> Provider:
    """Адаптер по коду источника ("yt" | "sc"). KeyError для неизвестного — проверяйте источник заранее."""
    return REGISTRY[source]


def shutdown_pending() -> None:
    """Остановка backend: отменить запросы, ждущие в очередях пулов (работающие потоки доживают сами)."""
    for p in REGISTRY.values():
        p.shutdown_pending()


def reset() -> None:
    """Чистое состояние всех адаптеров (тесты): без пауз, новые пулы."""
    for p in REGISTRY.values():
        p.reset()
