"""Exchange-state admin routes (Phase 14.6b).

  GET /v1/admin/exchange-state              → all rows
  GET /v1/admin/exchange-state/{exchange}   → one row (404 if absent)

Reads from the exchange_state table populated by the runner. The
runner writes to this table inside the same transaction that closes
the corresponding etl_runs row, so a `last_eod_run_at` value here
implies the EoD's etl_runs row reflects that same run.

The GUI's per-exchange status panel polls the list endpoint; the
per-exchange endpoint is for drill-down (e.g. "show me NSE detail").

Both endpoints sit under `require_admin_token` like the rest of
`/v1/admin/*`.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import Engine

from ....persistence.exchange_state_repo import (
    ExchangeState,
    ExchangeStateRepository,
)
from ..dependencies import get_db_engine, require_admin_token
from ..schemas import ExchangeStateListPayload, ExchangeStatePayload

logger = logging.getLogger(__name__)


router = APIRouter(
    prefix="/v1/admin/exchange-state",
    tags=["admin", "exchange-state"],
    dependencies=[Depends(require_admin_token)],
)


@router.get("", response_model=ExchangeStateListPayload)
def admin_exchange_state_list(
    db_engine: Engine = Depends(get_db_engine),
) -> ExchangeStateListPayload:
    """Return every row from exchange_state, ordered by exchange code.

    Payload is small (one row per configured exchange, at most ~10);
    no pagination required. Empty exchanges list = no exchange has
    had any ETL activity yet (or the table was wiped).
    """
    with db_engine.connect() as conn:
        rows = ExchangeStateRepository(conn).list_all()
    return ExchangeStateListPayload(
        exchanges=[_to_payload(r) for r in rows],
        total=len(rows),
        snapshot_taken_at=datetime.now(timezone.utc),
    )


@router.get("/{exchange}", response_model=ExchangeStatePayload)
def admin_exchange_state_one(
    exchange: str,
    db_engine: Engine = Depends(get_db_engine),
) -> ExchangeStatePayload:
    """Return the row for one exchange, or 404 if there isn't one.

    A 404 here means the ETL has never written state for `exchange`
    (not necessarily that the exchange is unknown/unconfigured —
    that distinction is the runner's job, not this read endpoint's).
    """
    with db_engine.connect() as conn:
        row = ExchangeStateRepository(conn).get(exchange)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"No exchange_state row for exchange={exchange!r}. "
                f"Either the exchange has had no ETL activity yet, "
                f"or the code is misspelled. Compare against the "
                f"list endpoint to see what's known."
            ),
        )
    return _to_payload(row)


# ---------- internal ---------- #


def _to_payload(s: ExchangeState) -> ExchangeStatePayload:
    """Field-for-field mapping. Pydantic handles datetime/date
    serialization to ISO-8601 strings on the wire."""
    return ExchangeStatePayload(
        exchange=s.exchange,
        initial_load_started_at=s.initial_load_started_at,
        initial_load_completed_at=s.initial_load_completed_at,
        last_eod_run_at=s.last_eod_run_at,
        last_eod_outcome=s.last_eod_outcome,
        last_eod_data_date=s.last_eod_data_date,
        last_eod_run_id=s.last_eod_run_id,
        last_intraday_run_at=s.last_intraday_run_at,
        last_listings_sync_at=s.last_listings_sync_at,
        last_listings_run_id=s.last_listings_run_id,
        last_tier_recompute_at=s.last_tier_recompute_at,
        updated_at=s.updated_at,
    )
