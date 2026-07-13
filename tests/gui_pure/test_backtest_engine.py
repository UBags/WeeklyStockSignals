"""Pure tests for trip_ema/gui/backtest_engine.py.

No Qt, no DB. Synthetic bar dicts drive every test so the engine's
state-machine + summary computations are pinned exactly.

Bars carry the same shape SignalsView gets from the /v1/signals/.../
history endpoint, but we only populate the fields the engine reads:
  bar_timestamp, low, close, display_bg, is_warmed_up,
  med_ema, long_ema, longest_ema
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone, UTC

import pytest

from trip_ema.gui.backtest_engine import (
    BacktestResult,
    OpenTrade,
    Trade,
    filter_bars_to_range,
    run_long_only_backtest,
)


# ---------- bar fixture helpers ---------- #


_T0 = datetime(2024, 1, 1, tzinfo=UTC)


def _bar(
    week: int,
    *,
    regime: str = "NEUTRAL",
    close: float = 100.0,
    low: float | None = None,
    high: float | None = None,
    med: float = 100.0,
    long_: float = 100.0,
    longest: float = 100.0,
    warmed: bool = True,
) -> dict:
    """Build one bar dict with sensible defaults.

    `low` defaults to `close - 1` so MAE has something to measure;
    `high` defaults to `close + 1`. EMAs default to 100.0 so the
    entry price is deterministic unless the test overrides them.
    """
    if low is None:
        low = close - 1.0
    if high is None:
        high = close + 1.0
    return {
        "bar_timestamp": _T0 + timedelta(weeks=week),
        "open": close, "high": high, "low": low, "close": close,
        "med_ema": med, "long_ema": long_, "longest_ema": longest,
        "display_bg": regime,
        "is_warmed_up": warmed,
    }


# ---------- run_long_only_backtest ---------- #


class TestEngineEmptyInputs:

    def test_empty_bars_zeroed_summary(self):
        r = run_long_only_backtest([])
        assert r.trades == []
        assert r.open_trade is None
        assert r.summary.n_trades == 0
        assert r.summary.total_return_pct == 0.0
        assert r.summary.cagr_pct == 0.0
        assert r.bars_evaluated == 0
        assert r.bars_skipped_warmup == 0

    def test_all_warmup_bars_no_trades(self):
        bars = [_bar(i, warmed=False, regime="BULLISH") for i in range(10)]
        r = run_long_only_backtest(bars)
        assert r.trades == []
        assert r.open_trade is None
        assert r.bars_skipped_warmup == 10
        assert r.bars_evaluated == 0

    def test_all_neutral_no_trades(self):
        bars = [_bar(i, regime="NEUTRAL", close=100.0) for i in range(10)]
        r = run_long_only_backtest(bars)
        assert r.trades == []
        assert r.open_trade is None


class TestEngineEntryExit:

    def test_single_complete_trade_with_profit(self):
        """NEUTRAL → BULLISH (entry) → NEUTRAL (exit).
        EMAs all 100 at entry → entry_price = 100.
        Exit close = 110 → return = +10%.
        """
        bars = [
            _bar(0, regime="NEUTRAL", close=100.0,
                 med=100.0, long_=100.0, longest=100.0),
            _bar(1, regime="BULLISH", close=105.0, low=99.0,
                 med=100.0, long_=100.0, longest=100.0),
            _bar(2, regime="BULLISH", close=108.0, low=104.0,
                 med=102.0, long_=101.0, longest=100.0),
            _bar(3, regime="NEUTRAL", close=110.0, low=107.0,
                 med=104.0, long_=102.0, longest=100.0),
        ]
        r = run_long_only_backtest(bars)

        assert len(r.trades) == 1
        t = r.trades[0]
        assert t.entry_bar_index == 1
        assert t.entry_price == 100.0
        assert t.exit_bar_index == 3
        assert t.exit_price == 110.0
        assert t.return_pct == pytest.approx(10.0)
        # Shares = 100_000 / 100 = 1000
        assert t.n_shares == pytest.approx(1000.0)
        # PnL = 1000 * (110 - 100) = 10000
        assert t.pnl_inr == pytest.approx(10_000.0)
        assert r.open_trade is None

    def test_single_complete_trade_with_loss(self):
        bars = [
            _bar(0, regime="NEUTRAL", close=100.0),
            _bar(1, regime="BULLISH", close=98.0, low=97.0,
                 med=100.0, long_=100.0, longest=100.0),
            _bar(2, regime="BEARISH", close=95.0, low=94.0),
        ]
        r = run_long_only_backtest(bars)
        assert len(r.trades) == 1
        t = r.trades[0]
        assert t.return_pct == pytest.approx(-5.0)
        assert t.pnl_inr == pytest.approx(-5000.0)

    def test_entry_picks_max_of_three_emas(self):
        """Entry price = max(med, long, longest). Even if all three
        differ the picker should select the highest."""
        bars = [
            _bar(0, regime="NEUTRAL", close=100.0),
            _bar(1, regime="BULLISH", close=130.0,
                 med=110.0, long_=125.0, longest=118.0),
            _bar(2, regime="NEUTRAL", close=150.0),
        ]
        r = run_long_only_backtest(bars)
        # max(110, 125, 118) = 125
        assert r.trades[0].entry_price == 125.0

    def test_first_bullish_bar_does_not_enter_without_prev_regime(self):
        """If the very first warmed-up bar is BULLISH, we have no
        prior bar to compare against — no transition to detect.
        Engine should NOT enter; it waits for a confirmed transition."""
        bars = [
            _bar(0, regime="BULLISH", close=100.0,
                 med=100.0, long_=100.0, longest=100.0),
            _bar(1, regime="BULLISH", close=110.0),
            _bar(2, regime="NEUTRAL", close=120.0),
        ]
        r = run_long_only_backtest(bars)
        # No transition observed → no trade
        assert r.trades == []
        assert r.open_trade is None

    def test_consecutive_bullish_bars_do_not_re_enter(self):
        """Within a single bullish run, only one entry. Subsequent
        bullish bars don't re-trigger."""
        bars = [
            _bar(0, regime="NEUTRAL", close=100.0),
            _bar(1, regime="BULLISH", close=105.0,
                 med=100.0, long_=100.0, longest=100.0),
            _bar(2, regime="BULLISH", close=108.0),
            _bar(3, regime="BULLISH", close=112.0),
            _bar(4, regime="BEARISH", close=110.0),
        ]
        r = run_long_only_backtest(bars)
        assert len(r.trades) == 1
        assert r.trades[0].entry_bar_index == 1
        assert r.trades[0].exit_bar_index == 4

    def test_multiple_trades_in_sequence(self):
        bars = [
            _bar(0, regime="NEUTRAL", close=100.0),
            _bar(1, regime="BULLISH", close=110.0,
                 med=100.0, long_=100.0, longest=100.0),
            _bar(2, regime="NEUTRAL", close=115.0),
            _bar(3, regime="NEUTRAL", close=120.0),
            _bar(4, regime="BULLISH", close=125.0,
                 med=120.0, long_=120.0, longest=120.0),
            _bar(5, regime="BEARISH", close=130.0),
        ]
        r = run_long_only_backtest(bars)
        assert len(r.trades) == 2
        assert r.trades[0].entry_price == 100.0
        assert r.trades[0].exit_price == 115.0
        assert r.trades[1].entry_price == 120.0
        assert r.trades[1].exit_price == 130.0


class TestEngineOpenTrade:

    def test_unclosed_trade_at_end_returned_separately(self):
        bars = [
            _bar(0, regime="NEUTRAL", close=100.0),
            _bar(1, regime="BULLISH", close=110.0, low=108.0,
                 med=100.0, long_=100.0, longest=100.0),
            _bar(2, regime="BULLISH", close=115.0, low=112.0),
        ]
        r = run_long_only_backtest(bars)
        assert r.trades == []
        assert r.open_trade is not None
        ot = r.open_trade
        assert ot.entry_price == 100.0
        assert ot.last_bar_close == 115.0
        assert ot.unrealized_return_pct == pytest.approx(15.0)
        assert ot.bars_held == 1   # last bar index - entry bar index

    def test_open_trade_does_not_pollute_summary(self):
        bars = [
            _bar(0, regime="NEUTRAL", close=100.0),
            _bar(1, regime="BULLISH", close=110.0,
                 med=100.0, long_=100.0, longest=100.0),
        ]
        r = run_long_only_backtest(bars)
        assert r.summary.n_trades == 0
        assert r.summary.total_return_pct == 0.0


class TestEngineMAE:

    def test_mae_tracks_lowest_low_during_trade(self):
        """MAE should be the most-negative drawdown observed inside
        the trade, expressed as a pct of entry_price."""
        bars = [
            _bar(0, regime="NEUTRAL", close=100.0),
            # Entry at 100. Subsequent lows: 95 then 85 then back up.
            _bar(1, regime="BULLISH", close=105.0, low=95.0,
                 med=100.0, long_=100.0, longest=100.0),
            _bar(2, regime="BULLISH", close=92.0, low=85.0),
            _bar(3, regime="BULLISH", close=98.0, low=90.0),
            _bar(4, regime="NEUTRAL", close=105.0, low=100.0),
        ]
        r = run_long_only_backtest(bars)
        t = r.trades[0]
        # min_low = 85; entry = 100; MAE = (85-100)/100 * 100 = -15%
        assert t.max_adverse_excursion_pct == pytest.approx(-15.0)

    def test_mae_is_zero_when_no_drawdown(self):
        """If every bar's low stays above entry_price, MAE = 0 (clamped
        from positive dust)."""
        bars = [
            _bar(0, regime="NEUTRAL", close=100.0),
            _bar(1, regime="BULLISH", close=110.0, low=105.0,
                 med=100.0, long_=100.0, longest=100.0),
            _bar(2, regime="BULLISH", close=120.0, low=115.0),
            _bar(3, regime="NEUTRAL", close=130.0, low=125.0),
        ]
        r = run_long_only_backtest(bars)
        assert r.trades[0].max_adverse_excursion_pct == 0.0


class TestEngineSummary:

    def test_compounded_total_return_two_winning_trades(self):
        """+10% then +10% → final capital = 100k * 1.1 * 1.1 = 121k.
        total_return_pct = (121k - 100k) / 100k = 21%."""
        bars = [
            _bar(0, regime="NEUTRAL", close=100.0),
            _bar(1, regime="BULLISH", close=105.0,
                 med=100.0, long_=100.0, longest=100.0),
            _bar(2, regime="NEUTRAL", close=110.0),   # +10% trade
            _bar(3, regime="NEUTRAL", close=110.0),
            _bar(4, regime="BULLISH", close=115.0,
                 med=110.0, long_=110.0, longest=110.0),
            _bar(5, regime="NEUTRAL", close=121.0),   # +10% trade
        ]
        r = run_long_only_backtest(bars)
        assert len(r.trades) == 2
        assert r.summary.total_return_pct == pytest.approx(21.0)
        assert r.summary.final_capital_inr == pytest.approx(121_000.0)

    def test_win_rate(self):
        # 2 wins, 1 loss → 66.67%
        bars = [
            _bar(0, regime="NEUTRAL", close=100.0),
            _bar(1, regime="BULLISH", close=110.0,
                 med=100.0, long_=100.0, longest=100.0),
            _bar(2, regime="NEUTRAL", close=110.0),
            _bar(3, regime="NEUTRAL", close=110.0),
            _bar(4, regime="BULLISH", close=115.0,
                 med=110.0, long_=110.0, longest=110.0),
            _bar(5, regime="NEUTRAL", close=110.0 * 0.95),  # -5% loss
            _bar(6, regime="NEUTRAL", close=104.5),
            _bar(7, regime="BULLISH", close=110.0,
                 med=104.5, long_=104.5, longest=104.5),
            _bar(8, regime="NEUTRAL", close=120.0),
        ]
        r = run_long_only_backtest(bars)
        s = r.summary
        assert s.n_trades == 3
        assert s.n_wins == 2
        assert s.n_losses == 1
        assert s.win_rate_pct == pytest.approx(66.67, abs=0.1)

    def test_cagr_with_two_year_span(self):
        """100k → 121k over exactly 2 years = 10% CAGR."""
        bars = [
            _bar(0, regime="NEUTRAL", close=100.0),
            _bar(1, regime="BULLISH", close=110.0,
                 med=100.0, long_=100.0, longest=100.0),
            _bar(2, regime="NEUTRAL", close=110.0),
            # Skip ahead ~104 weeks so the duration is 2 years
            _bar(105, regime="NEUTRAL", close=110.0),
            _bar(106, regime="BULLISH", close=115.0,
                 med=110.0, long_=110.0, longest=110.0),
            _bar(107, regime="NEUTRAL", close=121.0),
        ]
        r = run_long_only_backtest(bars)
        # duration ≈ 107-1 = 106 weeks ≈ 2.03 years
        # final capital = 121k. CAGR = (1.21)^(1/2.03) - 1 ≈ 0.0996
        assert r.summary.cagr_pct == pytest.approx(10.0, abs=0.5)

    def test_worst_mae_picks_most_negative(self):
        bars = [
            _bar(0, regime="NEUTRAL", close=100.0),
            # Trade 1: -3% MAE
            _bar(1, regime="BULLISH", close=105.0, low=97.0,
                 med=100.0, long_=100.0, longest=100.0),
            _bar(2, regime="NEUTRAL", close=110.0),
            _bar(3, regime="NEUTRAL", close=110.0),
            # Trade 2: -8% MAE (worst)
            _bar(4, regime="BULLISH", close=115.0, low=101.2,
                 med=110.0, long_=110.0, longest=110.0),
            _bar(5, regime="NEUTRAL", close=120.0),
        ]
        r = run_long_only_backtest(bars)
        # Trade 1: entry 100, low 97 → MAE = -3%
        # Trade 2: entry 110, low 101.2 → MAE = (101.2-110)/110*100 = -8%
        assert r.summary.worst_mae_pct == pytest.approx(-8.0, abs=0.1)


class TestEngineCapitalParameter:

    def test_custom_capital_changes_share_count(self):
        bars = [
            _bar(0, regime="NEUTRAL", close=100.0),
            _bar(1, regime="BULLISH", close=110.0,
                 med=100.0, long_=100.0, longest=100.0),
            _bar(2, regime="NEUTRAL", close=110.0),
        ]
        r = run_long_only_backtest(bars, capital_per_trade=50_000.0)
        # 50_000 / 100 = 500 shares
        assert r.trades[0].n_shares == pytest.approx(500.0)
        assert r.summary.starting_capital_inr == 50_000.0

    def test_zero_capital_raises(self):
        with pytest.raises(ValueError):
            run_long_only_backtest([], capital_per_trade=0)

    def test_negative_capital_raises(self):
        with pytest.raises(ValueError):
            run_long_only_backtest([], capital_per_trade=-100)


# ---------- filter_bars_to_range ---------- #


class TestFilterBarsToRange:

    def test_none_years_returns_all_bars(self):
        bars = [_bar(i) for i in range(5)]
        out = filter_bars_to_range(bars, years=None)
        assert out == bars

    def test_excludes_bars_older_than_cutoff(self):
        # Reference = 2026-06-18; cutoff for 2y = ~2024-06-18.
        # Build bars from 2024-01-01 + N weeks.
        bars = [_bar(i) for i in range(150)]   # 150 weeks ≈ 2.9 years
        ref = datetime(2026, 12, 31, tzinfo=UTC)
        out = filter_bars_to_range(bars, years=2, reference_dt=ref)
        # cutoff ≈ 2024-12-31; bars start 2024-01-01.
        # Bars 0..51 are < cutoff, bars 52+ are >=. Allow ±1 for rounding.
        assert len(out) == pytest.approx(len(bars) - 52, abs=1)

    def test_tz_naive_bar_is_rejected(self):
        """A naive bar_timestamp is an error, not a comparison problem.

        This test used to assert the opposite — that a naive bar "should still
        compare correctly" against a tz-aware reference. It cannot. A naive
        datetime names no instant, so there is no correct comparison to make; the
        old code grafted whichever tzinfo the OTHER operand happened to carry
        ("prefer the more-specific one") and produced an answer that silently
        depended on the caller. A backtest window a week out is exactly the kind of
        wrong that nobody notices.

        Every producer of this payload emits an aware timestamp. A naive one means
        something upstream dropped the zone, and that is worth a traceback.
        """
        bar_naive = {
            "bar_timestamp": datetime(2025, 6, 1),  # naive
            "open": 0, "high": 0, "low": 0, "close": 0,
            "display_bg": "NEUTRAL", "is_warmed_up": True,
            "med_ema": 100, "long_ema": 100, "longest_ema": 100,
        }
        ref = datetime(2026, 6, 1, tzinfo=UTC)
        with pytest.raises(ValueError, match="naive"):
            filter_bars_to_range([bar_naive], years=2, reference_dt=ref)