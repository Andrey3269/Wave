"""
Фасад совместимости backend/stream.py (S8): прежние имена указывают на модули streaming/*, а присваивание
(monkeypatch в тестах) меняет значение в модуле-владельце — иначе подмена резолвера «не доходила» бы до кода.
"""
from __future__ import annotations

import pytest

import stream
from streaming import config, errors, proxy, ranges, resolver


@pytest.mark.parametrize("name, owner", [
    ("_extract_sync", resolver), ("_cache", resolver), ("SOURCES", resolver), ("Resolved", resolver),
    ("STREAM_CHUNK_BYTES", proxy), ("_heads", proxy), ("_connect", proxy), ("CHUNK_SIZE", proxy),
    ("ERROR_HEADER", errors), ("StreamUnavailable", errors), ("RANGE_RE", ranges), ("YTDLP_PROXY", config),
])
def test_names_resolve_to_owner_objects(name, owner):
    assert getattr(stream, name) is getattr(owner, name)


def test_assignment_reaches_owner_module(monkeypatch):
    monkeypatch.setattr(stream, "STREAM_CHUNK_BYTES", 1234)
    assert proxy.STREAM_CHUNK_BYTES == 1234
    assert proxy._cap_range("bytes=0-") == "bytes=0-1233"          # код владельца видит подмену

    def fake(source, track_id):
        return resolver.Resolved("https://cdn.test/x", {}, 2e9)
    monkeypatch.setattr(stream, "_extract_sync", fake)
    assert resolver._extract_sync is fake


def test_monkeypatch_undo_restores_owner():
    original = proxy.STREAM_CHUNK_BYTES
    mp = pytest.MonkeyPatch()
    mp.setattr(stream, "STREAM_CHUNK_BYTES", 1)
    mp.undo()
    assert proxy.STREAM_CHUNK_BYTES == original


def test_unknown_name_raises_attribute_error():
    with pytest.raises(AttributeError):
        stream.no_such_name  # noqa: B018
    assert not hasattr(stream, "definitely_missing")


def test_production_code_does_not_import_the_facade():
    """Приложение импортирует streaming.* напрямую; фасад — только для совместимости (тесты, внешние вызовы)."""
    from pathlib import Path
    backend = Path(__file__).resolve().parents[2] / "backend"
    offenders = [p.name for p in backend.rglob("*.py")
                 if p.name != "stream.py" and any(line.strip() in ("import stream", "from stream import")
                                                  or line.strip().startswith("from stream import ")
                                                  for line in p.read_text(encoding="utf-8").splitlines())]
    assert offenders == []
