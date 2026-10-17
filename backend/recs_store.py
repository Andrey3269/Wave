"""
Управление рекомендациями пользователем (S14): исключения и сброс сигналов персонализации.

* Исключения — таблица `rec_exclusions` (схема v2, persistence/migrations.py): конкретный трек (`kind = "track"`,
  `value = "источник:id"`) или исполнитель (`kind = "artist"`, `value` — нормализованное имя, одинаковое для YouTube
  Music и SoundCloud). Рекомендации никогда не предлагают исключённое; история, избранное, плейлисты и загрузки от
  исключения не меняются.
* Сброс сигналов — отметка времени `recs_reset_at` в служебной строке таблицы settings (в /api/settings её нет, PUT
  /api/settings её не принимает). Рекомендации перестают учитывать прослушивания, пропуски и избранное, записанные до
  отметки; сами записи не удаляются, поэтому сброс можно отменить (отметка удаляется). Защита от повторов (недавно
  игравшее не предлагается) и исключения продолжают действовать.
"""
from __future__ import annotations

import re
import time

from persistence import db as store

EXCLUSIONS_MAX = 5000            # предел числа исключений: таблица не растёт без ограничений
LABEL_MAX = 300
RESET_KEY = "recs_reset_at"
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class TooMany(Exception):
    """Достигнут предел EXCLUSIONS_MAX."""


def norm(s: str | None) -> str:
    """Нормализованное имя: регистр и знаки препинания не различаются (так же сравнивает recs.py)."""
    return re.sub(r"\W+", " ", (s or "").lower()).strip()


def clean_label(s: str | None) -> str:
    return _CONTROL.sub(" ", s or "").strip()[:LABEL_MAX]


def _row(r) -> dict:
    return {"kind": r["kind"], "value": r["value"], "label": r["label"], "created_at": r["created_at"]}


def exclusions() -> list[dict]:
    return [_row(r) for r in store.query("SELECT kind, value, label, created_at FROM rec_exclusions ORDER BY created_at DESC")]


def excluded() -> tuple[set[str], set[str]]:
    """(ключи исключённых треков, нормализованные имена исключённых исполнителей)."""
    tracks, artists = set(), set()
    for r in store.query("SELECT kind, value FROM rec_exclusions"):
        (tracks if r["kind"] == "track" else artists).add(r["value"])
    return tracks, artists


def add(kind: str, value: str, label: str) -> dict:
    """Добавляет исключение (повтор — только обновляет подпись, дата остаётся первой). Возвращает запись."""
    with store.transaction() as db:
        exists = db.execute("SELECT 1 FROM rec_exclusions WHERE kind = ? AND value = ?", (kind, value)).fetchone()
        if not exists and db.execute("SELECT COUNT(*) FROM rec_exclusions").fetchone()[0] >= EXCLUSIONS_MAX:
            raise TooMany
        db.execute("INSERT INTO rec_exclusions (kind, value, label, created_at) VALUES (?, ?, ?, ?)"
                   " ON CONFLICT (kind, value) DO UPDATE SET label = excluded.label",
                   (kind, value, clean_label(label) or value, time.time()))
        row = db.execute("SELECT kind, value, label, created_at FROM rec_exclusions WHERE kind = ? AND value = ?",
                         (kind, value)).fetchone()
    return {"kind": row[0], "value": row[1], "label": row[2], "created_at": row[3]}


def remove(kind: str, value: str) -> None:
    store.query("DELETE FROM rec_exclusions WHERE kind = ? AND value = ?", (kind, value))


def reset_at() -> float | None:
    rows = store.query("SELECT value FROM settings WHERE key = ?", (RESET_KEY,))
    try:
        return float(rows[0]["value"]) if rows else None
    except (TypeError, ValueError):
        return None                  # испорченная строка: сброса нет (сигналы учитываются, как без отметки)


def set_reset(ts: float | None = None) -> float:
    ts = time.time() if ts is None else ts
    store.query("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (RESET_KEY, repr(ts)))
    return ts


def clear_reset() -> None:
    store.query("DELETE FROM settings WHERE key = ?", (RESET_KEY,))
