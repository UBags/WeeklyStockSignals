"""Tests for StockIndicatorStateRepository.

Mirrors the test style of test_migration_0020.py — uses the
db_engine fixture from tests/persistence/conftest.py.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from trip_ema.domain.stock_indicator_state import StockIndicatorState
from trip_ema.persistence import transaction
from trip_ema.persistence.stock_indicator_state_repo import (
    StockIndicatorStateRepository,
)


# ============================================================
# Helpers
# ============================================================

def _seed_ticker(conn, ticker_id: int = 1) -> int:
    """Insert one ticker so the FK on stock_indicator_state can be
    satisfied. Post-0019 tickers schema only requires (exchange, symbol)
    as NOT NULL without defaults."""
    conn.execute(text("""
        INSERT INTO tickers (ticker_id, exchange, symbol)
        VALUES (:id, 'NSE', :sym)
        ON CONFLICT (ticker_id) DO NOTHING
    """), {"id": ticker_id, "sym": f"TEST{ticker_id}"})
    return ticker_id


def _make_state(
    ticker_id: int,
    bar_timestamp: datetime,
    *,
    timeframe: str = "1W",
    med_ema: float = 100.0,
    display_regime: int = 1,
    atr: float = 5.0,
) -> StockIndicatorState:
    return StockIndicatorState(
        ticker_id=ticker_id,
        timeframe=timeframe,
        bar_timestamp=bar_timestamp,
        med_ema=med_ema,
        long_ema=med_ema,
        longest_ema=med_ema,
        atr=atr,
        upper_inner_band=med_ema + 5,
        lower_inner_band=med_ema - 5,
        upper_mid_band=med_ema + 10,
        lower_mid_band=med_ema - 10,
        upper_outer_band=med_ema + 20,
        lower_outer_band=med_ema - 20,
        display_regime=display_regime,
        computed_at=datetime.now(timezone.utc),
    )


_BT = lambda days_offset: datetime(  # noqa: E731
    2026, 6, 1, tzinfo=timezone.utc,
) + timedelta(days=days_offset)


# ============================================================
# Writes
# ============================================================

class TestStockIndicatorStateUpserts:

    def test_upsert_one_inserts_then_reads_back(self, db_engine):
        with transaction(db_engine) as conn:
            tid = _seed_ticker(conn)
            StockIndicatorStateRepository(conn).upsert_one(
                _make_state(tid, _BT(0), display_regime=1),
            )
        with db_engine.connect() as conn:
            row = StockIndicatorStateRepository(conn).get_at(
                tid, "1W", _BT(0),
            )
        assert row is not None
        assert row.ticker_id == tid
        assert row.timeframe == "1W"
        assert row.display_regime == 1
        assert row.med_ema == 100.0

    def test_upsert_one_is_idempotent(self, db_engine):
        with transaction(db_engine) as conn:
            tid = _seed_ticker(conn)
            r = StockIndicatorStateRepository(conn)
            r.upsert_one(_make_state(tid, _BT(0), med_ema=100.0))
            r.upsert_one(_make_state(tid, _BT(0), med_ema=200.0))
        with db_engine.connect() as conn:
            row = StockIndicatorStateRepository(conn).get_at(
                tid, "1W", _BT(0),
            )
        assert row.med_ema == 200.0  # second write wins

    def test_upsert_many_batch_write(self, db_engine):
        with transaction(db_engine) as conn:
            tid = _seed_ticker(conn)
            states = [_make_state(tid, _BT(i)) for i in range(5)]
            n = StockIndicatorStateRepository(conn).upsert_many(states)
        assert n == 5
        with db_engine.connect() as conn:
            cnt = StockIndicatorStateRepository(
                conn,
            ).count_for_ticker(tid, "1W")
        assert cnt == 5

    def test_upsert_many_empty_list_is_noop(self, db_engine):
        with transaction(db_engine) as conn:
            n = StockIndicatorStateRepository(conn).upsert_many([])
        assert n == 0

    def test_different_timeframes_coexist(self, db_engine):
        """Same ticker, same bar_timestamp, different timeframe →
        two separate rows."""
        with transaction(db_engine) as conn:
            tid = _seed_ticker(conn)
            r = StockIndicatorStateRepository(conn)
            r.upsert_one(_make_state(tid, _BT(0), timeframe="1W"))
            r.upsert_one(_make_state(tid, _BT(0), timeframe="1D"))
        with db_engine.connect() as conn:
            r = StockIndicatorStateRepository(conn)
            weekly = r.get_at(tid, "1W", _BT(0))
            daily = r.get_at(tid, "1D", _BT(0))
        assert weekly is not None and weekly.timeframe == "1W"
        assert daily is not None and daily.timeframe == "1D"


# ============================================================
# Validation
# ============================================================

class TestStockIndicatorStateValidation:

    def test_rejects_bad_regime(self, db_engine):
        with transaction(db_engine) as conn:
            tid = _seed_ticker(conn)
            with pytest.raises(ValueError, match="display_regime"):
                StockIndicatorStateRepository(conn).upsert_one(
                    _make_state(tid, _BT(0), display_regime=2),
                )

    def test_rejects_negative_atr(self, db_engine):
        with transaction(db_engine) as conn:
            tid = _seed_ticker(conn)
            with pytest.raises(ValueError, match="atr"):
                StockIndicatorStateRepository(conn).upsert_one(
                    _make_state(tid, _BT(0), atr=-1.0),
                )

    def test_rejects_zero_ema(self, db_engine):
        with transaction(db_engine) as conn:
            tid = _seed_ticker(conn)
            with pytest.raises(ValueError, match="EMA values"):
                StockIndicatorStateRepository(conn).upsert_one(
                    _make_state(tid, _BT(0), med_ema=0.0),
                )

    def test_upsert_many_validates_all_before_writing_any(self, db_engine):
        """If any row is invalid, NOTHING is written."""
        with transaction(db_engine) as conn:
            tid = _seed_ticker(conn)
            good = _make_state(tid, _BT(0))
            bad = _make_state(tid, _BT(1), display_regime=99)
            with pytest.raises(ValueError, match="display_regime"):
                StockIndicatorStateRepository(conn).upsert_many(
                    [good, bad],
                )
        # The first state was NOT written — validation ran before
        # the INSERT.
        with db_engine.connect() as conn:
            assert StockIndicatorStateRepository(
                conn,
            ).count_for_ticker(tid, "1W") == 0


# ============================================================
# Reads
# ============================================================

class TestStockIndicatorStateReads:

    def test_get_latest_for_ticker(self, db_engine):
        with transaction(db_engine) as conn:
            tid = _seed_ticker(conn)
            r = StockIndicatorStateRepository(conn)
            r.upsert_one(_make_state(tid, _BT(0), med_ema=100.0))
            r.upsert_one(_make_state(tid, _BT(7), med_ema=110.0))
            r.upsert_one(_make_state(tid, _BT(14), med_ema=120.0))
        with db_engine.connect() as conn:
            latest = StockIndicatorStateRepository(
                conn,
            ).get_latest_for_ticker(tid, "1W")
        assert latest is not None
        assert latest.bar_timestamp == _BT(14)
        assert latest.med_ema == 120.0

    def test_get_latest_returns_none_when_no_rows(self, db_engine):
        with transaction(db_engine) as conn:
            tid = _seed_ticker(conn)
        with db_engine.connect() as conn:
            latest = StockIndicatorStateRepository(
                conn,
            ).get_latest_for_ticker(tid, "1W")
        assert latest is None

    def test_get_at_or_before_finds_recent(self, db_engine):
        with transaction(db_engine) as conn:
            tid = _seed_ticker(conn)
            r = StockIndicatorStateRepository(conn)
            r.upsert_one(_make_state(tid, _BT(0), med_ema=100.0))
            r.upsert_one(_make_state(tid, _BT(7), med_ema=110.0))
        with db_engine.connect() as conn:
            # Query mid-way: should get _BT(7), not _BT(14)
            row = StockIndicatorStateRepository(
                conn,
            ).get_at_or_before(tid, "1W", _BT(10))
        assert row is not None
        assert row.bar_timestamp == _BT(7)

    def test_get_at_or_before_returns_none_when_all_after(
        self, db_engine,
    ):
        with transaction(db_engine) as conn:
            tid = _seed_ticker(conn)
            StockIndicatorStateRepository(conn).upsert_one(
                _make_state(tid, _BT(10)),
            )
        with db_engine.connect() as conn:
            # Query before any row → None
            row = StockIndicatorStateRepository(
                conn,
            ).get_at_or_before(tid, "1W", _BT(0))
        assert row is None

    def test_get_history_newest_first(self, db_engine):
        with transaction(db_engine) as conn:
            tid = _seed_ticker(conn)
            r = StockIndicatorStateRepository(conn)
            for i in range(5):
                r.upsert_one(_make_state(tid, _BT(i * 7)))
        with db_engine.connect() as conn:
            history = StockIndicatorStateRepository(
                conn,
            ).get_history_for_ticker(tid, "1W")
        assert len(history) == 5
        timestamps = [s.bar_timestamp for s in history]
        assert timestamps == sorted(timestamps, reverse=True)

    def test_get_history_with_date_range_and_limit(self, db_engine):
        with transaction(db_engine) as conn:
            tid = _seed_ticker(conn)
            r = StockIndicatorStateRepository(conn)
            for i in range(10):
                r.upsert_one(_make_state(tid, _BT(i * 7)))
        with db_engine.connect() as conn:
            r = StockIndicatorStateRepository(conn)
            history = r.get_history_for_ticker(
                tid, "1W",
                from_dt=_BT(2 * 7), to_dt=_BT(7 * 7),
            )
        # bars 2,3,4,5,6,7 → 6 rows
        assert len(history) == 6
        with db_engine.connect() as conn:
            limited = StockIndicatorStateRepository(
                conn,
            ).get_history_for_ticker(tid, "1W", limit=3)
        assert len(limited) == 3

    def test_count_for_ticker(self, db_engine):
        with transaction(db_engine) as conn:
            tid = _seed_ticker(conn)
            r = StockIndicatorStateRepository(conn)
            for i in range(7):
                r.upsert_one(_make_state(tid, _BT(i * 7)))
        with db_engine.connect() as conn:
            assert StockIndicatorStateRepository(
                conn,
            ).count_for_ticker(tid, "1W") == 7
            # Different timeframe → 0
            assert StockIndicatorStateRepository(
                conn,
            ).count_for_ticker(tid, "1D") == 0


# ============================================================
# Delete
# ============================================================

class TestStockIndicatorStateDelete:

    def test_delete_for_ticker(self, db_engine):
        with transaction(db_engine) as conn:
            tid = _seed_ticker(conn)
            r = StockIndicatorStateRepository(conn)
            for i in range(3):
                r.upsert_one(_make_state(tid, _BT(i * 7)))
        with transaction(db_engine) as conn:
            n = StockIndicatorStateRepository(
                conn,
            ).delete_for_ticker(tid, "1W")
        assert n == 3
        with db_engine.connect() as conn:
            assert StockIndicatorStateRepository(
                conn,
            ).count_for_ticker(tid, "1W") == 0

    def test_delete_for_ticker_scoped_by_timeframe(self, db_engine):
        with transaction(db_engine) as conn:
            tid = _seed_ticker(conn)
            r = StockIndicatorStateRepository(conn)
            r.upsert_one(_make_state(tid, _BT(0), timeframe="1W"))
            r.upsert_one(_make_state(tid, _BT(0), timeframe="1D"))
        with transaction(db_engine) as conn:
            n = StockIndicatorStateRepository(
                conn,
            ).delete_for_ticker(tid, "1W")
        assert n == 1  # only the 1W row
        with db_engine.connect() as conn:
            r = StockIndicatorStateRepository(conn)
            assert r.count_for_ticker(tid, "1W") == 0
            assert r.count_for_ticker(tid, "1D") == 1
