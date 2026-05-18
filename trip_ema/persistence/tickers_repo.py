"""Tickers repository.

All access to the `tickers` table goes through this class. Returns domain
objects (`TickerRef`), never ORM rows or tuples.

Repos take a Connection in their constructor — they don't own the engine.
The caller (typically inside a `with transaction(engine) as conn:` block)
controls the transaction boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from sqlalchemy import insert, select
from sqlalchemy.engine import Connection

from ..domain import TickerRef
from .tables import tickers


@dataclass(frozen=True, slots=True)
class TickerRecord:
    """A ticker as it lives in the DB — includes the surrogate ID and
    activity flag, which are not part of the domain TickerRef but are
    needed inside the persistence layer for joins and filtering.

    Phase 8 B4 added:
      - `tz`: the exchange's IANA timezone (e.g. 'Asia/Kolkata').
        Used for week-anchor computation. Defaults to 'UTC' for
        pre-Phase-8 rows.
      - `source_timeframe`: the resolution at which the ETL writes bars
        for this ticker. The engine reads bars at this resolution and
        aggregates to its target timeframe internally.
    """

    ticker_id: int
    ref: TickerRef
    is_active: bool
    full_name: str | None
    tz: str = "UTC"
    source_timeframe: str = "1W"


def _row_to_record(row) -> TickerRecord:
    return TickerRecord(
        ticker_id=row.ticker_id,
        ref=TickerRef(
            exchange=row.exchange,
            symbol=row.symbol,
            instrument=row.instrument,
        ),
        is_active=row.is_active,
        full_name=row.full_name,
        tz=getattr(row, "tz", "UTC"),
        source_timeframe=getattr(row, "source_timeframe", "1W"),
    )


class TickersRepository:
    def __init__(self, conn: Connection):
        self._conn = conn

    # ---------- Reads ---------- #

    def find_active(self) -> list[TickerRecord]:
        """Return every active ticker. Used by `prime_all_tickers()` at startup."""
        stmt = select(tickers).where(tickers.c.is_active.is_(True))
        return [_row_to_record(r) for r in self._conn.execute(stmt)]

    def get_by_ref(self, ref: TickerRef) -> TickerRecord | None:
        """Look up a ticker by its domain reference."""
        stmt = select(tickers).where(
            tickers.c.exchange == ref.exchange,
            tickers.c.symbol == ref.symbol,
            tickers.c.instrument == ref.instrument,
        )
        row = self._conn.execute(stmt).first()
        return _row_to_record(row) if row else None

    def get_by_id(self, ticker_id: int) -> TickerRecord | None:
        stmt = select(tickers).where(tickers.c.ticker_id == ticker_id)
        row = self._conn.execute(stmt).first()
        return _row_to_record(row) if row else None

    def get_ids_for_refs(self, refs: Iterable[TickerRef]) -> dict[TickerRef, int]:
        """Batch lookup. Returns only refs that exist in the DB; callers can
        diff against the input to find missing ones."""
        refs_list = list(refs)
        if not refs_list:
            return {}
        # Build a list of tuples for IN (...) — Postgres tuple-IN is the
        # cleanest way to do a batch composite-key lookup.
        out: dict[TickerRef, int] = {}
        for ref in refs_list:
            rec = self.get_by_ref(ref)
            if rec is not None:
                out[ref] = rec.ticker_id
        return out

    # ---------- Writes ---------- #

    def insert(
        self,
        ref: TickerRef,
        *,
        full_name: str | None = None,
        is_active: bool = True,
        created_at,
        tz: str = "UTC",
        source_timeframe: str = "1W",
    ) -> int:
        """Insert a new ticker. Returns the assigned ticker_id.

        Caller passes `created_at` explicitly (rather than letting the DB
        default kick in) to keep timestamps deterministic in tests.

        Phase 8 B4: `tz` and `source_timeframe` are required schema
        columns but default to 'UTC' / '1W' to preserve backward
        compat with existing test fixtures.
        """
        stmt = (
            insert(tickers)
            .values(
                exchange=ref.exchange,
                symbol=ref.symbol,
                instrument=ref.instrument,
                full_name=full_name,
                is_active=is_active,
                created_at=created_at,
                tz=tz,
                source_timeframe=source_timeframe,
            )
            .returning(tickers.c.ticker_id)
        )
        return self._conn.execute(stmt).scalar_one()
