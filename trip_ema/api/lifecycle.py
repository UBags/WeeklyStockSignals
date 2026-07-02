"""Lifecycle — application startup orchestration.

# Phase 3d — legacy prime path removed

`_load_latest_indicator_states` and `_load_ticker_refs` were the two
SELECTs behind `prime_cache_from_db`. Both were removed alongside
the `indicator_state` table drop in migration 0032.

`prime_cache_from_db` is kept as a no-op stub — its callers
(`app.py::_build_app_state` at boot, `admin.py::admin_prime_all`
via POST) still invoke it, and they still expect a `PrimeCacheResult`
back. The stub returns zero counts and logs a one-line "no-op".

The `SignalCache` this used to populate is still built in `app.py`,
but with `latest_indicator_state` removed from `CachedSnapshot`, the
cache is a shell that only tracks `stale_since` per (ticker_id,
timeframe) — and post-Phase-3b nothing calls `mark_stale` either.
The cache exists so the read endpoints keep working, but it's
structurally empty.

A follow-up phase can remove the SignalCache class entirely once
nothing reads it.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import Engine

from ..cache.signal_cache import SignalCache
from ..domain import Timeframe

logger = logging.getLogger(__name__)


# ============================================================
# Result type — kept for wire-compat with app.py + admin route
# ============================================================


@dataclass(frozen=True, slots=True)
class PrimeCacheResult:
    """Summary of one `prime_cache_from_db` invocation.

    Post-Phase-3d all fields are always 0 — the legacy prime path
    no longer exists. Kept as a dataclass with defaults so callers
    that unpack fields (LifecycleResultPayload builder in admin.py,
    the log line in app.py) don't need conditionals.
    """
    tickers_seen: int = 0
    snapshots_populated: int = 0
    indicator_only: int = 0
    signal_event_only: int = 0
    both: int = 0


# ============================================================
# Public entry point — no-op stub
# ============================================================


def prime_cache_from_db(
    *,
    db_engine: Engine,
    cache: SignalCache,
    timeframe: Timeframe = Timeframe.WEEKLY,
) -> PrimeCacheResult:
    """No-op stub. The legacy `indicator_state` table was dropped in
    migration 0032; there is nothing left to prime from. Returns an
    all-zero PrimeCacheResult so callers keep working.

    Args are kept for signature-compat with the pre-3d caller sites:
      - app.py's _build_app_state calls this at startup.
      - admin.py's POST /v1/admin/prime-all route calls this too.

    Both keep the log line "Cache primed: 0 snapshots" without
    changes — the value 0 is now the true state.
    """
    logger.info(
        "prime_cache_from_db: no-op post-Phase-3d "
        "(legacy indicator_state read removed; cache stays empty)"
    )
    return PrimeCacheResult()
