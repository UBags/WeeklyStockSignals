"""Unit tests for the cross-entity enrichment layer (spec §11)."""
import math
from datetime import date, datetime, timedelta, timezone

from trip_ema.fundamentals.signal_cross_entity import (
    compute_rs_ema_by_week,
    index_by_week,
)

UTC = timezone.utc


def _weeks(n):
    m0 = datetime(2024, 1, 1, tzinfo=UTC)
    return [m0 + timedelta(weeks=i) for i in range(n)]


def test_rs_ema_rebases_to_one_and_tracks_outperformance():
    M = _weeks(20)
    closes = [(M[i], 100 * 1.02 ** i) for i in range(20)]
    index = {M[i]: 500 * 1.01 ** i for i in range(20)}
    r = compute_rs_ema_by_week(closes, index)
    assert abs(r[M[0]] - 1.0) < 1e-9
    assert r[M[-1]] > 1.0


def test_rs_ema_underperformance_below_one():
    M = _weeks(20)
    closes = [(M[i], 100.0) for i in range(20)]
    index = {M[i]: 500 * 1.01 ** i for i in range(20)}
    r = compute_rs_ema_by_week(closes, index)
    assert r[M[-1]] < 1.0


def test_rs_ema_missing_index_week_is_nan():
    M = _weeks(6)
    closes = [(M[i], 100.0 + i) for i in range(6)]
    index = {M[i]: 500.0 for i in range(6) if i != 3}
    r = compute_rs_ema_by_week(closes, index)
    assert math.isnan(r[M[3]])
    assert not math.isnan(r[M[4]])


def test_index_by_week_takes_last_value_per_week():
    M = _weeks(2)

    def wof(d):
        return M[0] if d < date(2024, 1, 8) else M[1]

    daily = [(date(2024, 1, 1), 10.0), (date(2024, 1, 5), 12.0),
             (date(2024, 1, 8), 20.0)]
    out = index_by_week(daily, wof)
    assert out[M[0]] == 12.0
    assert out[M[1]] == 20.0
