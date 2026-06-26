"""Integration tests for IndustryIndicatorEnrichmentRepository.

DB integration: uses the `db_engine` fixture. Tests CRUD + pattern-
label validation. Seeds the parent industry_indicator_state row
first (FK precondition).
"""
from __future__ import annotations

from datetime import datetime, timedelta, UTC

import pytest
from sqlalchemy import text

from trip_ema.domain.industry_indicator_enrichment import (
    IndustryIndicatorEnrichment,
)
from trip_ema.domain.industry_indicator_state import (
    IndustryIndicatorState,
)
from trip_ema.persistence import transaction
from trip_ema.persistence.industry_indicator_enrichment_repo import (
    IndustryIndicatorEnrichmentRepository,
)
from trip_ema.persistence.industry_indicator_state_repo import (
    IndustryIndicatorStateRepository,
)


# ============================================================
# Helpers
# ============================================================

def _seed_industry(conn, industry_name: str) -> str:
    """Insert a parent industry_index row."""
    conn.execute(text("""
        INSERT INTO industry_index (
            industry_name, sector_name, methodology,
            base_value, base_date, rebalance_schedule,
            min_constituents, status
        ) VALUES (
            :n, 'Technology', 'MODIFIED_LASPEYRES',
            1000.0, '2025-01-01', 'SEMIANNUAL_APR_OCT', 10, 'ACTIVE'
        )
        ON CONFLICT (industry_name) DO NOTHING
    """), {"n": industry_name})
    return industry_name


def _seed_state(
    conn, industry_name: str,
    *, bar_timestamp: datetime, timeframe: str = "Weekly",
) -> None:
    """Insert a parent industry_indicator_state row (FK target)."""
    state = IndustryIndicatorState(
        industry_name=industry_name,
        timeframe=timeframe,
        bar_timestamp=bar_timestamp,
        med_ema=100.0, long_ema=99.0, longest_ema=98.0,
        atr=5.0,
        upper_inner_band=105.0, lower_inner_band=95.0,
        upper_mid_band=110.0, lower_mid_band=90.0,
        upper_outer_band=115.0, lower_outer_band=85.0,
        display_regime=1,
        computed_at=datetime.now(UTC),
    )
    IndustryIndicatorStateRepository(conn).upsert_one(state)


def _make_enrichment(
    industry_name: str = "IT - Services",
    *,
    timeframe: str = "Weekly",
    bar_timestamp: datetime | None = None,
    med_pattern_label: str = "NA",
    med_turn_buy: bool = False,
) -> IndustryIndicatorEnrichment:
    if bar_timestamp is None:
        bar_timestamp = datetime(2025, 10, 6, tzinfo=UTC)
    return IndustryIndicatorEnrichment(
        industry_name=industry_name,
        timeframe=timeframe,
        bar_timestamp=bar_timestamp,
        pierces_upper_mid=False,
        pierces_lower_mid=False,
        dist_to_med_in_hl_units=0.5,
        dist_to_long_in_hl_units=0.4,
        dist_to_longest_in_hl_units=0.3,
        mean_dist_to_med_in_hl_units=0.55,
        mean_dist_to_long_in_hl_units=0.45,
        mean_dist_to_longest_in_hl_units=0.35,
        bars_since_med_turn=5,
        bars_since_long_turn=8,
        bars_since_longest_turn=12,
        pct_change_since_med_turn=0.05,
        pct_change_since_long_turn=0.08,
        pct_change_since_longest_turn=0.12,
        med_pattern_label=med_pattern_label,
        long_pattern_label="NA",
        longest_pattern_label="NA",
        med_turn_buy=med_turn_buy,
        med_turn_sell=False,
        long_turn_buy=False,
        long_turn_sell=False,
        longest_turn_buy=False,
        longest_turn_sell=False,
        computed_at=datetime.now(UTC),
    )


# ============================================================
# Writes
# ============================================================

class TestUpsertOne:

    def test_inserts_a_fresh_row(self, db_engine):
        bt = datetime(2025, 10, 6, tzinfo=UTC)
        with transaction(db_engine) as conn:
            _seed_industry(conn, "IT - Services")
            _seed_state(conn, "IT - Services", bar_timestamp=bt)
            repo = IndustryIndicatorEnrichmentRepository(conn)
            repo.upsert_one(_make_enrichment(
                "IT - Services", bar_timestamp=bt,
            ))

        with db_engine.connect() as conn:
            cnt = conn.execute(text("""
                SELECT COUNT(*) FROM industry_indicator_enrichment
                 WHERE industry_name = 'IT - Services'
            """)).scalar_one()
        assert cnt == 1

    def test_idempotent_on_pk(self, db_engine):
        bt = datetime(2025, 10, 6, tzinfo=UTC)
        with transaction(db_engine) as conn:
            _seed_industry(conn, "Idemp")
            _seed_state(conn, "Idemp", bar_timestamp=bt)
            repo = IndustryIndicatorEnrichmentRepository(conn)
            repo.upsert_one(_make_enrichment(
                "Idemp", bar_timestamp=bt, med_turn_buy=False,
            ))
            repo.upsert_one(_make_enrichment(
                "Idemp", bar_timestamp=bt, med_turn_buy=True,
            ))

        with db_engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT med_turn_buy FROM industry_indicator_enrichment
                 WHERE industry_name = 'Idemp'
            """)).fetchall()
        assert len(rows) == 1
        assert rows[0][0] is True

    def test_rejects_invalid_pattern_label(self, db_engine):
        bt = datetime(2025, 10, 6, tzinfo=UTC)
        with transaction(db_engine) as conn:
            _seed_industry(conn, "BadPattern")
            _seed_state(conn, "BadPattern", bar_timestamp=bt)
            repo = IndustryIndicatorEnrichmentRepository(conn)
            with pytest.raises(ValueError, match="pattern_label"):
                repo.upsert_one(_make_enrichment(
                    "BadPattern", bar_timestamp=bt,
                    med_pattern_label="NOT_A_REAL_LABEL",
                ))


class TestUpsertMany:

    def test_empty_input_no_op(self, db_engine):
        with transaction(db_engine) as conn:
            repo = IndustryIndicatorEnrichmentRepository(conn)
            n = repo.upsert_many([])
        assert n == 0

    def test_inserts_n_rows(self, db_engine):
        ind = "BulkUp"
        base = datetime(2025, 10, 6, tzinfo=UTC)
        with transaction(db_engine) as conn:
            _seed_industry(conn, ind)
            for i in range(5):
                _seed_state(
                    conn, ind,
                    bar_timestamp=base + timedelta(weeks=i),
                )
            repo = IndustryIndicatorEnrichmentRepository(conn)
            enrichments = [
                _make_enrichment(
                    ind, bar_timestamp=base + timedelta(weeks=i),
                )
                for i in range(5)
            ]
            n = repo.upsert_many(enrichments)
        assert n == 5

        with db_engine.connect() as conn:
            cnt = conn.execute(text("""
                SELECT COUNT(*) FROM industry_indicator_enrichment
                 WHERE industry_name = :n
            """), {"n": ind}).scalar_one()
        assert cnt == 5

    def test_validates_all_before_writing_any(self, db_engine):
        ind = "AllOrNothing"
        bt = datetime(2025, 10, 6, tzinfo=UTC)
        with transaction(db_engine) as conn:
            _seed_industry(conn, ind)
            _seed_state(conn, ind, bar_timestamp=bt)
            repo = IndustryIndicatorEnrichmentRepository(conn)
            good = _make_enrichment(ind, bar_timestamp=bt)
            bad = _make_enrichment(
                ind,
                bar_timestamp=bt + timedelta(weeks=1),
                med_pattern_label="GARBAGE",
            )
            with pytest.raises(ValueError):
                repo.upsert_many([good, bad])

        with db_engine.connect() as conn:
            cnt = conn.execute(text("""
                SELECT COUNT(*) FROM industry_indicator_enrichment
                 WHERE industry_name = :n
            """), {"n": ind}).scalar_one()
        assert cnt == 0


class TestDeleteForIndustry:

    def test_removes_all_rows(self, db_engine):
        ind = "DeleteMe"
        base = datetime(2025, 10, 6, tzinfo=UTC)
        with transaction(db_engine) as conn:
            _seed_industry(conn, ind)
            for i in range(3):
                _seed_state(
                    conn, ind,
                    bar_timestamp=base + timedelta(weeks=i),
                )
            repo = IndustryIndicatorEnrichmentRepository(conn)
            repo.upsert_many([
                _make_enrichment(
                    ind, bar_timestamp=base + timedelta(weeks=i),
                )
                for i in range(3)
            ])
            n = repo.delete_for_industry(ind, "Weekly")
        assert n == 3


# ============================================================
# Reads
# ============================================================

class TestReads:

    def _seed_three_bars(self, db_engine, industry: str):
        base = datetime(2025, 10, 6, tzinfo=UTC)
        with transaction(db_engine) as conn:
            _seed_industry(conn, industry)
            repo = IndustryIndicatorEnrichmentRepository(conn)
            for i in range(3):
                bt = base + timedelta(weeks=i)
                _seed_state(conn, industry, bar_timestamp=bt)
                repo.upsert_one(_make_enrichment(
                    industry, bar_timestamp=bt,
                    med_turn_buy=(i == 1),
                ))

    def test_get_at_exact_match(self, db_engine):
        ind = "GetAt"
        self._seed_three_bars(db_engine, ind)
        bt = datetime(2025, 10, 13, tzinfo=UTC)
        with db_engine.connect() as conn:
            e = IndustryIndicatorEnrichmentRepository(conn).get_at(
                ind, "Weekly", bt,
            )
        assert e is not None
        assert e.industry_name == ind
        assert e.med_turn_buy is True

    def test_get_at_miss_returns_none(self, db_engine):
        self._seed_three_bars(db_engine, "GetAtMiss")
        with db_engine.connect() as conn:
            e = IndustryIndicatorEnrichmentRepository(conn).get_at(
                "GetAtMiss", "Weekly",
                datetime(2030, 1, 1, tzinfo=UTC),
            )
        assert e is None

    def test_get_at_or_before(self, db_engine):
        ind = "GetBefore"
        self._seed_three_bars(db_engine, ind)
        with db_engine.connect() as conn:
            e = IndustryIndicatorEnrichmentRepository(
                conn,
            ).get_at_or_before(
                ind, "Weekly",
                datetime(2025, 10, 16, tzinfo=UTC),
            )
        assert e is not None
        assert e.bar_timestamp == datetime(2025, 10, 13, tzinfo=UTC)

    def test_get_latest_for_industry(self, db_engine):
        ind = "Latest"
        self._seed_three_bars(db_engine, ind)
        with db_engine.connect() as conn:
            e = IndustryIndicatorEnrichmentRepository(
                conn,
            ).get_latest_for_industry(ind, "Weekly")
        assert e is not None
        assert e.bar_timestamp == datetime(2025, 10, 20, tzinfo=UTC)

    def test_get_history_newest_first(self, db_engine):
        ind = "History"
        self._seed_three_bars(db_engine, ind)
        with db_engine.connect() as conn:
            history = IndustryIndicatorEnrichmentRepository(
                conn,
            ).get_history_for_industry(ind, "Weekly")
        assert len(history) == 3
        assert history[0].bar_timestamp > history[2].bar_timestamp

    def test_get_history_with_limit(self, db_engine):
        ind = "HistoryLim"
        self._seed_three_bars(db_engine, ind)
        with db_engine.connect() as conn:
            history = IndustryIndicatorEnrichmentRepository(
                conn,
            ).get_history_for_industry(ind, "Weekly", limit=2)
        assert len(history) == 2

    def test_count_for_industry(self, db_engine):
        ind = "Count"
        self._seed_three_bars(db_engine, ind)
        with db_engine.connect() as conn:
            n = IndustryIndicatorEnrichmentRepository(
                conn,
            ).count_for_industry(ind, "Weekly")
        assert n == 3


# ============================================================
# FK + cascade
# ============================================================

class TestFKAndCascade:

    def test_fk_blocks_enrichment_without_parent_state(self, db_engine):
        """Inserting enrichment without parent state should fail
        with IntegrityError at the DB level."""
        from sqlalchemy.exc import IntegrityError
        ind = "NoParent"
        bt = datetime(2025, 10, 6, tzinfo=UTC)
        with transaction(db_engine) as conn:
            _seed_industry(conn, ind)
            # NO _seed_state — FK should reject
            repo = IndustryIndicatorEnrichmentRepository(conn)
            with pytest.raises(IntegrityError):
                repo.upsert_one(_make_enrichment(
                    ind, bar_timestamp=bt,
                ))

    def test_cascade_delete_state_removes_enrichment(self, db_engine):
        """ON DELETE CASCADE from enrichment FK back to state."""
        ind = "Cascade"
        bt = datetime(2025, 10, 6, tzinfo=UTC)
        with transaction(db_engine) as conn:
            _seed_industry(conn, ind)
            _seed_state(conn, ind, bar_timestamp=bt)
            IndustryIndicatorEnrichmentRepository(conn).upsert_one(
                _make_enrichment(ind, bar_timestamp=bt),
            )

        # Delete the state row directly
        with transaction(db_engine) as conn:
            IndustryIndicatorStateRepository(
                conn,
            ).delete_for_industry(ind, "Weekly")

        with db_engine.connect() as conn:
            cnt = IndustryIndicatorEnrichmentRepository(
                conn,
            ).count_for_industry(ind, "Weekly")
        assert cnt == 0
