"""A missing opening print must never reach the database as 0.0.

Yahoo writes open=0.0 when an instrument had no opening trade. ohlcv_bars.open
is NOT NULL, so that zero persists and surfaces as an entry price in
forward_walk. Substitution happens at ingestion, and every substitution is
logged -- the 2026-07-19 backfill found 1,676 such bars only because a
downstream guard refused them.
"""
from __future__ import annotations

import logging
from datetime import date

from trip_ema.etl.yfinance_source import _open_or_substitute


class _Bar:
    def __init__(self, close):
        self.close = close


def test_a_positive_open_is_returned_unchanged():
    assert _open_or_substitute(
        101.5, 103.0, 100.0, 102.0, [], "X.NS", date(2009, 1, 2),
    ) == 101.5


def test_zero_open_takes_the_previous_close():
    got = _open_or_substitute(
        0.0, 103.0, 100.0, 102.0, [_Bar(99.5)], "BANKBEES.NS",
        date(2009, 1, 2),
    )
    assert got == 99.5


def test_zero_open_with_no_previous_bar_falls_back_to_typical_price():
    got = _open_or_substitute(
        0.0, 103.0, 99.0, 102.0, [], "GOLDBEES.NS", date(2009, 1, 2),
    )
    assert got == (103.0 + 99.0 + 102.0) / 3.0


def test_a_non_positive_previous_close_is_not_used():
    """A zero previous close is itself a missing open we substituted, or bad
    data. Chaining off it would propagate the defect forward."""
    got = _open_or_substitute(
        0.0, 103.0, 99.0, 102.0, [_Bar(0.0)], "X.NS", date(2009, 1, 2),
    )
    assert got == (103.0 + 99.0 + 102.0) / 3.0


def test_a_negative_open_is_also_substituted():
    got = _open_or_substitute(
        -1.0, 103.0, 99.0, 102.0, [_Bar(98.0)], "X.NS", date(2009, 1, 2),
    )
    assert got == 98.0


def test_every_substitution_is_logged(caplog):
    """Silent repair is how 1,676 bad bars got in unnoticed."""
    with caplog.at_level(logging.WARNING, logger="trip_ema.etl.yfinance_source"):
        _open_or_substitute(
            0.0, 103.0, 100.0, 102.0, [_Bar(99.5)], "BANKBEES.NS",
            date(2009, 1, 2),
        )
        _open_or_substitute(
            0.0, 103.0, 99.0, 102.0, [], "GOLDBEES.NS", date(2009, 1, 2),
        )
    msgs = [r.getMessage() for r in caplog.records]
    assert len(msgs) == 2
    assert "BANKBEES.NS" in msgs[0] and "previous close" in msgs[0]
    assert "GOLDBEES.NS" in msgs[1] and "typical price" in msgs[1]


def test_a_valid_open_logs_nothing(caplog):
    with caplog.at_level(logging.WARNING, logger="trip_ema.etl.yfinance_source"):
        _open_or_substitute(
            101.5, 103.0, 100.0, 102.0, [_Bar(99.5)], "X.NS", date(2009, 1, 2),
        )
    assert caplog.records == []


def test_the_substitute_always_lies_inside_the_days_range():
    """A substituted open outside [low, high] would be an impossible bar."""
    for prev in (None, 50.0, 150.0):
        bars = [] if prev is None else [_Bar(prev)]
        got = _open_or_substitute(
            0.0, 103.0, 99.0, 102.0, bars, "X.NS", date(2009, 1, 2),
        )
        if prev in (50.0, 150.0):
            # Previous close CAN sit outside today's range -- a gap. That is
            # real market behaviour and is preserved, not clamped.
            assert got == prev
        else:
            assert 99.0 <= got <= 103.0
