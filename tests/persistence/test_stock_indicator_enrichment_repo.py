"""Tests for StockIndicatorEnrichmentRepository.

Mirrors test_stock_indicator_state_repo.py but also exercises the FK
chain — each enrichment row requires a state row to exist first.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from trip_ema.domain.stock_indicator_enrichment import (
    StockIndicatorEnrichment,
)
from trip_ema.domain.stock_indicator_state import StockIndicatorState
from trip_ema.persistence import transaction
from trip_ema.persistence.stock_indicator_enrichment_repo import (
    StockIndicatorEnrichmentRepository,
)
from trip_ema.persistence.stock_indicator_state_repo import (
    StockIndicatorStateRepository,
)


# ============================================================
# Helpers
# ============================================================

_BT = lambda days_offset: datetime(  # noqa: E731
    2026, 6, 1, tzinfo=timezone.utc,
) + timedelta(days=days_offset)


def _seed_ticker(conn, ticker_id: int = 1) -> int:
    conn.execute(text("""
        INSERT INTO tickers (ticker_id, exchange, symbol)
        VALUES (:id, 'NSE', :sym)
        ON CONFLICT (ticker_id) DO NOTHING
    """), {"id": ticker_id, "sym": f"TEST{ticker_id}"})
    return ticker_id


def _seed_state(
    conn, ticker_id: int, bar_timestamp: datetime,
    *, timeframe: str = "1W",
) -> None:
    """Insert a state row so the enrichment FK is satisfied."""
    StockIndicatorStateRepository(conn).upsert_one(
        StockIndicatorState(
            ticker_id=ticker_id,
            timeframe=timeframe,
            bar_timestamp=bar_timestamp,
            med_ema=100.0,
            long_ema=100.0,
            longest_ema=100.0,
            atr=5.0,
            upper_inner_band=105.0,
            lower_inner_band=95.0,
            upper_mid_band=110.0,
            lower_mid_band=90.0,
            upper_outer_band=120.0,
            lower_outer_band=80.0,
            display_regime=1,
            computed_at=datetime.now(timezone.utc),
        ),
    )


def _make_enrichment(
    ticker_id: int,
    bar_timestamp: datetime,
    *,
    timeframe: str = "1W",
    med_pattern_label: str = "NA",
    bars_since_med_turn: int | None = 5,
) -> StockIndicatorEnrichment:
    return StockIndicatorEnrichment(
        ticker_id=ticker_id,
        timeframe=timeframe,
        bar_timestamp=bar_timestamp,
        pierces_upper_mid=False,
        pierces_lower_mid=False,
        dist_to_med_in_hl_units=0.5,
        dist_to_long_in_hl_units=0.7,
        dist_to_longest_in_hl_units=1.0,
        mean_dist_to_med_in_hl_units=0.4,
        mean_dist_to_long_in_hl_units=0.6,
        mean_dist_to_longest_in_hl_units=0.9,
        bars_since_med_turn=bars_since_med_turn,
        bars_since_long_turn=10,
        bars_since_longest_turn=20,
        pct_change_since_med_turn=0.02,
        pct_change_since_long_turn=0.05,
        pct_change_since_longest_turn=0.10,
        med_pattern_label=med_pattern_label,
        long_pattern_label="NA",
        longest_pattern_label="NA",
        med_turn_buy=False,
        med_turn_sell=False,
        long_turn_buy=False,
        long_turn_sell=False,
        longest_turn_buy=False,
        longest_turn_sell=False,
        computed_at=datetime.now(timezone.utc),
    )


# ============================================================
# Writes
# ============================================================

class TestStockIndicatorEnrichmentUpserts:

    def test_upsert_one_after_seeding_state(self, db_engine):
        with transaction(db_engine) as conn:
            tid = _seed_ticker(conn)
            _seed_state(conn, tid, _BT(0))
            StockIndicatorEnrichmentRepository(conn).upsert_one(
                _make_enrichment(tid, _BT(0)),
            )
        with db_engine.connect() as conn:
            row = StockIndicatorEnrichmentRepository(conn).get_at(
                tid, "1W", _BT(0),
            )
        assert row is not None
        assert row.ticker_id == tid
        assert row.med_pattern_label == "NA"

    def test_upsert_one_is_idempotent(self, db_engine):
        with transaction(db_engine) as conn:
            tid = _seed_ticker(conn)
            _seed_state(conn, tid, _BT(0))
            r = StockIndicatorEnrichmentRepository(conn)
            r.upsert_one(_make_enrichment(tid, _BT(0),
                                          bars_since_med_turn=5))
            r.upsert_one(_make_enrichment(tid, _BT(0),
                                          bars_since_med_turn=99))
        with db_engine.connect() as conn:
            row = StockIndicatorEnrichmentRepository(conn).get_at(
                tid, "1W", _BT(0),
            )
        assert row.bars_since_med_turn == 99

    def test_upsert_many_batch(self, db_engine):
        with transaction(db_engine) as conn:
            tid = _seed_ticker(conn)
            states_then_enrichments = []
            for i in range(5):
                _seed_state(conn, tid, _BT(i))
                states_then_enrichments.append(
                    _make_enrichment(tid, _BT(i)),
                )
            n = StockIndicatorEnrichmentRepository(conn).upsert_many(
                states_then_enrichments,
            )
        assert n == 5
        with db_engine.connect() as conn:
            assert StockIndicatorEnrichmentRepository(
                conn,
            ).count_for_ticker(tid, "1W") == 5

    def test_fk_violation_when_no_state_row(self, db_engine):
        """Enrichment row without a state row should fail with FK
        IntegrityError. This is the schema-level safety net behind
        the runner's count_for_ticker pre-check."""
        from sqlalchemy.exc import IntegrityError
        with transaction(db_engine) as conn:
            tid = _seed_ticker(conn)
            # Note: NO _seed_state call here.
            with pytest.raises(IntegrityError, match=r"foreign key"):
                StockIndicatorEnrichmentRepository(conn).upsert_one(
                    _make_enrichment(tid, _BT(0)),
                )


# ============================================================
# Validation
# ============================================================

class TestStockIndicatorEnrichmentValidation:

    def test_rejects_unknown_pattern_label(self, db_engine):
        with transaction(db_engine) as conn:
            tid = _seed_ticker(conn)
            _seed_state(conn, tid, _BT(0))
            with pytest.raises(ValueError, match="pattern_label"):
                StockIndicatorEnrichmentRepository(conn).upsert_one(
                    _make_enrichment(
                        tid, _BT(0),
                        med_pattern_label="MADE_UP_LABEL",
                    ),
                )

    def test_accepts_all_canonical_pattern_labels(self, db_engine):
        from trip_ema.domain.stock_indicator_enrichment import (
            PATTERN_LABELS,
        )
        with transaction(db_engine) as conn:
            tid = _seed_ticker(conn)
            for i, label in enumerate(PATTERN_LABELS):
                _seed_state(conn, tid, _BT(i))
                StockIndicatorEnrichmentRepository(conn).upsert_one(
                    _make_enrichment(
                        tid, _BT(i), med_pattern_label=label,
                    ),
                )
        with db_engine.connect() as conn:
            assert StockIndicatorEnrichmentRepository(
                conn,
            ).count_for_ticker(tid, "1W") == len(PATTERN_LABELS)


# ============================================================
# Reads
# ============================================================

class TestStockIndicatorEnrichmentReads:

    def test_get_latest_for_ticker(self, db_engine):
        with transaction(db_engine) as conn:
            tid = _seed_ticker(conn)
            r = StockIndicatorEnrichmentRepository(conn)
            for i in (0, 7, 14):
                _seed_state(conn, tid, _BT(i))
                r.upsert_one(_make_enrichment(
                    tid, _BT(i), bars_since_med_turn=i,
                ))
        with db_engine.connect() as conn:
            latest = StockIndicatorEnrichmentRepository(
                conn,
            ).get_latest_for_ticker(tid, "1W")
        assert latest is not None
        assert latest.bar_timestamp == _BT(14)
        assert latest.bars_since_med_turn == 14

    def test_get_at_or_before(self, db_engine):
        with transaction(db_engine) as conn:
            tid = _seed_ticker(conn)
            r = StockIndicatorEnrichmentRepository(conn)
            _seed_state(conn, tid, _BT(0))
            r.upsert_one(_make_enrichment(tid, _BT(0)))
            _seed_state(conn, tid, _BT(7))
            r.upsert_one(_make_enrichment(tid, _BT(7)))
        with db_engine.connect() as conn:
            row = StockIndicatorEnrichmentRepository(
                conn,
            ).get_at_or_before(tid, "1W", _BT(10))
        assert row is not None
        assert row.bar_timestamp == _BT(7)

    def test_get_history_newest_first(self, db_engine):
        with transaction(db_engine) as conn:
            tid = _seed_ticker(conn)
            r = StockIndicatorEnrichmentRepository(conn)
            for i in range(5):
                _seed_state(conn, tid, _BT(i * 7))
                r.upsert_one(_make_enrichment(tid, _BT(i * 7)))
        with db_engine.connect() as conn:
            history = StockIndicatorEnrichmentRepository(
                conn,
            ).get_history_for_ticker(tid, "1W")
        assert len(history) == 5
        timestamps = [e.bar_timestamp for e in history]
        assert timestamps == sorted(timestamps, reverse=True)


# ============================================================
# Delete + cascade
# ============================================================

class TestStockIndicatorEnrichmentDelete:

    def test_delete_for_ticker(self, db_engine):
        with transaction(db_engine) as conn:
            tid = _seed_ticker(conn)
            r = StockIndicatorEnrichmentRepository(conn)
            for i in range(3):
                _seed_state(conn, tid, _BT(i * 7))
                r.upsert_one(_make_enrichment(tid, _BT(i * 7)))
        with transaction(db_engine) as conn:
            n = StockIndicatorEnrichmentRepository(
                conn,
            ).delete_for_ticker(tid, "1W")
        assert n == 3

    def test_state_delete_cascades_to_enrichment(self, db_engine):
        """Schema-level CASCADE: deleting the state row drops the
        enrichment row too (FK ON DELETE CASCADE from migration 0021).
        Locks in the architectural invariant."""
        with transaction(db_engine) as conn:
            tid = _seed_ticker(conn)
            _seed_state(conn, tid, _BT(0))
            StockIndicatorEnrichmentRepository(conn).upsert_one(
                _make_enrichment(tid, _BT(0)),
            )

        # Sanity: both rows present
        with db_engine.connect() as conn:
            assert StockIndicatorStateRepository(
                conn,
            ).count_for_ticker(tid, "1W") == 1
            assert StockIndicatorEnrichmentRepository(
                conn,
            ).count_for_ticker(tid, "1W") == 1

        # Delete state row → enrichment row should cascade away
        with transaction(db_engine) as conn:
            StockIndicatorStateRepository(conn).delete_for_ticker(
                tid, "1W",
            )

        with db_engine.connect() as conn:
            assert StockIndicatorStateRepository(
                conn,
            ).count_for_ticker(tid, "1W") == 0
            assert StockIndicatorEnrichmentRepository(
                conn,
            ).count_for_ticker(tid, "1W") == 0, (
                "Enrichment row should have cascaded away with "
                "the state row"
            )
