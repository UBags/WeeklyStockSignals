"""Integration tests for `prime_cache_from_db`.

Seed `tickers`, `indicator_state`, and `signal_events`; run the new
prime; verify the SignalCache holds the right snapshots.

The two SELECTs (DISTINCT ON over indicator_state and signal_events)
are the heart of the new lifecycle — these tests pin their semantics.
"""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone, UTC

import pytest

from trip_ema.api.lifecycle import (
    PrimeCacheResult,
    prime_cache_from_db,
)
from trip_ema.cache.signal_cache import SignalCache
from trip_ema.domain import ChangeSignal, Timeframe


# ---------------------------------------------------------------------------
# Sandbox fixture (same pattern as Steps 4-7).
# ---------------------------------------------------------------------------


def _pg_available() -> bool:
    try:
        import psycopg2  # noqa: F401
    except ImportError:
        return False
    url = os.environ.get(
        "TRIP_EMA_TEST_DB_URL",
        "postgresql://postgres@/postgres",
    )
    try:
        import psycopg2
        conn = psycopg2.connect(url)
        conn.close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _pg_available(),
    reason="sandbox Postgres not reachable; set TRIP_EMA_TEST_DB_URL",
)


_SCHEMA_DDL = """
    CREATE TABLE tickers (
        ticker_id   SERIAL PRIMARY KEY,
        exchange    VARCHAR(8)  NOT NULL,
        symbol      VARCHAR(64) NOT NULL,
        instrument  VARCHAR(8)  NOT NULL
    );
    CREATE TABLE industry_index (
        industry_name VARCHAR(128) PRIMARY KEY
    );
    CREATE TABLE indicator_state (
        ticker_id        INTEGER REFERENCES tickers(ticker_id),
        timeframe        VARCHAR(8),
        industry_name    VARCHAR(128) REFERENCES industry_index(industry_name),
        trade_date       DATE NOT NULL,
        med_ema          DOUBLE PRECISION NOT NULL,
        long_ema         DOUBLE PRECISION NOT NULL,
        longest_ema      DOUBLE PRECISION NOT NULL,
        atr              DOUBLE PRECISION NOT NULL,
        upper_inner_band DOUBLE PRECISION NOT NULL,
        lower_inner_band DOUBLE PRECISION NOT NULL,
        upper_mid_band   DOUBLE PRECISION NOT NULL,
        lower_mid_band   DOUBLE PRECISION NOT NULL,
        upper_outer_band DOUBLE PRECISION NOT NULL,
        lower_outer_band DOUBLE PRECISION NOT NULL,
        display_regime   SMALLINT NOT NULL,
        computed_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    CREATE TABLE signal_events (
        event_id        BIGSERIAL PRIMARY KEY,
        ticker_id       INTEGER REFERENCES tickers(ticker_id),
        timeframe       VARCHAR(8),
        industry_name   VARCHAR(128) REFERENCES industry_index(industry_name),
        bar_timestamp   TIMESTAMPTZ NOT NULL,
        signal_type     VARCHAR(16) NOT NULL,
        regime_before   INTEGER,
        regime_after    INTEGER,
        price_at_signal DOUBLE PRECISION NOT NULL,
        computed_at     TIMESTAMPTZ NOT NULL
    );
    INSERT INTO tickers (ticker_id, exchange, symbol, instrument) VALUES
        (1806, 'NSE', 'SANSERA', 'EQ'),
        (2200, 'NSE', 'TCS',     'EQ'),
        (3000, 'NSE', 'INFY',    'EQ');
"""


@pytest.fixture
def engine(request):
    import psycopg2
    from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT
    from sqlalchemy import create_engine

    db_url = os.environ.get(
        "TRIP_EMA_TEST_DB_URL",
        "postgresql://postgres@/postgres",
    )
    test_db = f"trip_ema_prime_test_{os.getpid()}_{abs(hash(request.node.name)) % 100000}"

    admin = psycopg2.connect(db_url)
    admin.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    with admin.cursor() as c:
        c.execute(f"DROP DATABASE IF EXISTS {test_db}")
        c.execute(f"CREATE DATABASE {test_db}")
    admin.close()

    test_url = db_url.rsplit("/", 1)[0] + f"/{test_db}"
    engine = create_engine(test_url)
    with engine.begin() as conn:
        conn.exec_driver_sql(_SCHEMA_DDL)

    yield engine

    engine.dispose()
    admin = psycopg2.connect(db_url)
    admin.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    with admin.cursor() as c:
        c.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = %s AND pid <> pg_backend_pid()",
            (test_db,),
        )
        c.execute(f"DROP DATABASE IF EXISTS {test_db}")
    admin.close()


def _ind_row(engine, ticker_id: int, trade_date: date,
             *, regime: int = 1, med: float = 100.0,
             computed_at: datetime = None) -> None:
    if computed_at is None:
        computed_at = datetime(2025, 6, 13, 18, 0, tzinfo=UTC)
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO indicator_state "
            "(ticker_id, timeframe, trade_date, med_ema, long_ema, longest_ema, atr, "
            " upper_inner_band, lower_inner_band, upper_mid_band, lower_mid_band, "
            " upper_outer_band, lower_outer_band, display_regime, computed_at) "
            "VALUES (%(tid)s, '1W', %(td)s, %(m)s, %(m)s, %(m)s, 1.5, "
            "        %(ui)s, %(li)s, %(um)s, %(lm)s, %(uo)s, %(lo)s, %(r)s, %(ca)s)",
            dict(tid=ticker_id, td=trade_date, m=med,
                 ui=med+0.3, li=med-0.3, um=med+3.0, lm=med-3.0,
                 uo=med+6.75, lo=med-6.75, r=regime, ca=computed_at),
        )


def _sig_row(engine, ticker_id: int,
             *, bar_timestamp: datetime, signal_type: str,
             price: float = 100.0, computed_at: datetime = None) -> None:
    if computed_at is None:
        computed_at = datetime(2025, 6, 13, 18, 0, tzinfo=UTC)
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO signal_events "
            "(ticker_id, timeframe, bar_timestamp, signal_type, "
            " price_at_signal, computed_at) "
            "VALUES (%(tid)s, '1W', %(bt)s, %(st)s, %(p)s, %(ca)s)",
            dict(tid=ticker_id, bt=bar_timestamp, st=signal_type,
                 p=price, ca=computed_at),
        )


# ===========================================================================
# Tests
# ===========================================================================


class TestPrimeCacheFromDB:

    def test_empty_db_produces_empty_cache(self, engine):
        cache = SignalCache()
        result = prime_cache_from_db(db_engine=engine, cache=cache)
        assert isinstance(result, PrimeCacheResult)
        assert result.tickers_seen == 0
        assert result.snapshots_populated == 0
        assert cache.snapshot_count() == 0

    def test_indicator_only_ticker_populates_with_no_event(self, engine):
        _ind_row(engine, 1806, date(2025, 6, 9))
        cache = SignalCache()
        result = prime_cache_from_db(db_engine=engine, cache=cache)
        assert result.tickers_seen == 1
        assert result.indicator_only == 1
        assert result.signal_event_only == 0
        assert result.both == 0
        snap = cache.get_snapshot(1806, Timeframe.WEEKLY)
        assert snap is not None
        assert snap.latest_indicator_state.trade_date == date(2025, 6, 9)
        assert snap.latest_signal_event is None
        assert snap.ticker_ref.symbol == "SANSERA"

    def test_signal_only_ticker_populates_with_no_indicator(self, engine):
        _sig_row(engine, 1806,
                 bar_timestamp=datetime(2025, 6, 9, tzinfo=UTC),
                 signal_type="BUY")
        cache = SignalCache()
        result = prime_cache_from_db(db_engine=engine, cache=cache)
        assert result.signal_event_only == 1
        snap = cache.get_snapshot(1806, Timeframe.WEEKLY)
        assert snap.latest_indicator_state is None
        assert snap.latest_signal_event is not None
        assert snap.latest_signal_event.signal == ChangeSignal.BUY

    def test_both_components_populate_full_snapshot(self, engine):
        _ind_row(engine, 1806, date(2025, 6, 9))
        _sig_row(engine, 1806,
                 bar_timestamp=datetime(2025, 6, 9, tzinfo=UTC),
                 signal_type="BUY")
        cache = SignalCache()
        result = prime_cache_from_db(db_engine=engine, cache=cache)
        assert result.both == 1
        snap = cache.get_snapshot(1806, Timeframe.WEEKLY)
        assert snap.latest_indicator_state is not None
        assert snap.latest_signal_event is not None

    def test_latest_indicator_state_picked_by_trade_date(self, engine):
        """The DISTINCT ON ordering must pick the most-recent
        trade_date, not the first-inserted row."""
        # Insert OLDER row first.
        _ind_row(engine, 1806, date(2025, 5, 26), med=95.0)
        # Then NEWER row.
        _ind_row(engine, 1806, date(2025, 6, 9),  med=110.0)
        cache = SignalCache()
        prime_cache_from_db(db_engine=engine, cache=cache)
        snap = cache.get_snapshot(1806, Timeframe.WEEKLY)
        assert snap.latest_indicator_state.trade_date == date(2025, 6, 9)
        assert snap.latest_indicator_state.med_ema == pytest.approx(110.0)

    def test_latest_signal_event_picked_by_computed_at(self, engine):
        """DISTINCT ON with ORDER BY computed_at DESC, event_id DESC
        picks the most recent emission. Two events on the same week
        but different computed_at — newest wins."""
        ts = datetime(2025, 6, 9, tzinfo=UTC)
        # Older computed_at.
        _sig_row(engine, 1806, bar_timestamp=ts,
                 signal_type="BUY", price=99.0,
                 computed_at=datetime(2025, 6, 10, tzinfo=UTC))
        # Newer computed_at — same week.
        _sig_row(engine, 1806, bar_timestamp=ts,
                 signal_type="EXIT_FROM_BUY", price=98.5,
                 computed_at=datetime(2025, 6, 13, tzinfo=UTC))
        cache = SignalCache()
        prime_cache_from_db(db_engine=engine, cache=cache)
        snap = cache.get_snapshot(1806, Timeframe.WEEKLY)
        assert snap.latest_signal_event.signal == ChangeSignal.EXIT_FROM_BUY

    def test_multi_ticker_prime(self, engine):
        # ind+sig for 1806
        _ind_row(engine, 1806, date(2025, 6, 9))
        _sig_row(engine, 1806,
                 bar_timestamp=datetime(2025, 6, 9, tzinfo=UTC),
                 signal_type="SELL")
        # ind only for 2200
        _ind_row(engine, 2200, date(2025, 6, 9))
        # sig only for 3000
        _sig_row(engine, 3000,
                 bar_timestamp=datetime(2025, 6, 9, tzinfo=UTC),
                 signal_type="BUY")
        cache = SignalCache()
        result = prime_cache_from_db(db_engine=engine, cache=cache)
        assert result.tickers_seen == 3
        assert result.snapshots_populated == 3
        assert result.both == 1
        assert result.indicator_only == 1
        assert result.signal_event_only == 1
        assert cache.snapshot_count() == 3
        assert cache.get_snapshot(1806, Timeframe.WEEKLY) is not None
        assert cache.get_snapshot(2200, Timeframe.WEEKLY) is not None
        assert cache.get_snapshot(3000, Timeframe.WEEKLY) is not None

    def test_ticker_ref_populated_from_tickers_table(self, engine):
        _ind_row(engine, 2200, date(2025, 6, 9))
        cache = SignalCache()
        prime_cache_from_db(db_engine=engine, cache=cache)
        snap = cache.get_snapshot(2200, Timeframe.WEEKLY)
        assert snap.ticker_ref.exchange == "NSE"
        assert snap.ticker_ref.symbol == "TCS"
        assert snap.ticker_ref.instrument == "EQ"
