"""Tests for `trip_ema.signals.generate_signals_in_live_production`.

Two layers:

  - **Skip-path tests** (`TestLiveSkipPaths`) — verify the various
    "no_bars" / "insufficient_history" / "warmup" early returns
    against a real Postgres (cheap, just empty seeds).

  - **End-to-end tests** (`TestLiveEndToEnd`) — real Postgres with
    synthetic bars + pre-seeded signal_events. Verify the right
    signal fires, computed_at is NOW(), idempotency holds, and
    prev_state is correctly threaded from the DB.

Both suites use the same Postgres-sandbox pattern as Steps 4 and 5.
Auto-skipped when the sandbox isn't reachable.
"""

from __future__ import annotations

import os
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from trip_ema.domain import (
    ChangeSignal,
    OHLCVBar,
    TickerRef,
    Timeframe,
)
from trip_ema.signals import (
    LiveTickResult,
    generate_live_signal_for_ticker,
)


# ---------------------------------------------------------------------------
# Bar helpers (same as Step 5's tests — duplicated rather than imported
# because tests are independent).
# ---------------------------------------------------------------------------


_IST = ZoneInfo("Asia/Kolkata")
_TICKER = TickerRef(exchange="NSE", symbol="SYNTH", instrument="EQ")


def _bar(day: date, *, price: float, vol: float = 1_000_000.0,
         hl_spread: float = 0.5) -> OHLCVBar:
    ts_ist = datetime.combine(day, time(9, 15), tzinfo=_IST)
    half = hl_spread / 2
    return OHLCVBar(
        timestamp=ts_ist.astimezone(timezone.utc),
        open=price, high=price + half, low=price - half, close=price,
        volume=vol,
    )


def _trading_days(start: date, n: int) -> list[date]:
    out: list[date] = []
    d = start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _flat_bars(start: date, n_days: int, price: float,
               hl_spread: float = 0.5) -> list[OHLCVBar]:
    return [_bar(d, price=price, hl_spread=hl_spread)
            for d in _trading_days(start, n_days)]


def _ramp_bars(start: date, n_days: int, *, p0: float, p1: float,
               hl_spread: float = 0.5) -> list[OHLCVBar]:
    days = _trading_days(start, n_days)
    out: list[OHLCVBar] = []
    for i, d in enumerate(days):
        frac = i / max(1, n_days - 1)
        out.append(_bar(d, price=p0 + frac * (p1 - p0), hl_spread=hl_spread))
    return out


# ---------------------------------------------------------------------------
# Sandbox fixture.
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
    CREATE TABLE ohlcv_bars (
        ticker_id     INTEGER NOT NULL REFERENCES tickers(ticker_id),
        timeframe     VARCHAR(8) NOT NULL,
        bar_timestamp TIMESTAMPTZ NOT NULL,
        open          DOUBLE PRECISION NOT NULL,
        high          DOUBLE PRECISION NOT NULL,
        low           DOUBLE PRECISION NOT NULL,
        close         DOUBLE PRECISION NOT NULL,
        volume        DOUBLE PRECISION NOT NULL,
        updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        adj_close     DOUBLE PRECISION,
        PRIMARY KEY (ticker_id, timeframe, bar_timestamp)
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
    CREATE UNIQUE INDEX uq_signal_events_ticker
        ON signal_events (ticker_id, timeframe, bar_timestamp, signal_type);
    INSERT INTO tickers (ticker_id, exchange, symbol, instrument)
        VALUES (1806, 'NSE', 'SYNTH', 'EQ');
"""


@pytest.fixture
def engine(request):
    """Throwaway database per test. Drops on teardown."""
    import psycopg2
    from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT
    from sqlalchemy import create_engine

    db_url = os.environ.get(
        "TRIP_EMA_TEST_DB_URL",
        "postgresql://postgres@/postgres",
    )
    # Unique per test so concurrent runs don't collide.
    test_db = f"trip_ema_live_test_{os.getpid()}_{abs(hash(request.node.name)) % 100000}"

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


def _seed_bars(engine, bars: list[OHLCVBar]) -> None:
    with engine.begin() as conn:
        for b in bars:
            conn.exec_driver_sql(
                "INSERT INTO ohlcv_bars "
                "(ticker_id, timeframe, bar_timestamp, open, high, low, close, volume) "
                "VALUES (%(tid)s, '1D', %(ts)s, %(o)s, %(h)s, %(l)s, %(c)s, %(v)s)",
                dict(tid=1806, ts=b.timestamp,
                     o=b.open, h=b.high, l=b.low, c=b.close, v=b.volume),
            )


# ===========================================================================
# Skip paths
# ===========================================================================


class TestLiveSkipPaths:
    """Early-return paths: no bars / insufficient history / warmup."""

    def test_no_bars_returns_skipped(self, engine):
        with engine.begin() as conn:
            result = generate_live_signal_for_ticker(
                conn,
                ticker_id=1806,
                ticker_ref=_TICKER,
                timeframe=Timeframe.WEEKLY,
                now=datetime(2025, 6, 1, 12, 0, tzinfo=timezone.utc),
            )
        assert isinstance(result, LiveTickResult)
        assert result.emitted is False
        assert result.skipped_reason == "no_bars"
        assert result.signal_kind == ChangeSignal.NO_CHANGE

    def test_insufficient_history_returns_skipped(self, engine):
        # One week's worth of bars — aggregates to one weekly bar
        # which is < 2.
        _seed_bars(engine, _flat_bars(date(2025, 6, 2), n_days=5, price=100.0))
        with engine.begin() as conn:
            result = generate_live_signal_for_ticker(
                conn,
                ticker_id=1806,
                ticker_ref=_TICKER,
                timeframe=Timeframe.WEEKLY,
                now=datetime(2025, 6, 6, 18, 0, tzinfo=timezone.utc),
            )
        assert result.emitted is False
        assert result.skipped_reason == "insufficient_history"

    def test_warmup_returns_skipped(self, engine):
        # 5 weeks of bars — two-plus weekly bars, but indicator series
        # is still NaN at the last index.
        _seed_bars(engine, _flat_bars(date(2025, 5, 5), n_days=25, price=100.0))
        with engine.begin() as conn:
            result = generate_live_signal_for_ticker(
                conn,
                ticker_id=1806,
                ticker_ref=_TICKER,
                timeframe=Timeframe.WEEKLY,
                now=datetime(2025, 6, 6, 18, 0, tzinfo=timezone.utc),
            )
        assert result.emitted is False
        assert result.skipped_reason == "warmup"

    def test_rejects_non_weekly_timeframe(self, engine):
        with engine.begin() as conn:
            with pytest.raises(NotImplementedError):
                generate_live_signal_for_ticker(
                    conn,
                    ticker_id=1806,
                    ticker_ref=_TICKER,
                    timeframe=Timeframe.DAILY,
                    now=datetime(2025, 6, 1, tzinfo=timezone.utc),
                )


# ===========================================================================
# End-to-end
# ===========================================================================


class TestLiveEndToEnd:
    """Real seed data, real Postgres. Exercise the full happy path."""

    def _seed_long_history_with_crash(self, engine):
        """Seed: 3 years flat at 100, sharp drop to 70, flat at 70.

        Crash ramp runs Dec 31 2024 → Jan 27 2025 over 20 trading days.
        First crash week (week of Mon Dec 30 2024) ends Fri Jan 3 2025,
        with weekly close ~ 93.7 — comfortably below the inner band
        for the spec's definite-RED rule (a). That's where the fresh
        SELL fires."""
        bars = (
            _flat_bars(date(2022, 1, 3), n_days=780, price=100.0)
            + _ramp_bars(date(2024, 12, 31), n_days=20, p0=100.0, p1=70.0)
            + _flat_bars(date(2025, 1, 29), n_days=60, price=70.0)
        )
        _seed_bars(engine, bars)

    # End-of-first-crash-week: the regime first turns RED here, so
    # leg 1 of rule B fires (NOT prev_regime_is_red AND close >=
    # lower_outer). Later weeks have prev_regime_is_red already, and
    # neither leg of rule B triggers on a steady-state RED — that's
    # the spec working correctly.
    _NOW_FRESH_RED = datetime(2025, 1, 3, 12, 30, tzinfo=timezone.utc)

    def test_emits_signal_with_now_as_computed_at(self, engine):
        self._seed_long_history_with_crash(engine)
        with engine.begin() as conn:
            result = generate_live_signal_for_ticker(
                conn,
                ticker_id=1806,
                ticker_ref=_TICKER,
                timeframe=Timeframe.WEEKLY,
                now=self._NOW_FRESH_RED,
            )
        assert result.emitted is True, (
            f"expected emission on fresh-RED week; got {result}"
        )
        with engine.begin() as conn:
            rows = list(conn.exec_driver_sql(
                "SELECT signal_type, computed_at FROM signal_events "
                "WHERE ticker_id = 1806"
            ))
        assert len(rows) == 1
        assert rows[0][1] == self._NOW_FRESH_RED, (
            f"computed_at should be the wall-clock `now`; "
            f"got {rows[0][1]} vs {self._NOW_FRESH_RED}"
        )

    def test_signal_fires_on_fresh_red_week(self, engine):
        """End of the first crash week: regime transitions from NEUTRAL
        to RED. State is FLAT (no prior signals). Leg 1 of rule B
        fires → SELL."""
        self._seed_long_history_with_crash(engine)
        with engine.begin() as conn:
            result = generate_live_signal_for_ticker(
                conn,
                ticker_id=1806,
                ticker_ref=_TICKER,
                timeframe=Timeframe.WEEKLY,
                now=self._NOW_FRESH_RED,
            )
        assert result.emitted is True
        assert result.signal_kind == ChangeSignal.SELL

    def test_two_consecutive_ticks_on_same_bar_are_idempotent(self, engine):
        """Two ticks on the same bar's week: the first emits SELL
        (state was FLAT). The second sees state=SELL from the row
        the first tick wrote, so rule B is blocked → NO_CHANGE.
        Row count stays at 1."""
        self._seed_long_history_with_crash(engine)

        with engine.begin() as conn:
            r1 = generate_live_signal_for_ticker(
                conn, 1806, _TICKER, Timeframe.WEEKLY,
                now=self._NOW_FRESH_RED,
            )
        # Second tick 4 hours later — same week, same provisional bar
        # shape, but state is now SELL from r1's row.
        now_later = self._NOW_FRESH_RED + timedelta(hours=4)
        with engine.begin() as conn:
            r2 = generate_live_signal_for_ticker(
                conn, 1806, _TICKER, Timeframe.WEEKLY, now=now_later,
            )

        assert r1.emitted is True and r1.signal_kind == ChangeSignal.SELL
        assert r2.emitted is False, (
            "state=SELL from r1's row should block r2's SELL via rule B's "
            f"`NOT state_is_SELL` guard. got {r2}"
        )
        with engine.begin() as conn:
            count = conn.exec_driver_sql(
                "SELECT COUNT(*) FROM signal_events WHERE ticker_id = 1806"
            ).scalar()
        assert count == 1, (
            "no new row on r2 since rule B was blocked by state-thread"
        )

    def test_prev_state_blocks_signal_via_signal_events(self, engine):
        """Pre-seed a SELL into signal_events on some earlier bar.
        The fresh-RED week would normally fire SELL via leg 1 of rule B,
        but the pre-seeded SELL means state=SELL, blocking it."""
        self._seed_long_history_with_crash(engine)
        # Seed a prior SELL at an arbitrary earlier date.
        seed_bar_ts = datetime(2024, 12, 16, tzinfo=_IST).astimezone(timezone.utc)
        with engine.begin() as conn:
            conn.exec_driver_sql(
                "INSERT INTO signal_events "
                "(ticker_id, timeframe, bar_timestamp, signal_type, "
                " price_at_signal, computed_at) "
                "VALUES (1806, '1W', %(ts)s, 'SELL', 100.0, %(ca)s)",
                dict(
                    ts=seed_bar_ts,
                    ca=datetime(2024, 12, 20, tzinfo=timezone.utc),
                ),
            )
        with engine.begin() as conn:
            result = generate_live_signal_for_ticker(
                conn, 1806, _TICKER, Timeframe.WEEKLY,
                now=self._NOW_FRESH_RED,
            )
        assert result.emitted is False, (
            "state=SELL from pre-seeded row must block another SELL"
        )
        assert result.signal_kind == ChangeSignal.NO_CHANGE
        # Only the seeded row exists.
        with engine.begin() as conn:
            count = conn.exec_driver_sql(
                "SELECT COUNT(*) FROM signal_events WHERE ticker_id = 1806"
            ).scalar()
        assert count == 1

    def test_cache_writer_invoked_on_emission(self, engine):
        self._seed_long_history_with_crash(engine)
        calls = []
        with engine.begin() as conn:
            result = generate_live_signal_for_ticker(
                conn, 1806, _TICKER, Timeframe.WEEKLY,
                now=self._NOW_FRESH_RED,
                signal_cache_writer=lambda tid, tf, ev: calls.append((tid, tf, ev)),
            )
        assert result.emitted is True
        assert len(calls) == 1
        assert calls[0][0] == 1806
        assert calls[0][1] == Timeframe.WEEKLY
        assert calls[0][2].signal == result.signal_kind

    def test_cache_writer_not_invoked_on_no_change(self, engine):
        """Pre-seed SELL → on the fresh-RED week, rule B is blocked
        → NO_CHANGE → cache must NOT be written."""
        self._seed_long_history_with_crash(engine)
        seed_bar_ts = datetime(2024, 12, 16, tzinfo=_IST).astimezone(timezone.utc)
        with engine.begin() as conn:
            conn.exec_driver_sql(
                "INSERT INTO signal_events "
                "(ticker_id, timeframe, bar_timestamp, signal_type, "
                " price_at_signal, computed_at) "
                "VALUES (1806, '1W', %(ts)s, 'SELL', 100.0, %(ca)s)",
                dict(
                    ts=seed_bar_ts,
                    ca=datetime(2024, 12, 20, tzinfo=timezone.utc),
                ),
            )
        calls = []
        with engine.begin() as conn:
            result = generate_live_signal_for_ticker(
                conn, 1806, _TICKER, Timeframe.WEEKLY,
                now=self._NOW_FRESH_RED,
                signal_cache_writer=lambda tid, tf, ev: calls.append((tid, tf, ev)),
            )
        assert result.emitted is False
        assert calls == [], "cache must not be written when no signal fires"
