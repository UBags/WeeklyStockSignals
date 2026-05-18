"""Listings sync.

Takes the parsed NseListings (from the parser) and applies them to
the `tickers` table:

  - New listing (symbol not present, ISIN not present): INSERT with
    tier=3, is_active=TRUE.
  - Symbol present, ISIN matches: UPDATE enrichment fields
    (full_name, listing_date, face_value, lot_size). Don't touch
    tier or is_active.
  - ISIN present under a different symbol: SYMBOL RENAME. Update
    tickers.symbol to the new one, write a symbol_history row.
  - Symbol present in DB but missing from incoming list: DELISTING
    candidate. Mark delisted_at and is_active=FALSE, but never
    DELETE — we keep history.

The logic is deliberately split from the I/O (downloader) and
parsing (parser) so it can be tested with synthetic inputs.

Idempotent: re-running the same sync produces no changes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .symbol_history_repo import SymbolHistoryRepository
from .types import InstrumentType, NseListing

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SyncResult:
    new_listings: int
    renamed_symbols: int
    enriched_existing: int
    delisted: int
    unchanged: int

    @property
    def total_changes(self) -> int:
        return (
            self.new_listings + self.renamed_symbols
            + self.enriched_existing + self.delisted
        )


class ListingsSync:
    """Apply NseListings to tickers table.

    Constructed with a SQLAlchemy Connection — the caller manages the
    transaction. Typically wrapped in a single transaction by the
    `refresh-listings` CLI subcommand so a partial failure rolls back
    cleanly.
    """

    def __init__(
        self,
        conn: Connection,
        exchange: str = "NSE",
        *,
        history_repo: Optional[SymbolHistoryRepository] = None,
    ):
        self._conn = conn
        self._exchange = exchange
        self._history = history_repo or SymbolHistoryRepository(conn)

    def sync(
        self,
        incoming: Iterable[NseListing],
        *,
        detect_delistings: bool = True,
    ) -> SyncResult:
        """Apply the incoming listings. Returns counts of each change
        type.

        If detect_delistings is True (default), any active ticker for
        this exchange that's absent from `incoming` is marked
        delisted. Set False when syncing only ETFs (don't accidentally
        mark all equities as delisted because they weren't in the ETF
        list)."""
        incoming_list = list(incoming)
        incoming_symbols = {l.symbol for l in incoming_list}
        # Build (isin → existing ticker) and (symbol → existing ticker)
        # maps over the active universe for this exchange.
        existing_by_isin: dict[str, dict] = {}
        existing_by_symbol: dict[str, dict] = {}
        for row in self._conn.execute(
            text("""
                SELECT ticker_id, symbol, isin, instrument_type, full_name,
                       listing_date, face_value, lot_size, is_active, delisted_at
                FROM tickers
                WHERE exchange = :ex AND is_active = TRUE
            """),
            {"ex": self._exchange},
        ):
            d = {
                "ticker_id": row.ticker_id, "symbol": row.symbol,
                "isin": row.isin, "instrument_type": row.instrument_type,
                "full_name": row.full_name, "listing_date": row.listing_date,
                "face_value": row.face_value, "lot_size": row.lot_size,
                "is_active": row.is_active, "delisted_at": row.delisted_at,
            }
            if row.isin:
                existing_by_isin[row.isin] = d
            existing_by_symbol[row.symbol] = d

        counts = {"new": 0, "renamed": 0, "enriched": 0, "delisted": 0, "unchanged": 0}

        # Pass 1: apply each incoming listing
        for listing in incoming_list:
            handled = self._apply_one(
                listing, existing_by_isin, existing_by_symbol, counts,
            )
            # Track processed symbols so we know what NOT to delist.
            # We need to consider both the listing's symbol AND any
            # rename target (existing.symbol) we just renamed away from.

        # Pass 2: delisting detection. A ticker is delisted iff its
        # symbol AND its ISIN are both absent from the incoming set.
        # Symbol-rename targets stay alive (their isin is in incoming
        # even though their old symbol is not).
        if detect_delistings:
            incoming_isins = {l.isin for l in incoming_list if l.isin}
            now = datetime.now(timezone.utc)
            for symbol, existing in existing_by_symbol.items():
                if symbol in incoming_symbols:
                    continue
                if existing["isin"] and existing["isin"] in incoming_isins:
                    # This symbol's ISIN is in the new list under a
                    # different symbol — already handled by the rename path.
                    continue
                # Genuinely missing — delist.
                self._conn.execute(
                    text("""
                        UPDATE tickers SET
                            is_active = FALSE,
                            delisted_at = :now
                        WHERE ticker_id = :tid
                    """),
                    {"now": now, "tid": existing["ticker_id"]},
                )
                counts["delisted"] += 1

        return SyncResult(
            new_listings=counts["new"],
            renamed_symbols=counts["renamed"],
            enriched_existing=counts["enriched"],
            delisted=counts["delisted"],
            unchanged=counts["unchanged"],
        )

    # ---------- internal ---------- #

    def _apply_one(
        self,
        listing: NseListing,
        existing_by_isin: dict,
        existing_by_symbol: dict,
        counts: dict,
    ) -> None:
        # Case 1: ISIN match → either unchanged, enrich, or rename.
        if listing.isin and listing.isin in existing_by_isin:
            existing = existing_by_isin[listing.isin]
            if existing["symbol"] == listing.symbol:
                # Same symbol, same ISIN — might need enrichment.
                if self._needs_enrichment(existing, listing):
                    self._update_enrichment(existing["ticker_id"], listing)
                    counts["enriched"] += 1
                else:
                    counts["unchanged"] += 1
            else:
                # Symbol rename detected
                old_symbol = existing["symbol"]
                self._history.record_rename(
                    existing["ticker_id"], old_symbol, listing.symbol,
                )
                self._update_enrichment(
                    existing["ticker_id"], listing,
                    update_symbol=True,
                )
                counts["renamed"] += 1
                logger.info(
                    "Symbol renamed via ISIN continuity",
                    extra={
                        "ticker_id": existing["ticker_id"],
                        "old_symbol": old_symbol,
                        "new_symbol": listing.symbol,
                        "isin": listing.isin,
                    },
                )
            return

        # Case 2: Symbol match but no ISIN match (or new listing has no ISIN).
        if listing.symbol in existing_by_symbol:
            existing = existing_by_symbol[listing.symbol]
            # If incoming has an ISIN and existing has none, that's
            # the first time we've learned the ISIN — fill it in
            # (treat as enrichment).
            if listing.isin and existing.get("isin") is None:
                if self._needs_enrichment(existing, listing):
                    self._update_enrichment(existing["ticker_id"], listing)
                    counts["enriched"] += 1
                else:
                    counts["unchanged"] += 1
            elif self._needs_enrichment(existing, listing):
                self._update_enrichment(existing["ticker_id"], listing)
                counts["enriched"] += 1
            else:
                counts["unchanged"] += 1
            return

        # Case 3: Brand-new listing.
        self._insert_new(listing)
        counts["new"] += 1

    def _needs_enrichment(self, existing: dict, listing: NseListing) -> bool:
        """Is the incoming listing strictly more-complete than what's
        in DB? We update if any field differs OR is filled in for the
        first time. Don't bother updating if all fields are the same."""
        if listing.full_name and listing.full_name != existing.get("full_name"):
            return True
        if listing.listing_date and listing.listing_date != existing.get("listing_date"):
            return True
        if listing.face_value is not None and listing.face_value != (
            float(existing.get("face_value")) if existing.get("face_value") is not None else None
        ):
            return True
        if listing.lot_size is not None and listing.lot_size != existing.get("lot_size"):
            return True
        if listing.isin and listing.isin != existing.get("isin"):
            return True
        # instrument_type changes — rare, but EQ→ETF would be a
        # mistake, so we log and overwrite.
        if listing.instrument_type.value != existing.get("instrument_type"):
            return True
        return False

    def _update_enrichment(
        self,
        ticker_id: int,
        listing: NseListing,
        *,
        update_symbol: bool = False,
    ) -> None:
        params = {
            "tid": ticker_id,
            "full_name": listing.full_name,
            "listing_date": listing.listing_date,
            "face_value": listing.face_value,
            "lot_size": listing.lot_size,
            "isin": listing.isin,
            "instrument_type": listing.instrument_type.value,
            "symbol": listing.symbol if update_symbol else None,
        }
        if update_symbol:
            self._conn.execute(
                text("""
                    UPDATE tickers SET
                        symbol           = :symbol,
                        full_name        = COALESCE(:full_name, full_name),
                        listing_date     = COALESCE(:listing_date, listing_date),
                        face_value       = COALESCE(:face_value, face_value),
                        lot_size         = COALESCE(:lot_size, lot_size),
                        isin             = COALESCE(:isin, isin),
                        instrument_type  = :instrument_type
                    WHERE ticker_id = :tid
                """),
                params,
            )
        else:
            self._conn.execute(
                text("""
                    UPDATE tickers SET
                        full_name        = COALESCE(:full_name, full_name),
                        listing_date     = COALESCE(:listing_date, listing_date),
                        face_value       = COALESCE(:face_value, face_value),
                        lot_size         = COALESCE(:lot_size, lot_size),
                        isin             = COALESCE(:isin, isin),
                        instrument_type  = :instrument_type
                    WHERE ticker_id = :tid
                """),
                params,
            )

    def _insert_new(self, listing: NseListing) -> int:
        result = self._conn.execute(
            text("""
                INSERT INTO tickers
                    (exchange, symbol, instrument, instrument_type, isin,
                     full_name, listing_date, face_value, lot_size,
                     is_active, tier)
                VALUES
                    (:exchange, :symbol, :instrument, :it, :isin,
                     :full_name, :listing_date, :face_value, :lot_size,
                     TRUE, 3)
                RETURNING ticker_id
            """),
            {
                "exchange": self._exchange,
                "symbol": listing.symbol,
                # Legacy `instrument` column (EQ/INDEX/...) — keep it
                # in step with instrument_type for back-compat.
                "instrument": listing.instrument_type.value,
                "it": listing.instrument_type.value,
                "isin": listing.isin,
                "full_name": listing.full_name,
                "listing_date": listing.listing_date,
                "face_value": listing.face_value,
                "lot_size": listing.lot_size,
            },
        )
        return result.scalar_one()
