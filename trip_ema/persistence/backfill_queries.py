"""Read-side queries for the ETL Status GUI's Backfill panel.

Three lightweight queries, all on the GUI thread (cheap reads,
no need for background workers):

  - active_symbols(exchange)      → list[str] for QCompleter
  - min_bar_date(exchange)        → date | None for From-date auto-fill
  - historical_signals_delete_count_for_range(...)
                                  → int for pre-flight modal
  - market_overview_stale_count_for_range(...)
                                  → int for pre-flight modal

The DELETE-count semantics MIRROR the actual backfill pipeline
(`backfill_pipeline._stage_delete_historical`):

    DELETE FROM historical_signals
     WHERE ticker_id = ANY(:tids)
       AND computed_at >= :from_dt

`from_dt` is `datetime.combine(monday_of(date_from), time(0,0),
tzinfo=UTC)` — the IST Monday of date_from anchored to UTC. The
deletion is ONE-SIDED (no upper bound) — the pipeline rebuilds
forward from from_dt to today's run point.

The market_overview "stale" count uses snapshot_date >= date_from
(also one-sided). These rows are NOT deleted by the current
backfill — they remain in place but reflect the pre-backfill
historical_signals. Stage 10 (future work) will rebuild them; for
now the modal warns the operator that they're stale.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..domain.market_session import (
    market_tz_name,
    monday_of,
    week_anchor,
)


def active_symbols(
    conn: Connection, *, exchange: str,
) -> list[str]:
    """All active, non-delisted ticker symbols for `exchange`,
    alphabetical. Powers the Stocks-input QCompleter and the
    ADD-button validation."""
    rows = conn.execute(
        text("""
            SELECT symbol FROM tickers
             WHERE exchange = :ex
               AND is_active = TRUE
               AND delisted_at IS NULL
             ORDER BY symbol
        """),
        {"ex": exchange},
    ).all()
    return [r.symbol for r in rows]


def min_bar_date(
    conn: Connection, *, exchange: str,
) -> date | None:
    """Earliest daily bar date on `exchange`. Used to auto-fill
    the From-date picker on view init + exchange change.

    Returns None if there are no daily bars for the exchange (fresh
    install — the caller falls back to a "today minus 15 years"
    default per the Step 13c design)."""
    row = conn.execute(
        text("""
            SELECT MIN(b.bar_timestamp AT TIME ZONE :tz)::date AS min_date
              FROM ohlcv_bars b
              JOIN tickers t ON b.ticker_id = t.ticker_id
             WHERE t.exchange = :ex
               AND b.timeframe = '1D'
        """),
        {"ex": exchange, "tz": market_tz_name(exchange)},
    ).first()
    return row.min_date if row and row.min_date else None


def _monday_of(d: date) -> date:
    """Monday of the local week containing `d`.

    L1-ANCHOR-RULE: delegates to the one implementation. It used to say "IST
    Monday" and "mirrors backfill_pipeline._monday_of" — both a hardcoded market
    and a promise no compiler checks.
    """
    return _domain_monday_of(d)


def _from_dt(date_from: date, exchange: str) -> datetime:
    """The instant the delete window opens: local midnight of the local Monday.

    It used to hardcode "IST Monday ... anchored to UTC" — a market and a clock,
    neither of which the caller chose. It also claimed to mirror
    _stage_delete_historical; both now call week_anchor(), so the mirror is a
    shared implementation rather than a promise.
    """
    return week_anchor(date_from, exchange)


def historical_signals_delete_count_for_range(
    conn: Connection,
    *,
    exchange: str,
    date_from: date,
    ticker_ids: list[int] | None = None,
) -> int:
    """Pre-flight count for the destructive modal. Mirrors the
    DELETE clause in `_stage_delete_historical` so the number we
    show is the number that will actually go.

    - ticker_ids=None: scope is ALL active tickers on `exchange`
    - ticker_ids=[...]: scope is the explicit ticker_ids set
    """
    from_dt = _from_dt(date_from, exchange)
    if ticker_ids is None:
        row = conn.execute(
            text("""
                SELECT COUNT(*) AS n
                  FROM historical_signals hs
                  JOIN tickers t ON hs.ticker_id = t.ticker_id
                 WHERE t.exchange = :ex
                   AND t.is_active = TRUE
                   AND t.delisted_at IS NULL
                   AND hs.computed_at >= :from_dt
            """),
            {"ex": exchange, "from_dt": from_dt},
        ).first()
    else:
        if not ticker_ids:
            return 0
        row = conn.execute(
            text("""
                SELECT COUNT(*) AS n
                  FROM historical_signals
                 WHERE ticker_id = ANY(:tids)
                   AND computed_at >= :from_dt
            """),
            {"tids": ticker_ids, "from_dt": from_dt},
        ).first()
    return int(row.n) if row else 0


def market_overview_stale_count_for_range(
    conn: Connection,
    *,
    exchange: str,
    date_from: date,
) -> int:
    """Count of market_overview_daily rows for snapshot_date >=
    date_from on this exchange. These rows are NOT deleted by the
    current backfill pipeline — they remain in place but reflect
    the pre-backfill historical_signals + indicator state.

    Stage 10 (future work) will rebuild them. Until then, the
    destructive modal warns the operator they're going stale.
    """
    row = conn.execute(
        text("""
            SELECT COUNT(*) AS n
              FROM market_overview_daily
             WHERE exchange = :ex
               AND snapshot_date >= :date_from
        """),
        {"ex": exchange, "date_from": date_from},
    ).first()
    return int(row.n) if row else 0


def resolve_ticker_ids_by_symbol(
    conn: Connection,
    *,
    exchange: str,
    symbols: list[str],
) -> tuple[list[int], list[str]]:
    """Resolve a list of operator-typed symbols to ticker_ids.
    Returns (resolved_ticker_ids, unresolved_symbols).

    Mirrors the symbol → ticker_id resolution in
    `cli.cmd_backfill` so the GUI catches typos BEFORE spawning
    the subprocess (the CLI would also reject them, but we want
    a friendly in-GUI error)."""
    if not symbols:
        return [], []
    normalized = [s.strip().upper() for s in symbols if s.strip()]
    if not normalized:
        return [], []

    rows = conn.execute(
        text("""
            SELECT ticker_id, symbol FROM tickers
             WHERE symbol = ANY(:syms)
               AND exchange = :ex
               AND is_active = TRUE
               AND delisted_at IS NULL
        """),
        {"syms": normalized, "ex": exchange},
    ).all()
    found = {r.symbol: int(r.ticker_id) for r in rows}
    unresolved = [s for s in normalized if s not in found]
    resolved = sorted(found.values())
    return resolved, unresolved
