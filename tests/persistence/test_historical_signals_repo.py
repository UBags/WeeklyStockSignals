"""Integration tests for HistoricalSignalsRepository.

DB integration via the `db_engine` fixture. Tests append-only writes
+ all read paths (filters, ordering, limit). No idempotency tests —
historical_signals is intentionally append-only with no uniqueness.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, UTC

import pytest
from sqlalchemy import text

from trip_ema.domain import TickerRef
from trip_ema.domain.signal_event import SignalEvent
from trip_ema.persistence import transaction
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
    if bar_timestamp is None:
        bar_timestamp = datetime(2025, 10, 6, tzinfo=UTC)
    if computed_at is None:
        computed_at = datetime.now(UTC)
    return SignalEvent(
        ticker_id=ticker_id,
        timeframe="Weekly",
        bar_timestamp=bar_timestamp,
        signal_type=signal_type,
        regime_before=regime_before,
        regime_after=regime_after,
        price_at_signal=price_at_signal,
        computed_at=computed_at,
    )


# ============================================================
# Writes — insert_many
# ============================================================

class TestInsertMany:

    def test_empty_input_no_op(self, db_engine):
        with transaction(db_engine) as conn:
            repo = HistoricalSignalsRepository(conn)
            n = repo.insert_many([])
        assert n == 0

    def test_appends_n_rows(self, db_engine):
        with transaction(db_engine) as conn:
            tid = _seed_ticker(conn)
            repo = HistoricalSignalsRepository(conn)
            base = datetime(2025, 1, 6, tzinfo=UTC)
            n = repo.insert_many([
                _make_signal(tid, bar_timestamp=base),
                _make_signal(
                    tid, bar_timestamp=base + timedelta(weeks=4),
                    signal_type="SELL", regime_before=1,
                    regime_after=-1,
                ),
                _make_signal(
                    tid, bar_timestamp=base + timedelta(weeks=8),
                    signal_type="EXIT_FROM_SELL",
                    regime_before=-1, regime_after=0,
                ),
            ])
        assert n == 3

        with db_engine.connect() as conn:
            count = HistoricalSignalsRepository(
                conn,
            ).count_for_ticker(tid)
        assert count == 3

    def test_rejects_duplicate_per_day(self, db_engine):
        """Per-day grain: UNIQUE(ticker_id, timeframe, signal_date)
        forbids a second historical row for the same ticker/day
        (rotation, which appended per-cycle copies, is gone)."""
        import pytest
        from sqlalchemy.exc import IntegrityError
        bt = datetime(2025, 10, 6, tzinfo=UTC)
        with transaction(db_engine) as conn:
            tid = _seed_ticker(conn)
            HistoricalSignalsRepository(conn).insert_many([_make_signal(
                tid, bar_timestamp=bt,
                computed_at=datetime(2025, 10, 8, tzinfo=UTC),
            )])
        with pytest.raises(IntegrityError):
            with transaction(db_engine) as conn:
                HistoricalSignalsRepository(conn).insert_many([_make_signal(
                    tid, bar_timestamp=bt,
                    computed_at=datetime(2025, 10, 9, tzinfo=UTC),
                )])

    def test_archived_at_uses_db_default_when_none(self, db_engine):
        with transaction(db_engine) as conn:
            tid = _seed_ticker(conn)
            repo = HistoricalSignalsRepository(conn)
            repo.insert_many(
                [_make_signal(tid)],
                archived_at=None,
            )

        with db_engine.connect() as conn:
            row = conn.execute(text("""
                SELECT archived_at FROM historical_signals
                 WHERE ticker_id = :tid
            """), {"tid": tid}).fetchone()
        assert row[0] is not None  # DB default NOW() fired

    def test_archived_at_explicit_value(self, db_engine):
        """All rows in one rotation batch share archived_at."""
        explicit = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        with transaction(db_engine) as conn:
            tid_a = _seed_ticker(conn, symbol="A")
            tid_b = _seed_ticker(conn, symbol="B")
            repo = HistoricalSignalsRepository(conn)
            repo.insert_many(
                [
                    _make_signal(tid_a),
                    _make_signal(
                        tid_b, signal_type="SELL",
                        regime_before=0, regime_after=-1,
                    ),
                ],
                archived_at=explicit,
            )

        with db_engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT archived_at FROM historical_signals
                 WHERE ticker_id IN (:a, :b)
                 ORDER BY ticker_id
            """), {"a": tid_a, "b": tid_b}).fetchall()
        # Both rows share the explicit archived_at
        assert rows[0][0] == explicit
        assert rows[1][0] == explicit


class TestInsertManyValidation:

    def test_rejects_unknown_signal_type(self, db_engine):
        with transaction(db_engine) as conn:
            tid = _seed_ticker(conn)
            repo = HistoricalSignalsRepository(conn)
            with pytest.raises(ValueError, match="signal_type"):
                repo.insert_many([_make_signal(
                    tid, signal_type="BOGUS",
                )])

    def test_rejects_equal_regimes(self, db_engine):
        with transaction(db_engine) as conn:
            tid = _seed_ticker(conn)
            repo = HistoricalSignalsRepository(conn)
            with pytest.raises(
                ValueError, match="regime_before == regime_after",
            ):
                repo.insert_many([_make_signal(
                    tid, regime_before=1, regime_after=1,
                )])

    def test_all_or_nothing_on_validation_failure(self, db_engine):
        with transaction(db_engine) as conn:
            tid = _seed_ticker(conn)
            repo = HistoricalSignalsRepository(conn)
            good = _make_signal(tid)
            bad = _make_signal(tid, price_at_signal=-1.0)
            with pytest.raises(ValueError):
                repo.insert_many([good, bad])

        with db_engine.connect() as conn:
            count = HistoricalSignalsRepository(conn).count()
        assert count == 0


# ============================================================
# Reads — get_for_ticker
# ============================================================

class TestGetForTicker:

    def _seed_three(self, db_engine, *, symbol: str = "RELIANCE") -> int:
        """Insert three signals at progressively later bars."""
        base = datetime(2025, 1, 6, tzinfo=UTC)
        with transaction(db_engine) as conn:
            tid = _seed_ticker(conn, symbol=symbol)
            HistoricalSignalsRepository(conn).insert_many([
                _make_signal(
                    tid, bar_timestamp=base,
                    signal_type="BUY",
                    regime_before=0, regime_after=1,
                ),
                _make_signal(
                    tid, bar_timestamp=base + timedelta(weeks=4),
                    signal_type="SELL",
                    regime_before=1, regime_after=-1,
                ),
                _make_signal(
                    tid, bar_timestamp=base + timedelta(weeks=8),
                    signal_type="EXIT_FROM_SELL",
                    regime_before=-1, regime_after=0,
                ),
            ])
        return tid

    def test_returns_all_newest_first(self, db_engine):
        tid = self._seed_three(db_engine)
        with db_engine.connect() as conn:
            rows = HistoricalSignalsRepository(
                conn,
            ).get_for_ticker(tid)
        assert len(rows) == 3
        # Newest bar first
        assert rows[0].signal_type == "EXIT_FROM_SELL"
        assert rows[1].signal_type == "SELL"
        assert rows[2].signal_type == "BUY"

    def test_with_limit(self, db_engine):
        tid = self._seed_three(db_engine)
        with db_engine.connect() as conn:
            rows = HistoricalSignalsRepository(
                conn,
            ).get_for_ticker(tid, limit=2)
        assert len(rows) == 2
        # Limit takes the freshest N
        assert rows[0].signal_type == "EXIT_FROM_SELL"
        assert rows[1].signal_type == "SELL"

    def test_with_kinds_filter(self, db_engine):
        tid = self._seed_three(db_engine)
        with db_engine.connect() as conn:
            rows = HistoricalSignalsRepository(
                conn,
            ).get_for_ticker(tid, kinds=["BUY", "SELL"])
        assert len(rows) == 2
        kinds = {r.signal_type for r in rows}
        assert kinds == {"BUY", "SELL"}

    def test_with_date_bounds(self, db_engine):
        tid = self._seed_three(db_engine)
        # Window covering only the middle bar (week 4)
        with db_engine.connect() as conn:
            rows = HistoricalSignalsRepository(
                conn,
            ).get_for_ticker(
                tid,
                from_date=date(2025, 1, 20),
                to_date=date(2025, 2, 10),
            )
        assert len(rows) == 1
        assert rows[0].signal_type == "SELL"

    def test_unknown_kind_raises(self, db_engine):
        tid = self._seed_three(db_engine)
        with db_engine.connect() as conn:
            with pytest.raises(ValueError, match="unknown signal_type"):
                HistoricalSignalsRepository(conn).get_for_ticker(
                    tid, kinds=["BUY", "TYPO"],
                )

    def test_empty_returns_empty_list(self, db_engine):
        with db_engine.connect() as conn:
            rows = HistoricalSignalsRepository(
                conn,
            ).get_for_ticker(99999)
        assert rows == []


# ============================================================
# Counts
# ============================================================

class TestCounts:

    def test_count_for_ticker(self, db_engine):
        with transaction(db_engine) as conn:
            tid_a = _seed_ticker(conn, symbol="A")
            tid_b = _seed_ticker(conn, symbol="B")
            repo = HistoricalSignalsRepository(conn)
            repo.insert_many([
                _make_signal(tid_a),
                _make_signal(
                    tid_a, bar_timestamp=datetime(
                        2025, 11, 1, tzinfo=UTC,
                    ),
                    signal_type="SELL",
                    regime_before=0, regime_after=-1,
                ),
                _make_signal(tid_b),
            ])

        with db_engine.connect() as conn:
            r = HistoricalSignalsRepository(conn)
            assert r.count_for_ticker(tid_a) == 2
            assert r.count_for_ticker(tid_b) == 1
            assert r.count() == 3

    def test_count_zero_for_unknown_ticker(self, db_engine):
        with db_engine.connect() as conn:
            r = HistoricalSignalsRepository(conn)
            assert r.count_for_ticker(99999) == 0
