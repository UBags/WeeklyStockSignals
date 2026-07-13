"""Integration tests for trip_ema.backfill.pipeline.

Uses real Postgres (db_engine fixture) + seeded synthetic OHLCV
data. The yfinance fetch (Stage 1) is skipped via refetch_bars=False
so tests don't depend on network or YFinanceSource mocking — the
fetcher is tested separately in tests/etl/test_fetch_bars_for_tickers.py.

What's covered:
  - Marker lifecycle: start row inserted, finish_at set on success
    AND on failure (so the live cycle's deferral check re-engages)
  - Unique-active constraint: second concurrent backfill raises
    IntegrityError; --release-lock pattern (release_locks then
    start) succeeds
  - Date normalization: date_from rounds DOWN to its Monday
  - Stage 7+8: historical_signals are deleted then re-inserted with
    correct archived_at (next Monday 16:30 IST)
  - Stage 9: current_signals reflects ONLY the latest-bar flip
    (UPSERT on flip, DELETE on no-flip) — not the latest signal ever
  - Idempotency: re-running with the same args produces the same
    final state (DELETE+INSERT semantics make this trivially true)
  - Partial-ticker scope leaves other tickers' historical/current
    rows untouched
  - Stage failure: BackfillResult.failed_stage set; finished_at
    still written; later stages skipped
"""
from __future__ import annotations

import os
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from trip_ema.backfill.pipeline import backfill, BackfillResult


@pytest.fixture(autouse=True)
def _clear_phase_ledger(db_engine):
    """Each test starts with an empty phase ledger so its first
    backfill() runs every phase (resume=True skips COMPLETE phases, and
    tests share a DB — without this, a phase COMPLETE'd by one test would
    be skipped by the next)."""
    from trip_ema.persistence import transaction as _txn
    with _txn(db_engine) as _c:
        _c.execute(text("DELETE FROM pipeline_phase_status"))
    yield
from trip_ema.persistence import transaction
from trip_ema.persistence.backfill_runs_repo import (
    BackfillRunsRepository,
)
from trip_ema.persistence.current_signals_repo import (
    CurrentSignalsRepository,
)
from trip_ema.persistence.historical_signals_repo import (
    HistoricalSignalsRepository,
)

_IST = ZoneInfo("Asia/Kolkata")


# ============================================================
# Synthetic data helpers
# ============================================================

class _MockConfig:
    """Minimal Config substitute for the pipeline.

    Stage 1 (yfinance) is the only stage that needs config; we
    always pass refetch_bars=False in these tests so config can
    be a stand-in."""
    class etl:
        yfinance_batch_size = 50
        yfinance_concurrency = 4

    class compute:
        stock_state_processes = 1
        """Stage 2 MUST run in-process here.

        These tests monkeypatch
        `trip_ema.backfill.pipeline.populate_stock_state_and_enrichment_for_ticker`
        to a no-op (see the `stub_populators` fixture). With the default of 3,
        stage 2 fans out to spawned child processes which re-import the pipeline
        module CLEAN — the monkeypatch lives in this process's memory and does
        not cross the boundary, so children would call the REAL populator
        against the fixture DB. The stub would be silently defeated and these
        tests would pass while testing something else entirely.

        1 keeps stage 2 in this process, where the patch applies. These are
        orchestration tests; the populator has its own test files.
        """


def _seed_ticker(
    db_engine, *, ticker_id: int, symbol: str,
    exchange: str = "NSE", tier: int = 2,
) -> None:
    with transaction(db_engine) as conn:
        conn.execute(
            text("""
                INSERT INTO tickers (
                    ticker_id, exchange, symbol, tier,
                    is_active, delisted_at
                ) VALUES (
                    :tid, :ex, :sym, :tier, TRUE, NULL
                )
                ON CONFLICT (ticker_id) DO NOTHING
            """),
            {
                "tid": ticker_id, "ex": exchange,
                "sym": symbol, "tier": tier,
            },
        )


def _seed_state_rows(
    db_engine, *, ticker_id: int,
    regime_sequence: list[tuple[date, int]],
) -> None:
    """Insert stock_indicator_state rows for a ticker.

    regime_sequence: list of (monday_date, display_regime) — one per
    weekly bar. The pipeline's Stage 8 + 9 read from this table.
    """
    with transaction(db_engine) as conn:
        for monday, regime in regime_sequence:
            bar_ts = datetime.combine(monday, time(0, 0), tzinfo=UTC)
            conn.execute(
                text("""
                    INSERT INTO stock_indicator_state (
                        ticker_id, timeframe, date, bar_timestamp,
                        med_ema, long_ema, longest_ema, atr,
                        upper_inner_band, lower_inner_band,
                        upper_mid_band, lower_mid_band,
                        upper_outer_band, lower_outer_band,
                        display_regime, computed_at
                    ) VALUES (
                        :tid, '1W', (:ts AT TIME ZONE 'Asia/Kolkata')::date, :ts,
                        100.0, 99.0, 98.0, 2.0,
                        102.0, 96.0, 104.0, 94.0, 106.0, 92.0,
                        :reg, NOW()
                    )
                    ON CONFLICT (ticker_id, timeframe, date)
                    DO UPDATE SET display_regime = EXCLUDED.display_regime
                """),
                {"tid": ticker_id, "ts": bar_ts, "reg": regime},
            )


def _seed_daily_close(
    db_engine, *, ticker_id: int,
    closes: list[tuple[date, float]],
) -> None:
    """Insert ohlcv_bars (daily timeframe) rows so the derive
    function can source price_at_signal."""
    with transaction(db_engine) as conn:
        for d, close in closes:
            bar_ts = datetime.combine(d, time(0, 0), tzinfo=UTC)
            conn.execute(
                text("""
                    INSERT INTO ohlcv_bars (
                        ticker_id, timeframe, bar_timestamp,
                        open, high, low, close, volume
                    ) VALUES (
                        :tid, '1D', :ts,
                        :c, :c, :c, :c, 1000
                    )
                    ON CONFLICT (ticker_id, timeframe, bar_timestamp)
                    DO UPDATE SET close = EXCLUDED.close
                """),
                {"tid": ticker_id, "ts": bar_ts, "c": close},
            )


def _count_active_markers(db_engine) -> int:
    with db_engine.connect() as conn:
        return BackfillRunsRepository(conn).is_running() and 1 or 0


def _hist_signals_for_ticker(db_engine, ticker_id: int):
    with db_engine.connect() as conn:
        return HistoricalSignalsRepository(conn).get_for_ticker(
            ticker_id,
        )


def _curr_signal_for_ticker(db_engine, ticker_id: int):
    with db_engine.connect() as conn:
        return CurrentSignalsRepository(conn).get_for_ticker(
            ticker_id,
        )


# ============================================================
# Mock the populator stages — Step 11b1 tests focus on the
# pipeline orchestration, not the populators (which have their
# own test files). We seed stock_indicator_state directly and
# skip the recompute stages via monkeypatching.
# ============================================================

@pytest.fixture
def stub_populators(monkeypatch):
    """Replace the V1 populator entry points the pipeline calls
    with no-ops so tests focus on orchestration logic. State data
    is pre-seeded; recompute would just overwrite with the same
    values."""
    def _noop_stock_state_and_enrichment(*args, **kwargs):
        # Stages 2 and 3 are ONE engine pass now (the merged runner writes both
        # stock_indicator_state and stock_indicator_enrichment), so a single
        # no-op covers both: stage 3 recomputes nothing.
        return (0, 0)

    def _noop_industry_iiv(*args, **kwargs):
        return {"succeeded": [], "failed": []}

    def _noop_industry_state(*args, **kwargs):
        return {"succeeded": [], "failed": []}

    def _noop_industry_enrichment(*args, **kwargs):
        return {"succeeded": [], "failed": []}

    monkeypatch.setattr(
        "trip_ema.backfill.pipeline."
        "populate_stock_state_and_enrichment_for_ticker",
        _noop_stock_state_and_enrichment,
    )
    monkeypatch.setattr(
        "trip_ema.backfill.pipeline."
        "populate_industry_index_value_for_all",
        _noop_industry_iiv,
    )
    monkeypatch.setattr(
        "trip_ema.orchestrator.industry_stages."
        "populate_industry_indicator_state_for_all",
        _noop_industry_state,
    )
    monkeypatch.setattr(
        "trip_ema.orchestrator.industry_stages."
        "populate_industry_indicator_enrichment_for_all",
        _noop_industry_enrichment,
    )


# ============================================================
# Marker lifecycle
# ============================================================

class TestMarkerLifecycle:

    def test_start_row_inserted_and_finished(
        self, db_engine, stub_populators,
    ):
        _seed_ticker(db_engine, ticker_id=1, symbol="TEST1")
        result = backfill(
            db_engine, _MockConfig(),
            date_from=date(2024, 6, 3),  # Monday
            date_to=date(2024, 6, 3),
            stocks=[1],
            refetch_bars=False,
        )
        assert result.run_id > 0
        # Marker row exists with finished_at set
        with db_engine.connect() as conn:
            row = BackfillRunsRepository(conn).get(result.run_id)
        assert row is not None
        assert row.finished_at is not None
        # Marker no longer active → live cycle can resume
        assert not BackfillRunsRepository(
            db_engine.connect(),
        ).is_running()

    def test_marker_finished_even_on_stage_failure(
        self, db_engine, monkeypatch,
    ):
        _seed_ticker(db_engine, ticker_id=1, symbol="TEST1")

        # Make stage 3d (industry index values) raise — NO per-ticker isolation
        # so the exception propagates and triggers the failed_stage
        # path. Stages 2+3 have per-ticker isolation by design;
        # failures there land in stock_state_failed[] without
        # aborting the pipeline.
        def _boom(*args, **kwargs):
            raise RuntimeError("simulated stage_4 failure")

        monkeypatch.setattr(
            "trip_ema.backfill.pipeline."
            "populate_industry_index_value_for_all",
            _boom,
        )
        # Earlier stages no-op so we reach stage 3d cleanly. Stages 2 and 3
        # are ONE engine pass now (the merged runner writes both
        # stock_indicator_state and stock_indicator_enrichment), so one no-op
        # covers both.
        monkeypatch.setattr(
            "trip_ema.backfill.pipeline."
            "populate_stock_state_and_enrichment_for_ticker",
            lambda *a, **k: (0, 0),
        )

        result = backfill(
            db_engine, _MockConfig(),
            date_from=date(2024, 6, 3),
            stocks=[1],
            refetch_bars=False,
        )
        # Pipeline did NOT crash — it caught the exception
        # internally, marked finished, and returned a result
        assert result.failed_stage is not None
        assert "industry_index_values" in result.failed_stage
        assert "simulated stage_4 failure" in result.error_message
        # Marker is finished — live cycle can resume
        with db_engine.connect() as conn:
            row = BackfillRunsRepository(conn).get(result.run_id)
            assert row.finished_at is not None
            assert not BackfillRunsRepository(conn).is_running()

    def test_concurrent_start_raises_integrity_error(
        self, db_engine, stub_populators,
    ):
        _seed_ticker(db_engine, ticker_id=1, symbol="TEST1")
        # Manually leave an orphan marker
        with transaction(db_engine) as conn:
            BackfillRunsRepository(conn).start(
                pid=os.getpid(),
                exchange="NSE",
                date_from=date(2024, 1, 1),
                date_to=date(2024, 12, 31),
                refetch_bars=False,
                ticker_scope="ALL",
            )
        # Second backfill attempt MUST fail
        with pytest.raises(IntegrityError):
            backfill(
                db_engine, _MockConfig(),
                date_from=date(2024, 6, 3),
                stocks=[1],
                refetch_bars=False,
            )


# ============================================================
# Date normalization
# ============================================================

class TestDateNormalization:

    def test_date_from_rounds_to_monday(
        self, db_engine, stub_populators,
    ):
        _seed_ticker(db_engine, ticker_id=1, symbol="TEST1")
        # Wednesday June 5 2024 → Monday June 3
        result = backfill(
            db_engine, _MockConfig(),
            date_from=date(2024, 6, 5),
            date_to=date(2024, 6, 5),
            stocks=[1],
            refetch_bars=False,
        )
        assert result.from_monday == date(2024, 6, 3)

    def test_date_to_defaults_to_today(
        self, db_engine, stub_populators,
    ):
        _seed_ticker(db_engine, ticker_id=1, symbol="TEST1")
        result = backfill(
            db_engine, _MockConfig(),
            date_from=date(2024, 6, 3),
            # date_to omitted
            stocks=[1],
            refetch_bars=False,
        )
        assert result.date_to == date.today()

    def test_date_from_after_date_to_raises(
        self, db_engine, stub_populators,
    ):
        _seed_ticker(db_engine, ticker_id=1, symbol="TEST1")
        with pytest.raises(ValueError, match="date_from"):
            backfill(
                db_engine, _MockConfig(),
                date_from=date(2024, 6, 10),
                date_to=date(2024, 6, 3),
                stocks=[1],
                refetch_bars=False,
            )


# ============================================================
# Ticker scope resolution
# ============================================================

class TestTickerScopeResolution:

    def test_all_scope_picks_up_active_only(
        self, db_engine, stub_populators,
    ):
        _seed_ticker(db_engine, ticker_id=1, symbol="ACTIVE1")
        _seed_ticker(db_engine, ticker_id=2, symbol="ACTIVE2")
        # Mark one inactive
        with transaction(db_engine) as conn:
            conn.execute(text(
                "UPDATE tickers SET is_active=FALSE WHERE ticker_id=2"
            ))
        result = backfill(
            db_engine, _MockConfig(),
            date_from=date(2024, 6, 3),
            date_to=date(2024, 6, 3),
            stocks=None,  # ALL
            refetch_bars=False,
        )
        assert result.ticker_scope_size == 1

    def test_partial_scope_validates_existence(
        self, db_engine, stub_populators,
    ):
        _seed_ticker(db_engine, ticker_id=1, symbol="EXISTS")
        # Request a non-existent ticker_id
        with pytest.raises(ValueError, match="not found"):
            backfill(
                db_engine, _MockConfig(),
                date_from=date(2024, 6, 3),
                date_to=date(2024, 6, 3),
                stocks=[999],
                refetch_bars=False,
            )

    def test_ticker_scope_label_set_correctly(
        self, db_engine, stub_populators,
    ):
        _seed_ticker(db_engine, ticker_id=1, symbol="T1")
        result = backfill(
            db_engine, _MockConfig(),
            date_from=date(2024, 6, 3),
            date_to=date(2024, 6, 3),
            stocks=None,
            refetch_bars=False,
        )
        assert result.ticker_scope_label == "ALL"

        # Second backfill with partial scope (after first finishes)
        result2 = backfill(
            db_engine, _MockConfig(),
            date_from=date(2024, 6, 3),
            date_to=date(2024, 6, 3),
            stocks=[1],
            refetch_bars=False,
            resume=False,  # 2nd run over same range — redo, don't skip
        )
        assert result2.ticker_scope_label == "1"


# ============================================================
# Historical signal emission (Stages 7 + 8)
# ============================================================

class TestHistoricalSignals:

    def test_no_emissions_when_state_flat(
        self, db_engine, stub_populators,
    ):
        """Flat regime (no flips) → no historical_signals."""
        _seed_ticker(db_engine, ticker_id=1, symbol="FLAT")
        # 4 weeks all regime=1 (BULLISH)
        _seed_state_rows(db_engine, ticker_id=1, regime_sequence=[
            (date(2024, 5, 27), 1),
            (date(2024, 6, 3), 1),
            (date(2024, 6, 10), 1),
            (date(2024, 6, 17), 1),
        ])
        _seed_daily_close(db_engine, ticker_id=1, closes=[
            (date(2024, 6, 21), 100.0),
        ])
        result = backfill(
            db_engine, _MockConfig(),
            date_from=date(2024, 6, 3),
            date_to=date(2024, 6, 17),
            stocks=[1],
            refetch_bars=False,
        )
        assert result.failed_stage is None
        assert result.historical_signals_inserted == 0
        assert _hist_signals_for_ticker(db_engine, 1) == []

    def test_emits_signals_at_each_flip(
        self, db_engine, stub_populators,
    ):
        """4 regime flips across 5 weeks → 4 historical_signals rows."""
        _seed_ticker(db_engine, ticker_id=1, symbol="FLIPPY")
        _seed_state_rows(db_engine, ticker_id=1, regime_sequence=[
            (date(2024, 5, 27), 1),    # bullish
            (date(2024, 6, 3), -1),    # → bearish    (flip)
            (date(2024, 6, 10), 1),    # → bullish    (flip)
            (date(2024, 6, 17), -1),   # → bearish    (flip)
            (date(2024, 6, 24), 1),    # → bullish    (flip)
        ])
        # Daily close for every Monday + the date_to
        _seed_daily_close(db_engine, ticker_id=1, closes=[
            (date(2024, 6, 3), 100.0),
            (date(2024, 6, 10), 101.0),
            (date(2024, 6, 17), 99.0),
            (date(2024, 6, 24), 102.0),
            (date(2024, 6, 28), 102.0),
        ])
        result = backfill(
            db_engine, _MockConfig(),
            date_from=date(2024, 6, 3),
            date_to=date(2024, 6, 24),
            stocks=[1],
            refetch_bars=False,
        )
        assert result.failed_stage is None
        # 4 flips at Mondays 6/3, 6/10, 6/17, 6/24
        assert result.historical_signals_inserted == 4
        hist = _hist_signals_for_ticker(db_engine, 1)
        assert len(hist) == 4
        # Sanity: all rows belong to this ticker, all are
        # regime-change types
        assert all(h.ticker_id == 1 for h in hist)

    def test_archived_at_is_set(
        self, db_engine, stub_populators,
    ):
        """Backfilled historical rows have archived_at set. Rotation was
        removed, so it defaults to NOW() (no per-row next-Monday stamp)."""
        _seed_ticker(db_engine, ticker_id=1, symbol="ARCH")
        _seed_state_rows(db_engine, ticker_id=1, regime_sequence=[
            (date(2024, 6, 3), 1),
            (date(2024, 6, 10), -1),   # flip at 6/10
        ])
        _seed_daily_close(db_engine, ticker_id=1, closes=[
            (date(2024, 6, 10), 100.0),
        ])
        backfill(
            db_engine, _MockConfig(),
            date_from=date(2024, 6, 10),
            date_to=date(2024, 6, 10),
            stocks=[1],
            refetch_bars=False,
        )
        with db_engine.connect() as conn:
            row = conn.execute(text("""
                SELECT archived_at FROM historical_signals
                 WHERE ticker_id = 1
                 ORDER BY computed_at DESC LIMIT 1
            """)).first()
        # Rotation removed: archived_at defaults to NOW() on backfilled rows.
        assert row.archived_at is not None

    def test_idempotent_rerun_produces_same_state(
        self, db_engine, stub_populators,
    ):
        """Re-running the backfill over the same range deletes the
        old historical rows and re-inserts. Final state is
        deterministic."""
        _seed_ticker(db_engine, ticker_id=1, symbol="REPEAT")
        _seed_state_rows(db_engine, ticker_id=1, regime_sequence=[
            (date(2024, 6, 3), 1),
            (date(2024, 6, 10), -1),
        ])
        _seed_daily_close(db_engine, ticker_id=1, closes=[
            (date(2024, 6, 10), 100.0),
        ])
        result_a = backfill(
            db_engine, _MockConfig(),
            date_from=date(2024, 6, 10),
            date_to=date(2024, 6, 10),
            stocks=[1],
            refetch_bars=False,
        )
        hist_a = _hist_signals_for_ticker(db_engine, 1)

        result_b = backfill(
            db_engine, _MockConfig(),
            date_from=date(2024, 6, 10),
            date_to=date(2024, 6, 10),
            stocks=[1],
            refetch_bars=False,
            resume=False,  # redo work — this tests idempotent REDO
        )
        hist_b = _hist_signals_for_ticker(db_engine, 1)

        # Same count, same regime/ticker values
        assert len(hist_a) == len(hist_b)
        assert result_a.historical_signals_inserted == \
            result_b.historical_signals_inserted
        for a, b in zip(hist_a, hist_b):
            assert a.ticker_id == b.ticker_id
            assert a.regime_before == b.regime_before
            assert a.regime_after == b.regime_after


# ============================================================
# Stage 9: current_signals semantics
# ============================================================

class TestCurrentSignalsRebuild:

    def test_historical_backfill_leaves_current_untouched_on_flip(
        self, db_engine, stub_populators,
    ):
        """Per-day model: current_signals is written only when a walked
        day == today. A historical backfill leaves current untouched even
        with a flip at the range end."""
        _seed_ticker(db_engine, ticker_id=1, symbol="FLIPNOW")
        _seed_state_rows(db_engine, ticker_id=1, regime_sequence=[
            (date(2024, 6, 3), 1),
            (date(2024, 6, 10), -1),    # flip at 6/10
        ])
        _seed_daily_close(db_engine, ticker_id=1, closes=[
            (date(2024, 6, 10), 100.0),
        ])
        result = backfill(
            db_engine, _MockConfig(),
            date_from=date(2024, 6, 10),
            date_to=date(2024, 6, 10),
            stocks=[1],
            refetch_bars=False,
        )
        assert result.current_signals_upserted == 0
        cur = _curr_signal_for_ticker(db_engine, 1)
        assert cur is None

    def test_historical_backfill_leaves_existing_current_untouched(
        self, db_engine, stub_populators,
    ):
        """Per-day model: a historical backfill does not touch
        current_signals, so a pre-seeded stale row is left in place."""
        _seed_ticker(db_engine, ticker_id=1, symbol="NOFLIP")
        # Seed an existing current_signals row that the backfill
        # should DELETE (since the latest bar shows no flip).
        _seed_state_rows(db_engine, ticker_id=1, regime_sequence=[
            (date(2024, 6, 3), 1),
            (date(2024, 6, 10), 1),    # NO flip
        ])
        _seed_daily_close(db_engine, ticker_id=1, closes=[
            (date(2024, 6, 10), 100.0),
        ])
        # Insert a stale current_signals row by hand
        with transaction(db_engine) as conn:
            conn.execute(text("""
                INSERT INTO current_signals (
                    ticker_id, timeframe, bar_timestamp, signal_type,
                    regime_before, regime_after, price_at_signal,
                    computed_at
                ) VALUES (
                    1, '1W', :ts, 'BUY',
                    -1, 1, 100.0, NOW()
                )
            """), {"ts": datetime(
                2024, 5, 27, 16, 30, tzinfo=_IST,
            )})

        result = backfill(
            db_engine, _MockConfig(),
            date_from=date(2024, 6, 10),
            date_to=date(2024, 6, 10),
            stocks=[1],
            refetch_bars=False,
        )
        assert result.current_signals_upserted == 0
        assert result.current_signals_deleted == 0
        assert _curr_signal_for_ticker(db_engine, 1) is not None


# ============================================================
# Partial-ticker scope isolation
# ============================================================

class TestPartialScopeIsolation:

    def test_partial_scope_leaves_other_tickers_untouched(
        self, db_engine, stub_populators,
    ):
        """Backfilling ticker 1 must not touch ticker 2's historical
        or current signal rows."""
        _seed_ticker(db_engine, ticker_id=1, symbol="SCOPED")
        _seed_ticker(db_engine, ticker_id=2, symbol="OUTSIDE")

        # Seed historical_signals for ticker 2 — must survive
        with transaction(db_engine) as conn:
            conn.execute(text("""
                INSERT INTO historical_signals (
                    ticker_id, timeframe, bar_timestamp, signal_type,
                    regime_before, regime_after, price_at_signal,
                    computed_at, archived_at, signal_date
                ) VALUES (
                    2, '1W', :bt, 'BUY',
                    -1, 1, 99.0, :ca, NOW(), :sd
                )
            """), {
                "bt": datetime(2024, 6, 3, 0, 0, tzinfo=UTC),
                "ca": datetime(2024, 6, 3, 16, 30, tzinfo=_IST),
                "sd": datetime(2024, 6, 3, 0, 0, tzinfo=UTC).date(),
            })

        _seed_state_rows(db_engine, ticker_id=1, regime_sequence=[
            (date(2024, 6, 3), 1),
            (date(2024, 6, 10), -1),
        ])
        _seed_daily_close(db_engine, ticker_id=1, closes=[
            (date(2024, 6, 10), 100.0),
        ])

        result = backfill(
            db_engine, _MockConfig(),
            date_from=date(2024, 6, 10),
            date_to=date(2024, 6, 10),
            stocks=[1],
            refetch_bars=False,
        )
        assert result.failed_stage is None
        # Ticker 2's historical row is untouched
        hist_2 = _hist_signals_for_ticker(db_engine, 2)
        assert len(hist_2) == 1


# ============================================================
# refetch_bars=False short-circuits Stage 1
# ============================================================

class TestRefetchBarsFlag:

    def test_refetch_false_sets_bars_fetch_run_id_none(
        self, db_engine, stub_populators,
    ):
        _seed_ticker(db_engine, ticker_id=1, symbol="NOFETCH")
        result = backfill(
            db_engine, _MockConfig(),
            date_from=date(2024, 6, 3),
            date_to=date(2024, 6, 3),
            stocks=[1],
            refetch_bars=False,
        )
        assert result.bars_fetch_run_id is None
        assert result.refetch_bars is False
