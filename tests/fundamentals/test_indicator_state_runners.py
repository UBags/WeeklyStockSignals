"""Tests for Phase 5-4 indicator state runners."""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pytest

from trip_ema.fundamentals.runners import (
    run_indicator_state_for_all_industries,
    run_indicator_state_for_industry,
)
from trip_ema.persistence import transaction
from trip_ema.persistence.fundamentals_repo import (
    FundamentalsRunRepository,
    IndicatorStateRepository,
    IndustryIndexRepository,
    IndustryIndexValueRepository,
)


# ---------- helpers ---------- #


def _setup_industry_with_values(
    db_engine, name: str = "IT - Services",
    *, n_bars: int = 80, base_date: date = date(2025, 10, 1),
) -> None:
    """Create an industry index and populate it with `n_bars` value
    rows starting from `base_date`. The values oscillate between
    900 and 1100 to give the indicator pipeline a non-trivial
    series to chew on."""
    with transaction(db_engine) as conn:
        IndustryIndexRepository(conn).upsert(
            name, sector_name="Technology", base_date=base_date,
        )
        repo = IndustryIndexValueRepository(conn)
        for i in range(n_bars):
            td = base_date + timedelta(days=i * 7)  # weekly bars
            value = 1000.0 + 50.0 * math.sin(i * 0.3)
            repo.upsert_value(
                name, trade_date=td, value=value,
                constituents_used=10, constituents_missing=0,
                renormalized=False,
                effective_rebalance_date=base_date,
            )


# ---------- run_indicator_state_for_industry ---------- #


class TestRunIndicatorStateForIndustry:

    def test_writes_indicator_state_rows(self, db_engine):
        _setup_industry_with_values(db_engine, n_bars=80)
        run_id = run_indicator_state_for_industry(
            db_engine, "IT - Services",
        )
        # Run record exists and is SUCCESS.
        with db_engine.connect() as conn:
            r = FundamentalsRunRepository(conn).get(run_id)
        assert r is not None
        assert r.status == "SUCCESS"
        assert r.phase == "index_indicators"
        assert r.succeeded > 0
        assert r.failed == 0

    def test_skipped_count_for_early_nan_bars(self, db_engine):
        """The early bars where EMAs haven't stabilized should be
        in the `skipped` count, not `succeeded`."""
        _setup_industry_with_values(db_engine, n_bars=80)
        run_id = run_indicator_state_for_industry(
            db_engine, "IT - Services",
        )
        with db_engine.connect() as conn:
            r = FundamentalsRunRepository(conn).get(run_id)
        # Some early bars must have been skipped due to leading NaN.
        # The longest EMA (base_len=16) needs at least that many bars
        # before it produces non-NaN values.
        assert r.skipped > 0
        # Total bars in input = 80.
        assert r.succeeded + r.skipped == 80

    def test_persisted_rows_have_expected_fields(self, db_engine):
        _setup_industry_with_values(db_engine, n_bars=80)
        run_indicator_state_for_industry(
            db_engine, "IT - Services",
        )
        with db_engine.connect() as conn:
            latest = IndicatorStateRepository(conn).get_latest_for_industry(
                "IT - Services",
            )
        assert latest is not None
        assert latest.industry_name == "IT - Services"
        assert latest.ticker_id is None
        assert latest.timeframe is None
        # All required fields must be finite.
        assert latest.med_ema > 0
        assert latest.long_ema > 0
        assert latest.longest_ema > 0
        assert latest.atr >= 0
        # Regime must be in valid range.
        assert latest.display_regime in (-1, 0, 1)

    def test_idempotent(self, db_engine):
        """Running twice produces the same end state. The second
        run hits skip-if-recent (because the first run's data is
        fresh) unless force=True."""
        _setup_industry_with_values(db_engine, n_bars=80)
        run_id_1 = run_indicator_state_for_industry(
            db_engine, "IT - Services",
        )
        with db_engine.connect() as conn:
            r1 = FundamentalsRunRepository(conn).get(run_id_1)
            count_after_1 = len(
                IndicatorStateRepository(conn).get_history_for_industry(
                    "IT - Services",
                )
            )
        # Second run — without force, hits skip-if-recent.
        run_id_2 = run_indicator_state_for_industry(
            db_engine, "IT - Services",
        )
        with db_engine.connect() as conn:
            r2 = FundamentalsRunRepository(conn).get(run_id_2)
            count_after_2 = len(
                IndicatorStateRepository(conn).get_history_for_industry(
                    "IT - Services",
                )
            )
        # The second run is the skip path: succeeded=0, skipped=1.
        assert r2.succeeded == 0
        assert r2.skipped == 1
        # Row count unchanged.
        assert count_after_1 == count_after_2

    def test_force_re_runs_despite_recent_data(self, db_engine):
        """With force=True, skip-if-recent is bypassed and rows
        are re-written."""
        _setup_industry_with_values(db_engine, n_bars=80)
        run_indicator_state_for_industry(
            db_engine, "IT - Services",
        )
        run_id_2 = run_indicator_state_for_industry(
            db_engine, "IT - Services", force=True,
        )
        with db_engine.connect() as conn:
            r2 = FundamentalsRunRepository(conn).get(run_id_2)
        # Second run actually re-wrote rows.
        assert r2.succeeded > 0

    def test_raises_if_industry_does_not_exist(self, db_engine):
        with pytest.raises(ValueError, match="does not exist"):
            run_indicator_state_for_industry(
                db_engine, "Nonexistent Industry",
            )

    def test_raises_if_industry_has_no_value_history(self, db_engine):
        """An indexed industry with no values — should raise so
        operators know to run compute-index-values first."""
        with transaction(db_engine) as conn:
            IndustryIndexRepository(conn).upsert(
                "Empty Industry", sector_name="Other",
                base_date=date(2025, 10, 1),
            )
        with pytest.raises(ValueError, match="has no value rows"):
            run_indicator_state_for_industry(
                db_engine, "Empty Industry",
            )

    def test_atomic_on_exception(self, db_engine, monkeypatch):
        """If the upsert fails mid-loop, no partial rows should be
        committed (the whole compute is in one transaction).

        We force a failure by patching the repo's upsert method to
        raise on the 5th call."""
        _setup_industry_with_values(db_engine, n_bars=80)

        call_counter = {"n": 0}
        original_upsert = IndicatorStateRepository.upsert_for_industry

        def patched_upsert(self, state):
            call_counter["n"] += 1
            if call_counter["n"] == 5:
                raise RuntimeError("simulated failure")
            return original_upsert(self, state)

        monkeypatch.setattr(
            IndicatorStateRepository, "upsert_for_industry", patched_upsert,
        )

        with pytest.raises(RuntimeError, match="simulated failure"):
            run_indicator_state_for_industry(
                db_engine, "IT - Services",
            )

        # Verify: zero rows persisted (transaction rolled back).
        with db_engine.connect() as conn:
            history = IndicatorStateRepository(conn).get_history_for_industry(
                "IT - Services",
            )
        assert len(history) == 0


# ---------- run_indicator_state_for_all_industries ---------- #


class TestRunIndicatorStateForAllIndustries:

    def test_succeeds_for_all_industries_with_values(self, db_engine):
        _setup_industry_with_values(db_engine, "Industry A", n_bars=80)
        _setup_industry_with_values(
            db_engine, "Industry B", n_bars=80,
            base_date=date(2025, 10, 1),
        )
        report = run_indicator_state_for_all_industries(db_engine)
        names_succeeded = {name for name, _, _ in report["succeeded"]}
        assert names_succeeded == {"Industry A", "Industry B"}
        assert report["failed"] == []
        # Both succeeded with non-zero row counts.
        for _, succeeded, _ in report["succeeded"]:
            assert succeeded > 0

    def test_skips_industries_with_no_values(self, db_engine):
        """An indexed industry with no value rows should be in
        `skipped`, not `failed`."""
        _setup_industry_with_values(db_engine, "Industry A", n_bars=80)
        with transaction(db_engine) as conn:
            IndustryIndexRepository(conn).upsert(
                "Industry Empty", sector_name="Other",
                base_date=date(2025, 10, 1),
            )
        report = run_indicator_state_for_all_industries(db_engine)
        # Industry A succeeded; Industry Empty skipped.
        names_succeeded = {name for name, _, _ in report["succeeded"]}
        names_skipped = {name for name, _ in report["skipped"]}
        assert names_succeeded == {"Industry A"}
        assert names_skipped == {"Industry Empty"}
        # Skip reason mentions the cause.
        for _, reason in report["skipped"]:
            assert "no value" in reason.lower()

    def test_continues_after_one_industry_fails(self, db_engine, monkeypatch):
        """If one industry raises, the runner should continue with
        the next one — error isolation matters for bulk operations."""
        _setup_industry_with_values(db_engine, "Industry Good", n_bars=80)
        _setup_industry_with_values(
            db_engine, "Industry Bad", n_bars=80,
            base_date=date(2025, 10, 1),
        )
        # Patch the per-industry runner to raise for "Industry Bad".
        from trip_ema.fundamentals import runners as runners_mod
        original = runners_mod.run_indicator_state_for_industry

        def patched(engine, name, **kw):
            if name == "Industry Bad":
                raise RuntimeError("simulated industry failure")
            return original(engine, name, **kw)

        monkeypatch.setattr(
            runners_mod, "run_indicator_state_for_industry", patched,
        )
        report = run_indicator_state_for_all_industries(db_engine)
        names_succeeded = {name for name, _, _ in report["succeeded"]}
        names_failed = {name for name, _ in report["failed"]}
        assert names_succeeded == {"Industry Good"}
        assert names_failed == {"Industry Bad"}
        # Error message preserved.
        for _, err in report["failed"]:
            assert "simulated" in err

    def test_empty_when_no_indices_exist(self, db_engine):
        """No indices at all → empty report (not an error)."""
        report = run_indicator_state_for_all_industries(db_engine)
        assert report["succeeded"] == []
        assert report["skipped"] == []
        assert report["failed"] == []

    def test_skips_retired_indices(self, db_engine):
        """Indices in RETIRED status should not be processed."""
        _setup_industry_with_values(db_engine, "Industry Active", n_bars=80)
        _setup_industry_with_values(
            db_engine, "Industry Retired", n_bars=80,
            base_date=date(2025, 10, 1),
        )
        with transaction(db_engine) as conn:
            IndustryIndexRepository(conn).set_status(
                "Industry Retired", "RETIRED",
            )
        report = run_indicator_state_for_all_industries(db_engine)
        names_succeeded = {name for name, _, _ in report["succeeded"]}
        assert names_succeeded == {"Industry Active"}
