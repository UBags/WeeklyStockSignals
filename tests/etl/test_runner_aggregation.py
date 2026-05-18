"""Tests for the intraday hourly→daily aggregation helper.
Pure logic; no I/O."""

from __future__ import annotations

from datetime import date

import pytest

from trip_ema.etl.runner import _aggregate_if_intraday
from trip_ema.etl.types import RawBar


def _bar(d, o=100, h=110, l=90, c=105, ac=105, v=1000):
    return RawBar(
        trading_date=d,
        open=o, high=h, low=l, close=c, adj_close=ac, volume=v,
    )


class TestIntradayAggregation:

    def test_non_intraday_passthrough(self):
        bars = [_bar(date(2026, 5, 14)), _bar(date(2026, 5, 15))]
        out = _aggregate_if_intraday(bars, intraday=False)
        assert out == bars

    def test_single_day_hourly_bars_become_one_daily(self):
        """Three hourly bars for one trading day collapse to one
        daily-provisional bar: first open, max high, min low, last close,
        summed volume."""
        bars = [
            _bar(date(2026, 5, 14), o=100, h=105, l=99, c=102, v=1000),
            _bar(date(2026, 5, 14), o=102, h=110, l=101, c=108, v=2000),
            _bar(date(2026, 5, 14), o=108, h=112, l=107, c=110, v=1500),
        ]
        out = _aggregate_if_intraday(bars, intraday=True)
        assert len(out) == 1
        d = out[0]
        # First open (note: aggregation sorts by trading_date but for
        # the same day, input order is preserved within the bucket)
        assert d.open == 100
        # Max high across the day
        assert d.high == 112
        # Min low
        assert d.low == 99
        # Last close
        assert d.close == 110
        # Summed volume
        assert d.volume == 4500

    def test_multiple_days_separate_buckets(self):
        bars = [
            _bar(date(2026, 5, 14), c=100),
            _bar(date(2026, 5, 14), c=101),
            _bar(date(2026, 5, 15), c=110),
            _bar(date(2026, 5, 15), c=111),
        ]
        out = _aggregate_if_intraday(bars, intraday=True)
        assert len(out) == 2
        dates = sorted(b.trading_date for b in out)
        assert dates == [date(2026, 5, 14), date(2026, 5, 15)]
        by_date = {b.trading_date: b for b in out}
        # 5/14 last close = 101; 5/15 last close = 111
        assert by_date[date(2026, 5, 14)].close == 101
        assert by_date[date(2026, 5, 15)].close == 111

    def test_empty_input_returns_empty(self):
        assert _aggregate_if_intraday([], intraday=True) == []

    def test_single_hourly_bar_becomes_single_daily(self):
        bars = [_bar(date(2026, 5, 14), o=100, h=105, l=99, c=102, v=1000)]
        out = _aggregate_if_intraday(bars, intraday=True)
        assert len(out) == 1
        assert out[0].open == 100
        assert out[0].close == 102
        assert out[0].volume == 1000
