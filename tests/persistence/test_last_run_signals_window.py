"""V1 read-path tests for trip_ema.gui.queries.last_run_signals.

Step 12a: this file was rewritten from scratch — the legacy test
scenarios pinned semantics that no longer apply in V1 (e.g.
"computed_at-based 60-second window", "any bar_timestamp newer
than 14 days ago"). V1 has tighter semantics tied to the exact
bar anchors.

V1 coloring rule (operator's spec):
  1. Check current_signals for a row at the current_bar (Monday
     00:00 UTC of the current IST week). If yes, color with its
     signal_type.
  2. Else, check historical_signals for a row at the current_bar
     OR the previous_bar (Monday of the prior IST week). If yes,
     color with the matching signal_type. When both bars match,
     the more recent (bar_timestamp DESC, computed_at DESC) wins.

A ticker has at most ONE entry in the result dict. Tickers with
no matching signal are absent (model treats absence as "no color").
"""
from __future__ import annotations

from datetime import datetime, timedelta, UTC

import pytest
from sqlalchemy import text

from trip_ema.domain import TickerRef
from trip_ema.domain.signal_event import SignalEvent
from trip_ema.gui.queries import last_run_signals
from trip_ema.persistence import TickersRepository, transaction
from trip_ema.persistence.current_signals_repo import (
    CurrentSignalsRepository,
)
from trip_ema.persistence.historical_signals_repo import (
    HistoricalSignalsRepository,
)


@pytest.fixture
def now():
    return datetime.now(UTC).replace(microsecond=0)


@pytest.fixture
def week_anchors(db_engine):
    """Return (current_monday_ts, previous_monday_ts) — same shape
    the query computes internally."""
    with db_engine.connect() as conn:
        # STEP-12C-POSTMORTEM: this fixture MUST match the SQL in
        # `_ist_week_anchors` in trip_ema/persistence/queries.py.
        # The OLD `::timestamptz` cast against a `::date` returns
        # Monday 00:00 in the SESSION TZ (Asia/Kolkata in Uddi's env),
        # not UTC. That made the fixture value 5.5h off from what
        # the production query filter computes, so signals seeded at
        # the fixture value never matched. Any change to the SQL below
        # must be mirrored in `_ist_week_anchors` and vice versa.
        row = conn.execute(text("""
            SELECT
                (date_trunc('week',
                            (NOW() AT TIME ZONE 'Asia/Kolkata'))::date::timestamp
                    AT TIME ZONE 'UTC')              AS current_monday_ts,
                ((date_trunc('week',
                             (NOW() AT TIME ZONE 'Asia/Kolkata'))::date
                  - INTERVAL '7 days'
                 )::timestamp
                    AT TIME ZONE 'UTC')              AS previous_monday_ts
        """)).first()
    return row.current_monday_ts, row.previous_monday_ts


def _insert_ticker(
    conn, symbol: str, *, exchange: str = "NSE", now=None,
) -> int:
    return TickersRepository(conn).insert(
        TickerRef(exchange, symbol, "EQ"),
        created_at=now or datetime.now(UTC),
    )


def _make_event(
    *, ticker_id: int, bar_ts: datetime, signal_type: str = "BUY",
    computed_at: datetime | None = None,
) -> SignalEvent:
    regime_map = {
        "BUY":            (-1,  1),
        "SELL":           ( 1, -1),
        "EXIT_FROM_BUY":  ( 1,  0),
        "EXIT_FROM_SELL": (-1,  0),
    }
    rb, ra = regime_map[signal_type]
    return SignalEvent(
        ticker_id=ticker_id,
        timeframe="1W",
        bar_timestamp=bar_ts,
        signal_type=signal_type,
        regime_before=rb,
        regime_after=ra,
        price_at_signal=100.0,
        computed_at=computed_at or bar_ts,
    )


# ============================================================
# Rule 1: current_signals at current_bar → colored
# ============================================================

def test_current_signals_at_current_bar_is_colored(
    db_engine, now, week_anchors,
):
    current_monday, _ = week_anchors
    with transaction(db_engine) as conn:
        tid = _insert_ticker(conn, "AAAA", now=now)
        CurrentSignalsRepository(conn).insert_many([
            _make_event(
                ticker_id=tid, bar_ts=current_monday,
                signal_type="BUY",
            ),
        ])

    with db_engine.connect() as conn:
        result = last_run_signals(conn, exchange="NSE")
    assert result.get(tid) == "BUY"


def test_current_signals_at_old_bar_NOT_colored(
    db_engine, now, week_anchors,
):
    """current_signals row exists but at a bar_timestamp older
    than current_bar → NOT colored. (In practice this shouldn't
    happen — the populator always writes to the latest bar — but
    the V1 read filter must verify the bar_timestamp match.)"""
    current_monday, previous_monday = week_anchors
    with transaction(db_engine) as conn:
        tid = _insert_ticker(conn, "BBBB", now=now)
        CurrentSignalsRepository(conn).insert_many([
            _make_event(
                ticker_id=tid, bar_ts=previous_monday,
                signal_type="BUY",
            ),
        ])

    with db_engine.connect() as conn:
        result = last_run_signals(conn, exchange="NSE")
    # The bar in current_signals is previous_monday, NOT current.
    # Rule 1 fails. Rule 2 should pick it up (historical at
    # current OR previous bar)... but it's not in historical
    # either. So absent.
    assert tid not in result


# ============================================================
# Rule 2: fall through to historical at current OR previous bar
# ============================================================

def test_historical_at_current_bar_is_colored(
    db_engine, now, week_anchors,
):
    """No current_signals for ticker, but historical_signals has
    a row at current_bar → colored."""
    current_monday, _ = week_anchors
    with transaction(db_engine) as conn:
        tid = _insert_ticker(conn, "CCCC", now=now)
        HistoricalSignalsRepository(conn).insert_many([
            _make_event(
                ticker_id=tid, bar_ts=current_monday,
                signal_type="SELL",
            ),
        ])

    with db_engine.connect() as conn:
        result = last_run_signals(conn, exchange="NSE")
    assert result.get(tid) == "SELL"


def test_historical_at_previous_bar_is_colored(
    db_engine, now, week_anchors,
):
    """No current_signals; historical at last Monday → colored."""
    _, previous_monday = week_anchors
    with transaction(db_engine) as conn:
        tid = _insert_ticker(conn, "DDDD", now=now)
        HistoricalSignalsRepository(conn).insert_many([
            _make_event(
                ticker_id=tid, bar_ts=previous_monday,
                signal_type="EXIT_FROM_BUY",
            ),
        ])

    with db_engine.connect() as conn:
        result = last_run_signals(conn, exchange="NSE")
    assert result.get(tid) == "EXIT_FROM_BUY"


def test_historical_two_weeks_back_NOT_colored(
    db_engine, now, week_anchors,
):
    _, previous_monday = week_anchors
    two_weeks_back = previous_monday - timedelta(days=7)
    with transaction(db_engine) as conn:
        tid = _insert_ticker(conn, "EEEE", now=now)
        HistoricalSignalsRepository(conn).insert_many([
            _make_event(
                ticker_id=tid, bar_ts=two_weeks_back,
                signal_type="BUY",
            ),
        ])

    with db_engine.connect() as conn:
        result = last_run_signals(conn, exchange="NSE")
    assert tid not in result


# ============================================================
# Precedence: current wins over historical
# ============================================================

def test_current_signals_wins_over_historical(
    db_engine, now, week_anchors,
):
    """Ticker has BOTH current_signals at current_bar AND
    historical_signals at previous_bar. current_signals' signal
    must be returned (current wins per V1 spec)."""
    current_monday, previous_monday = week_anchors
    with transaction(db_engine) as conn:
        tid = _insert_ticker(conn, "FFFF", now=now)
        CurrentSignalsRepository(conn).insert_many([
            _make_event(
                ticker_id=tid, bar_ts=current_monday,
                signal_type="BUY",
            ),
        ])
        HistoricalSignalsRepository(conn).insert_many([
            _make_event(
                ticker_id=tid, bar_ts=previous_monday,
                signal_type="SELL",
            ),
        ])

    with db_engine.connect() as conn:
        result = last_run_signals(conn, exchange="NSE")
    assert result.get(tid) == "BUY"   # current wins


def test_historical_newer_bar_wins_when_two_match(
    db_engine, now, week_anchors,
):
    """No current_signals; historical has rows at BOTH current
    and previous bar. The newer (current_bar) wins."""
    current_monday, previous_monday = week_anchors
    with transaction(db_engine) as conn:
        tid = _insert_ticker(conn, "GGGG", now=now)
        HistoricalSignalsRepository(conn).insert_many([
            _make_event(
                ticker_id=tid, bar_ts=previous_monday,
                signal_type="BUY",
            ),
            _make_event(
                ticker_id=tid, bar_ts=current_monday,
                signal_type="SELL",
            ),
        ])

    with db_engine.connect() as conn:
        result = last_run_signals(conn, exchange="NSE")
    assert result.get(tid) == "SELL"   # newer bar wins


# ============================================================
# No signals → absent (not present with None)
# ============================================================

def test_ticker_with_no_signals_absent_from_result(
    db_engine, now,
):
    with transaction(db_engine) as conn:
        tid = _insert_ticker(conn, "HHHH", now=now)

    with db_engine.connect() as conn:
        result = last_run_signals(conn, exchange="NSE")
    assert tid not in result


# ============================================================
# Filters
# ============================================================

def test_exchange_filter_excludes_other_exchanges(
    db_engine, now, week_anchors,
):
    current_monday, _ = week_anchors
    with transaction(db_engine) as conn:
        nse_tid = _insert_ticker(
            conn, "NSEONE", exchange="NSE", now=now,
        )
        bse_tid = _insert_ticker(
            conn, "BSEONE", exchange="BSE", now=now,
        )
        CurrentSignalsRepository(conn).insert_many([
            _make_event(
                ticker_id=nse_tid, bar_ts=current_monday,
                signal_type="BUY",
            ),
            _make_event(
                ticker_id=bse_tid, bar_ts=current_monday,
                signal_type="BUY",
            ),
        ])

    with db_engine.connect() as conn:
        result = last_run_signals(conn, exchange="NSE")
    assert nse_tid in result
    assert bse_tid not in result
