"""
Ограниченный кэш в памяти: LRU + TTL (S7, находка M9). Без внешних зависимостей.

Раньше кэши каталога и текстов были обычными словарями без лимита: число записей росло с каждым новым
артистом и текстом песни, пока приложение открыто. Теперь у каждого кэша есть:
  * maxsize — сколько записей хранится максимум; при переполнении вытесняется давно не использованная;
  * ttl — сколько секунд запись считается свежей; устаревшая не отдаётся и удаляется при обращении,
    а при переполнении устаревшие удаляются раньше живых.

TTL читается в момент обращения (атрибут `ttl` можно поменять, например, в тестах — это действует и на уже
сохранённые записи). Кэш используется из event loop приложения; блокировка всё равно есть, потому что
очистка из настроек и тесты могут обращаться из других потоков, а стоит она немного.
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Callable, Generic, Hashable, Iterator, TypeVar

V = TypeVar("V")
_MISSING = object()


class TTLCache(Generic[V]):
    def __init__(self, maxsize: int, ttl: float, clock: Callable[[], float] = time.monotonic):
        if maxsize < 1:
            raise ValueError("maxsize должен быть не меньше 1")
        self.maxsize = maxsize
        self.ttl = ttl
        self._clock = clock
        self._data: OrderedDict[Hashable, tuple[float, V]] = OrderedDict()   # ключ -> (время записи, значение)
        self._lock = threading.Lock()

    def _fresh(self, stored_at: float, now: float) -> bool:
        return now - stored_at < self.ttl

    def get(self, key: Hashable, default=None):
        """Свежее значение (и отметка «недавно использовано») или default. Устаревшая запись удаляется."""
        with self._lock:
            hit = self._data.get(key, _MISSING)
            if hit is _MISSING:
                return default
            stored_at, value = hit
            if not self._fresh(stored_at, self._clock()):
                del self._data[key]
                return default
            self._data.move_to_end(key)
            return value

    def set(self, key: Hashable, value: V) -> None:
        with self._lock:
            now = self._clock()
            self._data[key] = (now, value)
            self._data.move_to_end(key)
            if len(self._data) > self.maxsize:
                # Сначала выбрасываем устаревшие, потом давно не использованные (начало OrderedDict)
                for k in [k for k, (t, _) in self._data.items() if not self._fresh(t, now)]:
                    del self._data[k]
                while len(self._data) > self.maxsize:
                    self._data.popitem(last=False)

    def pop(self, key: Hashable, default=None):
        with self._lock:
            hit = self._data.pop(key, _MISSING)
            return default if hit is _MISSING else hit[1]

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    # Словарный интерфейс для существующего кода и тестов (`key in cache`, `cache[key] = v`, `len`, `bool`)
    def __setitem__(self, key: Hashable, value: V) -> None:
        self.set(key, value)

    def __getitem__(self, key: Hashable) -> V:
        value = self.get(key, _MISSING)
        if value is _MISSING:
            raise KeyError(key)
        return value

    def __contains__(self, key: object) -> bool:
        return self.get(key, _MISSING) is not _MISSING

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)

    def __bool__(self) -> bool:
        return len(self) > 0

    def __iter__(self) -> Iterator[Hashable]:
        with self._lock:
            return iter(list(self._data))

    def stats(self) -> dict:
        return {"items": len(self), "maxsize": self.maxsize, "ttl": self.ttl}
