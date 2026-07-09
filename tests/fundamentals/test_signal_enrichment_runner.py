"""Unit test for signal-enrichment input folding (spec 19 runner)."""
from datetime import date, datetime, timezone
from types import SimpleNamespace as NS

from trip_ema.fundamentals.signal_enrichment_runner import _fold_inputs

UTC = timezone.utc


def _state(d, monday, med):
    return NS(date=d, bar_timestamp=monday, med_ema=med,
              long_ema=med - 1, longest_ema=med - 2, atr=1.2)


def test_fold_inputs_converged_is_max_date_and_signals_dedup():
    m1 = datetime(2024, 1, 1, tzinfo=UTC)
    m2 = datetime(2024, 1, 8, tzinfo=UTC)
    states = [                                    # deliberately unordered
        _state(date(2024, 1, 2), m1, 11.0),
        _state(date(2024, 1, 9), m2, 21.0),
        _state(date(2024, 1, 1), m1, 10.0),
        _state(date(2024, 1, 3), m1, 12.0),       # max date, week 1
        _state(date(2024, 1, 8), m2, 20.0),
    ]
    signals = [
        NS(signal_date=date(2024, 1, 3), signal_type="BUY"),
        NS(signal_date=date(2024, 1, 3), signal_type="BUY"),
        NS(signal_date=date(2024, 1, 3), signal_type="EXIT_FROM_SELL"),
        NS(signal_date=date(2024, 1, 9), signal_type="SELL"),
        NS(signal_date=None, signal_type="BUY"),
    ]
    sbd, cbm, sbday = _fold_inputs(states, signals)

    assert set(sbd) == {date(2024, 1, 1), date(2024, 1, 2),
                        date(2024, 1, 3), date(2024, 1, 8),
                        date(2024, 1, 9)}
    assert sbd[date(2024, 1, 3)] == (12.0, 11.0, 10.0, 1.2)
    # converged = the week's last (max-date) row, not first-seen
    assert cbm[m1] == (12.0, 11.0, 10.0, 1.2)
    assert cbm[m2] == (21.0, 20.0, 19.0, 1.2)
    assert set(cbm) == {m1, m2}
    # grouped + deduped by (date, type); None dropped
    assert sorted(sbday[date(2024, 1, 3)]) == ["BUY", "EXIT_FROM_SELL"]
    assert sbday[date(2024, 1, 9)] == ["SELL"]
    assert None not in sbday and len(sbday) == 2
