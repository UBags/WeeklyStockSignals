"""Integration tests for IndustryIndicatorStateRepository.

DB integration: uses the `db_engine` fixture from
tests/persistence/conftest.py. Tests CRUD + validation + idempotency.
"""
from __future__ import annotations

from datetime import datetime, timedelta, UTC

import pytest
from sqlalchemy import text

from trip_ema.domain.industry_indicator_state import (
    IndustryIndicatorState,
)
from trip_ema.persistence import transaction
from trip_ema.persistence.industry_indicator_state_repo import (
    IndustryIndicatorStateRepository,
)


# ============================================================
# Helpers
# ============================================================

def _seed_industry(conn, industry_name: str) -> str:
    """Insert a parent industry_index row (FK target)."""
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


def _make_state(
    industry_name: str = "IT - Services",
    *,
    timeframe: str = "Weekly",
    bar_timestamp: datetime | None = None,
    date=None,
    display_regime: int = 1,
    med_ema: float = 100.0,
    atr: float = 5.0,
) -> IndustryIndicatorState:
    if bar_timestamp is None:
        bar_timestamp = datetime(2025, 10, 6, tzinfo=UTC)
    if date is None:
        date = bar_timestamp.date()
    return IndustryIndicatorState(
        industry_name=industry_name,
        timeframe=timeframe,
        bar_timestamp=bar_timestamp,
        date=date,
        med_ema=med_ema,
        long_ema=99.0,
        longest_ema=98.0,
        atr=atr,
        upper_inner_band=105.0,
        lower_inner_band=95.0,
        upper_mid_band=110.0,
        lower_mid_band=90.0,
        upper_outer_band=115.0,
        lower_outer_band=85.0,
        display_regime=display_regime,
        computed_at=datetime.now(UTC),
    )


# ============================================================
# Writes
# ============================================================

class TestUpsertOne:

    def test_inserts_a_fresh_row(self, db_engine):
        with transaction(db_engine) as conn:
            _seed_industry(conn, "IT - Services")
            repo = IndustryIndicatorStateRepository(conn)
            repo.upsert_one(_make_state("IT - Services"))

        with db_engine.connect() as conn:
            cnt = conn.execute(text("""
                SELECT COUNT(*) FROM industry_indicator_state
                 WHERE industry_name = 'IT - Services'
            """)).scalar_one()
        assert cnt == 1

    def test_idempotent_on_pk(self, db_engine):
        """Second upsert on the same PK overwrites, doesn't duplicate."""
        with transaction(db_engine) as conn:
            _seed_industry(conn, "Idemp")
            repo = IndustryIndicatorStateRepository(conn)
            repo.upsert_one(_make_state("Idemp", med_ema=100.0))
            repo.upsert_one(_make_state("Idemp", med_ema=200.0))

        with db_engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT med_ema FROM industry_indicator_state
                 WHERE industry_name = 'Idemp'
            """)).fetchall()
        assert len(rows) == 1
        assert float(rows[0][0]) == 200.0

    @pytest.mark.parametrize("invalid_regime", [-2, 2, 5])
    def test_rejects_invalid_regime(self, db_engine, invalid_regime):
        with transaction(db_engine) as conn:
            _seed_industry(conn, "BadRegime")
            repo = IndustryIndicatorStateRepository(conn)
            with pytest.raises(ValueError, match="display_regime"):
                repo.upsert_one(
                    _make_state(
                        "BadRegime", display_regime=invalid_regime,
                    ),
                )

    def test_rejects_negative_atr(self, db_engine):
        with transaction(db_engine) as conn:
            _seed_industry(conn, "BadATR")
            repo = IndustryIndicatorStateRepository(conn)
            with pytest.raises(ValueError, match="atr"):
                repo.upsert_one(_make_state("BadATR", atr=-0.1))

    def test_rejects_nonpositive_ema(self, db_engine):
        with transaction(db_engine) as conn:
            _seed_industry(conn, "BadEMA")
            repo = IndustryIndicatorStateRepository(conn)
            with pytest.raises(ValueError, match="EMA"):
                repo.upsert_one(_make_state("BadEMA", med_ema=0.0))


class TestUpsertMany:

    def test_empty_input_no_op(self, db_engine):
        with transaction(db_engine) as conn:
            repo = IndustryIndicatorStateRepository(conn)
            n = repo.upsert_many([])
        assert n == 0

    def test_inserts_n_rows(self, db_engine):
        ind = "Bulk Up"
        with transaction(db_engine) as conn:
            _seed_industry(conn, ind)
            repo = IndustryIndicatorStateRepository(conn)
            base = datetime(2025, 10, 6, tzinfo=UTC)
            states = [
                _make_state(
                    ind, bar_timestamp=base + timedelta(weeks=i),
                )
                for i in range(5)
            ]
            n = repo.upsert_many(states)
        assert n == 5

        with db_engine.connect() as conn:
            cnt = conn.execute(text("""
                SELECT COUNT(*) FROM industry_indicator_state
                 WHERE industry_name = :n
            """), {"n": ind}).scalar_one()
        assert cnt == 5

    def test_validates_all_before_writing_any(self, db_engine):
        """If any row is invalid, no rows are written."""
        ind = "All-or-Nothing"
        with transaction(db_engine) as conn:
            _seed_industry(conn, ind)
            repo = IndustryIndicatorStateRepository(conn)
            good = _make_state(ind)
            bad = _make_state(ind, display_regime=5)
            with pytest.raises(ValueError):
                repo.upsert_many([good, bad])

        with db_engine.connect() as conn:
            cnt = conn.execute(text("""
                SELECT COUNT(*) FROM industry_indicator_state
                 WHERE industry_name = :n
            """), {"n": ind}).scalar_one()
        assert cnt == 0


class TestDeleteForIndustry:

    def test_removes_all_rows(self, db_engine):
        ind = "DeleteMe"
        with transaction(db_engine) as conn:
            _seed_industry(conn, ind)
            repo = IndustryIndicatorStateRepository(conn)
            base = datetime(2025, 10, 6, tzinfo=UTC)
            repo.upsert_many([
                _make_state(ind, bar_timestamp=base + timedelta(weeks=i))
                for i in range(3)
            ])
            n = repo.delete_for_industry(ind, "Weekly")
        assert n == 3

        with db_engine.connect() as conn:
            cnt = conn.execute(text("""
                SELECT COUNT(*) FROM industry_indicator_state
                 WHERE industry_name = :n
            """), {"n": ind}).scalar_one()
        assert cnt == 0

    def test_only_affects_target_timeframe(self, db_engine):
        ind = "TFScoped"
        bt = datetime(2025, 10, 6, tzinfo=UTC)
        with transaction(db_engine) as conn:
            _seed_industry(conn, ind)
            repo = IndustryIndicatorStateRepository(conn)
            repo.upsert_one(_make_state(
                ind, timeframe="Weekly", bar_timestamp=bt,
            ))
            repo.upsert_one(_make_state(
                ind, timeframe="Daily", bar_timestamp=bt,
            ))
            n = repo.delete_for_industry(ind, "Weekly")
        assert n == 1

        with db_engine.connect() as conn:
            tfs = [
                r[0] for r in conn.execute(text("""
                    SELECT timeframe FROM industry_indicator_state
                     WHERE industry_name = :n
                """), {"n": ind}).fetchall()
            ]
        assert tfs == ["Daily"]


# ============================================================
# Reads
# ============================================================

class TestReads:

    def _seed_three_bars(self, db_engine, industry: str):
        base = datetime(2025, 10, 6, tzinfo=UTC)
        with transaction(db_engine) as conn:
            _seed_industry(conn, industry)
            repo = IndustryIndicatorStateRepository(conn)
            for i in range(3):
                repo.upsert_one(_make_state(
                    industry,
                    bar_timestamp=base + timedelta(weeks=i),
                    med_ema=100.0 + i,
                ))

    def test_get_at_exact_match(self, db_engine):
        ind = "GetAt"
        self._seed_three_bars(db_engine, ind)
        bt = datetime(2025, 10, 13, tzinfo=UTC)
        with db_engine.connect() as conn:
            s = IndustryIndicatorStateRepository(conn).get_at(
                ind, "Weekly", bt,
            )
        assert s is not None
        assert s.industry_name == ind
        assert s.med_ema == 101.0

    def test_get_at_miss_returns_none(self, db_engine):
        ind = "GetAtMiss"
        self._seed_three_bars(db_engine, ind)
        with db_engine.connect() as conn:
            s = IndustryIndicatorStateRepository(conn).get_at(
                ind, "Weekly",
                datetime(2030, 1, 1, tzinfo=UTC),
            )
        assert s is None

    def test_get_at_or_before(self, db_engine):
        ind = "GetAtBefore"
        self._seed_three_bars(db_engine, ind)
        # Ask for a timestamp 3 days AFTER the middle bar
        with db_engine.connect() as conn:
            s = IndustryIndicatorStateRepository(conn).get_at_or_before(
                ind, "Weekly",
                datetime(2025, 10, 16, tzinfo=UTC),
            )
        # Should return the middle bar (Oct 13)
        assert s is not None
        assert s.bar_timestamp == datetime(2025, 10, 13, tzinfo=UTC)

    def test_get_latest_for_industry(self, db_engine):
        ind = "Latest"
        self._seed_three_bars(db_engine, ind)
        with db_engine.connect() as conn:
            s = IndustryIndicatorStateRepository(
                conn,
            ).get_latest_for_industry(ind, "Weekly")
        assert s is not None
        assert s.bar_timestamp == datetime(2025, 10, 20, tzinfo=UTC)

    def test_get_latest_no_rows_returns_none(self, db_engine):
        with transaction(db_engine) as conn:
            _seed_industry(conn, "EmptyLatest")
        with db_engine.connect() as conn:
            s = IndustryIndicatorStateRepository(
                conn,
            ).get_latest_for_industry("EmptyLatest", "Weekly")
        assert s is None

    def test_get_history_newest_first(self, db_engine):
        ind = "History"
        self._seed_three_bars(db_engine, ind)
        with db_engine.connect() as conn:
            history = IndustryIndicatorStateRepository(
                conn,
            ).get_history_for_industry(ind, "Weekly")
        assert len(history) == 3
        # Newest first
        assert history[0].bar_timestamp > history[2].bar_timestamp

    def test_get_history_with_limit(self, db_engine):
        ind = "HistoryLim"
        self._seed_three_bars(db_engine, ind)
        with db_engine.connect() as conn:
            history = IndustryIndicatorStateRepository(
                conn,
            ).get_history_for_industry(ind, "Weekly", limit=2)
        assert len(history) == 2

    def test_get_history_with_date_bounds(self, db_engine):
        ind = "HistoryBounds"
        self._seed_three_bars(db_engine, ind)
        with db_engine.connect() as conn:
            history = IndustryIndicatorStateRepository(
                conn,
            ).get_history_for_industry(
                ind, "Weekly",
                from_dt=datetime(2025, 10, 13, tzinfo=UTC),
                to_dt=datetime(2025, 10, 13, tzinfo=UTC),
            )
        assert len(history) == 1
        assert history[0].med_ema == 101.0

    def test_count_for_industry(self, db_engine):
        ind = "Count"
        self._seed_three_bars(db_engine, ind)
        with db_engine.connect() as conn:
            n = IndustryIndicatorStateRepository(
                conn,
            ).count_for_industry(ind, "Weekly")
        assert n == 3
