"""
Единая модель трека и карточки каталога (S7, TARGET_ARCHITECTURE §1).

Здесь только форма данных, общая для всех источников. Приведение ответов YouTube Music и SoundCloud
к этой форме — в адаптерах (backend/providers/*). Модель `Track` перенесена из main.py без изменений:
main.Track остаётся тем же классом (main импортирует его отсюда), схема OpenAPI и поля ответа те же.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class Track(BaseModel):
    id: str                                  # ID внутри источника (videoId и т.п.)
    source: Literal["yt", "sc"]        # источник
    title: str
    artist: str                              # для отображения: "A, B"
    artists: list[str]
    album: str | None = None
    duration: int | None = None              # секунды
    cover: str | None = None                 # URL обложки (крупная версия)
    explicit: bool = False
    stream_url: str                          # относительный URL нашего прокси
    artist_id: str | None = None             # id первого артиста (страница артиста)
    artist_ids: list[str | None] = []        # id КАЖДОГО артиста, в том же порядке, что и artists
    plays: int | None = None                 # прослушивания (есть только у SoundCloud)
    released: str | None = None              # дата выхода ISO (есть только у SoundCloud)
    url: str | None = None                   # публичная ссылка на трек (для «Поделиться»)


# Карточки каталога (как раньше в catalog.py): artist {id, source, kind, name, cover, subtitle}
# и album {id, source, kind, title, artist, year, cover, track_count, playlist_id}.
def artist_card(source, id_, name, cover, subtitle=None) -> dict:
    return {"id": str(id_), "source": source, "kind": "artist", "name": name, "cover": cover, "subtitle": subtitle}


def album_card(source, id_, title, artist, year, cover, count=None, playlist_id=None) -> dict:
    return {"id": str(id_), "source": source, "kind": "album", "title": title or "Без названия",
            "artist": artist, "year": (year or "")[:4] or None, "cover": cover, "track_count": count,
            "playlist_id": playlist_id}
