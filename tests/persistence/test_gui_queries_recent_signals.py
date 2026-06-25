"""DB test for trip_ema.gui.queries.ticker_universe_with_recent_signals.

Pins down the rolling 'this week + last week' window semantics:
  - A ticker with any signal event in the current or previous IST
    week comes back with has_recent_signal=True.
  - A ticker with NO signal events ever comes back with has_recent
    _signal=False (LEFT JOIN preserves it in the result).
  - A ticker whose only signal events are older than the cutoff
    comes back with has_recent_signal=False.

Ordering contract: rows arrive sorted by (has_recent_signal DESC,
symbol ASC). The Signals tab dropdown depends on this ordering for
its one-pass partition.

Lives in tests/persistence/ rather than tests/gui/ because it
exercises the SQL layer, not Qt — the test uses the real Postgres
fixture from tests/persistence/conftest.py.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone, UTC

import pytest

from trip_ema.domain import (
    ChangeSignal,
    SignalEvent,
    TickerRef,
    Timeframe,
)
from trip_ema.gui.queries import ticker_universe_with_recent_signals
from trip_ema.persistence import (
    SignalsRepository,
    TickersRepository,
    transaction,
)


@pytest.fixture
def now():
    return datetime.now(UTC).replace(microsecond=0)


def _insert_ticker(conn, symbol: str, *, exchange: str = "NSE", now=None) -> int:
    return TickersRepository(conn).insert(
        TickerRef(exchange, symbol, "EQ"),
        created_at=now or datetime.now(UTC),
    )


def _insert_signal(conn, ticker_id: int, ref: TickerRef, bar_ts: datetime) -> None:
    SignalsRepository(conn).insert_event(
        ticker_id,
        SignalEvent(
            ref, Timeframe.WEEKLY, bar_ts,
            ChangeSignal.BUY, 100.0, bar_ts,
        ),
    )


# ---------- Recent-flag truth table ---------- #


def test_ticker_with_signal_in_current_week_is_flagged_recent(db_engine, now):
    """Insert one ticker with a fresh signal — expect has_recent_signal=True."""
    ref = TickerRef("NSE", "RELIANCE", "EQ")
    with transaction(db_engine) as conn:
        tid = _insert_ticker(conn, "RELIANCE", now=now)
        _insert_signal(conn, tid, ref, now)  # bar_ts == now → in current week

    with db_engine.connect() as conn:
        rows = ticker_universe_with_recent_signals(conn, exchange="NSE")

    [matching] = [r for r in rows if r.symbol == "RELIANCE"]
    assert matching.has_recent_signal is True


def test_ticker_with_no_signals_is_flagged_not_recent(db_engine, now):
    """A ticker that's never fired a signal should appear (LEFT JOIN)
    with has_recent_signal=False — not filtered out."""
    with transaction(db_engine) as conn:
        _insert_ticker(conn, "QUIET", now=now)

    with db_engine.connect() as conn:
        rows = ticker_universe_with_recent_signals(conn, exchange="NSE")

    [matching] = [r for r in rows if r.symbol == "QUIET"]
    assert matching.has_recent_signal is False


def test_ticker_with_only_old_signals_is_flagged_not_recent(db_engine, now):
    """A ticker whose only signal events are older than 2 weeks should
    fall into the 'no recent' bucket. We use 30 days back as a clearly-
    outside-window timestamp, avoiding any borderline week-boundary
    interaction."""
    ref = TickerRef("NSE", "STALE", "EQ")
    old_ts = now - timedelta(days=30)
    with transaction(db_engine) as conn:
        tid = _insert_ticker(conn, "STALE", now=now)
        _insert_signal(conn, tid, ref, old_ts)

    with db_engine.connect() as conn:
        rows = ticker_universe_with_recent_signals(conn, exchange="NSE")

    [matching] = [r for r in rows if r.symbol == "STALE"]
    assert matching.has_recent_signal is False


# ---------- Ordering contract ---------- #


def test_ordering_recent_then_alpha_within_each_section(db_engine, now):
    """The full dropdown contract: recent rows alphabetical first, then
    non-recent rows alphabetical. The consumer (partition_ticker_rows)
    splits at the True->False boundary in one pass."""
    ref_a = TickerRef("NSE", "AARTIIND", "EQ")
    ref_h = TickerRef("NSE", "HDFCBANK", "EQ")
    with transaction(db_engine) as conn:
        # Two tickers with recent signals: HDFCBANK + AARTIIND
        tid_a = _insert_ticker(conn, "AARTIIND", now=now)
        _insert_signal(conn, tid_a, ref_a, now)
        tid_h = _insert_ticker(conn, "HDFCBANK", now=now)
        _insert_signal(conn, tid_h, ref_h, now)
        # Two tickers without any signals: WIPRO + ZEEL
        _insert_ticker(conn, "WIPRO", now=now)
        _insert_ticker(conn, "ZEEL", now=now)

    with db_engine.connect() as conn:
        rows = ticker_universe_with_recent_signals(conn, exchange="NSE")

    # Filter to just the test tickers to avoid coupling to any
    # tickers other tests may have left behind.
    test_symbols = {"AARTIIND", "HDFCBANK", "WIPRO", "ZEEL"}
    relevant = [r for r in rows if r.symbol in test_symbols]
    assert [r.symbol for r in relevant] == [
        "AARTIIND", "HDFCBANK", "WIPRO", "ZEEL",
    ]
    assert [r.has_recent_signal for r in relevant] == [
        True, True, False, False,
    ]


def test_exchange_filter_excludes_other_exchanges(db_engine, now):
    """exchange='NSE' should not return BSE/NYSE rows."""
    with transaction(db_engine) as conn:
        _insert_ticker(conn, "NSESYM", exchange="NSE", now=now)
        _insert_ticker(conn, "BSESYM", exchange="BSE", now=now)

    with db_engine.connect() as conn:
        nse_rows = ticker_universe_with_recent_signals(conn, exchange="NSE")
        all_rows = ticker_universe_with_recent_signals(conn)

    nse_syms = {r.symbol for r in nse_rows}
    all_syms = {r.symbol for r in all_rows}
    assert "NSESYM" in nse_syms
    assert "BSESYM" not in nse_syms
    # Without an exchange filter we see both
    assert {"NSESYM", "BSESYM"} <= all_syms


def test_inactive_ticker_excluded(db_engine, now):
    """Tickers with is_active=FALSE are filtered out of the dropdown.

    Uses the repo's public API. The query under test filters on both
    is_active AND delisted_at; this case covers the is_active gate.
    """
    with transaction(db_engine) as conn:
        TickersRepository(conn).insert(
            TickerRef("NSE", "INACTIVE", "EQ"),
            created_at=now, is_active=False,
        )

    with db_engine.connect() as conn:
        rows = ticker_universe_with_recent_signals(conn, exchange="NSE")

    assert "INACTIVE" not in {r.symbol for r in rows}


def test_delisted_ticker_excluded(db_engine, now):
    """Tickers with delisted_at IS NOT NULL are filtered out of the
    dropdown, even when is_active is still TRUE.

    No repo method writes to `delisted_at`, so we set it directly
    via SQL. This case covers the delisted_at gate of the WHERE
    clause (`is_active = TRUE AND delisted_at IS NULL`).
    """
    from sqlalchemy import text as _sql_text
    with transaction(db_engine) as conn:
        TickersRepository(conn).insert(
            TickerRef("NSE", "DELISTED", "EQ"), created_at=now,
        )
        conn.execute(
            _sql_text("""
                UPDATE tickers SET delisted_at = :ts
                WHERE exchange = 'NSE' AND symbol = 'DELISTED'
            """),
            {"ts": now},
        )

    with db_engine.connect() as conn:
        rows = ticker_universe_with_recent_signals(conn, exchange="NSE")

    assert "DELISTED" not in {r.symbol for r in rows}