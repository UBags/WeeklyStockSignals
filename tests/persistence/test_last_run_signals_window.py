"""Regression test for trip_ema.gui.queries.last_run_signals.

Pins the new contract: the dict returned is keyed on signal events whose
`bar_timestamp` falls in the current or previous IST week, NOT on
`computed_at` (the earlier behaviour, which flashed every ticker after a
full backfill rewrote every event's computed_at to NOW()).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text as _sql_text

from trip_ema.domain import ChangeSignal, SignalEvent, TickerRef, Timeframe
from trip_ema.gui.queries import last_run_signals
from trip_ema.persistence import (
    SignalsRepository,
    TickersRepository,
    transaction,
)


@pytest.fixture
def now():
    return datetime.now(timezone.utc).replace(microsecond=0)


def _mk_ticker(conn, symbol: str, *, exchange: str = "NSE", now) -> int:
    return TickersRepository(conn).insert(
        TickerRef(exchange, symbol, "EQ"), created_at=now,
    )


def _mk_signal(
    conn, ticker_id: int, ref: TickerRef,
    *, bar_ts: datetime, computed_at: datetime,
    signal: ChangeSignal = ChangeSignal.BUY,
) -> None:
    SignalsRepository(conn).insert_event(
        ticker_id,
        SignalEvent(ref, Timeframe.WEEKLY, bar_ts, signal, 100.0, computed_at),
    )


def test_ticker_with_current_week_bar_is_colored(db_engine, now):
    ref = TickerRef("NSE", "FRESHBAR", "EQ")
    with transaction(db_engine) as conn:
        tid = _mk_ticker(conn, "FRESHBAR", now=now)
        _mk_signal(conn, tid, ref, bar_ts=now, computed_at=now)

    with db_engine.connect() as conn:
        out = last_run_signals(conn, exchange="NSE")

    assert out.get(tid) == "BUY"


def test_ticker_with_old_bar_and_fresh_computed_at_is_NOT_colored(db_engine, now):
    """The regression: a regen-style row whose computed_at is NOW() but
    whose bar_timestamp is months old must not appear in the dict.

    Earlier the 60s computed_at window included this row, painting every
    historical signal green after a full backfill.
    """
    ref = TickerRef("NSE", "BATCHED", "EQ")
    old_bar = now - timedelta(days=120)
    with transaction(db_engine) as conn:
        tid = _mk_ticker(conn, "BATCHED", now=now)
        _mk_signal(conn, tid, ref, bar_ts=old_bar, computed_at=now)

    with db_engine.connect() as conn:
        out = last_run_signals(conn, exchange="NSE")

    assert tid not in out, (
        f"BATCHED was colored despite bar_timestamp being {old_bar.date()}"
    )


def test_ticker_with_previous_week_bar_is_colored(db_engine, now):
    """The window is two weeks wide: this week + last week."""
    ref = TickerRef("NSE", "LASTWEEK", "EQ")
    # Pick a bar_timestamp clearly inside the previous IST week:
    # 9 days back lands in the prior week regardless of which weekday today is.
    prev_week_bar = now - timedelta(days=9)
    with transaction(db_engine) as conn:
        tid = _mk_ticker(conn, "LASTWEEK", now=now)
        _mk_signal(conn, tid, ref, bar_ts=prev_week_bar, computed_at=now)

    with db_engine.connect() as conn:
        out = last_run_signals(conn, exchange="NSE")

    assert out.get(tid) == "BUY"


def test_ticker_with_three_week_old_bar_is_NOT_colored(db_engine, now):
    """Anything older than the start of last week is out of window."""
    ref = TickerRef("NSE", "STALEBAR", "EQ")
    with transaction(db_engine) as conn:
        tid = _mk_ticker(conn, "STALEBAR", now=now)
        _mk_signal(
            conn, tid, ref,
            bar_ts=now - timedelta(days=21), computed_at=now,
        )

    with db_engine.connect() as conn:
        out = last_run_signals(conn, exchange="NSE")

    assert tid not in out


def test_latest_signal_wins_when_ticker_has_multiple_recent_events(db_engine, now):
    """Two events both in window → the later bar_timestamp wins."""
    ref = TickerRef("NSE", "TWOSIGS", "EQ")
    earlier = now - timedelta(days=8)   # last week
    later = now                          # this week
    with transaction(db_engine) as conn:
        tid = _mk_ticker(conn, "TWOSIGS", now=now)
        _mk_signal(conn, tid, ref, bar_ts=earlier,
                   computed_at=earlier, signal=ChangeSignal.BUY)
        _mk_signal(conn, tid, ref, bar_ts=later,
                   computed_at=later, signal=ChangeSignal.SELL)

    with db_engine.connect() as conn:
        out = last_run_signals(conn, exchange="NSE")

    assert out.get(tid) == "SELL"


def test_exchange_filter_excludes_other_exchanges(db_engine, now):
    with transaction(db_engine) as conn:
        nse_id = _mk_ticker(conn, "NSESIG", exchange="NSE", now=now)
        bse_id = _mk_ticker(conn, "BSESIG", exchange="BSE", now=now)
        _mk_signal(conn, nse_id, TickerRef("NSE", "NSESIG", "EQ"),
                   bar_ts=now, computed_at=now)
        _mk_signal(conn, bse_id, TickerRef("BSE", "BSESIG", "EQ"),
                   bar_ts=now, computed_at=now)

    with db_engine.connect() as conn:
        nse_out = last_run_signals(conn, exchange="NSE")

    assert nse_id in nse_out
    assert bse_id not in nse_out