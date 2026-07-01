"""DB tests for trip_ema.gui.queries.ticker_universe_with_recent_signals.

Step 12a: V1 read path. The function reads current_signals +
historical_signals (no JOIN, two separate queries composed in
Python) to flag each active ticker with has_recent_signal.

V1 semantics for "recent":
  - current_signals has a row at the current_bar (Monday 00:00 UTC
    of the current IST week, i.e. today's Monday) → recent=True
  - else historical_signals has a row at current_bar OR
    previous_bar (Monday of the prior IST week) → recent=True
  - else recent=False

Ordering contract: (has_recent_signal DESC, symbol ASC). The
Signals tab dropdown depends on this for its one-pass partition.
Step 12b adds the industry-grouped + rank-score ordering at the
view layer; the query keeps the simple contract.
"""
from __future__ import annotations

from datetime import datetime, timedelta, UTC

import pytest
from sqlalchemy import text

from trip_ema.domain import TickerRef
from trip_ema.domain.signal_event import SignalEvent
from trip_ema.gui.queries import ticker_universe_with_recent_signals
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
    the query computes internally. Tests use these to seed at
    exact bar boundaries."""
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


def _row_by_symbol(rows, sym):
    matches = [r for r in rows if r.symbol == sym]
    assert len(matches) == 1, (
        f"expected exactly one row for {sym}, got {len(matches)}"
    )
    return matches[0]


# ============================================================
# V1 recency semantics
# ============================================================

def test_ticker_with_current_signal_at_current_bar_is_recent(
    db_engine, now, week_anchors,
):
    """current_signals has a row at this Monday → recent=True."""
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
        rows = ticker_universe_with_recent_signals(
            conn, exchange="NSE",
        )
    row = _row_by_symbol(rows, "AAAA")
    assert row.has_recent_signal is True
    assert row.latest_signal == "BUY"


def test_ticker_with_historical_at_current_bar_is_recent(
    db_engine, now, week_anchors,
):
    """current_signals empty for this ticker; historical at
    current bar → recent=True."""
    current_monday, _ = week_anchors
    with transaction(db_engine) as conn:
        tid = _insert_ticker(conn, "BBBB", now=now)
        HistoricalSignalsRepository(conn).insert_many([
            _make_event(
                ticker_id=tid, bar_ts=current_monday,
                signal_type="SELL",
            ),
        ])

    with db_engine.connect() as conn:
        rows = ticker_universe_with_recent_signals(
            conn, exchange="NSE",
        )
    row = _row_by_symbol(rows, "BBBB")
    assert row.has_recent_signal is True
    assert row.latest_signal == "SELL"


def test_ticker_with_historical_at_previous_bar_is_recent(
    db_engine, now, week_anchors,
):
    """current_signals empty; historical at last Monday →
    recent=True."""
    _, previous_monday = week_anchors
    with transaction(db_engine) as conn:
        tid = _insert_ticker(conn, "CCCC", now=now)
        HistoricalSignalsRepository(conn).insert_many([
            _make_event(
                ticker_id=tid, bar_ts=previous_monday,
                signal_type="EXIT_FROM_BUY",
            ),
        ])

    with db_engine.connect() as conn:
        rows = ticker_universe_with_recent_signals(
            conn, exchange="NSE",
        )
    row = _row_by_symbol(rows, "CCCC")
    assert row.has_recent_signal is True
    assert row.latest_signal == "EXIT_FROM_BUY"


def test_ticker_with_historical_two_weeks_back_is_not_recent(
    db_engine, now, week_anchors,
):
    """Historical signal older than previous_monday → not recent."""
    _, previous_monday = week_anchors
    two_weeks_back = previous_monday - timedelta(days=7)
    with transaction(db_engine) as conn:
        tid = _insert_ticker(conn, "DDDD", now=now)
        HistoricalSignalsRepository(conn).insert_many([
            _make_event(
                ticker_id=tid, bar_ts=two_weeks_back,
                signal_type="BUY",
            ),
        ])

    with db_engine.connect() as conn:
        rows = ticker_universe_with_recent_signals(
            conn, exchange="NSE",
        )
    row = _row_by_symbol(rows, "DDDD")
    assert row.has_recent_signal is False
    assert row.latest_signal is None


def test_ticker_with_no_signals_is_not_recent(
    db_engine, now,
):
    """No row in either table → recent=False (LEFT JOIN preserves)."""
    with transaction(db_engine) as conn:
        _insert_ticker(conn, "EEEE", now=now)

    with db_engine.connect() as conn:
        rows = ticker_universe_with_recent_signals(
            conn, exchange="NSE",
        )
    row = _row_by_symbol(rows, "EEEE")
    assert row.has_recent_signal is False
    assert row.latest_signal is None


def test_current_signals_wins_over_historical_at_previous_bar(
    db_engine, now, week_anchors,
):
    """Ticker has BOTH current_signals at current bar AND
    historical at previous bar. The current_signals signal_type
    must win (per V1 spec: check current first)."""
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
        rows = ticker_universe_with_recent_signals(
            conn, exchange="NSE",
        )
    row = _row_by_symbol(rows, "FFFF")
    assert row.has_recent_signal is True
    assert row.latest_signal == "BUY"   # current wins


# ============================================================
# Ordering
# ============================================================

def test_ordering_recent_then_alpha(
    db_engine, now, week_anchors,
):
    """Result rows ordered by (has_recent_signal DESC, symbol ASC).
    Step 12b layers industry/rank ordering on top inside the view."""
    current_monday, _ = week_anchors
    with transaction(db_engine) as conn:
        a = _insert_ticker(conn, "AAA", now=now)
        b = _insert_ticker(conn, "BBB", now=now)
        c = _insert_ticker(conn, "CCC", now=now)
        d = _insert_ticker(conn, "DDD", now=now)
        # Recent: B and D
        CurrentSignalsRepository(conn).insert_many([
            _make_event(
                ticker_id=b, bar_ts=current_monday,
                signal_type="BUY",
            ),
            _make_event(
                ticker_id=d, bar_ts=current_monday,
                signal_type="SELL",
            ),
        ])

    with db_engine.connect() as conn:
        rows = ticker_universe_with_recent_signals(
            conn, exchange="NSE",
        )
    # Filter to the 4 we inserted (the fixture DB may have others
    # from other tests if they share state)
    syms = [r.symbol for r in rows if r.symbol in {
        "AAA", "BBB", "CCC", "DDD",
    }]
    # Recent (B, D) before non-recent (A, C); alpha within each group
    assert syms == ["BBB", "DDD", "AAA", "CCC"]


# ============================================================
# Filters
# ============================================================

def test_exchange_filter_excludes_other_exchanges(
    db_engine, now, week_anchors,
):
    """exchange='NSE' should not include BSE tickers."""
    current_monday, _ = week_anchors
    with transaction(db_engine) as conn:
        nse_tid = _insert_ticker(
            conn, "NSEONLY", exchange="NSE", now=now,
        )
        bse_tid = _insert_ticker(
            conn, "BSEONLY", exchange="BSE", now=now,
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
        nse_rows = ticker_universe_with_recent_signals(
            conn, exchange="NSE",
        )
    symbols = {r.symbol for r in nse_rows}
    assert "NSEONLY" in symbols
    assert "BSEONLY" not in symbols


def test_inactive_ticker_excluded(
    db_engine, now, week_anchors,
):
    current_monday, _ = week_anchors
    with transaction(db_engine) as conn:
        tid = _insert_ticker(conn, "INACTIVE", now=now)
        # Mark inactive
        conn.execute(text("""
            UPDATE tickers SET is_active = FALSE
             WHERE ticker_id = :tid
        """), {"tid": tid})
        CurrentSignalsRepository(conn).insert_many([
            _make_event(
                ticker_id=tid, bar_ts=current_monday,
                signal_type="BUY",
            ),
        ])

    with db_engine.connect() as conn:
        rows = ticker_universe_with_recent_signals(
            conn, exchange="NSE",
        )
    assert "INACTIVE" not in {r.symbol for r in rows}


def test_delisted_ticker_excluded(
    db_engine, now, week_anchors,
):
    current_monday, _ = week_anchors
    with transaction(db_engine) as conn:
        tid = _insert_ticker(conn, "DELISTED", now=now)
        conn.execute(text("""
            UPDATE tickers SET delisted_at = NOW()
             WHERE ticker_id = :tid
        """), {"tid": tid})
        CurrentSignalsRepository(conn).insert_many([
            _make_event(
                ticker_id=tid, bar_ts=current_monday,
                signal_type="BUY",
            ),
        ])

    with db_engine.connect() as conn:
        rows = ticker_universe_with_recent_signals(
            conn, exchange="NSE",
        )
    assert "DELISTED" not in {r.symbol for r in rows}
