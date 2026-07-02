"""V1 read-path tests for trip_ema.gui.queries.last_run_signals.

last_run_signals returns {ticker_id: SignalColorInputs}, where
SignalColorInputs carries the raw DB inputs the coloring rule needs:
  - current_signal  : a flip on the current weekly bar (Monday 00:00
                      UTC of the current IST week) — from current_signals,
                      else historical_signals@current_bar (post-rotation).
  - previous_signal : a flip on the previous weekly bar — from
                      historical_signals@previous_bar.
  - current_regime  : display_regime of the current weekly bar (exercised
                      in the model tests, not seeded here).

Unlike the pre-refactor contract, current and previous bars no longer
collapse into a single "recent signal" — they surface as separate
fields. A ticker with neither is absent from the dict (model treats
absence as "no color").
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
# current-bar signal: current_signals at current_bar
# ============================================================

def test_current_signals_at_current_bar_sets_current_signal(
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
    assert result[tid].current_signal == "BUY"
    assert result[tid].previous_signal is None


def test_current_signals_at_old_bar_is_absent(
    db_engine, now, week_anchors,
):
    """current_signals row exists but at a bar_timestamp older than
    current_bar → not picked up. current_signals is only read at the
    current bar; previous-bar signals come from historical. Nothing in
    historical → absent. (Shouldn't happen in practice — the populator
    always writes the latest bar — but the read filter must verify the
    bar_timestamp match.)"""
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
    assert tid not in result


# ============================================================
# current-bar signal from historical; previous-bar signal
# ============================================================

def test_historical_at_current_bar_sets_current_signal(
    db_engine, now, week_anchors,
):
    """No current_signals for ticker, but historical_signals has a row
    at current_bar → current_signal set (post-rotation case)."""
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
    assert result[tid].current_signal == "SELL"
    assert result[tid].previous_signal is None


def test_historical_at_previous_bar_sets_previous_signal(
    db_engine, now, week_anchors,
):
    """No current_signals; historical at last Monday → previous_signal
    set, current_signal None."""
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
    assert result[tid].current_signal is None
    assert result[tid].previous_signal == "EXIT_FROM_BUY"


def test_historical_two_weeks_back_is_absent(
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
# current_signals precedence + current/previous split
# ============================================================

def test_current_signals_wins_over_historical_at_current_bar(
    db_engine, now, week_anchors,
):
    """Ticker has BOTH current_signals AND historical at the current
    bar. current_signals' signal_type is the current_signal (it wins
    for the current bar)."""
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
    # current bar from current_signals, previous bar from historical —
    # both surface as separate fields.
    assert result[tid].current_signal == "BUY"
    assert result[tid].previous_signal == "SELL"


def test_historical_current_and_previous_surface_separately(
    db_engine, now, week_anchors,
):
    """No current_signals; historical has rows at BOTH current and
    previous bar. They no longer collapse — current_signal comes from
    the current bar, previous_signal from the previous bar."""
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
    assert result[tid].current_signal == "SELL"    # current bar
    assert result[tid].previous_signal == "BUY"    # previous bar


# ============================================================
# No signals → absent (not present with an empty inputs)
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
