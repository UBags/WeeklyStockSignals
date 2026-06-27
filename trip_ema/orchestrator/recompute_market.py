"""V1 cycle compute primitive: `recompute_market`.

The function the V1 cycle's compute stage invokes on every tick.
Composes Steps 2-5 + the IIV populator into the sequence the
original vision (a-c) prescribed.

Pipeline (live mode):

    1. Stock state — incremental for active tickers on `exchange`
    2. Stock enrichment — incremental for the same tickers
    3. Rotate current_signals → historical_signals (atomic)
    4. Derive signals from latest state-row pairs → current_signals
    5. Cache rebuild (if a cache is provided)
    6. IIV refresh — recent date window across active industries
    7. Industry state — incremental across active industries
    8. Industry enrichment — incremental across active industries

Each stage is wrapped in a try/except. A stage-level error halts
the remaining stages and is recorded in `stage_errors`. Per-ticker /
per-industry errors inside a stage are isolated by the existing
populators (Step 2's incremental mode preserves the populator's
per-ticker error isolation).

Transactions:
  - Each populator opens its own transaction.
  - Rotation (Step 4) opens its own transaction.
  - Derive (Step 3) opens its own transaction.
  - Cache rebuild is in-memory (no DB).

Mode:
  - 'live' (this step): full pipeline including rotation, derive
    to current_signals, and cache rebuild.
  - 'backfill' (Step 11): not yet implemented here. Will skip
    rotation + cache rebuild and write signals directly to
    historical_signals.

The function returns a `RecomputeMarketResult` with aggregated
counters from each stage. Per-ticker detail is logged by the
underlying populators but NOT surfaced in the result (would bloat
the report; the test path verifies via DB state).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, UTC
from typing import Literal

from sqlalchemy.engine import Engine

from ..cache.current_signals_cache import CurrentSignalsCache
from ..fundamentals.v1_runners import (
    populate_industry_index_value_for_all,
    populate_industry_indicator_enrichment_for_all,
    populate_industry_indicator_state_for_all,
    populate_stock_indicator_enrichment_for_exchange,
    populate_stock_indicator_state_for_exchange,
)
from ..persistence import transaction
from ..persistence.current_signals_repo import CurrentSignalsRepository
from .derive_signals import derive_signals_for_all_active_stocks
from .rotate_signals import rotate_current_to_historical

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RecomputeMarketResult:
    """Aggregated outcome of one `recompute_market` invocation.

    Counters reflect the underlying populators' reports. Per-ticker /
    per-industry detail (which tickers succeeded vs failed vs skipped)
    is logged but NOT surfaced here — fetch it from the
    fundamentals_run table for forensics.

    `stage_errors` collects stage-level halts (the stage threw at the
    framework level, not at a per-ticker level). An empty list means
    every stage completed.
    """
    mode: Literal["live"]
    exchange: str
    as_of: datetime
    started_at: datetime
    finished_at: datetime

    # Stage 1
    stock_state_succeeded: int
    stock_state_failed: int
    stock_state_skipped: int

    # Stage 2
    stock_enrichment_succeeded: int
    stock_enrichment_failed: int
    stock_enrichment_skipped: int

    # Stage 3
    signals_rotated: int

    # Stage 4
    signals_emitted: int

    # Stage 5 — cache rebuild
    cache_rebuilt: bool

    # Stage 6
    iiv_succeeded: int
    iiv_failed: int
    iiv_skipped: int

    # Stage 7
    industry_state_succeeded: int
    industry_state_failed: int
    industry_state_skipped: int

    # Stage 8
    industry_enrichment_succeeded: int
    industry_enrichment_failed: int
    industry_enrichment_skipped: int

    # If any stage raised at the framework level, captured here as
    # (stage_name, error_message) tuples in execution order.
    stage_errors: list[tuple[str, str]] = field(default_factory=list)


# ====================================================================
# Stage helpers
# ====================================================================

def _stage_stock_state(
    db_engine: Engine,
    exchange: str,
    n_weeks_to_upsert: int,
) -> tuple[int, int, int]:
    """Returns (succeeded_count, failed_count, skipped_count)."""
    report = populate_stock_indicator_state_for_exchange(
        db_engine, exchange, "1W",
        mode="incremental", n_weeks_to_upsert=n_weeks_to_upsert,
    )
    return (
        len(report.get("succeeded", [])),
        len(report.get("failed", [])),
        len(report.get("skipped", [])),
    )


def _stage_stock_enrichment(
    db_engine: Engine,
    exchange: str,
    n_weeks_to_upsert: int,
) -> tuple[int, int, int]:
    report = populate_stock_indicator_enrichment_for_exchange(
        db_engine, exchange, "1W",
        mode="incremental", n_weeks_to_upsert=n_weeks_to_upsert,
    )
    return (
        len(report.get("succeeded", [])),
        len(report.get("failed", [])),
        len(report.get("skipped", [])),
    )


def _stage_iiv_refresh(
    db_engine: Engine,
    as_of: datetime,
    n_weeks_to_upsert: int,
) -> tuple[int, int, int]:
    """Refresh industry_index_value for the trailing window
    `as_of - n_weeks_to_upsert weeks` through `as_of`. Required
    before industry state recomputes because the state populator
    reads from IIV."""
    to_date = as_of.date()
    from_date = (
        as_of - timedelta(weeks=n_weeks_to_upsert)
    ).date()
    report = populate_industry_index_value_for_all(
        db_engine,
        from_date=from_date,
        to_date=to_date,
    )
    return (
        len(report.get("succeeded", [])),
        len(report.get("failed", [])),
        len(report.get("skipped", [])),
    )


def _stage_industry_state(
    db_engine: Engine,
    n_weeks_to_upsert: int,
) -> tuple[int, int, int]:
    report = populate_industry_indicator_state_for_all(
        db_engine,
        mode="incremental", n_weeks_to_upsert=n_weeks_to_upsert,
    )
    return (
        len(report.get("succeeded", [])),
        len(report.get("failed", [])),
        len(report.get("skipped", [])),
    )


def _stage_industry_enrichment(
    db_engine: Engine,
    n_weeks_to_upsert: int,
) -> tuple[int, int, int]:
    report = populate_industry_indicator_enrichment_for_all(
        db_engine,
        mode="incremental", n_weeks_to_upsert=n_weeks_to_upsert,
    )
    return (
        len(report.get("succeeded", [])),
        len(report.get("failed", [])),
        len(report.get("skipped", [])),
    )


def _stage_rotate_signals(db_engine: Engine) -> int:
    """Returns rows_moved."""
    with transaction(db_engine) as conn:
        return rotate_current_to_historical(conn)


def _stage_derive_and_insert(
    db_engine: Engine, as_of: datetime,
) -> int:
    """Derive signals from latest state pairs and insert into
    current_signals. Returns rows inserted."""
    with transaction(db_engine) as conn:
        signals = derive_signals_for_all_active_stocks(
            conn, now=as_of,
        )
        return CurrentSignalsRepository(conn).insert_many(signals)


def _stage_rebuild_cache(
    db_engine: Engine,
    cache: CurrentSignalsCache,
) -> bool:
    """Read the freshly-written current_signals back and atomically
    rebuild the cache. Returns True when rebuild ran."""
    with db_engine.connect() as conn:
        snapshot = CurrentSignalsRepository(conn).list_all()
    cache.rebuild_from(snapshot)
    return True


# ====================================================================
# Public entry point
# ====================================================================

def recompute_market(
    db_engine: Engine,
    *,
    mode: Literal["live"] = "live",
    as_of: datetime | None = None,
    n_weeks_to_upsert: int = 2,
    cache: CurrentSignalsCache | None = None,
    exchange: str = "NSE",
) -> RecomputeMarketResult:
    """Run one pass through the V1 cycle compute pipeline.

    Args:
        db_engine: SQLAlchemy Engine. Each stage opens its own
            transaction; nothing here pins one transaction across
            stages.
        mode: 'live' only for now. Backfill mode lands in Step 11
            (rewires generate_and_backfill_signals.py).
        as_of: cycle's wall-clock instant. Defaults to NOW (UTC).
            Stamped into derived signals' `computed_at` and used as
            the trailing-window anchor for IIV refresh.
        n_weeks_to_upsert: how many recent weekly bars each
            populator rewrites this cycle. Default 2 covers the
            in-progress week + any late correction to the
            just-finalized prior week.
        cache: optional CurrentSignalsCache to rebuild at the end
            of the cycle. If None, the cache rebuild stage is
            skipped (the cycle still produces correct DB state).
        exchange: which exchange's active tickers to recompute.
            V1 = 'NSE'. Industry side is always all-industries
            (no exchange filter — industries are NSE-only in V1).

    Returns:
        RecomputeMarketResult with per-stage counters + a list of
        stage_errors (empty when all stages completed).
    """
    if mode != "live":
        raise NotImplementedError(
            f"recompute_market: only mode='live' is implemented in "
            f"Step 6. Backfill mode (mode={mode!r}) is added in "
            f"Step 11 when generate_and_backfill_signals.py rewires "
            f"to this function."
        )

    if as_of is None:
        as_of = datetime.now(UTC)
    started_at = datetime.now(UTC)
    logger.info(
        "recompute_market[%s] starting: as_of=%s "
        "n_weeks_to_upsert=%d cache=%s",
        exchange, as_of.isoformat(), n_weeks_to_upsert,
        "yes" if cache is not None else "no",
    )

    # Counters — initialised here and filled stage by stage so that
    # a halt partway through still produces a coherent result.
    stage_errors: list[tuple[str, str]] = []
    stock_state = (0, 0, 0)
    stock_enrichment = (0, 0, 0)
    signals_rotated = 0
    signals_emitted = 0
    cache_rebuilt = False
    iiv = (0, 0, 0)
    industry_state = (0, 0, 0)
    industry_enrichment = (0, 0, 0)

    # ---- Stage 1: stock state ----
    try:
        stock_state = _stage_stock_state(
            db_engine, exchange, n_weeks_to_upsert,
        )
        logger.info(
            "recompute_market[%s] stage 1 stock_state: "
            "succeeded=%d failed=%d skipped=%d",
            exchange, *stock_state,
        )
    except Exception as e:
        msg = f"stock_state stage halted: {e}"
        logger.exception("recompute_market[%s] %s", exchange, msg)
        stage_errors.append(("stock_state", str(e)))

    # ---- Stage 2: stock enrichment ----
    if not stage_errors:
        try:
            stock_enrichment = _stage_stock_enrichment(
                db_engine, exchange, n_weeks_to_upsert,
            )
            logger.info(
                "recompute_market[%s] stage 2 stock_enrichment: "
                "succeeded=%d failed=%d skipped=%d",
                exchange, *stock_enrichment,
            )
        except Exception as e:
            msg = f"stock_enrichment stage halted: {e}"
            logger.exception("recompute_market[%s] %s", exchange, msg)
            stage_errors.append(("stock_enrichment", str(e)))

    # ---- Stage 3: rotate signals ----
    if not stage_errors:
        try:
            signals_rotated = _stage_rotate_signals(db_engine)
            logger.info(
                "recompute_market[%s] stage 3 rotation: "
                "rows_moved=%d", exchange, signals_rotated,
            )
        except Exception as e:
            msg = f"rotation stage halted: {e}"
            logger.exception("recompute_market[%s] %s", exchange, msg)
            stage_errors.append(("rotation", str(e)))

    # ---- Stage 4: derive signals + insert into current ----
    if not stage_errors:
        try:
            signals_emitted = _stage_derive_and_insert(
                db_engine, as_of,
            )
            logger.info(
                "recompute_market[%s] stage 4 derive: emitted=%d",
                exchange, signals_emitted,
            )
        except Exception as e:
            msg = f"derive stage halted: {e}"
            logger.exception("recompute_market[%s] %s", exchange, msg)
            stage_errors.append(("derive", str(e)))

    # ---- Stage 5: cache rebuild ----
    if not stage_errors and cache is not None:
        try:
            cache_rebuilt = _stage_rebuild_cache(db_engine, cache)
            logger.info(
                "recompute_market[%s] stage 5 cache_rebuild: "
                "entries=%d", exchange, cache.signal_count(),
            )
        except Exception as e:
            msg = f"cache_rebuild stage halted: {e}"
            logger.exception("recompute_market[%s] %s", exchange, msg)
            stage_errors.append(("cache_rebuild", str(e)))

    # ---- Stage 6: IIV refresh ----
    if not stage_errors:
        try:
            iiv = _stage_iiv_refresh(
                db_engine, as_of, n_weeks_to_upsert,
            )
            logger.info(
                "recompute_market[%s] stage 6 iiv: "
                "succeeded=%d failed=%d skipped=%d",
                exchange, *iiv,
            )
        except Exception as e:
            msg = f"iiv stage halted: {e}"
            logger.exception("recompute_market[%s] %s", exchange, msg)
            stage_errors.append(("iiv", str(e)))

    # ---- Stage 7: industry state ----
    if not stage_errors:
        try:
            industry_state = _stage_industry_state(
                db_engine, n_weeks_to_upsert,
            )
            logger.info(
                "recompute_market[%s] stage 7 industry_state: "
                "succeeded=%d failed=%d skipped=%d",
                exchange, *industry_state,
            )
        except Exception as e:
            msg = f"industry_state stage halted: {e}"
            logger.exception("recompute_market[%s] %s", exchange, msg)
            stage_errors.append(("industry_state", str(e)))

    # ---- Stage 8: industry enrichment ----
    if not stage_errors:
        try:
            industry_enrichment = _stage_industry_enrichment(
                db_engine, n_weeks_to_upsert,
            )
            logger.info(
                "recompute_market[%s] stage 8 industry_enrichment: "
                "succeeded=%d failed=%d skipped=%d",
                exchange, *industry_enrichment,
            )
        except Exception as e:
            msg = f"industry_enrichment stage halted: {e}"
            logger.exception("recompute_market[%s] %s", exchange, msg)
            stage_errors.append(("industry_enrichment", str(e)))

    finished_at = datetime.now(UTC)
    duration_s = (finished_at - started_at).total_seconds()
    logger.info(
        "recompute_market[%s] finished in %.2fs (errors=%d)",
        exchange, duration_s, len(stage_errors),
    )

    return RecomputeMarketResult(
        mode=mode,
        exchange=exchange,
        as_of=as_of,
        started_at=started_at,
        finished_at=finished_at,
        stock_state_succeeded=stock_state[0],
        stock_state_failed=stock_state[1],
        stock_state_skipped=stock_state[2],
        stock_enrichment_succeeded=stock_enrichment[0],
        stock_enrichment_failed=stock_enrichment[1],
        stock_enrichment_skipped=stock_enrichment[2],
        signals_rotated=signals_rotated,
        signals_emitted=signals_emitted,
        cache_rebuilt=cache_rebuilt,
        iiv_succeeded=iiv[0],
        iiv_failed=iiv[1],
        iiv_skipped=iiv[2],
        industry_state_succeeded=industry_state[0],
        industry_state_failed=industry_state[1],
        industry_state_skipped=industry_state[2],
        industry_enrichment_succeeded=industry_enrichment[0],
        industry_enrichment_failed=industry_enrichment[1],
        industry_enrichment_skipped=industry_enrichment[2],
        stage_errors=stage_errors,
    )
