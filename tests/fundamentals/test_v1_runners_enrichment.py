"""Integration tests for V1 stock indicator ENRICHMENT runners.

Mirrors test_v1_runners.py but for the enrichment counterpart, and
includes the side-effect lockdown: the enrichment runner must write
ONLY to stock_indicator_enrichment and fundamentals_run.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from trip_ema.fundamentals.v1_runners import (
    PHASE_V1_STOCK_ENRICHMENT,
    populate_stock_indicator_enrichment_for_exchange,
    populate_stock_indicator_enrichment_for_ticker,
    populate_stock_indicator_state_for_ticker,
)
from trip_ema.persistence import transaction


# ============================================================
# Helpers (same shape as test_v1_runners.py)
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
    conn, ticker_id: int, n: int = 400,
    *, start: date = date(2025, 1, 6),
) -> int:
    """400 daily bars → ~57 weekly bars after aggregation."""
    rows = []
    for i in range(n):
        d = start + timedelta(days=i)
        ts = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
        close = 100.0 + 5.0 * math.sin(i / 4.0) + i * 0.05
        rows.append({
            "tid": ticker_id, "tf": "1D", "bt": ts,
            "o": close - 0.5, "h": close + 1.0,
            "l": close - 1.0, "c": close,
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
# Per-ticker enrichment runner
# ============================================================

class TestPopulateStockIndicatorEnrichmentForTicker:

    def test_writes_enrichment_and_only_enrichment(self, db_engine):
        """Happy path + side-effect lockdown. The enrichment runner
        must write only to stock_indicator_enrichment and
        fundamentals_run. State rows are unchanged (already exist
        from the prerequisite Step 4 run). Zero rows to signals,
        legacy indicator_state, or industry tables."""
        ticker_id = 7001
        with transaction(db_engine) as conn:
            _seed_ticker(conn, ticker_id=ticker_id, symbol="ALPHA")
            _seed_daily_bars(conn, ticker_id, n=400)

        # Prerequisite: run state populator first (Step 4).
        populate_stock_indicator_state_for_ticker(
            db_engine, ticker_id, "1W",
        )

        # Baseline counts AFTER state has run.
        with db_engine.connect() as conn:
            baseline_state = _count(
                conn, "stock_indicator_state",
                "ticker_id = :tid", tid=ticker_id,
            )
            baseline_enrichment = _count(
                conn, "stock_indicator_enrichment",
                "ticker_id = :tid", tid=ticker_id,
            )
            baseline_signal_events = _count(
                conn, "signal_events",
                "ticker_id = :tid", tid=ticker_id,
            )
            baseline_current = _count(
                conn, "current_signals",
                "ticker_id = :tid", tid=ticker_id,
            )
            baseline_historical = _count(
                conn, "historical_signals",
                "ticker_id = :tid", tid=ticker_id,
            )
            baseline_legacy_state = _count(
                conn, "indicator_state",
                "ticker_id = :tid", tid=ticker_id,
            )
            baseline_industry_state = _count(
                conn, "industry_indicator_state",
            )
            baseline_industry_enrichment = _count(
                conn, "industry_indicator_enrichment",
            )

        # ---- Act ----
        run_id = populate_stock_indicator_enrichment_for_ticker(
            db_engine, ticker_id, "1W",
        )
        assert run_id > 0

        # ---- Assert: enrichment gained rows ----
        with db_engine.connect() as conn:
            new_enrichment = _count(
                conn, "stock_indicator_enrichment",
                "ticker_id = :tid", tid=ticker_id,
            )
        assert new_enrichment > baseline_enrichment

        # ---- Assert: fundamentals_run audit row ----
        with db_engine.connect() as conn:
            run_row = conn.execute(text("""
                SELECT phase, status, succeeded, failed
                  FROM fundamentals_run
                 WHERE run_id = :rid
            """), {"rid": run_id}).one()
        assert run_row.phase == PHASE_V1_STOCK_ENRICHMENT
        assert run_row.status == "SUCCESS"
        assert run_row.succeeded > 0
        assert run_row.failed == 0

        # ---- Assert: state unchanged (Step 4's responsibility) ----
        with db_engine.connect() as conn:
            assert _count(
                conn, "stock_indicator_state",
                "ticker_id = :tid", tid=ticker_id,
            ) == baseline_state, (
                "Enrichment runner must NOT modify state rows"
            )

        # ---- Assert: ZERO writes elsewhere ----
        with db_engine.connect() as conn:
            assert _count(
                conn, "signal_events",
                "ticker_id = :tid", tid=ticker_id,
            ) == baseline_signal_events
            assert _count(
                conn, "current_signals",
                "ticker_id = :tid", tid=ticker_id,
            ) == baseline_current
            assert _count(
                conn, "historical_signals",
                "ticker_id = :tid", tid=ticker_id,
            ) == baseline_historical
            assert _count(
                conn, "indicator_state",
                "ticker_id = :tid", tid=ticker_id,
            ) == baseline_legacy_state, (
                "V1 enrichment runner must NOT write to legacy "
                "indicator_state"
            )
            assert _count(
                conn, "industry_indicator_state",
            ) == baseline_industry_state
            assert _count(
                conn, "industry_indicator_enrichment",
            ) == baseline_industry_enrichment

    def test_emits_same_bar_set_as_state_runner(self, db_engine):
        """The bar_timestamps in stock_indicator_enrichment should
        exactly match those in stock_indicator_state for this ticker.
        Otherwise the FK ON DELETE CASCADE invariant is broken."""
        ticker_id = 7002
        with transaction(db_engine) as conn:
            _seed_ticker(conn, ticker_id=ticker_id, symbol="MATCH")
            _seed_daily_bars(conn, ticker_id, n=400)

        populate_stock_indicator_state_for_ticker(
            db_engine, ticker_id, "1W",
        )
        populate_stock_indicator_enrichment_for_ticker(
            db_engine, ticker_id, "1W",
        )

        with db_engine.connect() as conn:
            state_bars = conn.execute(text("""
                SELECT bar_timestamp FROM stock_indicator_state
                 WHERE ticker_id = :tid AND timeframe = '1W'
                 ORDER BY bar_timestamp
            """), {"tid": ticker_id}).all()
            enrich_bars = conn.execute(text("""
                SELECT bar_timestamp FROM stock_indicator_enrichment
                 WHERE ticker_id = :tid AND timeframe = '1W'
                 ORDER BY bar_timestamp
            """), {"tid": ticker_id}).all()
        assert [r.bar_timestamp for r in state_bars] == [
            r.bar_timestamp for r in enrich_bars
        ]

    def test_idempotent_second_run_overwrites(self, db_engine):
        ticker_id = 7003
        with transaction(db_engine) as conn:
            _seed_ticker(conn, ticker_id=ticker_id, symbol="BETA")
            _seed_daily_bars(conn, ticker_id, n=400)
        populate_stock_indicator_state_for_ticker(
            db_engine, ticker_id, "1W",
        )
        populate_stock_indicator_enrichment_for_ticker(
            db_engine, ticker_id, "1W",
        )
        with db_engine.connect() as conn:
            first_count = _count(
                conn, "stock_indicator_enrichment",
                "ticker_id = :tid", tid=ticker_id,
            )

        populate_stock_indicator_enrichment_for_ticker(
            db_engine, ticker_id, "1W",
        )
        with db_engine.connect() as conn:
            second_count = _count(
                conn, "stock_indicator_enrichment",
                "ticker_id = :tid", tid=ticker_id,
            )
        assert second_count == first_count

    def test_raises_when_state_not_populated_first(self, db_engine):
        """The runner does a count_for_ticker pre-check on state to
        give a clean error rather than letting the FK violation
        surface as a less helpful IntegrityError."""
        ticker_id = 7004
        with transaction(db_engine) as conn:
            _seed_ticker(conn, ticker_id=ticker_id, symbol="NOSTATE")
            _seed_daily_bars(conn, ticker_id, n=400)
        # Note: NO state populator call.
        with pytest.raises(ValueError, match="no state rows"):
            populate_stock_indicator_enrichment_for_ticker(
                db_engine, ticker_id, "1W",
            )

    def test_raises_for_nonexistent_ticker(self, db_engine):
        with pytest.raises(ValueError, match="does not exist"):
            populate_stock_indicator_enrichment_for_ticker(
                db_engine, 999_999_888, "1W",
            )

    def test_raises_for_unknown_timeframe(self, db_engine):
        ticker_id = 7005
        with transaction(db_engine) as conn:
            _seed_ticker(conn, ticker_id=ticker_id, symbol="UNKTF")
            _seed_daily_bars(conn, ticker_id, n=400)
        with pytest.raises(ValueError, match="unknown timeframe"):
            populate_stock_indicator_enrichment_for_ticker(
                db_engine, ticker_id, "99X",
            )


# ============================================================
# Per-exchange enrichment runner
# ============================================================

class TestPopulateStockIndicatorEnrichmentForExchange:

    def test_isolates_per_ticker_failures(self, db_engine):
        """A ticker with no state rows goes to `skipped`. A ticker
        with state populated goes to `succeeded`. The bulk run
        completes regardless."""
        good_id = 8001
        nostate_id = 8002
        with transaction(db_engine) as conn:
            _seed_ticker(conn, ticker_id=good_id, symbol="GOOD")
            _seed_daily_bars(conn, good_id, n=400)
            _seed_ticker(conn, ticker_id=nostate_id, symbol="NOSTATE")
            _seed_daily_bars(conn, nostate_id, n=400)

        # Populate state for good_id only.
        populate_stock_indicator_state_for_ticker(
            db_engine, good_id, "1W",
        )

        report = populate_stock_indicator_enrichment_for_exchange(
            db_engine, "NSE", "1W",
        )

        succeeded_ids = [tid for tid, _, _ in report["succeeded"]]
        skipped_ids = [tid for tid, _, _ in report["skipped"]]
        assert good_id in succeeded_ids, (
            "Ticker with state rows should succeed"
        )
        assert nostate_id in skipped_ids, (
            "Ticker without state rows should be skipped, not failed"
        )

    def test_other_exchange_tickers_ignored(self, db_engine):
        nse_id = 8101
        bse_id = 8102
        with transaction(db_engine) as conn:
            _seed_ticker(
                conn, ticker_id=nse_id, symbol="ONNSE", exchange="NSE",
            )
            _seed_daily_bars(conn, nse_id, n=400)
            _seed_ticker(
                conn, ticker_id=bse_id, symbol="ONBSE", exchange="BSE",
            )
            _seed_daily_bars(conn, bse_id, n=400)

        # Populate state for both
        populate_stock_indicator_state_for_ticker(
            db_engine, nse_id, "1W",
        )
        populate_stock_indicator_state_for_ticker(
            db_engine, bse_id, "1W",
        )

        report = populate_stock_indicator_enrichment_for_exchange(
            db_engine, "NSE", "1W",
        )
        all_touched = (
            {tid for tid, _, _ in report["succeeded"]}
            | {tid for tid, _, _ in report["skipped"]}
            | {tid for tid, _, _ in report["failed"]}
        )
        assert nse_id in all_touched
        assert bse_id not in all_touched, (
            "BSE ticker should not have been touched by an "
            "NSE-scoped bulk run"
        )
