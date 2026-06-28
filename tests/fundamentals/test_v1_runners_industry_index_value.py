"""Integration tests for V1 industry index value populator.

Covers per-industry + bulk runners, resume semantics, error paths,
and side-effect lockdown.

Most tests run with NO constituents seeded — the runner's behavior
(walking dates, opening + closing fundamentals_run, skipping when
compute returns None) is the focus. One happy-path test seeds full
constituent data to verify values actually land in
industry_index_value.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone, UTC

import pytest
from sqlalchemy import text

from trip_ema.domain import TickerRef
from trip_ema.fundamentals.v1_runners import (
    PHASE_V1_INDUSTRY_INDEX_VALUE,
    populate_industry_index_value_for_all,
    populate_industry_index_value_for_industry,
)
from trip_ema.persistence import transaction
from trip_ema.persistence.fundamentals_repo import (
    FundamentalsRunRepository,
    IndustryIndexConstituentRepository,
    IndustryIndexRepository,
    IndustryIndexValueRepository,
)
from trip_ema.persistence.tickers_repo import TickersRepository


# ============================================================
# Helpers
# ============================================================

def _seed_industry(
    conn, industry_name: str,
    *, base_date: date = date(2025, 10, 1), status: str = "ACTIVE",
) -> str:
    """Insert one industry_index row directly (not via repo.upsert
    which has methodology/schedule validation we don't want to mock
    out for these tests)."""
    conn.execute(text("""
        INSERT INTO industry_index (
            industry_name, sector_name, methodology,
            base_value, base_date, rebalance_schedule,
            min_constituents, status
        ) VALUES (
            :n, 'Technology', 'MODIFIED_LASPEYRES',
            1000.0, :bd, 'SEMIANNUAL_APR_OCT',
            10, :st
        )
        ON CONFLICT (industry_name) DO NOTHING
    """), {"n": industry_name, "bd": base_date, "st": status})
    return industry_name


def _seed_mcap_day(
    conn, *,
    industry_name: str | None = None,
    trade_date: date,
    n_synthetic_tickers: int = 1,
) -> None:
    """Insert synthetic daily_market_cap rows so that
    _list_trading_days_in_range picks up this date.

    We use a placeholder ticker_id (large negative IDs) that doesn't
    collide with any real test ticker. The compute layer ignores
    these rows during constituent lookups because they aren't
    referenced by any industry_index_constituent.
    """
    placeholder_tid = -(hash((trade_date, industry_name)) % 10_000)
    # First ensure the placeholder ticker exists (FK from
    # daily_market_cap).
    conn.execute(text("""
        INSERT INTO tickers (ticker_id, exchange, symbol)
        VALUES (:id, 'NSE', :sym)
        ON CONFLICT (ticker_id) DO NOTHING
    """), {"id": placeholder_tid, "sym": f"PLACE{abs(placeholder_tid)}"})
    conn.execute(text("""
        INSERT INTO daily_market_cap (
            ticker_id, trade_date, close_price, shares_used,
            market_cap, source
        ) VALUES (:tid, :td, 100.0, 1000000, 100000000.0, 'COMPUTED')
        ON CONFLICT (ticker_id, trade_date) DO NOTHING
    """), {"tid": placeholder_tid, "td": trade_date})


def _seed_dates(conn, dates: list[date]) -> None:
    """Seed multiple trading days into daily_market_cap so the
    runner has a date_list to walk."""
    for d in dates:
        _seed_mcap_day(conn, trade_date=d)


def _seed_full_industry_with_constituent(
    conn, industry_name: str,
    *, rebalance_date: date, value_date: date,
) -> int:
    """Seed an industry that compute_index_value_for_date can
    actually compute a value for: one ticker, classified as a
    constituent, with mcap on both rebalance_date and value_date.

    Returns the ticker_id of the constituent.
    """
    # 1. The ticker.
    tid = TickersRepository(conn).insert(
        TickerRef("NSE", "TESTCO", "EQ"),
        created_at=datetime(2024, 1, 1, tzinfo=UTC),
    )
    # 2. Index.
    _seed_industry(conn, industry_name, base_date=rebalance_date)
    # 3. Constituent (full weight = 1.0).
    IndustryIndexConstituentRepository(conn).insert_constituent(
        industry_name,
        ticker_id=tid,
        rebalance_date=rebalance_date,
        weight_at_rebalance=1.0,
        market_cap_at_rebalance=1.0e10,
        close_at_rebalance=100.0,
    )
    # 4. mcap on both dates (rebalance + value_date).
    for d, close in [(rebalance_date, 100.0), (value_date, 110.0)]:
        conn.execute(text("""
            INSERT INTO daily_market_cap (
                ticker_id, trade_date, close_price,
                shares_used, market_cap, source
            ) VALUES (:tid, :td, :c, 100000000, :mc, 'COMPUTED')
            ON CONFLICT (ticker_id, trade_date) DO NOTHING
        """), {"tid": tid, "td": d, "c": close,
               "mc": close * 100000000})
    return tid


def _count(conn, table: str, where: str = "1=1", **params) -> int:
    return conn.execute(
        text(f"SELECT COUNT(*) FROM {table} WHERE {where}"),
        params,
    ).scalar_one()


def _get_run(conn, run_id: int):
    return FundamentalsRunRepository(conn).get(run_id)


# ============================================================
# Per-industry runner — happy paths
# ============================================================

class TestPopulateIndustryIndexValueForIndustry:

    def test_no_trading_days_returns_noop_success(self, db_engine):
        """Empty date range → opens + closes a zero-row SUCCESS run."""
        industry = "Empty Industry"
        with transaction(db_engine) as conn:
            _seed_industry(conn, industry, base_date=date(2030, 1, 1))

        run_id = populate_industry_index_value_for_industry(
            db_engine, industry,
            from_date=date(2030, 1, 1), to_date=date(2030, 1, 5),
        )

        with db_engine.connect() as conn:
            run = _get_run(conn, run_id)
        assert run.phase == PHASE_V1_INDUSTRY_INDEX_VALUE
        assert run.status == "SUCCESS"
        assert run.succeeded == 0
        assert run.failed == 0
        assert run.skipped == 0
        assert run.notes is not None
        assert "no trading days" in run.notes.lower()

    def test_runs_full_range_skipping_when_no_constituents(
        self, db_engine,
    ):
        """When the industry has no constituents seeded,
        compute_index_value_for_date returns None for every date.
        The runner should walk the full range and classify them all
        as 'skipped', not 'failed'."""
        industry = "No-Constituents Industry"
        d0 = date(2025, 10, 1)
        d1 = date(2025, 10, 2)
        d2 = date(2025, 10, 3)
        with transaction(db_engine) as conn:
            _seed_industry(conn, industry, base_date=d0)
            _seed_dates(conn, [d0, d1, d2])

        run_id = populate_industry_index_value_for_industry(
            db_engine, industry, from_date=d0, to_date=d2,
        )

        with db_engine.connect() as conn:
            run = _get_run(conn, run_id)
        assert run.status == "SUCCESS"
        assert run.skipped == 3
        assert run.succeeded == 0
        assert run.failed == 0

    def test_real_constituents_writes_index_value_row(
        self, db_engine,
    ):
        """End-to-end with a full constituent setup: at least one
        industry_index_value row lands."""
        industry = "Full Setup Industry"
        rd = date(2025, 10, 1)
        td = date(2025, 10, 2)

        with transaction(db_engine) as conn:
            _seed_full_industry_with_constituent(
                conn, industry,
                rebalance_date=rd, value_date=td,
            )

        baseline = 0
        with db_engine.connect() as conn:
            baseline = _count(
                conn, "industry_index_value",
                "industry_name = :n", n=industry,
            )

        run_id = populate_industry_index_value_for_industry(
            db_engine, industry, from_date=rd, to_date=td,
        )

        with db_engine.connect() as conn:
            run = _get_run(conn, run_id)
            new_count = _count(
                conn, "industry_index_value",
                "industry_name = :n", n=industry,
            )
        assert run.status == "SUCCESS"
        # Both rd and td should have produced values.
        assert new_count > baseline
        assert run.succeeded >= 1

    def test_idempotent_second_run_overwrites(self, db_engine):
        """upsert_value is idempotent; second call doesn't add rows."""
        industry = "Idempotent Industry"
        rd = date(2025, 10, 1)
        td = date(2025, 10, 2)
        with transaction(db_engine) as conn:
            _seed_full_industry_with_constituent(
                conn, industry,
                rebalance_date=rd, value_date=td,
            )

        populate_industry_index_value_for_industry(
            db_engine, industry, from_date=rd, to_date=td,
        )
        with db_engine.connect() as conn:
            first_count = _count(
                conn, "industry_index_value",
                "industry_name = :n", n=industry,
            )

        populate_industry_index_value_for_industry(
            db_engine, industry, from_date=rd, to_date=td,
        )
        with db_engine.connect() as conn:
            second_count = _count(
                conn, "industry_index_value",
                "industry_name = :n", n=industry,
            )
        assert second_count == first_count


# ============================================================
# Per-industry runner — error paths
# ============================================================

class TestPopulateIndustryIndexValueErrors:

    def test_raises_for_nonexistent_industry(self, db_engine):
        with pytest.raises(ValueError, match="does not exist"):
            populate_industry_index_value_for_industry(
                db_engine, "Never Existed",
                from_date=date(2025, 10, 1),
                to_date=date(2025, 10, 5),
            )

    def test_raises_when_from_date_after_to_date(self, db_engine):
        industry = "Inverted Range"
        with transaction(db_engine) as conn:
            _seed_industry(conn, industry)
        with pytest.raises(ValueError, match="from_date"):
            populate_industry_index_value_for_industry(
                db_engine, industry,
                from_date=date(2025, 10, 5),
                to_date=date(2025, 10, 1),
            )


# ============================================================
# Resume semantics
# ============================================================

class TestPopulateIndustryIndexValueResume:

    def _seed_partial_run(
        self, db_engine, *,
        industry_name: str,
        date_list: list[date],
        cursor_index: int,
        succeeded: int = 0, failed: int = 0, skipped: int = 0,
    ) -> int:
        """Construct a RUNNING run that looks like it was
        interrupted partway. Returns run_id."""
        with transaction(db_engine) as conn:
            run_id = FundamentalsRunRepository(conn).open(
                phase=PHASE_V1_INDUSTRY_INDEX_VALUE,
                exchange="ALL",
                notes=f"Partial run for {industry_name}",
            )
            FundamentalsRunRepository(conn).update_progress(
                run_id,
                succeeded=succeeded, failed=failed, skipped=skipped,
                progress_json={
                    "industry_name": industry_name,
                    "date_list": [d.isoformat() for d in date_list],
                    "cursor_index": cursor_index,
                    "from_date": date_list[0].isoformat(),
                    "to_date": date_list[-1].isoformat(),
                },
            )
        return run_id

    def test_resume_continues_from_cursor(self, db_engine):
        """Resume picks up at cursor_index. After completion the
        run is closed with cumulative counters."""
        industry = "Resume Test"
        dates = [date(2025, 10, i) for i in (1, 2, 3, 4, 5)]
        with transaction(db_engine) as conn:
            _seed_industry(conn, industry, base_date=date(2030, 1, 1))
            # base_date in future → all dates pre-base → all skipped

        run_id = self._seed_partial_run(
            db_engine,
            industry_name=industry,
            date_list=dates,
            cursor_index=2,
            skipped=2,  # We claim 2 dates have already been skipped
        )

        completed_id = populate_industry_index_value_for_industry(
            db_engine, industry,
            resume_run_id=run_id,
        )
        assert completed_id == run_id  # same run, not a new one

        with db_engine.connect() as conn:
            run = _get_run(conn, run_id)
        assert run.status == "SUCCESS"
        # 2 (carryover) + 3 (resumed) = 5 total
        assert run.skipped == 5

    def test_resume_with_nonexistent_run_id_raises(self, db_engine):
        industry = "X"
        with transaction(db_engine) as conn:
            _seed_industry(conn, industry)
        with pytest.raises(ValueError, match="no fundamentals_run"):
            populate_industry_index_value_for_industry(
                db_engine, industry, resume_run_id=999_999_999,
            )

    def test_resume_with_wrong_phase_raises(self, db_engine):
        """Trying to resume a run from a different phase should
        fail fast."""
        industry = "X2"
        with transaction(db_engine) as conn:
            _seed_industry(conn, industry)
            # Open a run for a DIFFERENT phase
            wrong_id = FundamentalsRunRepository(conn).open(
                phase="v1_stock_indicator_state",
                exchange="NSE",
                notes="wrong phase",
            )
        with pytest.raises(ValueError, match="phase"):
            populate_industry_index_value_for_industry(
                db_engine, industry, resume_run_id=wrong_id,
            )

    def test_resume_with_wrong_industry_raises(self, db_engine):
        industry_a = "Industry A"
        industry_b = "Industry B"
        with transaction(db_engine) as conn:
            _seed_industry(conn, industry_a)
            _seed_industry(conn, industry_b)
        # Open a partial run for industry A
        run_id = self._seed_partial_run(
            db_engine, industry_name=industry_a,
            date_list=[date(2025, 10, 1)], cursor_index=0,
        )
        # Try to resume but for industry B
        with pytest.raises(ValueError, match="industry"):
            populate_industry_index_value_for_industry(
                db_engine, industry_b, resume_run_id=run_id,
            )

    def test_resume_already_closed_run_raises(self, db_engine):
        """Only RUNNING runs can be resumed; SUCCESS / FAILED ones
        are terminal."""
        industry = "Closed Resume"
        d0 = date(2025, 10, 1)
        with transaction(db_engine) as conn:
            _seed_industry(conn, industry, base_date=d0)
            _seed_dates(conn, [d0])

        # Run + close it normally first.
        first_id = populate_industry_index_value_for_industry(
            db_engine, industry, from_date=d0, to_date=d0,
        )

        # Now try to resume it — it's already SUCCESS.
        with pytest.raises(ValueError, match="RUNNING"):
            populate_industry_index_value_for_industry(
                db_engine, industry, resume_run_id=first_id,
            )


# ============================================================
# Side-effect lockdown
# ============================================================

class TestPopulateIndustryIndexValueSideEffects:

    def test_writes_only_to_index_value_and_fundamentals_run(
        self, db_engine,
    ):
        """The runner writes only to industry_index_value and
        fundamentals_run. Zero rows to stock state/enrichment,
        industry indicator state/enrichment, signals, legacy
        indicator_state."""
        industry = "Side Effects"
        rd = date(2025, 10, 1)
        td = date(2025, 10, 2)
        with transaction(db_engine) as conn:
            _seed_full_industry_with_constituent(
                conn, industry,
                rebalance_date=rd, value_date=td,
            )

        with db_engine.connect() as conn:
            baseline_stock_state = _count(
                conn, "stock_indicator_state",
            )
            baseline_stock_enrich = _count(
                conn, "stock_indicator_enrichment",
            )
            baseline_industry_state = _count(
                conn, "industry_indicator_state",
            )
            baseline_industry_enrich = _count(
                conn, "industry_indicator_enrichment",
            )
            # === STEP 15B-FIXUP2-r3-baseline: signal_events baseline removed ===
            baseline_current = _count(conn, "current_signals")
            baseline_historical = _count(conn, "historical_signals")
            baseline_legacy = _count(conn, "indicator_state")

        populate_industry_index_value_for_industry(
            db_engine, industry, from_date=rd, to_date=td,
        )

        with db_engine.connect() as conn:
            assert _count(
                conn, "stock_indicator_state",
            ) == baseline_stock_state
            assert _count(
                conn, "stock_indicator_enrichment",
            ) == baseline_stock_enrich
            assert _count(
                conn, "industry_indicator_state",
            ) == baseline_industry_state
            assert _count(
                conn, "industry_indicator_enrichment",
            ) == baseline_industry_enrich
            # === STEP 15B-FIXUP2-r3-assert: signal_events assert removed ===
            assert _count(conn, "current_signals") == baseline_current
            assert _count(
                conn, "historical_signals",
            ) == baseline_historical
            assert _count(
                conn, "indicator_state",
            ) == baseline_legacy


# ============================================================
# Bulk runner
# ============================================================

class TestPopulateIndustryIndexValueForAll:

    def test_iterates_active_industries(self, db_engine):
        d0 = date(2025, 10, 1)
        d1 = date(2025, 10, 2)
        ind_a = "Bulk A"
        ind_b = "Bulk B"
        retired = "Bulk Retired"
        with transaction(db_engine) as conn:
            _seed_industry(conn, ind_a, base_date=d0)
            _seed_industry(conn, ind_b, base_date=d0)
            _seed_industry(
                conn, retired, base_date=d0, status="RETIRED",
            )
            _seed_dates(conn, [d0, d1])

        report = populate_industry_index_value_for_all(
            db_engine, from_date=d0, to_date=d1,
        )

        all_names = (
            {n for n, *_ in report["succeeded"]}
            | {n for n, *_ in report["skipped"]}
            | {n for n, *_ in report["failed"]}
        )
        assert ind_a in all_names
        assert ind_b in all_names
        # RETIRED industries are ignored by list_active() and not
        # touched by the bulk runner.
        assert retired not in all_names

    def test_per_industry_isolation(self, db_engine):
        """Even if one industry's date setup is empty, the rest
        still complete."""
        d0 = date(2025, 10, 1)
        ind_with_data = "Bulk Has Data"
        ind_no_data = "Bulk No Data"
        with transaction(db_engine) as conn:
            _seed_industry(conn, ind_with_data, base_date=d0)
            _seed_industry(conn, ind_no_data, base_date=d0)
            _seed_dates(conn, [d0])
            # ind_no_data has no daily_market_cap seeded

        report = populate_industry_index_value_for_all(
            db_engine, from_date=d0, to_date=d0,
        )

        all_names = (
            {n for n, *_ in report["succeeded"]}
            | {n for n, *_ in report["skipped"]}
            | {n for n, *_ in report["failed"]}
        )
        assert ind_with_data in all_names
        assert ind_no_data in all_names
