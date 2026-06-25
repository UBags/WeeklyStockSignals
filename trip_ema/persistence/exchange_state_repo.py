"""Repository for the exchange_state table (Phase 14.6b).

One row per exchange. The runner writes after each ETL pass; the
admin endpoint reads. Both the snapshot dataclass and the writes
use COALESCE semantics: writing to one field never clobbers others
that weren't passed in.

Initial-load semantics:
  - `record_backfill_started` sets `initial_load_started_at` only
    on the very first backfill against this exchange (i.e. when
    no row exists yet). Subsequent backfills do NOT overwrite it
    — it's a lifecycle marker.
  - `record_backfill_completed` sets `initial_load_completed_at`
    only when its current value is NULL. Same lifecycle reasoning.

The connection is supplied by the caller; the repo doesn't manage
transactions. That matches `EtlStatusRepository`'s contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection


@dataclass(frozen=True, slots=True)
class ExchangeState:
    """A single row from exchange_state."""
    exchange: str
    initial_load_started_at: Optional[datetime]
    initial_load_completed_at: Optional[datetime]
    last_eod_run_at: Optional[datetime]
    last_eod_outcome: Optional[str]
    last_eod_data_date: Optional[date]
    last_eod_run_id: Optional[int]
    last_intraday_run_at: Optional[datetime]
    last_listings_sync_at: Optional[datetime]
    last_listings_run_id: Optional[int]
    last_tier_recompute_at: Optional[datetime]
    updated_at: datetime


class ExchangeStateRepository:
    """Reads + writes against exchange_state. Single-row-per-exchange.

    All writes are upserts: if the row for `exchange` doesn't exist,
    one is created with NULL in all unspecified columns. If it does,
    only the columns the caller specifies are updated.
    """

    def __init__(self, conn: Connection):
        self._conn = conn

    # ---------- reads ---------- #

    def get(self, exchange: str) -> Optional[ExchangeState]:
        row = self._conn.execute(
            text("""
                SELECT exchange,
                       initial_load_started_at,
                       initial_load_completed_at,
                       last_eod_run_at,
                       last_eod_outcome,
                       last_eod_data_date,
                       last_eod_run_id,
                       last_intraday_run_at,
                       last_listings_sync_at,
                       last_listings_run_id,
                       last_tier_recompute_at,
                       updated_at
                FROM exchange_state
                WHERE exchange = :ex
            """),
            {"ex": exchange},
        ).first()
        if row is None:
            return None
        return _row_to_state(row)

    def list_all(self) -> list[ExchangeState]:
        rows = self._conn.execute(
            text("""
                SELECT exchange,
                       initial_load_started_at,
                       initial_load_completed_at,
                       last_eod_run_at,
                       last_eod_outcome,
                       last_eod_data_date,
                       last_eod_run_id,
                       last_intraday_run_at,
                       last_listings_sync_at,
                       last_listings_run_id,
                       last_tier_recompute_at,
                       updated_at
                FROM exchange_state
                ORDER BY exchange
            """)
        ).fetchall()
        return [_row_to_state(r) for r in rows]

    # ---------- writes ---------- #

    def record_eod(
        self,
        exchange: str,
        *,
        run_at: datetime,
        outcome: str,
        data_date: Optional[date],
        run_id: Optional[int],
    ) -> None:
        """Called after an EoD pass closes.

        `outcome` is the stringified RunOutcome. `data_date` is the
        latest bar date covered by the pass (typically `date.today()`);
        if NULL is appropriate (e.g. no tickers fetched), pass None.
        """
        self._conn.execute(
            text("""
                INSERT INTO exchange_state (
                    exchange,
                    last_eod_run_at,
                    last_eod_outcome,
                    last_eod_data_date,
                    last_eod_run_id,
                    updated_at
                ) VALUES (
                    :ex, :run_at, :outcome, :data_date, :run_id, NOW()
                )
                ON CONFLICT (exchange) DO UPDATE SET
                    last_eod_run_at    = EXCLUDED.last_eod_run_at,
                    last_eod_outcome   = EXCLUDED.last_eod_outcome,
                    last_eod_data_date = EXCLUDED.last_eod_data_date,
                    last_eod_run_id    = EXCLUDED.last_eod_run_id,
                    updated_at         = NOW()
            """),
            {
                "ex": exchange,
                "run_at": run_at,
                "outcome": outcome,
                "data_date": data_date,
                "run_id": run_id,
            },
        )

    def record_intraday(
        self,
        exchange: str,
        *,
        run_at: datetime,
    ) -> None:
        """Called after an intraday pass closes."""
        self._conn.execute(
            text("""
                INSERT INTO exchange_state (
                    exchange, last_intraday_run_at, updated_at
                ) VALUES (
                    :ex, :run_at, NOW()
                )
                ON CONFLICT (exchange) DO UPDATE SET
                    last_intraday_run_at = EXCLUDED.last_intraday_run_at,
                    updated_at           = NOW()
            """),
            {"ex": exchange, "run_at": run_at},
        )

    def record_listings_sync(
        self,
        exchange: str,
        *,
        run_at: datetime,
        run_id: Optional[int],
    ) -> None:
        """Called after refresh_listings closes successfully."""
        self._conn.execute(
            text("""
                INSERT INTO exchange_state (
                    exchange,
                    last_listings_sync_at,
                    last_listings_run_id,
                    updated_at
                ) VALUES (
                    :ex, :run_at, :run_id, NOW()
                )
                ON CONFLICT (exchange) DO UPDATE SET
                    last_listings_sync_at = EXCLUDED.last_listings_sync_at,
                    last_listings_run_id  = EXCLUDED.last_listings_run_id,
                    updated_at            = NOW()
            """),
            {"ex": exchange, "run_at": run_at, "run_id": run_id},
        )

    def record_tier_recompute(
        self,
        exchange: str,
        *,
        run_at: datetime,
    ) -> None:
        """Called after recompute_tiers closes."""
        self._conn.execute(
            text("""
                INSERT INTO exchange_state (
                    exchange, last_tier_recompute_at, updated_at
                ) VALUES (
                    :ex, :run_at, NOW()
                )
                ON CONFLICT (exchange) DO UPDATE SET
                    last_tier_recompute_at = EXCLUDED.last_tier_recompute_at,
                    updated_at             = NOW()
            """),
            {"ex": exchange, "run_at": run_at},
        )

    def record_backfill_started(
        self,
        exchange: str,
        *,
        started_at: datetime,
    ) -> None:
        """Called at the START of a backfill, BEFORE work begins.

        Sets initial_load_started_at ONLY if it is currently NULL.
        Subsequent backfills don't overwrite it — initial_load_*
        are one-shot lifecycle markers.

        If no row exists for this exchange, one is created with
        initial_load_started_at set.
        """
        self._conn.execute(
            text("""
                INSERT INTO exchange_state (
                    exchange, initial_load_started_at, updated_at
                ) VALUES (
                    :ex, :started_at, NOW()
                )
                ON CONFLICT (exchange) DO UPDATE SET
                    initial_load_started_at = COALESCE(
                        exchange_state.initial_load_started_at,
                        EXCLUDED.initial_load_started_at
                    ),
                    updated_at = NOW()
            """),
            {"ex": exchange, "started_at": started_at},
        )

    def record_backfill_completed(
        self,
        exchange: str,
        *,
        completed_at: datetime,
        outcome: str,
    ) -> None:
        """Called at the END of a backfill.

        - Sets initial_load_completed_at ONLY if currently NULL AND
          the backfill outcome was 'success'. A failed/partial first
          backfill leaves initial_load_completed_at NULL so the
          operator knows the load is still incomplete.
        - Always touches updated_at so the GUI sees the activity.

        Does NOT touch initial_load_started_at — that's
        `record_backfill_started`'s job.
        """
        self._conn.execute(
            text("""
                INSERT INTO exchange_state (
                    exchange,
                    initial_load_completed_at,
                    updated_at
                ) VALUES (
                    :ex,
                    CASE WHEN :outcome = 'success' THEN :completed_at ELSE NULL END,
                    NOW()
                )
                ON CONFLICT (exchange) DO UPDATE SET
                    initial_load_completed_at = CASE
                        WHEN exchange_state.initial_load_completed_at IS NOT NULL
                            THEN exchange_state.initial_load_completed_at
                        WHEN :outcome = 'success'
                            THEN :completed_at
                        ELSE NULL
                    END,
                    updated_at = NOW()
            """),
            {
                "ex": exchange,
                "completed_at": completed_at,
                "outcome": outcome,
            },
        )


# ---------- helpers ---------- #


def _row_to_state(row: Any) -> ExchangeState:
    return ExchangeState(
        exchange=row.exchange,
        initial_load_started_at=row.initial_load_started_at,
        initial_load_completed_at=row.initial_load_completed_at,
        last_eod_run_at=row.last_eod_run_at,
        last_eod_outcome=row.last_eod_outcome,
        last_eod_data_date=row.last_eod_data_date,
        last_eod_run_id=row.last_eod_run_id,
        last_intraday_run_at=row.last_intraday_run_at,
        last_listings_sync_at=row.last_listings_sync_at,
        last_listings_run_id=row.last_listings_run_id,
        last_tier_recompute_at=row.last_tier_recompute_at,
        updated_at=row.updated_at,
    )
