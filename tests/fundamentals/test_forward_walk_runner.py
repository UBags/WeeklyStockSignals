"""Tests for the per-ticker forward-walk runner core (spec §4).

These cover the PURE pairing + excess-return logic — the part where the
historical bar_timestamp trap once dropped 79.5% of signals. The thin I/O
loaders (SQL reads) are exercised by the Phase-4 integration tests against a
live DB. Everything here keys on signal_date and trading dates.
"""
from __future__ import annotations

from datetime import date

from trip_ema.domain.forward_walk import (
    DATA_END,
    LONG,
    NEXT_SIGNAL,
    SHORT,
    WalkBar,
)
from trip_ema.fundamentals.forward_walk_runner import (
    _DatedBar,
    _make_censor_predicate,
    _Signal,
    asof_backward,
    compute_ticker_walks,
    series_total_return,
)


def D(m: int, d: int) -> date:
    return date(2024, m, d)


def bar(dt: date, o: float, h: float, low: float, c: float) -> _DatedBar:
    return _DatedBar(dt, WalkBar(o, h, low, c))


def sig(seid: int, dt: date, typ: str, atr: float | None = 2.0) -> _Signal:
    return _Signal(seid, dt, typ, atr)


# five consecutive trading days
BARS = [
    bar(D(1, 1), 100, 101, 99, 100),
    bar(D(1, 2), 100, 106, 100, 105),
    bar(D(1, 3), 105, 107, 104, 106),
    bar(D(1, 4), 106, 108, 103, 104),
    bar(D(1, 5), 104, 105, 100, 101),
]
MKT = [(D(1, 2), 200.0), (D(1, 3), 206.0)]   # +3% over [01-02, 01-03]


# --------------------------------------------------------------------------- #
# asof-backward + total return
# --------------------------------------------------------------------------- #
def test_asof_backward():
    s = [(D(1, 2), 10.0), (D(1, 4), 20.0)]
    assert asof_backward(s, D(1, 1)) is None      # before first
    assert asof_backward(s, D(1, 2)) == 10.0       # exact
    assert asof_backward(s, D(1, 3)) == 10.0        # gap → prior
    assert asof_backward(s, D(1, 4)) == 20.0
    assert asof_backward(s, D(1, 5)) == 20.0        # after last → last
    assert asof_backward([], D(1, 1)) is None


def test_series_total_return():
    s = [(D(1, 2), 100.0), (D(1, 4), 110.0)]
    assert abs(series_total_return(s, D(1, 2), D(1, 4)) - 0.10) < 1e-12
    assert series_total_return(s, D(1, 1), D(1, 4)) is None       # v0 missing
    assert series_total_return([(D(1, 2), 0.0)], D(1, 2), D(1, 2)) is None


# --------------------------------------------------------------------------- #
# pairing
# --------------------------------------------------------------------------- #
def test_consecutive_pairing_and_terminators():
    signals = [
        sig(10, D(1, 1), "BUY"),
        sig(20, D(1, 3), "EXIT_FROM_BUY"),   # terminator only, no row
        sig(30, D(1, 4), "SELL"),            # last → DATA_END
    ]
    rows = compute_ticker_walks(signals, BARS, market_series=MKT,
                                industry_series=None)
    assert len(rows) == 2                     # EXIT_FROM_BUY produced no row

    long_row = rows[0]
    assert long_row.signal_enrichment_id == 10 and long_row.side == LONG
    assert long_row.entry_date == D(1, 2)     # first bar STRICTLY after 01-01
    assert long_row.exit_date == D(1, 3)      # window end N = 01-03 inclusive
    assert long_row.exit_reason == NEXT_SIGNAL
    assert long_row.window_censored is False
    assert long_row.bars_in_window == 2
    assert long_row.result is not None
    assert long_row.result.entry_open == 100  # d2 open, not the signal bar

    sell_row = rows[1]
    assert sell_row.side == SHORT
    assert sell_row.exit_reason == DATA_END
    assert sell_row.window_censored is True    # right-censored
    assert sell_row.result is not None         # ...but still walked (real trade)


def test_exit_from_sell_is_also_terminator():
    signals = [
        sig(10, D(1, 1), "SELL"),
        sig(20, D(1, 3), "EXIT_FROM_SELL"),
    ]
    rows = compute_ticker_walks(signals, BARS, market_series=MKT,
                                industry_series=None)
    assert len(rows) == 1 and rows[0].side == SHORT
    assert rows[0].exit_reason == NEXT_SIGNAL and rows[0].exit_date == D(1, 3)


def test_signal_at_data_end_is_skipped():
    rows = compute_ticker_walks([sig(10, D(1, 5), "BUY")], BARS,
                                market_series=MKT, industry_series=None)
    assert rows == []                          # no bar strictly after 01-05


def test_no_bars_returns_empty():
    assert compute_ticker_walks([sig(10, D(1, 1), "BUY")], [],
                                market_series=MKT, industry_series=None) == []


# --------------------------------------------------------------------------- #
# excess returns + industry fallback
# --------------------------------------------------------------------------- #
def test_industry_falls_back_to_market_when_absent():
    rows = compute_ticker_walks([sig(10, D(1, 1), "BUY"), sig(20, D(1, 3), "SELL")],
                                BARS, market_series=MKT, industry_series=None)
    r = rows[0].result
    # realized long over [d2,d3] = 106/100 - 1 = 0.06; market +3%
    assert abs(r.realized_ret - 0.06) < 1e-12
    assert abs(r.excess_ret_market - 0.03) < 1e-12
    # no industry coverage → industry excess == market excess
    assert r.excess_ret_industry == r.excess_ret_market


def test_industry_series_used_when_covered():
    ind = [(D(1, 2), 50.0), (D(1, 3), 49.0)]   # -2% over the window
    rows = compute_ticker_walks([sig(10, D(1, 1), "BUY"), sig(20, D(1, 3), "SELL")],
                                BARS, market_series=MKT, industry_series=ind)
    r = rows[0].result
    assert abs(r.excess_ret_industry - (0.06 - (-0.02))) < 1e-12   # 0.08
    assert abs(r.excess_ret_market - 0.03) < 1e-12
    assert r.excess_ret_industry != r.excess_ret_market


def test_industry_fallback_when_series_present_but_uncovered():
    # industry series exists but starts after the window → no coverage → market
    ind = [(D(2, 1), 50.0)]
    rows = compute_ticker_walks([sig(10, D(1, 1), "BUY"), sig(20, D(1, 3), "SELL")],
                                BARS, market_series=MKT, industry_series=ind)
    r = rows[0].result
    assert r.excess_ret_industry == r.excess_ret_market


# --------------------------------------------------------------------------- #
# data-quality censoring
# --------------------------------------------------------------------------- #
def test_quality_censored_suppresses_result_but_keeps_bookkeeping():
    rows = compute_ticker_walks(
        [sig(10, D(1, 1), "BUY"), sig(20, D(1, 3), "SELL")],
        BARS, market_series=MKT, industry_series=None,
        censored=lambda _e, _x: True,
    )
    long_row = rows[0]
    assert long_row.result is None             # no fabricated excursions
    assert long_row.window_censored is True
    assert long_row.bars_in_window == 2        # bookkeeping still recorded
    assert long_row.side == LONG and long_row.entry_date == D(1, 2)
    assert long_row.forward_walk_done is True


def test_atr20_flows_from_signal_into_result():
    rows = compute_ticker_walks([sig(10, D(1, 1), "BUY", atr=3.0),
                                 sig(20, D(1, 3), "SELL")],
                                BARS, market_series=MKT, industry_series=None)
    r = rows[0].result
    # mfe over [d2,d3] long: max((106-100)/100,(107-100)/100)=0.07 → atr units
    assert r.mfe_atr20 is not None
    assert abs(r.mfe_atr20 - (r.mfe_pct * 100 / 3.0)) < 1e-9


def test_censor_predicate_true_when_window_spans_a_censor_date():
    pred = _make_censor_predicate([D(1, 3)])
    assert pred(D(1, 2), D(1, 4)) is True      # spans
    assert pred(D(1, 3), D(1, 5)) is True      # inclusive start
    assert pred(D(1, 1), D(1, 3)) is True      # inclusive end


def test_censor_predicate_false_when_window_misses_the_censor_date():
    pred = _make_censor_predicate([D(1, 3)])
    assert pred(D(1, 4), D(1, 6)) is False     # entirely after
    assert pred(D(1, 1), D(1, 2)) is False     # entirely before


def test_censor_predicate_empty_never_censors():
    assert _make_censor_predicate([])(D(1, 1), D(1, 9)) is False
