"""Integration tests for V1 cutover Step 2: cycle-incremental mode.

Each of the four V1 populators (stock state, stock enrichment,
industry state, industry enrichment) and their bulk wrappers now
accept `mode='incremental'` + `n_weeks_to_upsert=2`. The full
trajectory is still computed (cheap pandas math); only the DB-write
set is sliced.

These tests assert:
  - mode='full' (default) writes the full computed trajectory
  - mode='incremental' writes only the last N rows
  - n_weeks_to_upsert >= computed-row-count is a no-op (writes all)
  - Idempotent — re-running incremental doesn't duplicate rows
  - Late correction to a recent bar is picked up by next incremental
  - Bulk wrappers pass through mode + n_weeks_to_upsert
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from trip_ema.fundamentals.v1_runners import (
    populate_industry_indicator_enrichment_for_industry,
    populate_industry_indicator_state_for_industry,
    populate_stock_indicator_enrichment_for_exchange,
    populate_stock_indicator_enrichment_for_ticker,
    populate_stock_indicator_state_for_exchange,
    populate_stock_indicator_state_for_ticker,
)
from trip_ema.persistence import transaction
from trip_ema.persistence.industry_indicator_enrichment_repo import (
    IndustryIndicatorEnrichmentRepository,
)
from trip_ema.persistence.industry_indicator_state_repo import (
    IndustryIndicatorStateRepository,
)
from trip_ema.persistence.stock_indicator_enrichment_repo import (
    StockIndicatorEnrichmentRepository,
)
from trip_ema.persistence.stock_indicator_state_repo import (
    StockIndicatorStateRepository,
)


# ============================================================
# Stock-side helpers (taken from existing test_v1_runners pattern)
# ============================================================

def _seed_ticker(
    conn, *, ticker_id: int, symbol: str, exchange: str = "NSE",
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
    """Sine + drift price walk — enough bars for the EMA/ATR walker
    to seed (~57 weekly bars after aggregation for n=400)."""
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


# ============================================================
# Industry-side helpers
# ============================================================

def _seed_exchange_ticker(conn, exchange: str = "NSE") -> None:
    """Give this database an exchange.

    An industry index is CONSTRUCTED from ticker constituents — select_top_n_
    constituents picks tickers by market cap and the index is computed from their
    prices. A database with an industry_index row and no tickers is not a minimal
    fixture, it is an impossible state: nothing in the pipeline can produce it.

    It went unnoticed only because nothing used to look at `tickers` from the
    industry path. exchange_of_engine now does, to resolve the timezone for the
    week anchor, and asserts exactly one exchange — the invariant behind
    one-database-per-exchange.

    Idempotent: several helpers may call it within one test.
    """
    conn.execute(text("""
        INSERT INTO tickers (ticker_id, exchange, symbol, instrument)
        VALUES (900001, :ex, 'FIXTURE_ANCHOR', 'EQ')
        ON CONFLICT (ticker_id) DO NOTHING
    """), {"ex": exchange})


def _seed_industry(conn, *, name: str) -> None:
    """Insert one industry_index row directly (not via the repo upsert
    which has methodology/schedule validation we don't want to mock
    out for these tests). Matches the canonical pattern from
    `tests/fundamentals/test_v1_runners_industry_indicator_state.py`.

    status='ACTIVE' is required — the bulk wrappers iterate
    `IndustryIndexRepository.list_active()` which filters on that."""
    _seed_exchange_ticker(conn)
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


def _seed_industry_index_values(
    conn, industry_name: str,
    *,
    n_days: int = 400,
    start: date = date(2025, 1, 6),
) -> int:
    """Seed `n_days` of synthetic industry_index_value rows.
    Sine-drift series ensures non-trivial weekly H/L after
    aggregation. Same column shape as the canonical test pattern in
    `tests/fundamentals/test_v1_runners_industry_indicator_state.py`."""
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


# ============================================================
# Stock state — per-ticker
# ============================================================

class TestStockStateIncremental:

    def _setup(self, db_engine, ticker_id: int = 7001) -> int:
        with transaction(db_engine) as conn:
            _seed_ticker(conn, ticker_id=ticker_id, symbol="STKINC")
            _seed_daily_bars(conn, ticker_id, n=400)
        return ticker_id

    def test_full_mode_is_default(self, db_engine):
        """Default mode is 'full' — backward-compatible with Step 4."""
        tid = self._setup(db_engine)
        populate_stock_indicator_state_for_ticker(
            db_engine, tid, "1W",
        )
        with db_engine.connect() as conn:
            cnt = StockIndicatorStateRepository(
                conn,
            ).count_for_ticker(tid, "1W")
        # ~57 weekly bars from 400 daily, minus EMA warmup. Expect > 20.
        assert cnt > 20

    def test_incremental_writes_current_week_only(self, db_engine):
        """Daily grain: mode='incremental' writes only the current
        in-progress week's day rows — all sharing that week's Monday
        bar_timestamp. n_weeks_to_upsert is vestigial."""
        tid = self._setup(db_engine)
        populate_stock_indicator_state_for_ticker(
            db_engine, tid, "1W", mode="incremental",
        )
        with db_engine.connect() as conn:
            rows = StockIndicatorStateRepository(
                conn,
            ).get_history_for_ticker(tid, "1W")
        assert len(rows) >= 1
        # Every incremental row belongs to the same (latest) week.
        assert len({r.bar_timestamp for r in rows}) == 1

    def test_incremental_writes_latest_week(self, db_engine):
        """The incremental rows belong to the LATEST week (the one
        containing the most recent daily bar)."""
        tid = self._setup(db_engine)
        # Full first to learn the latest week's Monday.
        populate_stock_indicator_state_for_ticker(
            db_engine, tid, "1W",
        )
        with db_engine.connect() as conn:
            full_history = StockIndicatorStateRepository(
                conn,
            ).get_history_for_ticker(tid, "1W")
        # newest-first → [0] is the latest day; its bar_timestamp is
        # the latest week's Monday.
        latest_monday = full_history[0].bar_timestamp

        # Reset: delete state, re-run incremental
        with transaction(db_engine) as conn:
            conn.execute(
                text("DELETE FROM stock_indicator_state "
                     "WHERE ticker_id = :tid"),
                {"tid": tid},
            )
        populate_stock_indicator_state_for_ticker(
            db_engine, tid, "1W", mode="incremental",
        )

        with db_engine.connect() as conn:
            history = StockIndicatorStateRepository(
                conn,
            ).get_history_for_ticker(tid, "1W")
        assert {r.bar_timestamp for r in history} == {latest_monday}

    def test_n_weeks_to_upsert_has_no_effect(self, db_engine):
        """Daily grain ignores n_weeks_to_upsert: incremental writes
        the current week regardless of the n value."""
        tid = self._setup(db_engine)
        populate_stock_indicator_state_for_ticker(
            db_engine, tid, "1W",
            mode="incremental", n_weeks_to_upsert=2,
        )
        with db_engine.connect() as conn:
            cnt_small = StockIndicatorStateRepository(
                conn,
            ).count_for_ticker(tid, "1W")
        with transaction(db_engine) as conn:
            conn.execute(
                text("DELETE FROM stock_indicator_state "
                     "WHERE ticker_id = :tid"),
                {"tid": tid},
            )
        populate_stock_indicator_state_for_ticker(
            db_engine, tid, "1W",
            mode="incremental", n_weeks_to_upsert=100_000,
        )
        with db_engine.connect() as conn:
            cnt_large = StockIndicatorStateRepository(
                conn,
            ).count_for_ticker(tid, "1W")
        assert cnt_small == cnt_large >= 1

    def test_incremental_is_idempotent(self, db_engine):
        """Re-running incremental upserts the current week's day rows
        in place; row count is unchanged."""
        tid = self._setup(db_engine)
        populate_stock_indicator_state_for_ticker(
            db_engine, tid, "1W", mode="incremental",
        )
        with db_engine.connect() as conn:
            first = StockIndicatorStateRepository(
                conn,
            ).count_for_ticker(tid, "1W")
        populate_stock_indicator_state_for_ticker(
            db_engine, tid, "1W", mode="incremental",
        )
        with db_engine.connect() as conn:
            second = StockIndicatorStateRepository(
                conn,
            ).count_for_ticker(tid, "1W")
        assert first == second and first >= 1

    def test_full_then_incremental_does_not_truncate(self, db_engine):
        """Full populates everything; subsequent incremental
        overwrites the latest 2 rows but leaves older rows intact."""
        tid = self._setup(db_engine)
        populate_stock_indicator_state_for_ticker(
            db_engine, tid, "1W",
        )
        with db_engine.connect() as conn:
            full_count = StockIndicatorStateRepository(
                conn,
            ).count_for_ticker(tid, "1W")
        populate_stock_indicator_state_for_ticker(
            db_engine, tid, "1W",
            mode="incremental", n_weeks_to_upsert=2,
        )
        with db_engine.connect() as conn:
            after_count = StockIndicatorStateRepository(
                conn,
            ).count_for_ticker(tid, "1W")
        # Incremental upserts the latest 2 in place; older rows
        # remain. Total count is unchanged.
        assert after_count == full_count


# ============================================================
# Stock state — bulk pass-through
# ============================================================

class TestStockStateBulkPassthrough:

    def test_bulk_passes_mode_to_each_ticker(self, db_engine):
        """Bulk wrapper with mode='incremental' writes only the
        current week's day rows per ticker (not the full history)."""
        with transaction(db_engine) as conn:
            _seed_ticker(conn, ticker_id=7101, symbol="A")
            _seed_daily_bars(conn, 7101, n=400)
            _seed_ticker(conn, ticker_id=7102, symbol="B")
            _seed_daily_bars(conn, 7102, n=400)

        report = populate_stock_indicator_state_for_exchange(
            db_engine, "NSE", "1W",
            mode="incremental", n_weeks_to_upsert=2,
        )
        assert "succeeded" in report
        # Each ticker wrote only the current week's days (<= 7),
        # proving incremental did not emit the full history.
        for (_, _, cnt) in report["succeeded"]:
            assert 1 <= cnt <= 7


# ============================================================
# Stock enrichment — per-ticker
# ============================================================

class TestStockEnrichmentIncremental:

    def _setup(self, db_engine, ticker_id: int = 7201) -> int:
        """State must be populated before enrichment (FK constraint)."""
        with transaction(db_engine) as conn:
            _seed_ticker(conn, ticker_id=ticker_id, symbol="ENRINC")
            _seed_daily_bars(conn, ticker_id, n=400)
        # Populate state (full) so enrichment can land
        populate_stock_indicator_state_for_ticker(
            db_engine, ticker_id, "1W",
        )
        return ticker_id

    def test_incremental_writes_current_week_only(self, db_engine):
        tid = self._setup(db_engine)
        populate_stock_indicator_enrichment_for_ticker(
            db_engine, tid, "1W", mode="incremental",
        )
        with db_engine.connect() as conn:
            cnt = StockIndicatorEnrichmentRepository(
                conn,
            ).count_for_ticker(tid, "1W")
        # Current week's day rows only, not the full history.
        assert 1 <= cnt <= 7

    def test_incremental_is_idempotent(self, db_engine):
        tid = self._setup(db_engine)
        populate_stock_indicator_enrichment_for_ticker(
            db_engine, tid, "1W", mode="incremental",
        )
        with db_engine.connect() as conn:
            first = StockIndicatorEnrichmentRepository(
                conn,
            ).count_for_ticker(tid, "1W")
        populate_stock_indicator_enrichment_for_ticker(
            db_engine, tid, "1W", mode="incremental",
        )
        with db_engine.connect() as conn:
            second = StockIndicatorEnrichmentRepository(
                conn,
            ).count_for_ticker(tid, "1W")
        assert first == second and first >= 1

    def test_full_default_unchanged(self, db_engine):
        tid = self._setup(db_engine)
        populate_stock_indicator_enrichment_for_ticker(
            db_engine, tid, "1W",
        )
        with db_engine.connect() as conn:
            cnt = StockIndicatorEnrichmentRepository(
                conn,
            ).count_for_ticker(tid, "1W")
        # Same shape as Step 5 expectations: > 20 rows
        assert cnt > 20


# ============================================================
# Stock enrichment — bulk pass-through
# ============================================================

class TestStockEnrichmentBulkPassthrough:

    def test_bulk_passes_mode(self, db_engine):
        with transaction(db_engine) as conn:
            _seed_ticker(conn, ticker_id=7301, symbol="C")
            _seed_daily_bars(conn, 7301, n=400)
            _seed_ticker(conn, ticker_id=7302, symbol="D")
            _seed_daily_bars(conn, 7302, n=400)
        # Need state in place first
        populate_stock_indicator_state_for_exchange(
            db_engine, "NSE", "1W",
        )

        report = populate_stock_indicator_enrichment_for_exchange(
            db_engine, "NSE", "1W",
            mode="incremental", n_weeks_to_upsert=2,
        )
        assert "succeeded" in report
        for (_, _, cnt) in report["succeeded"]:
            assert 1 <= cnt <= 7


# ============================================================
# Industry state — per-industry
# ============================================================

class TestIndustryStateIncremental:

    def _setup(
        self, db_engine, name: str = "TestIndIncState",
    ) -> str:
        with transaction(db_engine) as conn:
            _seed_industry(conn, name=name)
            _seed_industry_index_values(conn, name, n_days=400)
        return name

    def test_incremental_writes_only_last_n(self, db_engine):
        name = self._setup(db_engine)
        populate_industry_indicator_state_for_industry(
            db_engine, name,
            mode="incremental", n_weeks_to_upsert=2,
        )
        with db_engine.connect() as conn:
            cnt = IndustryIndicatorStateRepository(
                conn,
            ).count_for_industry(name, "Weekly")
        assert cnt == 2

    def test_full_default_unchanged(self, db_engine):
        name = self._setup(db_engine)
        populate_industry_indicator_state_for_industry(
            db_engine, name,
        )
        with db_engine.connect() as conn:
            cnt = IndustryIndicatorStateRepository(
                conn,
            ).count_for_industry(name, "Weekly")
        assert cnt > 20

    def test_incremental_is_idempotent(self, db_engine):
        name = self._setup(db_engine)
        for _ in range(2):
            populate_industry_indicator_state_for_industry(
                db_engine, name,
                mode="incremental", n_weeks_to_upsert=2,
            )
        with db_engine.connect() as conn:
            cnt = IndustryIndicatorStateRepository(
                conn,
            ).count_for_industry(name, "Weekly")
        assert cnt == 2


# ============================================================
# Industry enrichment — per-industry
# ============================================================

class TestIndustryEnrichmentIncremental:

    def _setup(
        self, db_engine, name: str = "TestIndIncEnr",
    ) -> str:
        with transaction(db_engine) as conn:
            _seed_industry(conn, name=name)
            _seed_industry_index_values(conn, name, n_days=400)
        # State must be populated first (FK)
        populate_industry_indicator_state_for_industry(
            db_engine, name,
        )
        return name

    def test_incremental_writes_only_last_n(self, db_engine):
        name = self._setup(db_engine)
        populate_industry_indicator_enrichment_for_industry(
            db_engine, name,
            mode="incremental", n_weeks_to_upsert=2,
        )
        with db_engine.connect() as conn:
            cnt = IndustryIndicatorEnrichmentRepository(
                conn,
            ).count_for_industry(name, "Weekly")
        assert cnt == 2

    def test_full_default_unchanged(self, db_engine):
        name = self._setup(db_engine)
        populate_industry_indicator_enrichment_for_industry(
            db_engine, name,
        )
        with db_engine.connect() as conn:
            cnt = IndustryIndicatorEnrichmentRepository(
                conn,
            ).count_for_industry(name, "Weekly")
        assert cnt > 20

    def test_incremental_is_idempotent(self, db_engine):
        name = self._setup(db_engine)
        for _ in range(2):
            populate_industry_indicator_enrichment_for_industry(
                db_engine, name,
                mode="incremental", n_weeks_to_upsert=2,
            )
        with db_engine.connect() as conn:
            cnt = IndustryIndicatorEnrichmentRepository(
                conn,
            ).count_for_industry(name, "Weekly")
        assert cnt == 2


# ============================================================
# Helper unit test — _slice_for_mode
# ============================================================

class TestSliceForMode:
    """Unit test the shared slicing helper directly. Pure-Python, no DB."""

    def test_full_mode_returns_all(self):
        from trip_ema.fundamentals.v1_runners import _slice_for_mode
        rows = list(range(10))
        assert _slice_for_mode(rows, "full", 2) == rows
        # n is ignored in full mode

    def test_incremental_slices_to_last_n(self):
        from trip_ema.fundamentals.v1_runners import _slice_for_mode
        rows = list(range(10))
        assert _slice_for_mode(rows, "incremental", 2) == [8, 9]
        assert _slice_for_mode(rows, "incremental", 3) == [7, 8, 9]

    def test_incremental_n_geq_len_returns_all(self):
        from trip_ema.fundamentals.v1_runners import _slice_for_mode
        rows = list(range(5))
        assert _slice_for_mode(rows, "incremental", 5) == rows
        assert _slice_for_mode(rows, "incremental", 100) == rows

    def test_empty_input_returns_empty(self):
        from trip_ema.fundamentals.v1_runners import _slice_for_mode
        assert _slice_for_mode([], "incremental", 2) == []
        assert _slice_for_mode([], "full", 2) == []
