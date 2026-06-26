"""Integration tests for CurrentSignalsRepository.

DB integration via the `db_engine` fixture. Tests CRUD + validation
+ idempotency. Seeds a parent ticker row (FK target) for each test.
"""
from __future__ import annotations

from datetime import datetime, UTC

import pytest
from sqlalchemy import text

from trip_ema.domain import TickerRef
from trip_ema.domain.signal_event import SignalEvent
from trip_ema.persistence import transaction
from trip_ema.persistence.current_signals_repo import (
    CurrentSignalsRepository,
)
from trip_ema.persistence.tickers_repo import TickersRepository


# ============================================================
# Helpers
# ============================================================

def _seed_ticker(conn, *, symbol: str = "RELIANCE") -> int:
    """Insert a parent ticker row, return its ticker_id."""
    ref = TickerRef("NSE", symbol, "EQ")
    return TickersRepository(conn).insert(
        ref, created_at=datetime.now(UTC),
    )


def _make_signal(
    ticker_id: int,
    *,
    timeframe: str = "Weekly",
    bar_timestamp: datetime | None = None,
    signal_type: str = "BUY",
    regime_before: int = 0,
    regime_after: int = 1,
    price_at_signal: float = 150.0,
) -> SignalEvent:
    if bar_timestamp is None:
        bar_timestamp = datetime(2025, 10, 6, tzinfo=UTC)
    return SignalEvent(
        ticker_id=ticker_id,
        timeframe=timeframe,
        bar_timestamp=bar_timestamp,
        signal_type=signal_type,
        regime_before=regime_before,
        regime_after=regime_after,
        price_at_signal=price_at_signal,
        computed_at=datetime.now(UTC),
    )


# ============================================================
# Writes — insert_many
# ============================================================

class TestInsertMany:

    def test_empty_input_no_op(self, db_engine):
        with transaction(db_engine) as conn:
            repo = CurrentSignalsRepository(conn)
            n = repo.insert_many([])
        assert n == 0

    def test_inserts_n_rows(self, db_engine):
        with transaction(db_engine) as conn:
            tid_a = _seed_ticker(conn, symbol="A")
            tid_b = _seed_ticker(conn, symbol="B")
            tid_c = _seed_ticker(conn, symbol="C")
            repo = CurrentSignalsRepository(conn)
            n = repo.insert_many([
                _make_signal(tid_a),
                _make_signal(tid_b, signal_type="SELL",
                             regime_before=0, regime_after=-1),
                _make_signal(tid_c, signal_type="EXIT_FROM_BUY",
                             regime_before=1, regime_after=0),
            ])
        assert n == 3

        with db_engine.connect() as conn:
            count = conn.execute(text(
                "SELECT COUNT(*) FROM current_signals"
            )).scalar_one()
        assert count == 3

    def test_idempotent_on_ticker_id_pk(self, db_engine):
        """Re-inserting for the same ticker overwrites the row."""
        with transaction(db_engine) as conn:
            tid = _seed_ticker(conn, symbol="IDEMP")
            repo = CurrentSignalsRepository(conn)
            repo.insert_many([_make_signal(
                tid, signal_type="BUY",
                regime_before=0, regime_after=1,
                price_at_signal=100.0,
            )])
            repo.insert_many([_make_signal(
                tid, signal_type="SELL",
                regime_before=0, regime_after=-1,
                price_at_signal=200.0,
            )])

        with db_engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT signal_type, price_at_signal
                FROM current_signals
                WHERE ticker_id = :tid
            """), {"tid": tid}).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "SELL"
        assert float(rows[0][1]) == 200.0


class TestInsertManyValidation:

    def test_rejects_unknown_signal_type(self, db_engine):
        with transaction(db_engine) as conn:
            tid = _seed_ticker(conn)
            repo = CurrentSignalsRepository(conn)
            with pytest.raises(ValueError, match="signal_type"):
                repo.insert_many([_make_signal(
                    tid, signal_type="BOGUS",
                )])

    @pytest.mark.parametrize("bad_regime", [-2, 2, 5])
    def test_rejects_invalid_regime_before(self, db_engine, bad_regime):
        with transaction(db_engine) as conn:
            tid = _seed_ticker(conn)
            repo = CurrentSignalsRepository(conn)
            with pytest.raises(ValueError, match="regime_before"):
                repo.insert_many([_make_signal(
                    tid, regime_before=bad_regime, regime_after=1,
                )])

    @pytest.mark.parametrize("bad_regime", [-3, 7])
    def test_rejects_invalid_regime_after(self, db_engine, bad_regime):
        with transaction(db_engine) as conn:
            tid = _seed_ticker(conn)
            repo = CurrentSignalsRepository(conn)
            with pytest.raises(ValueError, match="regime_after"):
                repo.insert_many([_make_signal(
                    tid, regime_before=0, regime_after=bad_regime,
                )])

    def test_rejects_equal_regimes(self, db_engine):
        """A signal IS a regime change by definition. Equal regimes
        → not a signal → reject."""
        with transaction(db_engine) as conn:
            tid = _seed_ticker(conn)
            repo = CurrentSignalsRepository(conn)
            with pytest.raises(
                ValueError, match="regime_before == regime_after",
            ):
                repo.insert_many([_make_signal(
                    tid, regime_before=1, regime_after=1,
                )])

    def test_rejects_nonpositive_price(self, db_engine):
        with transaction(db_engine) as conn:
            tid = _seed_ticker(conn)
            repo = CurrentSignalsRepository(conn)
            with pytest.raises(ValueError, match="price_at_signal"):
                repo.insert_many([_make_signal(
                    tid, price_at_signal=0.0,
                )])

    def test_all_or_nothing_on_validation_failure(self, db_engine):
        """If any row is invalid, no rows are written. Validates
        before any DB write."""
        with transaction(db_engine) as conn:
            tid_a = _seed_ticker(conn, symbol="A")
            tid_b = _seed_ticker(conn, symbol="B")
            repo = CurrentSignalsRepository(conn)
            good = _make_signal(tid_a)
            bad = _make_signal(tid_b, signal_type="GARBAGE")
            with pytest.raises(ValueError):
                repo.insert_many([good, bad])

        with db_engine.connect() as conn:
            count = conn.execute(text(
                "SELECT COUNT(*) FROM current_signals"
            )).scalar_one()
        assert count == 0


# ============================================================
# Writes — truncate
# ============================================================

class TestTruncate:

    def test_removes_all_rows(self, db_engine):
        with transaction(db_engine) as conn:
            tid_a = _seed_ticker(conn, symbol="A")
            tid_b = _seed_ticker(conn, symbol="B")
            repo = CurrentSignalsRepository(conn)
            repo.insert_many([
                _make_signal(tid_a),
                _make_signal(tid_b, signal_type="SELL",
                             regime_before=0, regime_after=-1),
            ])

        with transaction(db_engine) as conn:
            CurrentSignalsRepository(conn).truncate()

        with db_engine.connect() as conn:
            count = conn.execute(text(
                "SELECT COUNT(*) FROM current_signals"
            )).scalar_one()
        assert count == 0

    def test_truncate_empty_is_no_op(self, db_engine):
        """TRUNCATE on already-empty table doesn't raise."""
        with transaction(db_engine) as conn:
            CurrentSignalsRepository(conn).truncate()


# ============================================================
# Reads
# ============================================================

class TestReads:

    def test_get_for_ticker_hit(self, db_engine):
        with transaction(db_engine) as conn:
            tid = _seed_ticker(conn)
            repo = CurrentSignalsRepository(conn)
            repo.insert_many([_make_signal(tid)])

        with db_engine.connect() as conn:
            s = CurrentSignalsRepository(conn).get_for_ticker(tid)
        assert s is not None
        assert s.ticker_id == tid
        assert s.signal_type == "BUY"

    def test_get_for_ticker_miss(self, db_engine):
        with db_engine.connect() as conn:
            s = CurrentSignalsRepository(conn).get_for_ticker(99999)
        assert s is None

    def test_list_all_orders_by_ticker_id(self, db_engine):
        with transaction(db_engine) as conn:
            tid_c = _seed_ticker(conn, symbol="C")
            tid_a = _seed_ticker(conn, symbol="A")
            tid_b = _seed_ticker(conn, symbol="B")
            repo = CurrentSignalsRepository(conn)
            # Insert in non-PK-sorted order
            repo.insert_many([
                _make_signal(tid_c),
                _make_signal(tid_a),
                _make_signal(tid_b),
            ])

        with db_engine.connect() as conn:
            rows = CurrentSignalsRepository(conn).list_all()

        assert len(rows) == 3
        # Sorted by ticker_id ascending
        assert rows[0].ticker_id < rows[1].ticker_id < rows[2].ticker_id

    def test_list_all_empty(self, db_engine):
        with db_engine.connect() as conn:
            rows = CurrentSignalsRepository(conn).list_all()
        assert rows == []

    def test_count(self, db_engine):
        with transaction(db_engine) as conn:
            tid_a = _seed_ticker(conn, symbol="A")
            tid_b = _seed_ticker(conn, symbol="B")
            repo = CurrentSignalsRepository(conn)
            repo.insert_many([
                _make_signal(tid_a),
                _make_signal(tid_b, signal_type="SELL",
                             regime_before=0, regime_after=-1),
            ])

        with db_engine.connect() as conn:
            n = CurrentSignalsRepository(conn).count()
        assert n == 2

    def test_count_empty(self, db_engine):
        with db_engine.connect() as conn:
            n = CurrentSignalsRepository(conn).count()
        assert n == 0
