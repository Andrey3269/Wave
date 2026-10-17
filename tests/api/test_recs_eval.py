"""
Офлайн-оценка рекомендаций (S14): метрики на детерминированных фикстурах истории (tests/wavetest/recs_eval.py)
не хуже базовой линии алгоритма до S14 (tests/fixtures/recs_eval/baseline.json).

Новая базовая линия записывается только осознанно: `WAVE_RECS_EVAL_PRINT=1 python -m pytest
tests/api/test_recs_eval.py -s` печатает метрики текущего кода в формате baseline.json.
"""
from __future__ import annotations

import json
import os

import pytest

from wavetest import recs_eval
from wavetest.fakes import load_fixture

BASELINE = load_fixture("recs_eval/baseline.json")


@pytest.mark.parametrize("make", recs_eval.SCENARIOS, ids=lambda f: f.__name__.strip("_"))
def test_metrics_not_worse_than_baseline(api, make):
    sc = make()
    got = recs_eval.run(api, sc)
    if os.environ.get("WAVE_RECS_EVAL_PRINT"):
        print(f"\nRECS_EVAL {json.dumps({sc['name']: got}, ensure_ascii=False)}")
    base = BASELINE["scenarios"][sc["name"]]
    assert got["new_artist_share"] >= base["new_artist_share"], (sc["name"], got, base)
    assert got["repeat_share"] <= base["repeat_share"], (sc["name"], got, base)
    assert got["skip_share"] <= base["skip_share"], (sc["name"], got, base)
    # Метрики нельзя улучшить пустой выдачей: треков без повторов не меньше 3/4 базовой линии и не меньше половины
    # страницы, если столько было и до S14
    fresh = lambda m: round(m["count"] * (1 - m["repeat_share"]))      # noqa: E731
    assert fresh(got) >= min(fresh(base) * 3 // 4, recs_eval.LIMIT // 2), (sc["name"], got, base)
    assert got["count"] > 0


def test_baseline_file_covers_every_scenario():
    assert set(BASELINE["scenarios"]) == {f()["name"] for f in recs_eval.SCENARIOS}
