"""Unit test for the pure per-ticker label folding."""
from datetime import date, datetime, timedelta, timezone

from trip_ema.fundamentals.signal_labels_runner import build_ticker_labels

UTC = timezone.utc


def _weeks(n, start=100.0, step=2.0):
    m0 = datetime(2024, 1, 1, tzinfo=UTC)
    return [(m0 + timedelta(weeks=i), start + step * i) for i in range(n)]


def test_build_labels_full_horizon():
    weeks = _weeks(20)
    enr = [(555, date(2024, 1, 22), "BUY")]
    atr = {date(2024, 1, 22): 3.0}
    labels = build_ticker_labels(enr, weeks, atr, lambda d: weeks[3][0])
    assert len(labels) == 1
    lab = labels[0]
    assert lab.signal_enrichment_id == 555
    assert lab.horizon_complete is True
    assert abs(lab.fwd_ret_4w - (weeks[7][1] / weeks[3][1] - 1)) < 1e-9


def test_build_labels_partial_horizon_null_class():
    weeks = _weeks(20)
    enr = [(556, date(2024, 5, 1), "BUY")]
    labels = build_ticker_labels(enr, weeks, {}, lambda d: weeks[18][0])
    assert labels[0].horizon_complete is False
    assert labels[0].fwd_ret_13w is None


def test_signal_not_in_weekly_series_is_skipped():
    weeks = _weeks(20)
    enr = [(557, date(2030, 1, 1), "BUY")]
    labels = build_ticker_labels(
        enr, weeks, {}, lambda d: datetime(2030, 1, 7, tzinfo=UTC),
    )
    assert labels == []
