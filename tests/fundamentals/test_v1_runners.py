"""Integration tests for V1 stock indicator state runners.

These tests run the full populate_stock_indicator_state_for_ticker
path end-to-end against a real DB: seed a ticker, seed OHLCV bars,
run, assert state was written and side-effect tables stayed empty.

Locks in the separation-of-concerns invariant for the V1 runner
(parallel to the Step 3 OHLCV runner test):
  - Writes to stock_indicator_state and fundamentals_run only.
  - Does NOT write to stock_indicator_enrichment, signal_events,
    current_signals, historical_signals, industry_indicator_state,
    industry_indicator_enrichment.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from trip_ema.fundamentals.v1_runners import (
    PHASE_V1_STOCK_STATE,
    populate_stock_indicator_state_for_exchange,
    populate_stock_indicator_state_for_ticker,
)
from trip_ema.persistence import transaction
from trip_ema.persistence.stock_indicator_state_repo import (
    StockIndicatorStateRepository,
)


# ============================================================
# Helpers
# ============================================================

def _seed_ticker(
    conn, *,
    ticker_id: int,
    symbol: str,
    exchange: str = "NSE",
) -> int:
    conn.execute(text("""
        INSERT INTO tickers (ticker_id, exchange, symbol)
        VALUES (:id, :ex, :sym)
        ON CONFLICT (ticker_id) DO NOTHING
    """), {"id": ticker_id, "ex": exchange, "sym": symbol})
    return ticker_id


def _seed_daily_bars(
    conn, ticker_id: int, n: int = 200,
    *, start: date = date(2025, 1, 6),  # Monday
) -> int:
    """Insert N consecutive daily OHLCV bars. Uses a price walk that
    exercises the EMA/ATR/regime walker (sine + drift)."""
    import math
    rows = []
    for i in range(n):
        d = start + timedelta(days=i)
        # Skip weekends only — the legacy weekly aggregator handles
        # the calendar but for simplicity we seed every day; that's
        # fine for an integration test of the V1 runner.
        ts = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
        close = 100.0 + 5.0 * math.sin(i / 4.0) + i * 0.05
        rows.append({
            "tid": ticker_id,
            "tf": "1D",
            "bt": ts,
            "o": close - 0.5,
            "h": close + 1.0,
            "l": close - 1.0,
            "c": close,
            "v": 1_000_000.0,
        })
    conn.execute(text("""
        INSERT INTO ohlcv_bars (
            ticker_id, timeframe, bar_timestamp,
            open, high, low, close, volume
        ) VALUES (
            :tid, :tf, :bt,
            :o, :h, :l, :c, :v
        )
        ON CONFLICT (ticker_id, timeframe, bar_timestamp) DO NOTHING
    """), rows)
    return n


def _count(conn, table: str, where: str = "1=1", **params) -> int:
    return conn.execute(
        text(f"SELECT COUNT(*) FROM {table} WHERE {where}"),
        params,
    ).scalar_one()


# ============================================================
# Per-ticker runner
# ============================================================

class TestPopulateStockIndicatorStateForTicker:

    def test_writes_state_and_only_state(self, db_engine):
        """Happy path + side-effect lockdown."""
        ticker_id = 5001
        with transaction(db_engine) as conn:
            _seed_ticker(conn, ticker_id=ticker_id, symbol="ALPHA")
            # 400 daily bars → ~57 weekly bars after aggregation,
            # comfortably above min_bars=30.
            _seed_daily_bars(conn, ticker_id, n=400)

        # Baseline counts on all touchable tables for this ticker.
        with db_engine.connect() as conn:
            baseline_state = _count(
                conn, "stock_indicator_state",
                "ticker_id = :tid", tid=ticker_id,
            )
            baseline_enrichment = _count(
                conn, "stock_indicator_enrichment",
                "ticker_id = :tid", tid=ticker_id,
            )
            # === STEP 15B-FIXUP2-r1-baseline: signal_events baseline removed ===
            baseline_current = _count(
                conn, "current_signals",
                "ticker_id = :tid", tid=ticker_id,
            )
            baseline_historical = _count(
                conn, "historical_signals",
                "ticker_id = :tid", tid=ticker_id,
            )
            baseline_industry_state = _count(
                conn, "industry_indicator_state",
            )
            baseline_industry_enrichment = _count(
                conn, "industry_indicator_enrichment",
            )

        # ---- Act ----
        run_id = populate_stock_indicator_state_for_ticker(
            db_engine, ticker_id, "1W",
        )

        # ---- Assert: run_id is sensible ----
        assert run_id > 0

        # ---- Assert: stock_indicator_state gained rows ----
        with db_engine.connect() as conn:
            new_state = _count(
                conn, "stock_indicator_state",
                "ticker_id = :tid", tid=ticker_id,
            )
        assert new_state > baseline_state, (
            "Expected stock_indicator_state to gain rows "
            f"(baseline={baseline_state}, after={new_state})"
        )

        # ---- Assert: fundamentals_run row was opened + closed ----
        with db_engine.connect() as conn:
            run_row = conn.execute(text("""
                SELECT phase, status, succeeded, failed, skipped
                  FROM fundamentals_run
                 WHERE run_id = :rid
            """), {"rid": run_id}).one()
        assert run_row.phase == PHASE_V1_STOCK_STATE
        assert run_row.status == "SUCCESS"
        assert run_row.succeeded > 0
        assert run_row.failed == 0

        # ---- Assert: ZERO side-effect writes elsewhere ----
        with db_engine.connect() as conn:
            assert _count(
                conn, "stock_indicator_enrichment",
                "ticker_id = :tid", tid=ticker_id,
            ) == baseline_enrichment, (
                "V1 stock-state runner must not write to enrichment "
                "table (that's Step 5)"
            )
            # === STEP 15B-FIXUP2-r1-assert: signal_events assert removed ===
            assert _count(
                conn, "current_signals",
                "ticker_id = :tid", tid=ticker_id,
            ) == baseline_current
            assert _count(
                conn, "historical_signals",
                "ticker_id = :tid", tid=ticker_id,
            ) == baseline_historical
            assert _count(
                conn, "industry_indicator_state",
            ) == baseline_industry_state
            assert _count(
                conn, "industry_indicator_enrichment",
            ) == baseline_industry_enrichment

    def test_idempotent_second_run_overwrites(self, db_engine):
        ticker_id = 5002
        with transaction(db_engine) as conn:
            _seed_ticker(conn, ticker_id=ticker_id, symbol="BETA")
            _seed_daily_bars(conn, ticker_id, n=400)

        populate_stock_indicator_state_for_ticker(
            db_engine, ticker_id, "1W",
        )
        with db_engine.connect() as conn:
            first_count = _count(
                conn, "stock_indicator_state",
                "ticker_id = :tid", tid=ticker_id,
            )

        # Run again — should be a no-op on row count (upserts overwrite)
        populate_stock_indicator_state_for_ticker(
            db_engine, ticker_id, "1W",
        )
        with db_engine.connect() as conn:
            second_count = _count(
                conn, "stock_indicator_state",
                "ticker_id = :tid", tid=ticker_id,
            )
        assert second_count == first_count

    def test_raises_for_nonexistent_ticker(self, db_engine):
        with pytest.raises(ValueError, match="does not exist"):
            populate_stock_indicator_state_for_ticker(
                db_engine, 999_999_999, "1W",
            )

    def test_raises_for_insufficient_bars(self, db_engine):
        ticker_id = 5003
        with transaction(db_engine) as conn:
            _seed_ticker(conn, ticker_id=ticker_id, symbol="SHORT")
            _seed_daily_bars(conn, ticker_id, n=5)  # below min_bars
        with pytest.raises(ValueError, match="bars"):
            populate_stock_indicator_state_for_ticker(
                db_engine, ticker_id, "1W",
            )

    def test_raises_for_unknown_timeframe(self, db_engine):
        ticker_id = 5004
        with transaction(db_engine) as conn:
            _seed_ticker(conn, ticker_id=ticker_id, symbol="UNKTF")
            _seed_daily_bars(conn, ticker_id, n=200)
        with pytest.raises(ValueError, match="unknown timeframe"):
            populate_stock_indicator_state_for_ticker(
                db_engine, ticker_id, "99X",
            )


# ============================================================
# Per-exchange runner
# ============================================================

class TestPopulateStockIndicatorStateForExchange:

    def test_isolates_per_ticker_failures(self, db_engine):
        """A ticker with insufficient bars goes to `skipped`. A ticker
        with enough bars goes to `succeeded`. The bulk run completes."""
        good_id = 6001
        short_id = 6002
        with transaction(db_engine) as conn:
            _seed_ticker(conn, ticker_id=good_id, symbol="GOOD")
            _seed_daily_bars(conn, good_id, n=400)
            _seed_ticker(conn, ticker_id=short_id, symbol="SHORT")
            _seed_daily_bars(conn, short_id, n=5)

        report = populate_stock_indicator_state_for_exchange(
            db_engine, "NSE", "1W",
        )

        succeeded_ids = [tid for tid, _, _ in report["succeeded"]]
        skipped_ids = [tid for tid, _, _ in report["skipped"]]
        assert good_id in succeeded_ids, (
            "Ticker with sufficient bars should succeed"
        )
        assert short_id in skipped_ids, (
            "Ticker with too few bars should be skipped, not failed"
        )

    def test_other_exchange_tickers_ignored(self, db_engine):
        nse_id = 6101
        bse_id = 6102
        with transaction(db_engine) as conn:
            _seed_ticker(
                conn, ticker_id=nse_id, symbol="ONNSE", exchange="NSE",
            )
            _seed_daily_bars(conn, nse_id, n=400)
            _seed_ticker(
                conn, ticker_id=bse_id, symbol="ONBSE", exchange="BSE",
            )
            _seed_daily_bars(conn, bse_id, n=400)

        report = populate_stock_indicator_state_for_exchange(
            db_engine, "NSE", "1W",
        )
        all_touched = (
            {tid for tid, _, _ in report["succeeded"]}
            | {tid for tid, _, _ in report["skipped"]}
            | {tid for tid, _, _ in report["failed"]}
        )
        assert nse_id in all_touched
        assert bse_id not in all_touched, (
            "BSE ticker should not have been touched by an NSE-scoped "
            "bulk run"
        )
