"""Integration tests for trip_ema.orchestrator.rotate_signals.

Verifies the atomic CTE-driven rotation:
  - rows leave current_signals
  - same rows arrive in historical_signals
  - archived_at is stamped on the new historical rows
  - all rows in one rotation share the same archived_at (NOW()
    is transaction-fixed in PostgreSQL)
"""
from __future__ import annotations

from datetime import datetime, UTC

import pytest
from sqlalchemy import text

from trip_ema.domain import TickerRef
from trip_ema.domain.signal_event import SignalEvent
from trip_ema.orchestrator.rotate_signals import (
    rotate_current_to_historical,
)
from trip_ema.persistence import transaction
from trip_ema.persistence.current_signals_repo import (
    CurrentSignalsRepository,
)
from trip_ema.persistence.historical_signals_repo import (
    HistoricalSignalsRepository,
)
from trip_ema.persistence.tickers_repo import TickersRepository


# ============================================================
# Helpers
# ============================================================

def _seed_ticker(conn, *, symbol: str = "RELIANCE") -> int:
    ref = TickerRef("NSE", symbol, "EQ")
    return TickersRepository(conn).insert(
        ref, created_at=datetime.now(UTC),
    )


def _make_signal(
    ticker_id: int,
    *,
    bar_timestamp: datetime | None = None,
    signal_type: str = "BUY",
    regime_before: int = 0,
    regime_after: int = 1,
    price_at_signal: float = 150.0,
    computed_at: datetime | None = None,
) -> SignalEvent:
    return SignalEvent(
        ticker_id=ticker_id,
        timeframe="1W",
        bar_timestamp=bar_timestamp or datetime(2025, 10, 6, tzinfo=UTC),
        signal_type=signal_type,
        regime_before=regime_before,
        regime_after=regime_after,
        price_at_signal=price_at_signal,
        computed_at=computed_at or datetime.now(UTC),
    )


# ============================================================
# Basic semantics
# ============================================================

class TestRotateBasics:

    def test_empty_returns_zero(self, db_engine):
        """current_signals already empty → no-op, returns 0."""
        with transaction(db_engine) as conn:
            n = rotate_current_to_historical(conn)
        assert n == 0

        with db_engine.connect() as conn:
            assert HistoricalSignalsRepository(conn).count() == 0

    def test_moves_one_row(self, db_engine):
        with transaction(db_engine) as conn:
            tid = _seed_ticker(conn)
            CurrentSignalsRepository(conn).insert_many(
                [_make_signal(tid)],
            )

        with transaction(db_engine) as conn:
            n = rotate_current_to_historical(conn)
        assert n == 1

        with db_engine.connect() as conn:
            assert CurrentSignalsRepository(conn).count() == 0
            assert HistoricalSignalsRepository(conn).count() == 1

    def test_moves_n_rows(self, db_engine):
        with transaction(db_engine) as conn:
            tids = [
                _seed_ticker(conn, symbol="A"),
                _seed_ticker(conn, symbol="B"),
                _seed_ticker(conn, symbol="C"),
            ]
            CurrentSignalsRepository(conn).insert_many([
                _make_signal(tids[0]),
                _make_signal(
                    tids[1], signal_type="SELL",
                    regime_before=0, regime_after=-1,
                ),
                _make_signal(
                    tids[2], signal_type="EXIT_FROM_BUY",
                    regime_before=1, regime_after=0,
                ),
            ])

        with transaction(db_engine) as conn:
            n = rotate_current_to_historical(conn)
        assert n == 3

        with db_engine.connect() as conn:
            assert CurrentSignalsRepository(conn).count() == 0
            assert HistoricalSignalsRepository(conn).count() == 3


# ============================================================
# Column-by-column fidelity
# ============================================================

class TestColumnFidelity:
    """All eight signal fields are copied verbatim into
    historical_signals (only archived_at is newly added)."""

    def test_columns_preserved(self, db_engine):
        bt = datetime(2025, 10, 6, tzinfo=UTC)
        ca = datetime(2025, 10, 9, 14, 30, tzinfo=UTC)
        with transaction(db_engine) as conn:
            tid = _seed_ticker(conn)
            CurrentSignalsRepository(conn).insert_many([
                _make_signal(
                    tid,
                    bar_timestamp=bt,
                    signal_type="EXIT_FROM_SELL",
                    regime_before=-1,
                    regime_after=0,
                    price_at_signal=247.85,
                    computed_at=ca,
                ),
            ])

        with transaction(db_engine) as conn:
            rotate_current_to_historical(conn)

        with db_engine.connect() as conn:
            row = conn.execute(text("""
                SELECT ticker_id, timeframe, bar_timestamp,
                       signal_type, regime_before, regime_after,
                       price_at_signal, computed_at
                  FROM historical_signals
                 WHERE ticker_id = :tid
            """), {"tid": tid}).fetchone()

        assert int(row[0]) == tid
        assert row[1] == "1W"
        assert row[2] == bt
        assert row[3] == "EXIT_FROM_SELL"
        assert int(row[4]) == -1
        assert int(row[5]) == 0
        assert float(row[6]) == 247.85
        assert row[7] == ca


# ============================================================
# archived_at semantics
# ============================================================

class TestArchivedAt:

    def test_archived_at_populated(self, db_engine):
        with transaction(db_engine) as conn:
            tid = _seed_ticker(conn)
            CurrentSignalsRepository(conn).insert_many(
                [_make_signal(tid)],
            )

        with transaction(db_engine) as conn:
            rotate_current_to_historical(conn)

        with db_engine.connect() as conn:
            row = conn.execute(text("""
                SELECT archived_at FROM historical_signals
                 WHERE ticker_id = :tid
            """), {"tid": tid}).fetchone()
        assert row[0] is not None

    def test_all_rows_share_same_archived_at(self, db_engine):
        """NOW() is transaction-fixed in PostgreSQL — every row
        inserted in one rotation statement should get the SAME
        archived_at. Useful for audit queries like 'which signals
        were archived in cycle X'."""
        with transaction(db_engine) as conn:
            tids = [
                _seed_ticker(conn, symbol="A"),
                _seed_ticker(conn, symbol="B"),
                _seed_ticker(conn, symbol="C"),
            ]
            CurrentSignalsRepository(conn).insert_many([
                _make_signal(tids[0]),
                _make_signal(
                    tids[1], signal_type="SELL",
                    regime_before=0, regime_after=-1,
                ),
                _make_signal(
                    tids[2], signal_type="EXIT_FROM_BUY",
                    regime_before=1, regime_after=0,
                ),
            ])

        with transaction(db_engine) as conn:
            rotate_current_to_historical(conn)

        with db_engine.connect() as conn:
            archived_set = {
                row[0] for row in conn.execute(text(
                    "SELECT archived_at FROM historical_signals"
                ))
            }
        assert len(archived_set) == 1   # single distinct value


# ============================================================
# Consecutive rotations
# ============================================================

class TestConsecutiveRotations:
    """Simulate two cycles' worth of rotation."""

    def test_two_cycles_accumulate_in_historical(self, db_engine):
        """Cycle 1: insert 2 signals, rotate.
        Cycle 2: insert 3 signals, rotate.
        Historical should now have 5 total rows."""
        with transaction(db_engine) as conn:
            tids = [
                _seed_ticker(conn, symbol="A"),
                _seed_ticker(conn, symbol="B"),
                _seed_ticker(conn, symbol="C"),
            ]

        # Cycle 1
        with transaction(db_engine) as conn:
            CurrentSignalsRepository(conn).insert_many([
                _make_signal(tids[0]),
                _make_signal(
                    tids[1], signal_type="SELL",
                    regime_before=0, regime_after=-1,
                ),
            ])
        with transaction(db_engine) as conn:
            n1 = rotate_current_to_historical(conn)
        assert n1 == 2

        # Cycle 2
        with transaction(db_engine) as conn:
            CurrentSignalsRepository(conn).insert_many([
                _make_signal(
                    tids[0],
                    bar_timestamp=datetime(
                        2025, 10, 13, tzinfo=UTC,
                    ),
                ),
                _make_signal(
                    tids[1], signal_type="EXIT_FROM_SELL",
                    regime_before=-1, regime_after=0,
                    bar_timestamp=datetime(
                        2025, 10, 13, tzinfo=UTC,
                    ),
                ),
                _make_signal(
                    tids[2], signal_type="BUY",
                    bar_timestamp=datetime(
                        2025, 10, 13, tzinfo=UTC,
                    ),
                ),
            ])
        with transaction(db_engine) as conn:
            n2 = rotate_current_to_historical(conn)
        assert n2 == 3

        with db_engine.connect() as conn:
            assert CurrentSignalsRepository(conn).count() == 0
            assert HistoricalSignalsRepository(conn).count() == 5

    def test_consecutive_rotations_have_distinct_archived_at(
        self, db_engine,
    ):
        """Rotations from different transactions get different
        archived_at (NOW() is per-transaction)."""
        with transaction(db_engine) as conn:
            tid = _seed_ticker(conn)

        # Cycle 1
        with transaction(db_engine) as conn:
            CurrentSignalsRepository(conn).insert_many(
                [_make_signal(tid)],
            )
        with transaction(db_engine) as conn:
            rotate_current_to_historical(conn)

        # Cycle 2 (some time later — sleep a tiny bit to ensure
        # NOW() advances)
        import time
        time.sleep(0.05)

        with transaction(db_engine) as conn:
            CurrentSignalsRepository(conn).insert_many([
                _make_signal(
                    tid,
                    bar_timestamp=datetime(
                        2025, 10, 13, tzinfo=UTC,
                    ),
                    signal_type="SELL",
                    regime_before=1, regime_after=-1,
                ),
            ])
        with transaction(db_engine) as conn:
            rotate_current_to_historical(conn)

        with db_engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT archived_at FROM historical_signals
                 WHERE ticker_id = :tid
                 ORDER BY archived_at
            """), {"tid": tid}).fetchall()
        assert len(rows) == 2
        # Two different rotation transactions → two different
        # archived_at values
        assert rows[0][0] != rows[1][0]
        assert rows[0][0] < rows[1][0]


# ============================================================
# Same-bar re-detection invariant
# ============================================================

class TestSameBarRotation:
    """Rotation supports the design's same-bar re-detection: if
    cycle N emitted a signal at bar B and cycle N+1 re-detects the
    same regime change at the same bar B, BOTH cycles' versions
    end up in historical_signals (no dedup, no uniqueness)."""

    def test_same_bar_emits_into_historical_twice(self, db_engine):
        bt = datetime(2025, 10, 6, tzinfo=UTC)
        ca1 = datetime(2025, 10, 7, 9, 30, tzinfo=UTC)
        ca2 = datetime(2025, 10, 7, 12, 15, tzinfo=UTC)
        with transaction(db_engine) as conn:
            tid = _seed_ticker(conn)

        # Cycle 1: signal at bar bt, computed_at ca1
        with transaction(db_engine) as conn:
            CurrentSignalsRepository(conn).insert_many([
                _make_signal(
                    tid, bar_timestamp=bt, computed_at=ca1,
                ),
            ])
        with transaction(db_engine) as conn:
            rotate_current_to_historical(conn)

        # Cycle 2: same bar bt re-detected (still mid-week);
        # new computed_at ca2
        with transaction(db_engine) as conn:
            CurrentSignalsRepository(conn).insert_many([
                _make_signal(
                    tid, bar_timestamp=bt, computed_at=ca2,
                ),
            ])
        with transaction(db_engine) as conn:
            rotate_current_to_historical(conn)

        with db_engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT computed_at FROM historical_signals
                 WHERE ticker_id = :tid AND bar_timestamp = :bt
                 ORDER BY computed_at
            """), {"tid": tid, "bt": bt}).fetchall()
        # Two distinct historical rows for the SAME bar
        assert len(rows) == 2
        assert rows[0][0] == ca1
        assert rows[1][0] == ca2
