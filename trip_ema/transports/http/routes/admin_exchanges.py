"""Configured-exchanges admin route (Phase 14.6c).

  GET /v1/admin/exchanges → the set of exchanges available for ETL
                            actions, sourced from config.market_hours

Returns *what can be operated on*, not *what has activity*. The
sibling /v1/admin/exchange-state endpoint answers "what's the
activity?"; this one answers "what's available to act on?".

A configured exchange may have zero exchange_state activity (e.g.
a freshly-added BSE entry that has never had a backfill). The GUI's
exchange picker uses this endpoint to populate its dropdown so the
operator can fire the first backfill against such an exchange.

Sits under `require_admin_token` like the rest of `/v1/admin/*`.
Cheap — pure config read, no DB.
"""

from __future__ import annotations

import logging
from datetime import datetime, UTC

from fastapi import APIRouter, Depends

# ISSUE-A2: MarketHoursService + ExchangeMarketStatusPayload imports.
from fastapi import HTTPException, status as _http_status

from ....config import Config
from ....etl.exchanges import configured_exchanges
from ....scheduler.market_hours import MarketHoursService
from ..dependencies import get_config, require_admin_token
from ..schemas import ExchangeListPayload, ExchangeMarketStatusPayload

logger = logging.getLogger(__name__)


router = APIRouter(
    prefix="/v1/admin/exchanges",
    tags=["admin", "exchanges"],
    dependencies=[Depends(require_admin_token)],
)


@router.get("", response_model=ExchangeListPayload)
def admin_exchanges_list(
    config: Config = Depends(get_config),
) -> ExchangeListPayload:
    """Return the sorted list of exchanges from config.market_hours.

    Sorted alphabetically so the GUI's dropdown order is deterministic
    regardless of how operators arranged config.yaml. An empty list
    means the operator hasn't configured any market_hours; the GUI
    should treat that as a misconfiguration warning.
    """
    exchanges = sorted(configured_exchanges(config))
    return ExchangeListPayload(
        exchanges=exchanges,
        total=len(exchanges),
        snapshot_taken_at=datetime.now(UTC),
    )


# ISSUE-A2: market-status per exchange for the GUI modal.
@router.get(
    "/{exchange}/market-status",
    response_model=ExchangeMarketStatusPayload,
)
def admin_exchange_market_status(
    exchange: str,
    config: Config = Depends(get_config),
) -> ExchangeMarketStatusPayload:
    """Return whether `exchange` is currently open.

    Consulted by the GUI's Confirm Intraday Cycle modal before it
    opens, so the modal can display accurate state (previously
    hardcoded to False, making the popup always say
    'market currently closed' even during trading hours).

    Returns 404 if the exchange is not configured in market_hours,
    matching the semantics of MarketHoursService.is_open which
    raises KeyError for unknown exchanges.
    """
    now = datetime.now(UTC)
    svc = MarketHoursService(config.market_hours)
    try:
        is_open = svc.is_open(exchange, now)
    except KeyError:
        raise HTTPException(
            status_code=_http_status.HTTP_404_NOT_FOUND,
            detail=(
                f"Exchange {exchange!r} not configured in market_hours"
            ),
        )
    return ExchangeMarketStatusPayload(
        exchange=exchange, is_open=is_open, checked_at=now,
    )
