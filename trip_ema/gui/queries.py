"""Read queries for the GUI views.

Pure SQL functions taking a Connection, returning typed records.
Kept separate from view code so the queries can be tested without
Qt.

These run on background workers (NEVER on the GUI thread) because
they may scan ~2300-row tables.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection


@dataclass(frozen=True, slots=True)
class TickerRow:
    """One row in the tickers tab's main table."""
    ticker_id: int
    exchange: str
    symbol: str
    instrument_type: str
    full_name: Optional[str]
    isin: Optional[str]
    tier: int
    is_active: bool
    delisted_at: Optional[datetime]
    last_successful_bar_date: Optional[date]
    consecutive_failures: int
    parked: bool


@dataclass(frozen=True, slots=True)
class SignalEventRow:
    """For the per-ticker detail panel."""
    event_id: int
    bar_timestamp: datetime
    timeframe: str
    signal: str
    price_at_signal: float


@dataclass(frozen=True, slots=True)
class SymbolHistoryRow:
    """For the rename-history panel."""
    history_id: int
    old_symbol: str
    new_symbol: str
    changed_at: datetime
    detected_via: str


def list_tickers(
    conn: Connection,
    *,
    exchange: str = "NSE",
    include_delisted: bool = False,
    tier_filter: Optional[int] = None,
    symbol_substr: Optional[str] = None,
    limit: int = 5000,
) -> list[TickerRow]:
    """The main tickers tab query. Left-joins etl_ticker_status so we
    get failure-count and parked info inline (NULL when the ticker
    has never been touched by the ETL — equivalent to 0/False)."""
    sql = """
        SELECT t.ticker_id, t.exchange, t.symbol, t.instrument_type,
               t.full_name, t.isin, t.tier, t.is_active, t.delisted_at,
               s.last_successful_bar_date,
               COALESCE(s.consecutive_failures, 0) AS consecutive_failures,
               COALESCE(s.parked, FALSE) AS parked
        FROM tickers t
        LEFT JOIN etl_ticker_status s ON t.ticker_id = s.ticker_id
        WHERE t.exchange = :ex
    """
    params: dict = {"ex": exchange}
    if not include_delisted:
        sql += " AND t.is_active = TRUE AND t.delisted_at IS NULL"
    if tier_filter is not None:
        sql += " AND t.tier = :tier"
        params["tier"] = tier_filter
    if symbol_substr:
        sql += " AND UPPER(t.symbol) LIKE :substr"
        params["substr"] = f"%{symbol_substr.upper()}%"
    sql += " ORDER BY t.tier ASC, t.symbol ASC LIMIT :lim"
    params["lim"] = limit

    rows = conn.execute(text(sql), params)
    return [
        TickerRow(
            ticker_id=r.ticker_id, exchange=r.exchange, symbol=r.symbol,
            instrument_type=r.instrument_type, full_name=r.full_name,
            isin=r.isin, tier=r.tier, is_active=r.is_active,
            delisted_at=r.delisted_at,
            last_successful_bar_date=r.last_successful_bar_date,
            consecutive_failures=r.consecutive_failures,
            parked=r.parked,
        )
        for r in rows
    ]


def recent_signal_events(
    conn: Connection,
    ticker_id: int,
    *,
    limit: int = 10,
) -> list[SignalEventRow]:
    """The detail panel's signal-history list."""
    rows = conn.execute(text("""
        SELECT event_id, bar_timestamp, timeframe, signal, price_at_signal
        FROM signal_events
        WHERE ticker_id = :tid
        ORDER BY bar_timestamp DESC
        LIMIT :lim
    """), {"tid": ticker_id, "lim": limit})
    return [
        SignalEventRow(
            event_id=r.event_id, bar_timestamp=r.bar_timestamp,
            timeframe=r.timeframe, signal=r.signal,
            price_at_signal=float(r.price_at_signal),
        )
        for r in rows
    ]


def ticker_symbol_history(
    conn: Connection, ticker_id: int,
) -> list[SymbolHistoryRow]:
    """Renames for one ticker, most-recent first."""
    rows = conn.execute(text("""
        SELECT history_id, old_symbol, new_symbol, changed_at, detected_via
        FROM symbol_history
        WHERE ticker_id = :tid
        ORDER BY changed_at DESC
    """), {"tid": ticker_id})
    return [
        SymbolHistoryRow(
            history_id=r.history_id, old_symbol=r.old_symbol,
            new_symbol=r.new_symbol, changed_at=r.changed_at,
            detected_via=r.detected_via,
        )
        for r in rows
    ]


def ticker_universe_summary(
    conn: Connection, *, exchange: str = "NSE",
) -> dict:
    """For the bottom-bar counts: active/delisted/parked/by-tier."""
    result = conn.execute(text("""
        SELECT
          COUNT(*) FILTER (WHERE t.is_active = TRUE AND t.delisted_at IS NULL)
            AS active,
          COUNT(*) FILTER (WHERE t.delisted_at IS NOT NULL) AS delisted,
          COUNT(*) FILTER (WHERE t.tier = 1 AND t.is_active = TRUE
                          AND t.delisted_at IS NULL) AS tier_1,
          COUNT(*) FILTER (WHERE t.tier = 2 AND t.is_active = TRUE
                          AND t.delisted_at IS NULL) AS tier_2,
          COUNT(*) FILTER (WHERE t.tier = 3 AND t.is_active = TRUE
                          AND t.delisted_at IS NULL) AS tier_3,
          COUNT(*) FILTER (WHERE s.parked = TRUE) AS parked
        FROM tickers t
        LEFT JOIN etl_ticker_status s ON t.ticker_id = s.ticker_id
        WHERE t.exchange = :ex
    """), {"ex": exchange}).first()
    return {
        "active": result.active,
        "delisted": result.delisted,
        "tier_1": result.tier_1,
        "tier_2": result.tier_2,
        "tier_3": result.tier_3,
        "parked": result.parked,
    }
