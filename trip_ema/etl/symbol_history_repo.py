"""Repository for the symbol_history table.

NSE renames symbols (mergers, rebranding); the ISIN is stable. The
listings sync detects this when an incoming listing's ISIN matches
an existing ticker whose symbol differs, and writes a row here. The
ticker row itself is updated in place to the new symbol so the rest
of the app keeps working with the canonical name.

Old-symbol lookups via this table let historical signal events tied
to the old name remain queryable.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SymbolHistoryRecord:
    history_id: int
    ticker_id: int
    old_symbol: str
    new_symbol: str
    changed_at: datetime
    detected_via: str


class SymbolHistoryRepository:

    def __init__(self, conn: Connection):
        self._conn = conn

    def record_rename(
        self,
        ticker_id: int,
        old_symbol: str,
        new_symbol: str,
        *,
        detected_via: str = "isin_continuity",
    ) -> int:
        """Insert a rename event. Returns history_id. The caller is
        expected to update tickers.symbol in the same transaction."""
        result = self._conn.execute(
            text("""
                INSERT INTO symbol_history
                    (ticker_id, old_symbol, new_symbol, detected_via)
                VALUES (:tid, :old, :new, :via)
                RETURNING history_id
            """),
            {
                "tid": ticker_id, "old": old_symbol,
                "new": new_symbol, "via": detected_via,
            },
        )
        return result.scalar_one()

    def list_for_ticker(self, ticker_id: int) -> list[SymbolHistoryRecord]:
        result = self._conn.execute(
            text("""
                SELECT history_id, ticker_id, old_symbol, new_symbol,
                       changed_at, detected_via
                FROM symbol_history
                WHERE ticker_id = :tid
                ORDER BY changed_at DESC
            """),
            {"tid": ticker_id},
        )
        return [
            SymbolHistoryRecord(
                history_id=r.history_id, ticker_id=r.ticker_id,
                old_symbol=r.old_symbol, new_symbol=r.new_symbol,
                changed_at=r.changed_at, detected_via=r.detected_via,
            )
            for r in result
        ]

    def find_ticker_for_old_symbol(self, old_symbol: str) -> Optional[int]:
        """If a query comes in for a symbol that's been renamed,
        return the canonical ticker_id. Returns the most recent
        match if the same old_symbol has been used by multiple
        tickers over time (unlikely but possible)."""
        result = self._conn.execute(
            text("""
                SELECT ticker_id FROM symbol_history
                WHERE old_symbol = :old
                ORDER BY changed_at DESC
                LIMIT 1
            """),
            {"old": old_symbol.upper()},
        ).scalar()
        return result
