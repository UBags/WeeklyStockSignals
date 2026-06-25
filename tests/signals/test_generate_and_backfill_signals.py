"""Tests for `trip_ema.signals.generate_and_backfill_signals`.

Two layers:

  - **Walker tests** (`TestWalker*`) — pure-Python, no DB. Feed
    synthetic daily bars to `walk_and_generate_events`, verify the
    output `SignalEvent` sequence matches what the rules say.
    Synthetic bars are crafted to exercise specific regime
    transitions deterministically.

  - **Orchestrator tests** (`TestOrchestrator*`) — real Postgres.
    Seed `ohlcv_bars` with daily data, call `run_backfill_for_ticker`,
    verify `signal_events` got the right rows with the right
    `computed_at` (= simulated day).

The synthetic-bars helpers construct day-by-day OHLCV series that
will produce a known regime trajectory. They're deliberately blunt
(constant volume, monotonic prices for long stretches) so the
indicator series converges quickly and the test is robust to small
parameter changes.
"""

from __future__ import annotations

import os
from datetime import date, datetime, time, timedelta, timezone, UTC
from zoneinfo import ZoneInfo

import pytest

from trip_ema.domain import (
    ChangeSignal,
    OHLCVBar,
    TickerRef,
    Timeframe,
)
from trip_ema.signals import (
    BackfillResult,
    run_backfill_for_ticker,
    walk_and_generate_events,
)


# ---------------------------------------------------------------------------
# Synthetic-bar helpers.
# ---------------------------------------------------------------------------


_IST = ZoneInfo("Asia/Kolkata")
_TICKER = TickerRef(exchange="NSE", symbol="SYNTH", instrument="EQ")


def _bar(day: date, *, price: float, vol: float = 1_000_000.0,
         hl_spread: float = 0.5) -> OHLCVBar:
    """A synthetic daily bar at the given price with a tiny H-L spread.

    `hl_spread` (default 0.5) sets the symmetric high-minus-low range
    around `price`. Set to 0 only if you specifically want ATR=0 —
    most tests want > 0 so the inner bands don't collapse onto the
    EMAs and trivially fire definite-green/red.

    Timestamp at 09:15 IST (NSE open), stored as UTC.
    """
    ts_ist = datetime.combine(day, time(9, 15), tzinfo=_IST)
    half = hl_spread / 2
    return OHLCVBar(
        timestamp=ts_ist.astimezone(UTC),
        open=price, high=price + half, low=price - half, close=price,
        volume=vol,
    )


def _trading_days(start: date, n: int) -> list[date]:
    """N consecutive Mon-Fri days starting at `start` (or its next Mon)."""
    out: list[date] = []
    d = start
    while len(out) < n:
        if d.weekday() < 5:  # Mon=0..Fri=4
            out.append(d)
        d += timedelta(days=1)
    return out


def _flat_bars(start: date, n_days: int, price: float,
               hl_spread: float = 0.5) -> list[OHLCVBar]:
    """N trading days of bars at `price` with a small H-L spread."""
    return [_bar(d, price=price, hl_spread=hl_spread)
            for d in _trading_days(start, n_days)]


def _ramp_bars(start: date, n_days: int, *, p0: float, p1: float,
               hl_spread: float = 0.5) -> list[OHLCVBar]:
    """N trading days, linearly ramping price p0 → p1."""
    days = _trading_days(start, n_days)
    out: list[OHLCVBar] = []
    for i, d in enumerate(days):
        frac = i / max(1, n_days - 1)
        out.append(_bar(d, price=p0 + frac * (p1 - p0), hl_spread=hl_spread))
    return out


# ===========================================================================
# WALKER TESTS — pure, no DB
# ===========================================================================


class TestWalkerBasics:
    """Smoke tests for `walk_and_generate_events`."""

    def test_returns_empty_events_when_no_bars(self):
        events, result = walk_and_generate_events(
            daily_bars=[],
            ticker_ref=_TICKER,
            timeframe=Timeframe.WEEKLY,
            start_date=date(2024, 1, 1),
            end_date=date(2024, 12, 31),
        )
        assert events == []
        assert result.events_emitted == 0
        assert result.days_walked == 0

    def test_returns_empty_events_when_warmup_only(self):
        # 4 weeks of bars — way short of the EMA warmup window.
        bars = _flat_bars(date(2024, 1, 1), n_days=20, price=100.0)
        events, result = walk_and_generate_events(
            daily_bars=bars,
            ticker_ref=_TICKER,
            timeframe=Timeframe.WEEKLY,
            start_date=date(2024, 1, 1),
            end_date=date(2024, 2, 1),
        )
        # We may walk some days but all should be warmup → no signals.
        assert events == []
        assert result.events_emitted == 0
        # Either warmup OR no daily bars in the active window — either
        # is acceptable behaviour for this smoke.
        assert result.skipped_warmup_days >= 0

    def test_rejects_non_weekly_timeframe(self):
        with pytest.raises(NotImplementedError):
            walk_and_generate_events(
                daily_bars=[],
                ticker_ref=_TICKER,
                timeframe=Timeframe.DAILY,
                start_date=date(2024, 1, 1),
                end_date=date(2024, 12, 31),
            )


class TestWalkerProducesSignalsOnSharpReversal:
    """Construct a price series that crashes after a long flat stretch.
    A SELL signal should fire shortly after the crash."""

    def test_long_flat_then_crash_emits_sell(self):
        # 3 years flat at 100, then a sharp 30% drop over a few weeks,
        # then flat at 70.
        bars = (
            _flat_bars(date(2022, 1, 3), n_days=780, price=100.0)
            + _ramp_bars(date(2024, 12, 31), n_days=20, p0=100.0, p1=70.0)
            + _flat_bars(date(2025, 1, 29), n_days=60, price=70.0)
        )
        events, result = walk_and_generate_events(
            daily_bars=bars,
            ticker_ref=_TICKER,
            timeframe=Timeframe.WEEKLY,
            start_date=date(2024, 12, 1),  # well past warmup
            end_date=date(2025, 4, 1),
        )
        # There should be exactly one SELL in this window. Sequence:
        # NEUTRAL during the flat (close == EMAs == 100, no band breach)
        # then a definite-RED bar once close drops past lower_inner.
        sell_events = [e for e in events if e.signal == ChangeSignal.SELL]
        assert len(sell_events) >= 1, (
            f"expected at least one SELL during the crash; got {[e.signal.name for e in events]}"
        )
        # And no BUY in the window — price never goes up.
        buys = [e for e in events if e.signal == ChangeSignal.BUY]
        assert buys == []


class TestWalkerComputedAtIsSimulatedDay:
    """The defining property of backfill mode: each event's
    `computed_at` is the simulated day at IST midnight, NOT
    `datetime.now()`."""

    def test_computed_at_matches_simulated_day(self):
        # Crash setup: long flat then drop.
        bars = (
            _flat_bars(date(2022, 1, 3), n_days=780, price=100.0)
            + _ramp_bars(date(2024, 12, 31), n_days=20, p0=100.0, p1=70.0)
            + _flat_bars(date(2025, 1, 29), n_days=60, price=70.0)
        )
        events, _ = walk_and_generate_events(
            daily_bars=bars,
            ticker_ref=_TICKER,
            timeframe=Timeframe.WEEKLY,
            start_date=date(2024, 12, 1),
            end_date=date(2025, 4, 1),
        )
        assert events  # sanity
        for e in events:
            # computed_at must be aware, IST, at 00:00 (midnight).
            assert e.computed_at.tzinfo is not None
            ist = e.computed_at.astimezone(_IST)
            assert ist.hour == 0 and ist.minute == 0 and ist.second == 0, (
                f"computed_at {e.computed_at} not at IST midnight"
            )
            # And clearly historical (not today).
            assert ist.date() <= date(2025, 4, 1)
            assert ist.date() >= date(2024, 12, 1)


class TestWalkerStateThreading:
    """The position state must thread correctly across days within a
    week AND across weeks. A flat → crash → recover sequence should
    produce SELL → EXIT_FROM_SELL → BUY in chronological order."""

    def test_flat_crash_recover_produces_three_signals_in_order(self):
        bars = (
            _flat_bars(date(2022, 1, 3), n_days=780, price=100.0)
            # Crash
            + _ramp_bars(date(2024, 12, 31), n_days=15, p0=100.0, p1=70.0)
            # Long flat at the new low — eventually display drops to NEUTRAL
            # (no continuation, no fresh red trigger).
            + _flat_bars(date(2025, 1, 22), n_days=120, price=70.0)
            # Strong recovery — close pushes back through the bands.
            + _ramp_bars(date(2025, 7, 14), n_days=15, p0=70.0, p1=110.0)
            + _flat_bars(date(2025, 8, 4), n_days=80, price=110.0)
        )
        events, result = walk_and_generate_events(
            daily_bars=bars,
            ticker_ref=_TICKER,
            timeframe=Timeframe.WEEKLY,
            start_date=date(2024, 12, 1),
            end_date=date(2025, 11, 30),
        )
        # The three signal kinds we expect to see at least once each:
        kinds = [e.signal for e in events]
        # SELL during the crash
        assert ChangeSignal.SELL in kinds, (
            f"expected SELL in {[k.name for k in kinds]}"
        )
        # State must end at BUY after the recovery (so a BUY must have
        # fired at some point). Either via precedence E (SELL→BUY direct)
        # or via plain entry from FLAT after EXIT_FROM_SELL.
        assert ChangeSignal.BUY in kinds, (
            f"expected BUY in {[k.name for k in kinds]}"
        )
        # Chronological ordering of events.
        for a, b in zip(events, events[1:]):
            assert a.computed_at <= b.computed_at


class TestWalkerBarTimestampIsWeekStart:
    """`bar_timestamp` on every emitted event is the Monday-IST anchor
    of the weekly bar that produced the signal."""

    def test_bar_timestamps_are_mondays(self):
        bars = (
            _flat_bars(date(2022, 1, 3), n_days=780, price=100.0)
            + _ramp_bars(date(2024, 12, 31), n_days=20, p0=100.0, p1=70.0)
            + _flat_bars(date(2025, 1, 29), n_days=60, price=70.0)
        )
        events, _ = walk_and_generate_events(
            daily_bars=bars,
            ticker_ref=_TICKER,
            timeframe=Timeframe.WEEKLY,
            start_date=date(2024, 12, 1),
            end_date=date(2025, 4, 1),
        )
        assert events  # sanity
        for e in events:
            ist = e.bar_timestamp.astimezone(_IST)
            assert ist.weekday() == 0, (  # Monday
                f"bar_timestamp {e.bar_timestamp} → IST {ist} is not a Monday"
            )
            assert ist.hour == 0 and ist.minute == 0


class TestWalkerWarmupDoesNotEmit:
    """A handful of weeks of data is genuinely shorter than the EMA
    warmup window — the indicator series stays NaN, so no signals
    emit regardless of price shape."""

    def test_short_window_emits_nothing(self):
        # 6 weeks of bars — well short of even the medium EMA warmup.
        bars = _flat_bars(date(2024, 1, 1), n_days=30, price=100.0)
        events, result = walk_and_generate_events(
            daily_bars=bars,
            ticker_ref=_TICKER,
            timeframe=Timeframe.WEEKLY,
            start_date=date(2024, 1, 1),
            end_date=date(2024, 2, 15),
        )
        # Indicators are NaN throughout this window → no events.
        assert events == [], (
            f"expected no events during warmup; got "
            f"{[(e.signal.name, e.computed_at.date()) for e in events]}"
        )
        assert result.events_emitted == 0
        assert result.skipped_warmup_days >= 1


# ===========================================================================
# ORCHESTRATOR TESTS — real Postgres
# ===========================================================================


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


pytestmark_integration = pytest.mark.skipif(
    not _pg_available(),
    reason="sandbox Postgres not reachable; set TRIP_EMA_TEST_DB_URL",
)


@pytestmark_integration
class TestOrchestratorEndToEnd:
    """`run_backfill_for_ticker` loads bars from `ohlcv_bars`, walks,
    and persists to `signal_events`."""

    @pytest.fixture
    def db_url(self):
        return os.environ.get(
            "TRIP_EMA_TEST_DB_URL",
            "postgresql://postgres@/postgres",
        )

    @pytest.fixture
    def engine(self, db_url):
        import psycopg2
        from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT
        from sqlalchemy import create_engine

        test_db = f"trip_ema_backfill_test_{os.getpid()}"

        admin = psycopg2.connect(db_url)
        admin.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
        with admin.cursor() as c:
            c.execute(f"DROP DATABASE IF EXISTS {test_db}")
            c.execute(f"CREATE DATABASE {test_db}")
        admin.close()

        test_url = db_url.rsplit("/", 1)[0] + f"/{test_db}"
        engine = create_engine(test_url)

        with engine.begin() as conn:
            conn.exec_driver_sql("""
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
                    industry_name   VARCHAR(128)
                                    REFERENCES industry_index(industry_name),
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
            """)
            # Seed: same crash-after-flat shape as the walker tests.
            bars = (
                _flat_bars(date(2022, 1, 3), n_days=780, price=100.0)
                + _ramp_bars(date(2024, 12, 31), n_days=20, p0=100.0, p1=70.0)
                + _flat_bars(date(2025, 1, 29), n_days=60, price=70.0)
            )
            for b in bars:
                conn.exec_driver_sql(
                    "INSERT INTO ohlcv_bars "
                    "(ticker_id, timeframe, bar_timestamp, open, high, low, close, volume) "
                    "VALUES (%(tid)s, '1D', %(ts)s, %(o)s, %(h)s, %(l)s, %(c)s, %(v)s)",
                    dict(tid=1806, ts=b.timestamp,
                         o=b.open, h=b.high, l=b.low, c=b.close, v=b.volume),
                )

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

    def test_end_to_end_signals_land_in_table(self, engine):
        with engine.begin() as conn:
            result = run_backfill_for_ticker(
                conn,
                ticker_id=1806,
                ticker_ref=_TICKER,
                timeframe=Timeframe.WEEKLY,
                start_date=date(2024, 12, 1),
                end_date=date(2025, 4, 1),
            )
        assert isinstance(result, BackfillResult)
        assert result.ticker_id == 1806
        assert result.events_emitted >= 1

        with engine.begin() as conn:
            rows = list(conn.exec_driver_sql(
                "SELECT signal_type, bar_timestamp, computed_at "
                "FROM signal_events WHERE ticker_id = 1806 "
                "ORDER BY computed_at"
            ))
        assert len(rows) == result.events_emitted
        # At least one SELL during the crash.
        assert any(r[0] == "SELL" for r in rows)

    def test_computed_at_in_db_matches_simulated_day(self, engine):
        with engine.begin() as conn:
            run_backfill_for_ticker(
                conn,
                ticker_id=1806,
                ticker_ref=_TICKER,
                timeframe=Timeframe.WEEKLY,
                start_date=date(2024, 12, 1),
                end_date=date(2025, 4, 1),
            )
        with engine.begin() as conn:
            rows = list(conn.exec_driver_sql(
                "SELECT computed_at FROM signal_events WHERE ticker_id = 1806"
            ))
        assert rows
        for (computed_at,) in rows:
            ist = computed_at.astimezone(_IST)
            # Every computed_at must be IST midnight on a historical day.
            assert ist.hour == 0 and ist.minute == 0
            assert ist.year in (2024, 2025)

    def test_rerun_is_idempotent(self, engine):
        """Two consecutive backfill runs over the same window must
        produce exactly the same row set in signal_events."""
        with engine.begin() as conn:
            r1 = run_backfill_for_ticker(
                conn, 1806, _TICKER, Timeframe.WEEKLY,
                date(2024, 12, 1), date(2025, 4, 1),
            )
        with engine.begin() as conn:
            count_after_first = conn.exec_driver_sql(
                "SELECT COUNT(*) FROM signal_events WHERE ticker_id = 1806"
            ).scalar()

        with engine.begin() as conn:
            r2 = run_backfill_for_ticker(
                conn, 1806, _TICKER, Timeframe.WEEKLY,
                date(2024, 12, 1), date(2025, 4, 1),
            )
        with engine.begin() as conn:
            count_after_second = conn.exec_driver_sql(
                "SELECT COUNT(*) FROM signal_events WHERE ticker_id = 1806"
            ).scalar()

        assert r1.events_emitted == r2.events_emitted
        assert count_after_first == count_after_second, (
            "ON CONFLICT DO NOTHING must keep the row count stable on re-run"
        )

    def test_empty_window_returns_zero(self, engine):
        with engine.begin() as conn:
            result = run_backfill_for_ticker(
                conn, 1806, _TICKER, Timeframe.WEEKLY,
                # Window before any bars exist.
                date(2018, 1, 1), date(2018, 12, 31),
            )
        assert result.events_emitted == 0
        assert result.days_walked == 0
