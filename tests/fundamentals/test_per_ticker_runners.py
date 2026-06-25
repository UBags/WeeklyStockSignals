"""Tests for Phase 7 per-ticker indicator state + signal event runners."""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone, UTC

import pytest

from trip_ema.domain import OHLCVBar, Timeframe, TickerRef
from trip_ema.fundamentals import IndicatorState
from trip_ema.fundamentals.runners import (
    run_indicator_state_for_all_tickers,
    run_indicator_state_for_ticker,
    run_signal_events_for_all_tickers,
    run_signal_events_for_ticker,
)
from trip_ema.persistence import transaction
from trip_ema.persistence.bars_repo import BarsRepository
from trip_ema.persistence.fundamentals_repo import (
    FundamentalsRunRepository,
    IndicatorStateRepository,
    SignalEventRepository,
)
from trip_ema.persistence.tickers_repo import TickersRepository


# ---------- helpers ---------- #


def _setup_ticker_with_bars(
    db_engine,
    symbol: str = "RELIANCE", exchange: str = "NSE",
    *, n_bars: int = 80,
    base_date: datetime = None,
    timeframe: Timeframe = Timeframe.WEEKLY,
) -> int:
    """Create a ticker and populate n_bars of OHLCV history.
    Returns ticker_id."""
    if base_date is None:
        base_date = datetime(2025, 1, 6, 9, 30, tzinfo=UTC)

    with transaction(db_engine) as conn:
        ref = TickerRef(exchange, symbol, "EQ")
        ticker_id = TickersRepository(conn).insert(
            ref, created_at=datetime.now(UTC),
        )
        bars_repo = BarsRepository(conn)
        for i in range(n_bars):
            ts = base_date + timedelta(days=i * 7)
            # Synthesize OHLCV with a sinusoid for the indicator
            # pipeline to chew on.
            close_v = 1000.0 + 50.0 * math.sin(i * 0.3)
            high_v = close_v + 10.0
            low_v = close_v - 10.0
            open_v = close_v - 5.0
            volume_v = 100000.0 + 10000.0 * (i % 5)
            bars_repo.upsert_bar(
                ticker_id, timeframe,
                OHLCVBar(
                    timestamp=ts, open=open_v, high=high_v,
                    low=low_v, close=close_v, volume=volume_v,
                ),
            )
    return ticker_id


# ============================================================
# run_indicator_state_for_ticker
# ============================================================


class TestRunIndicatorStateForTicker:

    def test_writes_indicator_state_rows(self, db_engine):
        tid = _setup_ticker_with_bars(db_engine, n_bars=80)
        run_id = run_indicator_state_for_ticker(db_engine, tid, "1W")
        with db_engine.connect() as conn:
            r = FundamentalsRunRepository(conn).get(run_id)
        assert r.status == "SUCCESS"
        assert r.succeeded > 0
        assert r.failed == 0

    def test_skipped_count_for_early_nan_bars(self, db_engine):
        tid = _setup_ticker_with_bars(db_engine, n_bars=80)
        run_id = run_indicator_state_for_ticker(db_engine, tid, "1W")
        with db_engine.connect() as conn:
            r = FundamentalsRunRepository(conn).get(run_id)
        # ~6 NaN-leading bars before EMAs stabilize.
        assert r.skipped > 0
        assert r.succeeded + r.skipped == 80

    def test_persisted_rows_have_ticker_identity(self, db_engine):
        tid = _setup_ticker_with_bars(db_engine, n_bars=80)
        run_indicator_state_for_ticker(db_engine, tid, "1W")
        with db_engine.connect() as conn:
            latest = IndicatorStateRepository(conn).get_latest_for_ticker(
                tid, "1W",
            )
        assert latest is not None
        assert latest.ticker_id == tid
        assert latest.timeframe == "1W"
        assert latest.industry_name is None
        assert latest.display_regime in (-1, 0, 1)

    def test_idempotent_via_skip_if_recent(self, db_engine):
        # Use recent dates so the skip-if-recent guard fires.
        today = datetime.now(UTC).replace(
            hour=9, minute=30, second=0, microsecond=0,
        )
        base = today - timedelta(days=80 * 7)
        tid = _setup_ticker_with_bars(
            db_engine, n_bars=80, base_date=base,
        )
        run_id_1 = run_indicator_state_for_ticker(db_engine, tid, "1W")
        run_id_2 = run_indicator_state_for_ticker(db_engine, tid, "1W")
        with db_engine.connect() as conn:
            r1 = FundamentalsRunRepository(conn).get(run_id_1)
            r2 = FundamentalsRunRepository(conn).get(run_id_2)
        assert r1.succeeded > 0
        # Second call skipped.
        assert r2.succeeded == 0
        assert r2.skipped == 1

    def test_force_re_runs(self, db_engine):
        today = datetime.now(UTC).replace(
            hour=9, minute=30, second=0, microsecond=0,
        )
        base = today - timedelta(days=80 * 7)
        tid = _setup_ticker_with_bars(
            db_engine, n_bars=80, base_date=base,
        )
        run_indicator_state_for_ticker(db_engine, tid, "1W")
        run_id_2 = run_indicator_state_for_ticker(
            db_engine, tid, "1W", force=True,
        )
        with db_engine.connect() as conn:
            r2 = FundamentalsRunRepository(conn).get(run_id_2)
        assert r2.succeeded > 0

    def test_raises_if_ticker_does_not_exist(self, db_engine):
        with pytest.raises(ValueError, match="does not exist"):
            run_indicator_state_for_ticker(db_engine, 9999, "1W")

    def test_raises_if_unknown_timeframe(self, db_engine):
        tid = _setup_ticker_with_bars(db_engine, n_bars=80)
        with pytest.raises(ValueError, match="unknown timeframe"):
            run_indicator_state_for_ticker(
                db_engine, tid, "bogus_tf",
            )

    def test_raises_if_insufficient_bars(self, db_engine):
        # Only 5 bars — below the default min_bars=30.
        tid = _setup_ticker_with_bars(db_engine, n_bars=5)
        with pytest.raises(ValueError, match="has only"):
            run_indicator_state_for_ticker(db_engine, tid, "1W")


# ============================================================
# run_indicator_state_for_all_tickers
# ============================================================


class TestRunIndicatorStateForAllTickers:

    def test_processes_all_active_tickers(self, db_engine):
        _setup_ticker_with_bars(db_engine, "RELIANCE", "NSE", n_bars=80)
        _setup_ticker_with_bars(db_engine, "TCS", "NSE", n_bars=80)
        report = run_indicator_state_for_all_tickers(db_engine, "1W")
        symbols = {s for _, s, _, _ in report["succeeded"]}
        assert symbols == {"RELIANCE", "TCS"}
        assert report["failed"] == []

    def test_filters_by_exchange(self, db_engine):
        _setup_ticker_with_bars(db_engine, "RELIANCE", "NSE", n_bars=80)
        _setup_ticker_with_bars(db_engine, "TATAMOTORS", "BSE", n_bars=80)
        report = run_indicator_state_for_all_tickers(
            db_engine, "1W", exchange="NSE",
        )
        symbols = {s for _, s, _, _ in report["succeeded"]}
        assert symbols == {"RELIANCE"}

    def test_skips_tickers_with_insufficient_bars(self, db_engine):
        _setup_ticker_with_bars(db_engine, "RELIANCE", "NSE", n_bars=80)
        _setup_ticker_with_bars(db_engine, "NEWLIST", "NSE", n_bars=10)
        report = run_indicator_state_for_all_tickers(db_engine, "1W")
        succeeded_symbols = {s for _, s, _, _ in report["succeeded"]}
        skipped_symbols = {s for _, s, _ in report["skipped"]}
        assert succeeded_symbols == {"RELIANCE"}
        assert skipped_symbols == {"NEWLIST"}

    def test_raises_on_unknown_timeframe(self, db_engine):
        _setup_ticker_with_bars(db_engine, "RELIANCE", "NSE", n_bars=80)
        with pytest.raises(ValueError, match="unknown timeframe"):
            run_indicator_state_for_all_tickers(db_engine, "bogus")

    def test_empty_when_no_tickers(self, db_engine):
        report = run_indicator_state_for_all_tickers(db_engine, "1W")
        assert report == {"succeeded": [], "skipped": [], "failed": []}


# ============================================================
# run_signal_events_for_ticker
# ============================================================


class TestRunSignalEventsForTicker:

    def test_writes_signal_events(self, db_engine):
        # First populate indicator_state.
        tid = _setup_ticker_with_bars(db_engine, n_bars=80)
        run_indicator_state_for_ticker(db_engine, tid, "1W")
        # Now compute signals.
        run_id = run_signal_events_for_ticker(db_engine, tid, "1W")
        with db_engine.connect() as conn:
            r = FundamentalsRunRepository(conn).get(run_id)
            events = SignalEventRepository(conn).get_for_ticker(tid, "1W")
        assert r.status == "SUCCESS"
        # The synthetic sinusoid should produce SOME regime
        # transitions and therefore some events.
        assert r.succeeded > 0
        assert len(events) > 0

    def test_events_have_ticker_identity(self, db_engine):
        tid = _setup_ticker_with_bars(db_engine, n_bars=80)
        run_indicator_state_for_ticker(db_engine, tid, "1W")
        run_signal_events_for_ticker(db_engine, tid, "1W")
        with db_engine.connect() as conn:
            events = SignalEventRepository(conn).get_for_ticker(tid, "1W")
        for e in events:
            assert e.ticker_id == tid
            assert e.timeframe == "1W"
            assert e.industry_name is None
            assert e.event_kind in ("BUY", "SELL", "EXIT_BUY", "EXIT_SELL")

    def test_close_at_event_uses_real_close(self, db_engine):
        """close_at_event should match the actual ohlcv_bars.close
        at the event date, not the EMA or any derived value."""
        tid = _setup_ticker_with_bars(db_engine, n_bars=80)
        run_indicator_state_for_ticker(db_engine, tid, "1W")
        run_signal_events_for_ticker(db_engine, tid, "1W")
        with db_engine.connect() as conn:
            events = SignalEventRepository(conn).get_for_ticker(tid, "1W")
            bars = BarsRepository(conn).fetch_history(
                tid, Timeframe.WEEKLY, limit=200,
            )
        # Build close lookup from bars and verify each event matches.
        close_by_date = {b.timestamp.date(): b.close for b in bars}
        for e in events:
            assert e.close_at_event == close_by_date[e.trade_date]

    def test_idempotent_via_skip_if_recent(self, db_engine):
        today = datetime.now(UTC).replace(
            hour=9, minute=30, second=0, microsecond=0,
        )
        base = today - timedelta(days=80 * 7)
        tid = _setup_ticker_with_bars(
            db_engine, n_bars=80, base_date=base,
        )
        run_indicator_state_for_ticker(db_engine, tid, "1W")
        run_signal_events_for_ticker(db_engine, tid, "1W")
        run_id_2 = run_signal_events_for_ticker(db_engine, tid, "1W")
        with db_engine.connect() as conn:
            r2 = FundamentalsRunRepository(conn).get(run_id_2)
        assert r2.succeeded == 0
        assert r2.skipped == 1

    def test_raises_if_ticker_missing(self, db_engine):
        with pytest.raises(ValueError, match="does not exist"):
            run_signal_events_for_ticker(db_engine, 9999, "1W")

    def test_raises_if_no_indicator_state(self, db_engine):
        tid = _setup_ticker_with_bars(db_engine, n_bars=80)
        # Don't run indicator_state first.
        with pytest.raises(ValueError, match="no indicator_state"):
            run_signal_events_for_ticker(db_engine, tid, "1W")

    def test_raises_on_unknown_timeframe(self, db_engine):
        tid = _setup_ticker_with_bars(db_engine, n_bars=80)
        with pytest.raises(ValueError, match="unknown timeframe"):
            run_signal_events_for_ticker(db_engine, tid, "bogus")


# ============================================================
# run_signal_events_for_all_tickers
# ============================================================


class TestRunSignalEventsForAllTickers:

    def test_processes_tickers_with_indicator_state(self, db_engine):
        tid_a = _setup_ticker_with_bars(
            db_engine, "RELIANCE", "NSE", n_bars=80,
        )
        tid_b = _setup_ticker_with_bars(
            db_engine, "TCS", "NSE", n_bars=80,
        )
        run_indicator_state_for_ticker(db_engine, tid_a, "1W")
        run_indicator_state_for_ticker(db_engine, tid_b, "1W")
        report = run_signal_events_for_all_tickers(db_engine, "1W")
        symbols = {s for _, s, _ in report["succeeded"]}
        assert symbols == {"RELIANCE", "TCS"}

    def test_skips_tickers_without_indicator_state(self, db_engine):
        tid_a = _setup_ticker_with_bars(
            db_engine, "RELIANCE", "NSE", n_bars=80,
        )
        _setup_ticker_with_bars(
            db_engine, "TCS", "NSE", n_bars=80,
        )
        # Only run indicator_state for one of them.
        run_indicator_state_for_ticker(db_engine, tid_a, "1W")
        report = run_signal_events_for_all_tickers(db_engine, "1W")
        succeeded_symbols = {s for _, s, _ in report["succeeded"]}
        skipped_symbols = {s for _, s, _ in report["skipped"]}
        assert succeeded_symbols == {"RELIANCE"}
        assert skipped_symbols == {"TCS"}

    def test_filters_by_exchange(self, db_engine):
        tid_a = _setup_ticker_with_bars(
            db_engine, "RELIANCE", "NSE", n_bars=80,
        )
        tid_b = _setup_ticker_with_bars(
            db_engine, "TATAMOTORS", "BSE", n_bars=80,
        )
        run_indicator_state_for_ticker(db_engine, tid_a, "1W")
        run_indicator_state_for_ticker(db_engine, tid_b, "1W")
        report = run_signal_events_for_all_tickers(
            db_engine, "1W", exchange="NSE",
        )
        symbols = {s for _, s, _ in report["succeeded"]}
        assert symbols == {"RELIANCE"}
