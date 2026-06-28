"""Integration tests for V1 industry indicator enrichment populator.

Tests assume Step 7's industry-indicator-state runner is available
(the enrichment runner pre-validates that state rows exist). The
state populator is invoked in the setup of each happy-path test so
the FK precondition is satisfied.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, UTC

import pytest
from sqlalchemy import text

from trip_ema.fundamentals.v1_runners import (
    PHASE_V1_INDUSTRY_INDICATOR_ENRICHMENT,
    populate_industry_indicator_enrichment_for_all,
    populate_industry_indicator_enrichment_for_industry,
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
    """Seed `num_days` consecutive daily IIV rows."""
    for i in range(num_days):
        td = start_date + timedelta(days=i)
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


def _setup_with_state(
    db_engine, industry: str, num_days: int = 70, min_bars: int = 5,
) -> None:
    """Seed industry + IIV history + run Step 7 state populator so
    the enrichment runner's FK precondition is satisfied."""
    with transaction(db_engine) as conn:
        _seed_industry(conn, industry)
        _seed_iiv_history(conn, industry, num_days=num_days)
    populate_industry_indicator_state_for_industry(
        db_engine, industry, min_bars=min_bars,
    )


# ============================================================
# Per-industry runner — happy paths
# ============================================================

class TestPopulateIndustryIndicatorEnrichmentForIndustry:

    def test_writes_rows_with_state_already_populated(self, db_engine):
        ind = "HappyPath"
        _setup_with_state(db_engine, ind)

        run_id = populate_industry_indicator_enrichment_for_industry(
            db_engine, ind, min_bars=5,
        )

        with db_engine.connect() as conn:
            run = FundamentalsRunRepository(conn).get(run_id)
            row_count = _count(
                conn, "industry_indicator_enrichment",
                "industry_name = :n", n=ind,
            )
            state_count = _count(
                conn, "industry_indicator_state",
                "industry_name = :n", n=ind,
            )
        assert run.phase == PHASE_V1_INDUSTRY_INDICATOR_ENRICHMENT
        assert run.status == "SUCCESS"
        assert row_count > 0
        # 1:1 with state rows (NaN-skip is identical between
        # compute_state_primitives and compute_enrichment_primitives)
        assert row_count == state_count
        assert run.succeeded == row_count

    def test_idempotent_second_run(self, db_engine):
        ind = "Idemp"
        _setup_with_state(db_engine, ind)

        populate_industry_indicator_enrichment_for_industry(
            db_engine, ind, min_bars=5,
        )
        with db_engine.connect() as conn:
            first_count = _count(
                conn, "industry_indicator_enrichment",
                "industry_name = :n", n=ind,
            )

        populate_industry_indicator_enrichment_for_industry(
            db_engine, ind, min_bars=5,
        )
        with db_engine.connect() as conn:
            second_count = _count(
                conn, "industry_indicator_enrichment",
                "industry_name = :n", n=ind,
            )
        assert second_count == first_count


# ============================================================
# Error paths
# ============================================================

class TestPopulateIndustryIndicatorEnrichmentErrors:

    def test_raises_for_unknown_industry(self, db_engine):
        with pytest.raises(ValueError, match="does not exist"):
            populate_industry_indicator_enrichment_for_industry(
                db_engine, "Never Existed",
            )

    def test_raises_when_state_not_populated(self, db_engine):
        """If Step 7 hasn't been run for this industry, the
        enrichment runner should fail-fast (not wait for FK to
        catch it on insert)."""
        ind = "NoState"
        with transaction(db_engine) as conn:
            _seed_industry(conn, ind)
            _seed_iiv_history(conn, ind, num_days=70)
        # NO state populator run

        with pytest.raises(ValueError, match="no state rows"):
            populate_industry_indicator_enrichment_for_industry(
                db_engine, ind, min_bars=5,
            )

    def test_raises_when_insufficient_bars(self, db_engine):
        ind = "TooShort"
        with transaction(db_engine) as conn:
            _seed_industry(conn, ind)
            # Just enough state to pass the FK precondition (but
            # bars will be too few for min_bars=30).
            _seed_iiv_history(conn, ind, num_days=70)
        populate_industry_indicator_state_for_industry(
            db_engine, ind, min_bars=5,
        )
        with pytest.raises(ValueError, match="weekly bars"):
            populate_industry_indicator_enrichment_for_industry(
                db_engine, ind, min_bars=30,
            )

    def test_rejects_non_weekly_timeframe(self, db_engine):
        ind = "BadTF"
        with transaction(db_engine) as conn:
            _seed_industry(conn, ind)
        with pytest.raises(ValueError, match="Weekly"):
            populate_industry_indicator_enrichment_for_industry(
                db_engine, ind, timeframe="Daily",
            )


# ============================================================
# Side-effect lockdown
# ============================================================

class TestSideEffects:

    def test_writes_only_to_enrichment_and_fundamentals_run(
        self, db_engine,
    ):
        ind = "SideEffects"
        _setup_with_state(db_engine, ind)

        with db_engine.connect() as conn:
            baseline = {
                "stock_indicator_state":
                    _count(conn, "stock_indicator_state"),
                "stock_indicator_enrichment":
                    _count(conn, "stock_indicator_enrichment"),
                "industry_indicator_state":
                    _count(conn, "industry_indicator_state"),
                "industry_index_value":
                    _count(conn, "industry_index_value"),
                # === STEP 15B-FIXUP2-r4-dict: signal_events dict entry removed ===
                "current_signals": _count(conn, "current_signals"),
                "historical_signals": _count(conn, "historical_signals"),
                "indicator_state": _count(conn, "indicator_state"),
            }

        populate_industry_indicator_enrichment_for_industry(
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

class TestPopulateIndustryIndicatorEnrichmentForAll:

    def test_iterates_active_only(self, db_engine):
        good = "Bulk Good"
        retired = "Bulk Retired"
        _setup_with_state(db_engine, good)
        with transaction(db_engine) as conn:
            _seed_industry(conn, retired, status="RETIRED")
            _seed_iiv_history(conn, retired, num_days=70)
        # State populator skips retired? Actually no — state populator
        # validates industry exists, doesn't filter by status. But the
        # BULK enrichment runner filters via list_active.

        report = populate_industry_indicator_enrichment_for_all(
            db_engine, min_bars=5,
        )

        all_names = (
            {n for n, *_ in report["succeeded"]}
            | {n for n, *_ in report["skipped"]}
            | {n for n, *_ in report["failed"]}
        )
        assert good in all_names
        assert retired not in all_names

    def test_per_industry_isolation(self, db_engine):
        """One industry with state → 'succeeded'. One with no state
        → 'skipped'. Both reported, no failures."""
        good = "Bulk Has State"
        no_state = "Bulk No State"
        _setup_with_state(db_engine, good)
        with transaction(db_engine) as conn:
            _seed_industry(conn, no_state)
            _seed_iiv_history(conn, no_state, num_days=70)

        report = populate_industry_indicator_enrichment_for_all(
            db_engine, min_bars=5,
        )

        succeeded_names = {n for n, *_ in report["succeeded"]}
        skipped_names = {n for n, *_ in report["skipped"]}
        failed_names = {n for n, *_ in report["failed"]}

        assert good in succeeded_names
        assert no_state in skipped_names
        assert not (failed_names & {good, no_state})
