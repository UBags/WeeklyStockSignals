"""Integration tests for SignalsReader.

DB integration: tests use the real Current + Historical repos against
the `db_engine` fixture (no mocks). Covers cache-hit / cache-miss
paths, edge cases on N, and the no-UNION ordering guarantee.
"""
from __future__ import annotations

from datetime import datetime, timedelta, UTC

import pytest

from trip_ema.domain import TickerRef
from trip_ema.domain.signal_event import SignalEvent
from trip_ema.persistence import transaction
from trip_ema.persistence.current_signals_repo import (
    CurrentSignalsRepository,
)
from trip_ema.persistence.historical_signals_repo import (
    HistoricalSignalsRepository,
)
from trip_ema.persistence.tickers_repo import TickersRepository
from trip_ema.signals.signals_reader import SignalsReader


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
    bar_timestamp: datetime,
    signal_type: str = "BUY",
    regime_before: int = 0,
    regime_after: int = 1,
    price_at_signal: float = 150.0,
    computed_at: datetime | None = None,
) -> SignalEvent:
    return SignalEvent(
        ticker_id=ticker_id,
        timeframe="Weekly",
        bar_timestamp=bar_timestamp,
        signal_type=signal_type,
        regime_before=regime_before,
        regime_after=regime_after,
        price_at_signal=price_at_signal,
        computed_at=computed_at or datetime.now(UTC),
    )


def _build_reader(conn) -> SignalsReader:
    return SignalsReader(
        current_repo=CurrentSignalsRepository(conn),
        historical_repo=HistoricalSignalsRepository(conn),
    )


# ============================================================
# last_n_for_ticker — current-hit path
# ============================================================

class TestLastNCurrentHit:

    def test_current_only_n1(self, db_engine):
        """Current has the signal; N=1 → 1 row from current only."""
        with transaction(db_engine) as conn:
            tid = _seed_ticker(conn)
            CurrentSignalsRepository(conn).insert_many([
                _make_signal(
                    tid, bar_timestamp=datetime(
                        2025, 10, 6, tzinfo=UTC,
                    ),
                ),
            ])

        with db_engine.connect() as conn:
            rows = _build_reader(conn).last_n_for_ticker(tid, 1)
        assert len(rows) == 1
        assert rows[0].ticker_id == tid

    def test_current_plus_historical(self, db_engine):
        """Current has 1; historical has 3; N=3 → 1 current + 2 historical."""
        base = datetime(2025, 1, 6, tzinfo=UTC)
        with transaction(db_engine) as conn:
            tid = _seed_ticker(conn)

            # Historical: 3 signals at increasing weeks
            HistoricalSignalsRepository(conn).insert_many([
                _make_signal(tid, bar_timestamp=base),
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
            # Current: signal at week 12 (latest)
            CurrentSignalsRepository(conn).insert_many([
                _make_signal(
                    tid, bar_timestamp=base + timedelta(weeks=12),
                    signal_type="BUY",
                ),
            ])

        with db_engine.connect() as conn:
            rows = _build_reader(conn).last_n_for_ticker(tid, 3)
        assert len(rows) == 3
        # First: current (week 12, BUY)
        assert rows[0].bar_timestamp == base + timedelta(weeks=12)
        # Then: 2 newest from historical (weeks 8 + 4)
        assert rows[1].bar_timestamp == base + timedelta(weeks=8)
        assert rows[2].bar_timestamp == base + timedelta(weeks=4)
        # Week 0 (oldest in historical) NOT included (N-1 = 2 from
        # historical, took the 2 newest)

    def test_n_larger_than_available_returns_what_exists(
        self, db_engine,
    ):
        base = datetime(2025, 1, 6, tzinfo=UTC)
        with transaction(db_engine) as conn:
            tid = _seed_ticker(conn)
            HistoricalSignalsRepository(conn).insert_many([
                _make_signal(tid, bar_timestamp=base),
            ])
            CurrentSignalsRepository(conn).insert_many([
                _make_signal(
                    tid, bar_timestamp=base + timedelta(weeks=4),
                ),
            ])

        with db_engine.connect() as conn:
            rows = _build_reader(conn).last_n_for_ticker(tid, 10)
        # Only 2 signals total exist; reader returns both, no padding
        assert len(rows) == 2


# ============================================================
# last_n_for_ticker — current-miss path
# ============================================================

class TestLastNCurrentMiss:

    def test_historical_only(self, db_engine):
        """Current has nothing for this ticker; historical has 3.
        N=3 → 3 from historical, no current query result."""
        base = datetime(2025, 1, 6, tzinfo=UTC)
        with transaction(db_engine) as conn:
            tid = _seed_ticker(conn)
            HistoricalSignalsRepository(conn).insert_many([
                _make_signal(tid, bar_timestamp=base),
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

        with db_engine.connect() as conn:
            rows = _build_reader(conn).last_n_for_ticker(tid, 3)
        assert len(rows) == 3
        # All 3 from historical, newest first
        assert rows[0].bar_timestamp == base + timedelta(weeks=8)
        assert rows[1].bar_timestamp == base + timedelta(weeks=4)
        assert rows[2].bar_timestamp == base

    def test_no_signals_at_all(self, db_engine):
        """Nothing in current, nothing in historical → empty."""
        with transaction(db_engine) as conn:
            tid = _seed_ticker(conn)

        with db_engine.connect() as conn:
            rows = _build_reader(conn).last_n_for_ticker(tid, 5)
        assert rows == []


# ============================================================
# last_n_for_ticker — edge cases on n
# ============================================================

class TestLastNEdgeCases:

    def test_n_zero_returns_empty(self, db_engine):
        with transaction(db_engine) as conn:
            tid = _seed_ticker(conn)
            CurrentSignalsRepository(conn).insert_many([
                _make_signal(
                    tid, bar_timestamp=datetime(
                        2025, 10, 6, tzinfo=UTC,
                    ),
                ),
            ])

        with db_engine.connect() as conn:
            rows = _build_reader(conn).last_n_for_ticker(tid, 0)
        assert rows == []

    def test_n_negative_raises(self, db_engine):
        with db_engine.connect() as conn:
            with pytest.raises(ValueError):
                _build_reader(conn).last_n_for_ticker(1, -1)


# ============================================================
# latest_for_ticker
# ============================================================

class TestLatestForTicker:

    def test_returns_current_when_present(self, db_engine):
        with transaction(db_engine) as conn:
            tid = _seed_ticker(conn)
            CurrentSignalsRepository(conn).insert_many([
                _make_signal(
                    tid, bar_timestamp=datetime(
                        2025, 10, 6, tzinfo=UTC,
                    ),
                    signal_type="BUY",
                ),
            ])
            HistoricalSignalsRepository(conn).insert_many([
                _make_signal(
                    tid, bar_timestamp=datetime(
                        2025, 9, 1, tzinfo=UTC,
                    ),
                    signal_type="SELL",
                    regime_before=1, regime_after=-1,
                ),
            ])

        with db_engine.connect() as conn:
            latest = _build_reader(conn).latest_for_ticker(tid)
        assert latest is not None
        # Returns the CURRENT one, not the historical
        assert latest.signal_type == "BUY"

    def test_falls_back_to_historical(self, db_engine):
        bt = datetime(2025, 9, 1, tzinfo=UTC)
        with transaction(db_engine) as conn:
            tid = _seed_ticker(conn)
            HistoricalSignalsRepository(conn).insert_many([
                _make_signal(tid, bar_timestamp=bt),
            ])

        with db_engine.connect() as conn:
            latest = _build_reader(conn).latest_for_ticker(tid)
        assert latest is not None
        assert latest.bar_timestamp == bt

    def test_returns_none_when_nothing(self, db_engine):
        with transaction(db_engine) as conn:
            tid = _seed_ticker(conn)

        with db_engine.connect() as conn:
            latest = _build_reader(conn).latest_for_ticker(tid)
        assert latest is None


# ============================================================
# Ordering invariant — no UNION drift
# ============================================================

class TestOrderingInvariant:

    def test_current_always_first_when_hit(self, db_engine):
        """Even when historical has signals at a LATER bar than
        current (shouldn't happen in normal cycle ordering, but
        defensive), the current row sorts first. This is the
        reader's contract: cache hit takes precedence."""
        base = datetime(2025, 1, 6, tzinfo=UTC)
        with transaction(db_engine) as conn:
            tid = _seed_ticker(conn)
            # Historical at week 20 (somehow newer than current —
            # not realistic in production but we want predictable
            # reader behavior)
            HistoricalSignalsRepository(conn).insert_many([
                _make_signal(
                    tid, bar_timestamp=base + timedelta(weeks=20),
                    signal_type="SELL",
                    regime_before=1, regime_after=-1,
                ),
            ])
            # Current at week 10 (older)
            CurrentSignalsRepository(conn).insert_many([
                _make_signal(
                    tid, bar_timestamp=base + timedelta(weeks=10),
                    signal_type="BUY",
                ),
            ])

        with db_engine.connect() as conn:
            rows = _build_reader(conn).last_n_for_ticker(tid, 2)
        assert len(rows) == 2
        # Current first regardless of bar_timestamp ordering
        assert rows[0].signal_type == "BUY"
        assert rows[1].signal_type == "SELL"
