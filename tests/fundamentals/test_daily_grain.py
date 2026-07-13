"""Tests for fundamentals/daily_grain.py + market_session.py — weekly
indicators sampled daily via a per-exchange prorated forming-week bar.

  * TestProration / TestSynthesize / TestMultiMarket — pure, no DB.
  * TestDailyGrainTrajectory / TestEnrichmentAlignment — exercise the
    compute layer end to end (no DB).
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, UTC
from zoneinfo import ZoneInfo

import pytest

from trip_ema.domain import OHLCVBar
from trip_ema.domain.market_session import (
    MarketSession,
    market_session_for,
)
from trip_ema.fundamentals.daily_grain import (
    compute_daily_grain_enrichment,
    compute_daily_grain_states,
    synthesize_forming_bar,
)

_IST = ZoneInfo("Asia/Kolkata")
_MONDAY = date(2026, 6, 15)  # a real Monday
_FUTURE = datetime(2030, 1, 1, tzinfo=UTC)  # every test day is closed


class _AllWeekdays:
    def is_trading_day(self, d: date) -> bool:
        return d.weekday() < 5


class _WedHoliday:
    def is_trading_day(self, d: date) -> bool:
        return d.weekday() < 5 and d != date(2026, 6, 17)


def _nse(cal=None) -> MarketSession:
    return MarketSession(
        "NSE", "Asia/Kolkata", time(9, 15), time(15, 30),
        cal or _AllWeekdays(),
    )


def _bar(d: date, o=100.0, h=101.0, low=99.0, c=100.0, v=1000.0) -> OHLCVBar:
    ts = datetime.combine(d, time(12, 0), tzinfo=_IST).astimezone(UTC)
    return OHLCVBar(timestamp=ts, open=o, high=h, low=low, close=c, volume=v)


class TestProration:
    @pytest.mark.parametrize("off,expected", [
        (0, 5.0), (1, 2.5), (2, 5.0 / 3.0), (3, 1.25), (4, 1.0),
    ])
    def test_daily_multipliers_full_week(self, off, expected):
        s = _nse()
        d = _MONDAY + timedelta(days=off)
        wtd = 100.0 * (off + 1)
        mult = s.imputed_weekly_volume(
            wtd, _MONDAY, d, _FUTURE.astimezone(_IST),
        ) / wtd
        assert mult == pytest.approx(expected, rel=1e-9)

    def test_day5_returns_real_volume(self):
        s = _nse()
        friday = _MONDAY + timedelta(days=4)
        assert s.imputed_weekly_volume(
            9999.0, _MONDAY, friday, _FUTURE.astimezone(_IST),
        ) == pytest.approx(9999.0)

    def test_holiday_week_uses_actual_session_count(self):
        s = _nse(_WedHoliday())
        assert s.week_total_hours(_MONDAY) == 4 * s.session_hours
        mult = s.imputed_weekly_volume(
            100.0, _MONDAY, _MONDAY, _FUTURE.astimezone(_IST),
        ) / 100.0
        assert mult == pytest.approx(4.0)

    def test_intraday_partial_session(self):
        s = _nse()
        wed = date(2026, 6, 17)
        now_mid = datetime(2026, 6, 17, 12, 15, tzinfo=_IST)  # 3h in
        elapsed = s.week_hours_elapsed(_MONDAY, wed, now_mid)
        assert elapsed == pytest.approx(12.5 + 3.0)


class TestSynthesize:
    def test_ohlc_is_week_to_date(self):
        from trip_ema.indicators.aggregation import _monday_of
        s = _nse()
        days = [
            _bar(_MONDAY, o=100, h=105, low=98, c=101),
            _bar(_MONDAY + timedelta(days=1), o=101, h=110, low=100, c=108),
            _bar(_MONDAY + timedelta(days=2), o=108, h=109, low=95, c=97),
        ]
        w = _monday_of(days[0].timestamp, tz="Asia/Kolkata")
        bar = synthesize_forming_bar(
            days, w, _MONDAY, _MONDAY + timedelta(days=2),
            _FUTURE.astimezone(_IST), s,
        )
        assert (bar.open, bar.high, bar.low, bar.close) == (100, 110, 95, 97)
        assert bar.timestamp == w
        assert bar.volume == pytest.approx(3000.0 * (31.25 / 18.75))


class TestMultiMarket:
    def test_session_hours_from_window(self):
        assert market_session_for("NSE").session_hours == pytest.approx(6.25)
        assert market_session_for("NYSE").session_hours == pytest.approx(6.5)
        assert market_session_for("LSE").session_hours == pytest.approx(8.5)

    def test_boundary_multipliers_market_independent(self):
        # At a session boundary the multiplier is session-count based;
        # session length cancels, so NSE and NYSE agree.
        nse = market_session_for("NSE")
        # use fake calendars so we don't depend on holiday data
        nse = _nse()
        nyse = MarketSession(
            "NYSE", "America/New_York", time(9, 30), time(16, 0),
            _AllWeekdays(),
        )
        fut_ny = datetime(2030, 1, 1, tzinfo=ZoneInfo("America/New_York"))
        for off, exp in [(0, 5.0), (1, 2.5), (4, 1.0)]:
            d = _MONDAY + timedelta(days=off)
            wtd = 100.0
            mn = nse.imputed_weekly_volume(
                wtd, _MONDAY, d, _FUTURE.astimezone(_IST)) / wtd
            my = nyse.imputed_weekly_volume(
                wtd, _MONDAY, d, fut_ny) / wtd
            assert mn == pytest.approx(exp)
            assert my == pytest.approx(exp)

    def test_unknown_exchange_falls_back(self):
        s = market_session_for("XYZ", fallback_tz="Asia/Tokyo")
        assert s.tz == "Asia/Tokyo"
        assert s.session_hours == pytest.approx(7.0)  # 09:00-16:00


def _gen_daily(n_weeks: int, start_monday: date) -> list[OHLCVBar]:
    bars: list[OHLCVBar] = []
    price = 100.0
    for w in range(n_weeks):
        wk = start_monday + timedelta(weeks=w)
        for dow in range(5):
            price *= 1.002
            bars.append(_bar(
                wk + timedelta(days=dow), o=price, h=price * 1.01,
                low=price * 0.99, c=price, v=1000.0 + dow * 10,
            ))
    return bars


class TestDailyGrainTrajectory:
    def test_row_per_target_day_after_warmup(self):
        bars = _gen_daily(40, date(2025, 1, 6))
        s = _nse()
        last = bars[-1].timestamp.astimezone(_IST).date()
        last_mon = last - timedelta(days=last.weekday())
        target = [last_mon + timedelta(days=i) for i in range(5)]
        rows = compute_daily_grain_states(
            bars, session=s, now=_FUTURE, target_days=target,
        )
        assert len(rows) == 5
        for r, d in zip(rows, target):
            assert r["trade_date"] == d
            assert r["bar_timestamp"].astimezone(_IST).date() == last_mon
            assert r["med_ema"] > 0

    def test_friday_row_matches_completed_week_compute(self):
        import pandas as pd
        from trip_ema.indicators.aggregation import daily_to_weekly
        from trip_ema.archive.v1_compute_primitives import (
            compute_state_primitives,
        )
        bars = _gen_daily(40, date(2025, 1, 6))
        s = _nse()
        last = bars[-1].timestamp.astimezone(_IST).date()
        friday = (last - timedelta(days=last.weekday())) + timedelta(days=4)
        dg = compute_daily_grain_states(
            bars, session=s, now=_FUTURE, target_days=[friday],
        )
        assert len(dg) == 1
        weekly = daily_to_weekly(
            bars, include_partial_last_week=True, tz="Asia/Kolkata",
        )
        prims = compute_state_primitives(
            pd.Series([b.close for b in weekly], dtype=float),
            pd.Series([b.volume for b in weekly], dtype=float),
            pd.Series([b.high for b in weekly], dtype=float),
            pd.Series([b.low for b in weekly], dtype=float),
            trade_dates=pd.Series([b.timestamp for b in weekly]),
        )
        wk_last = prims[-1]
        for k in ("med_ema", "long_ema", "longest_ema", "atr",
                  "upper_inner_band", "lower_inner_band",
                  "upper_mid_band", "lower_mid_band",
                  "upper_outer_band", "lower_outer_band",
                  "display_regime"):
            assert dg[0][k] == pytest.approx(wk_last[k], rel=1e-9), k

    def test_warmup_days_omitted(self):
        bars = _gen_daily(40, date(2025, 1, 6))
        s = _nse()
        first = date(2025, 1, 6)
        target = [first + timedelta(days=i) for i in range(5)]
        assert compute_daily_grain_states(
            bars, session=s, now=_FUTURE, target_days=target,
        ) == []

    def test_full_mode_emits_many_days(self):
        bars = _gen_daily(40, date(2025, 1, 6))
        s = _nse()
        rows = compute_daily_grain_states(bars, session=s, now=_FUTURE)
        assert len(rows) > 100
        dts = [r["trade_date"] for r in rows]
        assert dts == sorted(dts) and len(dts) == len(set(dts))


class TestEnrichmentAlignment:
    def test_enrichment_days_align_with_state_days(self):
        """Enrichment must emit a row for exactly the same trading days
        the state runner does — that's the 1:1 FK invariant."""
        bars = _gen_daily(40, date(2025, 1, 6))
        s = _nse()
        state_rows = compute_daily_grain_states(bars, session=s, now=_FUTURE)
        enrich_rows = compute_daily_grain_enrichment(
            bars, session=s, now=_FUTURE,
        )
        state_days = {r["trade_date"] for r in state_rows}
        enrich_days = {r["trade_date"] for r in enrich_rows}
        assert state_days == enrich_days
        # And enrichment carries its own fields + the same anchor.
        sample = enrich_rows[-1]
        assert "med_pattern_label" in sample
        assert "bars_since_med_turn" in sample
        assert sample["trade_date"] in state_days
