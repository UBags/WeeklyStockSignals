"""Merged daily-grain stock state + enrichment — one engine pass, both tables.

WHY
  compute_daily_grain_via_engine returns (state_rows, enrich_rows): it computes
  BOTH on every call. But:

      compute_daily_grain_states()     -> engine pass, DISCARDS enrich_rows
      compute_daily_grain_enrichment() -> engine pass, DISCARDS state_rows

  So backfill stage 2 fetches every ticker's full daily history, runs the whole
  engine, and throws away half the result — and stage 3 then RE-FETCHES the same
  bars and RE-RUNS the identical engine to throw away the other half.

  Two full passes over ~13M bars to produce two halves of one computation.
  Measured: stock_state ~1h30m on NSE, with stock_enrichment doing the same
  work again.

WHAT THIS DOES
  One fetch, one engine pass, both tables written. The engine call is literally
  identical, so the emitted rows are byte-for-byte what the two separate runners
  produce — this is a de-duplication, not a reimplementation.

  State is written BEFORE enrichment: stock_indicator_enrichment's FK cascades
  back to stock_indicator_state per row, so the parent must exist first. This
  ordering also removes the old "enrichment requires state to be populated
  first" precondition — within a ticker it is now guaranteed by construction.

  The old runners are left in place (they are still the reference for the
  per-ticker repair path, and populate_stock_indicator_enrichment_for_ticker is
  called directly elsewhere).
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Literal

from sqlalchemy import text
from sqlalchemy.engine import Engine

from ..domain import Timeframe
from ..domain.stock_indicator_enrichment import StockIndicatorEnrichment
from ..domain.stock_indicator_state import StockIndicatorState
from ..persistence import transaction
from ..persistence.bars_repo import BarsRepository
from ..persistence.fundamentals_repo import FundamentalsRunRepository
from ..persistence.stock_indicator_enrichment_repo import (
    StockIndicatorEnrichmentRepository,
)
from ..persistence.stock_indicator_state_repo import (
    StockIndicatorStateRepository,
)
from ..persistence.tickers_repo import TickersRepository
from .indicator_primitives import INDICATOR_DEFAULTS
from ..domain.market_session import market_session_for
from .state_carry_engine import compute_daily_grain_via_engine
from .v1_runners import PHASE_V1_STOCK_ENRICHMENT, PHASE_V1_STOCK_STATE

logger = logging.getLogger(__name__)


def populate_stock_state_and_enrichment_for_ticker(
    db_engine: Engine,
    ticker_id: int,
    timeframe: str,
    *,
    min_bars: int = 30,
    mode: Literal["full", "incremental"] = "full",
) -> tuple[int, int]:
    """Compute state + enrichment for one ticker in a single engine pass.

    Returns (state_run_id, enrichment_run_id).
    """
    try:
        Timeframe(timeframe)
    except ValueError as e:
        raise ValueError(
            f"populate_stock_state_and_enrichment_for_ticker: unknown "
            f"timeframe {timeframe!r}"
        ) from e

    with db_engine.connect() as conn:
        ticker = TickersRepository(conn).get_by_id(ticker_id)
        if ticker is None:
            raise ValueError(
                f"populate_stock_state_and_enrichment_for_ticker: "
                f"ticker_id={ticker_id} does not exist"
            )
        daily_bars = BarsRepository(conn).fetch_history(
            ticker_id, Timeframe.DAILY, limit=100000,
        )
        tz_row = conn.execute(text(
            "SELECT COALESCE(tz, 'UTC') AS tz "
            "FROM tickers WHERE ticker_id = :tid"
        ), {"tid": ticker_id}).first()
    tz = tz_row.tz if tz_row else "UTC"

    if len(daily_bars) < min_bars:
        raise ValueError(
            f"populate_stock_state_and_enrichment_for_ticker: "
            f"ticker_id={ticker_id} has only {len(daily_bars)} daily bars "
            f"(need at least {min_bars})"
        )

    session = market_session_for(ticker.ref.exchange)
    now = datetime.now(UTC)

    target_days = None
    if mode == "incremental":
        zone = session.zone
        last_local = daily_bars[-1].timestamp.astimezone(zone).date()
        last_monday = last_local - timedelta(days=last_local.weekday())
        target_days = sorted({
            d for d in (
                b.timestamp.astimezone(zone).date() for b in daily_bars
            )
            if (d - timedelta(days=d.weekday())) == last_monday
        })

    # THE POINT: one pass, both halves kept.
    state_prims, enrich_prims = compute_daily_grain_via_engine(
        daily_bars, session=session, now=now,
        params=INDICATOR_DEFAULTS, target_days=target_days,
    )

    audit_now = datetime.now(UTC)

    states = [
        StockIndicatorState(
            ticker_id=ticker_id,
            timeframe=timeframe,
            date=p["trade_date"],
            bar_timestamp=p["bar_timestamp"],
            med_ema=p["med_ema"],
            long_ema=p["long_ema"],
            longest_ema=p["longest_ema"],
            atr=p["atr"],
            upper_inner_band=p["upper_inner_band"],
            lower_inner_band=p["lower_inner_band"],
            upper_mid_band=p["upper_mid_band"],
            lower_mid_band=p["lower_mid_band"],
            upper_outer_band=p["upper_outer_band"],
            lower_outer_band=p["lower_outer_band"],
            display_regime=p["display_regime"],
            computed_at=audit_now,
        )
        for p in state_prims
    ]

    enrichments = [
        StockIndicatorEnrichment(
            ticker_id=ticker_id,
            timeframe=timeframe,
            date=p["trade_date"],
            bar_timestamp=p["bar_timestamp"],
            pierces_upper_mid=p["pierces_upper_mid"],
            pierces_lower_mid=p["pierces_lower_mid"],
            dist_to_med_in_hl_units=p["dist_to_med_in_hl_units"],
            dist_to_long_in_hl_units=p["dist_to_long_in_hl_units"],
            dist_to_longest_in_hl_units=p["dist_to_longest_in_hl_units"],
            mean_dist_to_med_in_hl_units=p["mean_dist_to_med_in_hl_units"],
            mean_dist_to_long_in_hl_units=p["mean_dist_to_long_in_hl_units"],
            mean_dist_to_longest_in_hl_units=(
                p["mean_dist_to_longest_in_hl_units"]
            ),
            bars_since_med_turn=p["bars_since_med_turn"],
            bars_since_long_turn=p["bars_since_long_turn"],
            bars_since_longest_turn=p["bars_since_longest_turn"],
            pct_change_since_med_turn=p["pct_change_since_med_turn"],
            pct_change_since_long_turn=p["pct_change_since_long_turn"],
            pct_change_since_longest_turn=p["pct_change_since_longest_turn"],
            med_pattern_label=p["med_pattern_label"],
            long_pattern_label=p["long_pattern_label"],
            longest_pattern_label=p["longest_pattern_label"],
            med_turn_buy=p["med_turn_buy"],
            med_turn_sell=p["med_turn_sell"],
            long_turn_buy=p["long_turn_buy"],
            long_turn_sell=p["long_turn_sell"],
            longest_turn_buy=p["longest_turn_buy"],
            longest_turn_sell=p["longest_turn_sell"],
            computed_at=audit_now,
        )
        for p in enrich_prims
    ]

    with transaction(db_engine) as conn:
        runs = FundamentalsRunRepository(conn)
        state_run = runs.open(
            phase=PHASE_V1_STOCK_STATE, exchange=ticker.ref.exchange,
            notes=(
                f"v1_stock_indicator_state daily-grain (merged) for "
                f"ticker_id={ticker_id} "
                f"({ticker.ref.exchange}:{ticker.ref.symbol}) "
                f"tf={timeframe} mode={mode}"
            ),
        )
        # State first: stock_indicator_enrichment FKs back to it per row.
        StockIndicatorStateRepository(conn).upsert_many(states)
        runs.close(
            state_run, status="SUCCESS",
            succeeded=len(states), failed=0, skipped=0,
        )

        enrich_run = runs.open(
            phase=PHASE_V1_STOCK_ENRICHMENT, exchange=ticker.ref.exchange,
            notes=(
                f"v1_stock_indicator_enrichment daily-grain (merged) for "
                f"ticker_id={ticker_id} "
                f"({ticker.ref.exchange}:{ticker.ref.symbol}) "
                f"tf={timeframe} mode={mode}"
            ),
        )
        StockIndicatorEnrichmentRepository(conn).upsert_many(enrichments)
        runs.close(
            enrich_run, status="SUCCESS",
            succeeded=len(enrichments), failed=0, skipped=0,
        )

    logger.info(
        "merged state+enrichment ticker_id=%d (%s:%s) tf=%s mode=%s: "
        "states=%d enrichments=%d from %d daily bars",
        ticker_id, ticker.ref.exchange, ticker.ref.symbol, timeframe, mode,
        len(states), len(enrichments), len(daily_bars),
    )
    return state_run, enrich_run
