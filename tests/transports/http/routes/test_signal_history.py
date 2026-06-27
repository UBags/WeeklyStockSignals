"""Tests for `trip_ema.transports.http.routes.signal_history`.

End-to-end against real Postgres. Seed `ohlcv_bars`, `indicator_state`,
and `signal_events`; call `build_signal_history_payload`; verify the
returned payload mirrors the persisted tables (no engine replay).

The bug this rewrite fixes (Issue 2 from the triage memo): the OLD
endpoint ran the live TickerEngine to recompute regime per bar each
chart request. With the old two-stage core→display regime logic, that
recomputation could and did disagree with the persisted
`indicator_state.display_regime` and `signal_events`. The new endpoint
returns exactly what those tables hold.
"""

from __future__ import annotations

import os
from datetime import date, datetime, time, timedelta, timezone, UTC
from zoneinfo import ZoneInfo

import pytest

from trip_ema.domain import TickerRef, Timeframe
from trip_ema.transports.http.routes.signal_history import (
    build_signal_history_payload,
)


_IST = ZoneInfo("Asia/Kolkata")
_TICKER = TickerRef(exchange="NSE", symbol="SANSERA", instrument="EQ")


# ---------------------------------------------------------------------------
# Sandbox fixture (same shape as Steps 4-6).
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


# Schema mirrors the production-relevant slice of indicator_state +
# ohlcv_bars + signal_events. We include only the columns
# build_signal_history_payload reads.
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
    CREATE UNIQUE INDEX uq_indicator_state_ticker
        ON indicator_state (ticker_id, timeframe, trade_date)
        WHERE ticker_id IS NOT NULL;
    -- Step 12a: V1 read path — current_signals +
    -- historical_signals replace the legacy signal_events table.
    CREATE TABLE current_signals (
        ticker_id       INTEGER PRIMARY KEY REFERENCES tickers(ticker_id),
        timeframe       VARCHAR(8) NOT NULL,
        bar_timestamp   TIMESTAMPTZ NOT NULL,
        signal_type     VARCHAR(16) NOT NULL,
        regime_before   SMALLINT NOT NULL,
        regime_after    SMALLINT NOT NULL,
        price_at_signal DOUBLE PRECISION NOT NULL,
        computed_at     TIMESTAMPTZ NOT NULL
    );
    CREATE TABLE historical_signals (
        signal_id       BIGSERIAL PRIMARY KEY,
        ticker_id       INTEGER NOT NULL REFERENCES tickers(ticker_id),
        timeframe       VARCHAR(8) NOT NULL,
        bar_timestamp   TIMESTAMPTZ NOT NULL,
        signal_type     VARCHAR(16) NOT NULL,
        regime_before   SMALLINT NOT NULL,
        regime_after    SMALLINT NOT NULL,
        price_at_signal DOUBLE PRECISION NOT NULL,
        computed_at     TIMESTAMPTZ NOT NULL,
        archived_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    INSERT INTO tickers (ticker_id, exchange, symbol, instrument)
        VALUES (1806, 'NSE', 'SANSERA', 'EQ');
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
    test_db = f"trip_ema_hist_test_{os.getpid()}_{abs(hash(request.node.name)) % 100000}"

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


# ---------------------------------------------------------------------------
# Seed helpers.
# ---------------------------------------------------------------------------


def _seed_daily_bars(engine, days_back: int, end_date: date,
                     base_price: float = 100.0,
                     hl_spread: float = 0.5) -> None:
    """Seed `days_back` Mon-Fri daily bars ending at `end_date`."""
    with engine.begin() as conn:
        d = end_date
        bars_inserted = 0
        while bars_inserted < days_back:
            if d.weekday() < 5:  # Mon-Fri
                ts = datetime.combine(d, time(9, 15), tzinfo=_IST)
                ts_utc = ts.astimezone(UTC)
                conn.exec_driver_sql(
                    "INSERT INTO ohlcv_bars "
                    "(ticker_id, timeframe, bar_timestamp, open, high, low, close, volume) "
                    "VALUES (1806, '1D', %(ts)s, %(p)s, %(h)s, %(l)s, %(p)s, 1000000)",
                    dict(ts=ts_utc, p=base_price,
                         h=base_price + hl_spread / 2,
                         l=base_price - hl_spread / 2),
                )
                bars_inserted += 1
            d -= timedelta(days=1)


def _seed_indicator_state(engine, trade_date_ist: date,
                          display_regime: int,
                          *, med=100.0, long_=100.0, longest=100.0,
                          atr=2.0) -> None:
    """Seed one indicator_state row for the given week."""
    upper_inner = max(med, long_, longest) + 0.2 * atr
    lower_inner = min(med, long_, longest) - 0.2 * atr
    upper_mid = max(med, long_, longest) + 2.0 * atr
    lower_mid = min(med, long_, longest) - 2.0 * atr
    upper_outer = max(med, long_, longest) + 4.5 * atr
    lower_outer = min(med, long_, longest) - 4.5 * atr
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO indicator_state "
            "(ticker_id, timeframe, trade_date, med_ema, long_ema, longest_ema, atr, "
            " upper_inner_band, lower_inner_band, upper_mid_band, lower_mid_band, "
            " upper_outer_band, lower_outer_band, display_regime) "
            "VALUES (1806, '1W', %(td)s, %(m)s, %(l)s, %(lg)s, %(a)s, "
            " %(ui)s, %(li)s, %(um)s, %(lm)s, %(uo)s, %(lo)s, %(dr)s)",
            dict(td=trade_date_ist, m=med, l=long_, lg=longest, a=atr,
                 ui=upper_inner, li=lower_inner,
                 um=upper_mid, lm=lower_mid,
                 uo=upper_outer, lo=lower_outer,
                 dr=display_regime),
        )


def _seed_signal_event(engine, bar_timestamp_ist: datetime,
                       signal_type: str, price: float,
                       computed_at_utc: datetime,
                       *, target: str = "historical") -> None:
    """Step 12a: V1 read path. Default writes to historical_signals
    (matches the bulk of chart-marker test scenarios which seed
    past-bar signals). Pass target='current' to write to
    current_signals instead — useful for the "current_signals
    contributes to the window" scenario.

    regime_before / regime_after are chosen consistently with
    signal_type so a CHECK constraint match holds in production
    parity tests; the test schema omits the CHECK itself but the
    NOT NULL columns must be populated.
    """
    regime_map = {
        "BUY":            (-1,  1),
        "SELL":           ( 1, -1),
        "EXIT_FROM_BUY":  ( 1,  0),
        "EXIT_FROM_SELL": (-1,  0),
    }
    rb, ra = regime_map.get(signal_type, (-1, 1))
    bar_utc = bar_timestamp_ist.astimezone(UTC)
    if target == "current":
        sql = (
            "INSERT INTO current_signals "
            "(ticker_id, timeframe, bar_timestamp, signal_type, "
            " regime_before, regime_after, price_at_signal, computed_at) "
            "VALUES (1806, '1W', %(bt)s, %(st)s, %(rb)s, %(ra)s, "
            "        %(p)s, %(ca)s)"
        )
    elif target == "historical":
        sql = (
            "INSERT INTO historical_signals "
            "(ticker_id, timeframe, bar_timestamp, signal_type, "
            " regime_before, regime_after, price_at_signal, computed_at) "
            "VALUES (1806, '1W', %(bt)s, %(st)s, %(rb)s, %(ra)s, "
            "        %(p)s, %(ca)s)"
        )
    else:
        raise ValueError(
            f"target must be 'current' or 'historical', got {target!r}"
        )
    with engine.begin() as conn:
        conn.exec_driver_sql(
            sql,
            dict(bt=bar_utc, st=signal_type, rb=rb, ra=ra,
                 p=price, ca=computed_at_utc),
        )


# ===========================================================================
# Tests
# ===========================================================================


class TestSignalHistoryPayload:

    def test_empty_returns_empty_payload(self, engine):
        """No bars, no indicators, no events → empty lists."""
        now = datetime(2025, 6, 20, 12, 0, tzinfo=UTC)
        with engine.begin() as conn:
            payload = build_signal_history_payload(
                conn,
                ticker_id=1806,
                ticker_ref=_TICKER,
                timeframe=Timeframe.WEEKLY,
                years_back=3,
                now=now,
            )
        assert payload.exchange == "NSE"
        assert payload.symbol == "SANSERA"
        assert payload.instrument == "EQ"
        assert payload.timeframe == "1W"
        assert payload.bars == []
        assert payload.signal_events == []
        assert payload.warmup_bars_count == 0

    def test_rejects_non_weekly_timeframe(self, engine):
        with engine.begin() as conn, pytest.raises(NotImplementedError):
            build_signal_history_payload(
                conn,
                ticker_id=1806,
                ticker_ref=_TICKER,
                timeframe=Timeframe.DAILY,
            )

    def test_bars_carry_ohlc_from_ohlcv_bars(self, engine):
        """OHLC fields in the payload come from `ohlcv_bars`
        (aggregated)."""
        # Seed two weeks of bars with distinct prices.
        with engine.begin() as conn:
            # Week of Mon June 9 2025 — prices ramping 100→102 across 5 days.
            for i, d in enumerate([date(2025, 6, 9), date(2025, 6, 10),
                                   date(2025, 6, 11), date(2025, 6, 12),
                                   date(2025, 6, 13)]):
                price = 100.0 + i * 0.5
                ts_utc = datetime.combine(d, time(9, 15), tzinfo=_IST).astimezone(UTC)
                conn.exec_driver_sql(
                    "INSERT INTO ohlcv_bars (ticker_id, timeframe, bar_timestamp, "
                    "open, high, low, close, volume) "
                    "VALUES (1806, '1D', %(ts)s, %(p)s, %(h)s, %(l)s, %(p)s, 1000)",
                    dict(ts=ts_utc, p=price, h=price + 0.5, l=price - 0.5),
                )
        now = datetime(2025, 6, 16, 12, 0, tzinfo=UTC)
        with engine.begin() as conn:
            payload = build_signal_history_payload(
                conn, ticker_id=1806, ticker_ref=_TICKER,
                timeframe=Timeframe.WEEKLY, years_back=3, now=now,
            )
        assert len(payload.bars) == 1
        bar = payload.bars[0]
        # Open = first daily bar's open = 100.0
        assert bar.open == pytest.approx(100.0)
        # Close = last daily bar's close = 102.0
        assert bar.close == pytest.approx(102.0)
        # High = max across the week = 102.5 (last bar's high)
        assert bar.high == pytest.approx(102.5)
        # Low = min across the week = 99.5 (first bar's low)
        assert bar.low == pytest.approx(99.5)

    def test_warmup_bars_have_null_indicators(self, engine):
        """When a weekly bar has no matching indicator_state row, the
        indicator fields are None and is_warmed_up is False."""
        _seed_daily_bars(engine, days_back=10, end_date=date(2025, 6, 16),
                         base_price=100.0)
        # NO indicator_state rows seeded.
        now = datetime(2025, 6, 16, 12, 0, tzinfo=UTC)
        with engine.begin() as conn:
            payload = build_signal_history_payload(
                conn, ticker_id=1806, ticker_ref=_TICKER,
                timeframe=Timeframe.WEEKLY, years_back=3, now=now,
            )
        assert len(payload.bars) >= 1
        for bar in payload.bars:
            assert bar.med_ema is None
            assert bar.atr is None
            assert bar.upper_inner is None
            assert bar.is_warmed_up is False
            assert bar.display_bg == "NEUTRAL"
            assert bar.core_bg == "NEUTRAL"
        assert payload.warmup_bars_count == len(payload.bars)

    def test_mature_bars_carry_indicators_from_indicator_state(self, engine):
        """Bars with a matching indicator_state row carry the EMAs,
        ATR, bands, and display_regime → display_bg."""
        # Seed two weekly bars' worth of daily data, but only an
        # indicator_state row for the second week.
        _seed_daily_bars(engine, days_back=10, end_date=date(2025, 6, 13),
                         base_price=100.0)
        # The week of Mon June 9 2025 — indicator_state.trade_date.
        _seed_indicator_state(
            engine,
            trade_date_ist=date(2025, 6, 9),
            display_regime=1,  # BULLISH
            med=101.0, long_=100.5, longest=100.0, atr=1.5,
        )
        now = datetime(2025, 6, 14, 12, 0, tzinfo=UTC)
        with engine.begin() as conn:
            payload = build_signal_history_payload(
                conn, ticker_id=1806, ticker_ref=_TICKER,
                timeframe=Timeframe.WEEKLY, years_back=3, now=now,
            )
        # Pick the bar for the week of June 9.
        june_9_bars = [
            b for b in payload.bars
            if b.bar_timestamp.astimezone(_IST).date() == date(2025, 6, 9)
        ]
        assert len(june_9_bars) == 1
        bar = june_9_bars[0]
        assert bar.is_warmed_up is True
        assert bar.med_ema == pytest.approx(101.0)
        assert bar.long_ema == pytest.approx(100.5)
        assert bar.longest_ema == pytest.approx(100.0)
        assert bar.atr == pytest.approx(1.5)
        # max(101, 100.5, 100) + 0.2*1.5 = 101.3
        assert bar.upper_inner == pytest.approx(101.3)
        # display_regime = 1 → CoreRegime.BULLISH → "BULLISH"
        assert bar.display_bg == "BULLISH"
        assert bar.core_bg == "BULLISH"

    def test_display_regime_minus_one_maps_to_bearish(self, engine):
        _seed_daily_bars(engine, days_back=5, end_date=date(2025, 6, 13))
        _seed_indicator_state(
            engine, trade_date_ist=date(2025, 6, 9),
            display_regime=-1,
        )
        now = datetime(2025, 6, 14, tzinfo=UTC)
        with engine.begin() as conn:
            payload = build_signal_history_payload(
                conn, ticker_id=1806, ticker_ref=_TICKER,
                timeframe=Timeframe.WEEKLY, years_back=3, now=now,
            )
        june_9_bars = [
            b for b in payload.bars
            if b.bar_timestamp.astimezone(_IST).date() == date(2025, 6, 9)
        ]
        assert len(june_9_bars) == 1
        assert june_9_bars[0].display_bg == "BEARISH"
        assert june_9_bars[0].core_bg == "BEARISH"

    def test_display_regime_zero_maps_to_neutral(self, engine):
        _seed_daily_bars(engine, days_back=5, end_date=date(2025, 6, 13))
        _seed_indicator_state(
            engine, trade_date_ist=date(2025, 6, 9),
            display_regime=0,
        )
        now = datetime(2025, 6, 14, tzinfo=UTC)
        with engine.begin() as conn:
            payload = build_signal_history_payload(
                conn, ticker_id=1806, ticker_ref=_TICKER,
                timeframe=Timeframe.WEEKLY, years_back=3, now=now,
            )
        june_9_bars = [
            b for b in payload.bars
            if b.bar_timestamp.astimezone(_IST).date() == date(2025, 6, 9)
        ]
        assert len(june_9_bars) == 1
        assert june_9_bars[0].display_bg == "NEUTRAL"
        assert june_9_bars[0].is_warmed_up is True

    def test_signal_events_returned_in_chronological_order(self, engine):
        _seed_daily_bars(engine, days_back=30, end_date=date(2025, 6, 13))
        # Seed three signals on different weeks.
        _seed_signal_event(
            engine,
            bar_timestamp_ist=datetime(2025, 5, 26, tzinfo=_IST),
            signal_type="BUY", price=100.0,
            computed_at_utc=datetime(2025, 5, 30, tzinfo=UTC),
        )
        _seed_signal_event(
            engine,
            bar_timestamp_ist=datetime(2025, 6, 2, tzinfo=_IST),
            signal_type="EXIT_FROM_BUY", price=99.0,
            computed_at_utc=datetime(2025, 6, 6, tzinfo=UTC),
        )
        _seed_signal_event(
            engine,
            bar_timestamp_ist=datetime(2025, 6, 9, tzinfo=_IST),
            signal_type="SELL", price=95.0,
            computed_at_utc=datetime(2025, 6, 13, tzinfo=UTC),
        )
        now = datetime(2025, 6, 16, 12, 0, tzinfo=UTC)
        with engine.begin() as conn:
            payload = build_signal_history_payload(
                conn, ticker_id=1806, ticker_ref=_TICKER,
                timeframe=Timeframe.WEEKLY, years_back=3, now=now,
            )
        assert len(payload.signal_events) == 3
        signals = [ev.signal for ev in payload.signal_events]
        assert signals == ["BUY", "EXIT_FROM_BUY", "SELL"]
        # Bar timestamps in ascending order.
        bts = [ev.bar_timestamp for ev in payload.signal_events]
        assert bts == sorted(bts)

    def test_signals_outside_window_are_excluded(self, engine):
        """A signal from 10 years ago shouldn't be in a 3-year window."""
        _seed_daily_bars(engine, days_back=5, end_date=date(2025, 6, 13))
        # Way out of window.
        _seed_signal_event(
            engine,
            bar_timestamp_ist=datetime(2010, 1, 4, tzinfo=_IST),
            signal_type="BUY", price=50.0,
            computed_at_utc=datetime(2010, 1, 8, tzinfo=UTC),
        )
        # In window.
        _seed_signal_event(
            engine,
            bar_timestamp_ist=datetime(2025, 6, 9, tzinfo=_IST),
            signal_type="SELL", price=95.0,
            computed_at_utc=datetime(2025, 6, 13, tzinfo=UTC),
        )
        now = datetime(2025, 6, 16, tzinfo=UTC)
        with engine.begin() as conn:
            payload = build_signal_history_payload(
                conn, ticker_id=1806, ticker_ref=_TICKER,
                timeframe=Timeframe.WEEKLY, years_back=3, now=now,
            )
        signals = [ev.signal for ev in payload.signal_events]
        assert signals == ["SELL"]

    def test_signal_event_computed_at_preserved(self, engine):
        """The bug Issue 1 hinted at: GUI's `Last 5 signals` reads
        computed_at, not bar_timestamp. The payload must carry the
        DB's computed_at value verbatim."""
        _seed_daily_bars(engine, days_back=5, end_date=date(2025, 6, 13))
        ca = datetime(2025, 6, 11, 11, 36, 9, tzinfo=UTC)
        _seed_signal_event(
            engine,
            bar_timestamp_ist=datetime(2025, 6, 9, tzinfo=_IST),
            signal_type="BUY", price=100.0,
            computed_at_utc=ca,
        )
        now = datetime(2025, 6, 16, tzinfo=UTC)
        with engine.begin() as conn:
            payload = build_signal_history_payload(
                conn, ticker_id=1806, ticker_ref=_TICKER,
                timeframe=Timeframe.WEEKLY, years_back=3, now=now,
            )
        assert len(payload.signal_events) == 1
        assert payload.signal_events[0].computed_at == ca

    def test_warmup_count_matches_unmatched_bars(self, engine):
        """warmup_bars_count == number of bars without indicator_state row."""
        # 4 weeks of dailies but only 2 indicator_state rows for the
        # latest 2 weeks.
        _seed_daily_bars(engine, days_back=20, end_date=date(2025, 6, 13))
        _seed_indicator_state(engine, date(2025, 6, 2),  display_regime=1)
        _seed_indicator_state(engine, date(2025, 6, 9),  display_regime=1)
        now = datetime(2025, 6, 14, tzinfo=UTC)
        with engine.begin() as conn:
            payload = build_signal_history_payload(
                conn, ticker_id=1806, ticker_ref=_TICKER,
                timeframe=Timeframe.WEEKLY, years_back=3, now=now,
            )
        warmup = [b for b in payload.bars if not b.is_warmed_up]
        mature = [b for b in payload.bars if b.is_warmed_up]
        assert payload.warmup_bars_count == len(warmup)
        # Exactly 2 mature bars (we seeded 2 indicator_state rows).
        assert len(mature) == 2

    def test_bars_sorted_oldest_to_newest(self, engine):
        _seed_daily_bars(engine, days_back=30, end_date=date(2025, 6, 13))
        now = datetime(2025, 6, 14, tzinfo=UTC)
        with engine.begin() as conn:
            payload = build_signal_history_payload(
                conn, ticker_id=1806, ticker_ref=_TICKER,
                timeframe=Timeframe.WEEKLY, years_back=3, now=now,
            )
        timestamps = [b.bar_timestamp for b in payload.bars]
        assert timestamps == sorted(timestamps), (
            "bars must be chronological for the chart's left→right walk"
        )
