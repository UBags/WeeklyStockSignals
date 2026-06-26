"""Integration tests for V1 industry indicator state populator.

Covers per-industry + bulk runners, error paths, idempotency, and
side-effect lockdown.

Tests seed industry_index_value rows directly and let the runner do
the daily→weekly aggregation. Most tests use a small min_bars to
keep seed sizes manageable (real prod runs with min_bars=30).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, UTC

import pytest
from sqlalchemy import text

from trip_ema.fundamentals.v1_runners import (
    PHASE_V1_INDUSTRY_INDICATOR_STATE,
    populate_industry_indicator_state_for_all,
    populate_industry_indicator_state_for_industry,
)
from trip_ema.persistence import transaction
from trip_ema.persistence.fundamentals_repo import (
    FundamentalsRunRepository,
)


# ============================================================
# Helpers
# ============================================================

def _seed_industry(
    conn, industry_name: str,
    *, status: str = "ACTIVE",
    base_date: date = date(2024, 1, 1),
) -> str:
    conn.execute(text("""
        INSERT INTO industry_index (
            industry_name, sector_name, methodology,
            base_value, base_date, rebalance_schedule,
            min_constituents, status
        ) VALUES (
            :n, 'Technology', 'MODIFIED_LASPEYRES',
            1000.0, :bd, 'SEMIANNUAL_APR_OCT', 10, :st
        )
        ON CONFLICT (industry_name) DO NOTHING
    """), {"n": industry_name, "bd": base_date, "st": status})
    return industry_name


def _seed_iiv_history(
    conn, industry_name: str,
    *,
    num_days: int,
    start_date: date = date(2024, 1, 1),
    base_value: float = 1000.0,
    drift_per_day: float = 5.0,
) -> None:
    """Seed `num_days` consecutive daily IIV rows. Values drift
    slowly to ensure non-trivial EMAs (constant values would
    produce ATR=0 which is allowed but uninteresting)."""
    for i in range(num_days):
        td = start_date + timedelta(days=i)
        # Smooth drift with slight oscillation so weekly H/L differ
        v = base_value + i * drift_per_day + (i % 3) * 2.0
        conn.execute(text("""
            INSERT INTO industry_index_value (
                industry_name, trade_date, value,
                constituents_used, constituents_missing,
                renormalized, effective_rebalance_date
            ) VALUES (
                :ind, :td, :v, 10, 0, FALSE, :ed
            )
            ON CONFLICT (industry_name, trade_date) DO NOTHING
        """), {"ind": industry_name, "td": td, "v": v,
               "ed": start_date})


def _count(conn, table: str, where: str = "1=1", **params) -> int:
    return conn.execute(
        text(f"SELECT COUNT(*) FROM {table} WHERE {where}"),
        params,
    ).scalar_one()


# ============================================================
# Per-industry runner — happy paths
# ============================================================

class TestPopulateIndustryIndicatorStateForIndustry:

    def test_writes_rows_for_industry_with_enough_history(
        self, db_engine,
    ):
        """End-to-end: 60+ daily IIV rows → ~9 weekly bars; with
        min_bars=5, we should get at least a few state rows."""
        ind = "HappyPath"
        with transaction(db_engine) as conn:
            _seed_industry(conn, ind)
            _seed_iiv_history(conn, ind, num_days=70)

        run_id = populate_industry_indicator_state_for_industry(
            db_engine, ind, min_bars=5,
        )

        with db_engine.connect() as conn:
            run = FundamentalsRunRepository(conn).get(run_id)
            row_count = _count(
                conn, "industry_indicator_state",
                "industry_name = :n", n=ind,
            )
        assert run.phase == PHASE_V1_INDUSTRY_INDICATOR_STATE
        assert run.status == "SUCCESS"
        assert row_count > 0
        assert run.succeeded == row_count

    def test_idempotent_second_run(self, db_engine):
        """Second run overwrites; row count unchanged."""
        ind = "Idemp"
        with transaction(db_engine) as conn:
            _seed_industry(conn, ind)
            _seed_iiv_history(conn, ind, num_days=70)

        populate_industry_indicator_state_for_industry(
            db_engine, ind, min_bars=5,
        )
        with db_engine.connect() as conn:
            first_count = _count(
                conn, "industry_indicator_state",
                "industry_name = :n", n=ind,
            )

        populate_industry_indicator_state_for_industry(
            db_engine, ind, min_bars=5,
        )
        with db_engine.connect() as conn:
            second_count = _count(
                conn, "industry_indicator_state",
                "industry_name = :n", n=ind,
            )
        assert second_count == first_count

    def test_bar_timestamps_are_monday_aligned(self, db_engine):
        """IST Monday-anchored weekly aggregation: every
        bar_timestamp should be a Monday in IST."""
        from zoneinfo import ZoneInfo
        ind = "MondayCheck"
        with transaction(db_engine) as conn:
            _seed_industry(conn, ind)
            _seed_iiv_history(conn, ind, num_days=70)

        populate_industry_indicator_state_for_industry(
            db_engine, ind, min_bars=5,
        )

        with db_engine.connect() as conn:
            timestamps = [
                r[0] for r in conn.execute(text("""
                    SELECT bar_timestamp
                      FROM industry_indicator_state
                     WHERE industry_name = :n
                """), {"n": ind}).fetchall()
            ]
        ist = ZoneInfo("Asia/Kolkata")
        for ts in timestamps:
            ist_local = ts.astimezone(ist)
            # weekday(): Monday=0
            assert ist_local.weekday() == 0, (
                f"bar_timestamp {ts} is not IST-Monday "
                f"(IST local weekday={ist_local.weekday()})"
            )


# ============================================================
# Error paths
# ============================================================

class TestPopulateIndustryIndicatorStateErrors:

    def test_raises_for_unknown_industry(self, db_engine):
        with pytest.raises(ValueError, match="does not exist"):
            populate_industry_indicator_state_for_industry(
                db_engine, "Never Existed",
            )

    def test_raises_for_industry_with_no_iiv_rows(self, db_engine):
        ind = "Empty"
        with transaction(db_engine) as conn:
            _seed_industry(conn, ind)
        with pytest.raises(ValueError, match="no value rows"):
            populate_industry_indicator_state_for_industry(
                db_engine, ind,
            )

    def test_raises_when_insufficient_bars(self, db_engine):
        """A handful of daily IIV rows → 1-2 weekly bars, fewer
        than min_bars."""
        ind = "TooShort"
        with transaction(db_engine) as conn:
            _seed_industry(conn, ind)
            # 10 days = ~2 weekly bars
            _seed_iiv_history(conn, ind, num_days=10)
        with pytest.raises(ValueError, match="weekly bars"):
            populate_industry_indicator_state_for_industry(
                db_engine, ind, min_bars=30,
            )

    def test_rejects_non_weekly_timeframe(self, db_engine):
        ind = "BadTF"
        with transaction(db_engine) as conn:
            _seed_industry(conn, ind)
            _seed_iiv_history(conn, ind, num_days=10)
        with pytest.raises(ValueError, match="Weekly"):
            populate_industry_indicator_state_for_industry(
                db_engine, ind, timeframe="Daily",
            )


# ============================================================
# Side-effect lockdown
# ============================================================

class TestSideEffects:

    def test_writes_only_to_state_and_fundamentals_run(self, db_engine):
        ind = "SideEffects"
        with transaction(db_engine) as conn:
            _seed_industry(conn, ind)
            _seed_iiv_history(conn, ind, num_days=70)

        with db_engine.connect() as conn:
            baseline = {
                "stock_indicator_state":
                    _count(conn, "stock_indicator_state"),
                "stock_indicator_enrichment":
                    _count(conn, "stock_indicator_enrichment"),
                "industry_indicator_enrichment":
                    _count(conn, "industry_indicator_enrichment"),
                "industry_index_value":
                    _count(conn, "industry_index_value"),
                "signal_events": _count(conn, "signal_events"),
                "current_signals": _count(conn, "current_signals"),
                "historical_signals": _count(conn, "historical_signals"),
                "indicator_state": _count(conn, "indicator_state"),
            }

        populate_industry_indicator_state_for_industry(
            db_engine, ind, min_bars=5,
        )

        with db_engine.connect() as conn:
            for table, baseline_count in baseline.items():
                actual = _count(conn, table)
                assert actual == baseline_count, (
                    f"runner unexpectedly wrote to {table}: "
                    f"baseline={baseline_count} after={actual}"
                )


# ============================================================
# Bulk runner
# ============================================================

class TestPopulateIndustryIndicatorStateForAll:

    def test_iterates_active_only(self, db_engine):
        good = "Bulk Good"
        retired = "Bulk Retired"
        with transaction(db_engine) as conn:
            _seed_industry(conn, good)
            _seed_industry(conn, retired, status="RETIRED")
            _seed_iiv_history(conn, good, num_days=70)
            _seed_iiv_history(conn, retired, num_days=70)

        report = populate_industry_indicator_state_for_all(
            db_engine, min_bars=5,
        )

        all_names = (
            {n for n, *_ in report["succeeded"]}
            | {n for n, *_ in report["skipped"]}
            | {n for n, *_ in report["failed"]}
        )
        assert good in all_names
        # RETIRED industries are filtered out by list_active()
        assert retired not in all_names

    def test_per_industry_isolation(self, db_engine):
        """One industry with insufficient history → 'skipped'.
        Another with enough → 'succeeded'. Both reported."""
        good = "Bulk Has Data"
        short = "Bulk Too Short"
        empty = "Bulk No IIV"
        with transaction(db_engine) as conn:
            _seed_industry(conn, good)
            _seed_industry(conn, short)
            _seed_industry(conn, empty)
            _seed_iiv_history(conn, good, num_days=70)
            _seed_iiv_history(conn, short, num_days=5)

        report = populate_industry_indicator_state_for_all(
            db_engine, min_bars=5,
        )

        succeeded_names = {n for n, *_ in report["succeeded"]}
        skipped_names = {n for n, *_ in report["skipped"]}
        failed_names = {n for n, *_ in report["failed"]}

        assert good in succeeded_names
        # Short and empty both raise ValueError → "skipped"
        assert short in skipped_names
        assert empty in skipped_names
        # No genuine failures
        assert not (failed_names & {good, short, empty})
