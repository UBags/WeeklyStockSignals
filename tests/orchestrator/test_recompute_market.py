"""Integration tests for `trip_ema.orchestrator.recompute_market`.

Synthetic fixture: 2-3 tickers + 1-2 industries + ~400 daily bars
each. Exercises the full live-mode pipeline (8 stages) and asserts
the resulting DB + cache state.

For per-stage internals, the underlying populators / primitives have
their own tests; here we verify they compose correctly.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone, UTC

import pytest
from sqlalchemy import text

from trip_ema.cache.current_signals_cache import CurrentSignalsCache
from trip_ema.fundamentals.v1_runners import (
    populate_industry_index_value_for_all,
    populate_industry_indicator_state_for_all,
    populate_stock_indicator_state_for_exchange,
)
from trip_ema.orchestrator.recompute_market import (
    RecomputeMarketResult,
    recompute_market,
)
from trip_ema.persistence import transaction


# ============================================================
# Fixtures — shared seed helpers
# ============================================================

def _seed_ticker(
    conn, *,
    ticker_id: int, symbol: str, exchange: str = "NSE",
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
    """Sine + drift; ~57 weekly bars after aggregation."""
    rows = []
    for i in range(n):
        d = start + timedelta(days=i)
        ts = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
        close = 100.0 + 5.0 * math.sin(i / 4.0) + i * 0.05
        rows.append({
            "tid": ticker_id, "tf": "1D", "bt": ts,
            "o": close - 0.5, "h": close + 1.0, "l": close - 1.0,
            "c": close, "v": 1_000_000.0,
        })
    conn.execute(text("""
        INSERT INTO ohlcv_bars (
            ticker_id, timeframe, bar_timestamp,
            open, high, low, close, volume
        ) VALUES (
            :tid, :tf, :bt, :o, :h, :l, :c, :v
        )
        ON CONFLICT (ticker_id, timeframe, bar_timestamp) DO NOTHING
    """), rows)
    return n


def _seed_industry(conn, *, name: str) -> None:
    """Canonical industry seed — mirrors
    tests/fundamentals/test_v1_runners_industry_indicator_state.py.
    status='ACTIVE' is required for the bulk wrappers."""
    conn.execute(text("""
        INSERT INTO industry_index (
            industry_name, sector_name, methodology,
            base_value, base_date, rebalance_schedule,
            min_constituents, status
        ) VALUES (
            :name, 'Technology', 'MODIFIED_LASPEYRES',
            1000.0, '2025-01-01', 'SEMIANNUAL_APR_OCT',
            10, 'ACTIVE'
        )
        ON CONFLICT (industry_name) DO NOTHING
    """), {"name": name})


def _seed_iiv_history(
    conn, industry_name: str,
    *, n_days: int = 400, start: date = date(2025, 1, 6),
) -> int:
    """Seed N daily industry_index_value rows. Canonical pattern."""
    for i in range(n_days):
        td = start + timedelta(days=i)
        v = 1000.0 + 50.0 * math.sin(i / 4.0) + i * 0.5
        conn.execute(text("""
            INSERT INTO industry_index_value (
                industry_name, trade_date, value,
                constituents_used, constituents_missing,
                renormalized, effective_rebalance_date
            ) VALUES (
                :ind, :td, :v, 10, 0, FALSE, :ed
            )
            ON CONFLICT (industry_name, trade_date) DO NOTHING
        """), {"ind": industry_name, "td": td, "v": v, "ed": start})
    return n_days


def _setup_minimal_universe(
    db_engine,
    *,
    n_tickers: int = 2,
    n_industries: int = 1,
) -> tuple[list[int], list[str]]:
    """Seed N tickers + M industries with enough history for the
    pipeline to run. Then prime stock + industry state full-history
    so the cycle can do incremental work."""
    ticker_ids: list[int] = []
    industry_names: list[str] = []

    with transaction(db_engine) as conn:
        for i in range(n_tickers):
            tid = 8000 + i
            _seed_ticker(conn, ticker_id=tid, symbol=f"TKR{i}")
            _seed_daily_bars(conn, tid, n=400)
            ticker_ids.append(tid)

        for i in range(n_industries):
            name = f"CycleInd{i}"
            _seed_industry(conn, name=name)
            _seed_iiv_history(conn, name, n_days=400)
            industry_names.append(name)

    # Prime stock_indicator_state full-history. Required so the
    # cycle's incremental compute has both "this week" and "last
    # week" rows to compare; derive_signals walks the latest two.
    populate_stock_indicator_state_for_exchange(
        db_engine, "NSE", "1W",
    )
    # Industry state also needs primed history so the incremental
    # cycle stage can do its work.
    populate_industry_indicator_state_for_all(db_engine)

    return ticker_ids, industry_names


# ============================================================
# Mode guard
# ============================================================

class TestModeGuard:

    def test_backfill_mode_raises_until_step_11(self, db_engine):
        with pytest.raises(NotImplementedError, match="backfill"):
            recompute_market(db_engine, mode="backfill")


# ============================================================
# Live mode — happy path
# ============================================================

class TestLiveModeHappyPath:

    def test_returns_result_with_no_stage_errors(self, db_engine):
        ticker_ids, _ = _setup_minimal_universe(db_engine)
        result = recompute_market(db_engine, mode="live")

        assert isinstance(result, RecomputeMarketResult)
        assert result.mode == "live"
        assert result.stage_errors == []
        # Both seeded tickers were processed (incremental on the 2
        # we seeded)
        assert result.stock_state_succeeded == len(ticker_ids)
        assert result.stock_enrichment_succeeded == len(ticker_ids)

    def test_iiv_refresh_runs(self, db_engine):
        """Stage 6 runs the IIV populator across active industries."""
        _, industry_names = _setup_minimal_universe(db_engine)
        result = recompute_market(db_engine, mode="live")
        # Each industry should have at least its IIV-stage entry
        # in succeeded OR skipped (depending on whether the
        # trailing window had any trading days).
        iiv_total = (
            result.iiv_succeeded
            + result.iiv_failed
            + result.iiv_skipped
        )
        assert iiv_total >= len(industry_names)

    def test_industry_stages_run(self, db_engine):
        _, industry_names = _setup_minimal_universe(db_engine)
        result = recompute_market(db_engine, mode="live")
        # Each industry processed by state + enrichment stages
        # (count includes succeeded + failed + skipped per
        # populator semantics)
        s = (
            result.industry_state_succeeded
            + result.industry_state_failed
            + result.industry_state_skipped
        )
        e = (
            result.industry_enrichment_succeeded
            + result.industry_enrichment_failed
            + result.industry_enrichment_skipped
        )
        assert s >= len(industry_names)
        assert e >= len(industry_names)


# ============================================================
# Live mode — stage outputs
# ============================================================

class TestLiveModeStageOutputs:

    def test_first_cycle_rotates_zero(self, db_engine):
        """First cycle: current_signals is empty before rotation,
        so signals_rotated == 0."""
        _setup_minimal_universe(db_engine)
        result = recompute_market(db_engine, mode="live")
        assert result.signals_rotated == 0

    def test_second_cycle_rotates_first_cycles_emissions(
        self, db_engine,
    ):
        """First cycle emits signals into current_signals. The
        SECOND cycle's rotation moves them into historical."""
        _setup_minimal_universe(db_engine)

        r1 = recompute_market(db_engine, mode="live")
        r2 = recompute_market(db_engine, mode="live")

        # Whatever cycle 1 emitted, cycle 2 rotated
        assert r2.signals_rotated == r1.signals_emitted

    def test_emitted_signals_land_in_current_signals(self, db_engine):
        _setup_minimal_universe(db_engine)
        result = recompute_market(db_engine, mode="live")

        with db_engine.connect() as conn:
            cnt = conn.execute(text(
                "SELECT COUNT(*) FROM current_signals"
            )).scalar_one()
        # current_signals count should match signals_emitted
        # (the result reports rows actually inserted)
        assert cnt == result.signals_emitted


# ============================================================
# Cache rebuild
# ============================================================

class TestCacheRebuild:

    def test_cache_not_rebuilt_when_none(self, db_engine):
        _setup_minimal_universe(db_engine)
        result = recompute_market(db_engine, mode="live", cache=None)
        assert result.cache_rebuilt is False

    def test_cache_rebuilt_when_provided(self, db_engine):
        _setup_minimal_universe(db_engine)
        cache = CurrentSignalsCache()
        result = recompute_market(
            db_engine, mode="live", cache=cache,
        )
        assert result.cache_rebuilt is True

    def test_cache_mirrors_current_signals(self, db_engine):
        """After the cycle, cache.signal_count() == current_signals
        row count."""
        _setup_minimal_universe(db_engine)
        cache = CurrentSignalsCache()
        result = recompute_market(
            db_engine, mode="live", cache=cache,
        )
        assert cache.signal_count() == result.signals_emitted

        with db_engine.connect() as conn:
            db_count = conn.execute(text(
                "SELECT COUNT(*) FROM current_signals"
            )).scalar_one()
        assert cache.signal_count() == db_count


# ============================================================
# Result determinism + as_of
# ============================================================

class TestAsOf:

    def test_as_of_defaults_to_now(self, db_engine):
        _setup_minimal_universe(db_engine)
        before = datetime.now(UTC)
        result = recompute_market(db_engine, mode="live")
        after = datetime.now(UTC)
        assert before <= result.as_of <= after

    def test_explicit_as_of_passes_through(self, db_engine):
        _setup_minimal_universe(db_engine)
        explicit = datetime(2026, 1, 15, 14, 30, tzinfo=UTC)
        result = recompute_market(
            db_engine, mode="live", as_of=explicit,
        )
        assert result.as_of == explicit


# ============================================================
# Idempotency
# ============================================================

class TestIdempotency:

    def test_consecutive_cycles_dont_explode(self, db_engine):
        """Two consecutive cycles complete without errors. The
        second cycle's signals_rotated equals the first's
        signals_emitted (rotation moved them); the second's
        signals_emitted equals the first's because the underlying
        state hasn't actually changed between cycles (same bars,
        same regime, same signals re-detected on the same bars).
        """
        _setup_minimal_universe(db_engine)

        r1 = recompute_market(db_engine, mode="live")
        r2 = recompute_market(db_engine, mode="live")

        assert r1.stage_errors == []
        assert r2.stage_errors == []
        # Same input → same emission count (idempotent compute)
        assert r2.signals_emitted == r1.signals_emitted


# ============================================================
# Exchange filter (stocks only)
# ============================================================

class TestExchangeFilter:

    def test_other_exchange_skips_stocks(self, db_engine):
        """Tickers on BSE are NOT touched by an NSE cycle (stock
        side filters by exchange). Industry side has no exchange
        filter so it still runs."""
        with transaction(db_engine) as conn:
            # NSE ticker — should be processed
            _seed_ticker(
                conn, ticker_id=8500, symbol="NSE_T", exchange="NSE",
            )
            _seed_daily_bars(conn, 8500, n=400)
            # BSE ticker — should NOT be processed by an NSE cycle
            _seed_ticker(
                conn, ticker_id=8501, symbol="BSE_T", exchange="BSE",
            )
            _seed_daily_bars(conn, 8501, n=400)

        populate_stock_indicator_state_for_exchange(
            db_engine, "NSE", "1W",
        )
        populate_stock_indicator_state_for_exchange(
            db_engine, "BSE", "1W",
        )

        result = recompute_market(
            db_engine, mode="live", exchange="NSE",
        )
        # Only NSE ticker processed in the stock-side stages
        assert result.stock_state_succeeded == 1
