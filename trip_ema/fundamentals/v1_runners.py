"""V1 clean-pipeline runners.

Step 4: stock indicator state populator. Reads `ohlcv_bars` for one
ticker (or many), computes the 12-core state via
`compute_state_primitives`, and upserts to `stock_indicator_state`.

Architectural invariants (these are the point of the clean pipeline):
  - This module touches ONLY the core state table. Enrichment is a
    separate stage (Step 5).
  - This module does NOT compute or write signals, does NOT update
    the in-memory signal cache, does NOT send notifications. Those
    are downstream pipeline stages.
  - The runner is idempotent — calling it twice for the same ticker
    overwrites the rows but is otherwise a no-op (no audit-trail
    bloat beyond two fundamentals_run rows).
  - One DB transaction per ticker. A failure for ticker X does not
    abort ticker Y in a bulk run.

The runner uses `_fetch_bars_at_timeframe` (the same helper as the
legacy `run_indicator_state_for_ticker`) so weekly aggregation
remains consistent between V1 and legacy paths during the side-by-
side validation period.
"""
from __future__ import annotations

import logging
from datetime import datetime, UTC

import pandas as pd
from sqlalchemy.engine import Engine

from ..domain import Timeframe
from ..domain.stock_indicator_enrichment import StockIndicatorEnrichment
from ..domain.stock_indicator_state import StockIndicatorState
from ..persistence.fundamentals_repo import FundamentalsRunRepository
from ..persistence.stock_indicator_enrichment_repo import (
    StockIndicatorEnrichmentRepository,
)
from ..persistence.stock_indicator_state_repo import (
    StockIndicatorStateRepository,
)
from ..persistence.tickers_repo import TickersRepository
from ..persistence import transaction
from .runners import _fetch_bars_at_timeframe
from .v1_compute_primitives import (
    compute_enrichment_primitives,
    compute_state_primitives,
)

logger = logging.getLogger(__name__)

# fundamentals_run.phase for V1 stock indicator state runs. Added to
# the RUN_PHASES tuple in fundamentals/__init__.py.
PHASE_V1_STOCK_STATE = "v1_stock_indicator_state"
PHASE_V1_STOCK_ENRICHMENT = "v1_stock_indicator_enrichment"


def populate_stock_indicator_state_for_ticker(
    db_engine: Engine,
    ticker_id: int,
    timeframe: str,
    *,
    force: bool = False,
    min_bars: int = 30,
) -> int:
    """Compute V1 stock indicator state for one (ticker, timeframe).

    Reads the full OHLCV history for this ticker+timeframe via
    `_fetch_bars_at_timeframe` (which handles 1D → 1W aggregation
    when only 1D is stored), runs `compute_state_primitives`, and
    upserts the resulting rows to `stock_indicator_state`.

    Returns the fundamentals_run.run_id. Raises ValueError if the
    ticker doesn't exist or has fewer than `min_bars` (insufficient
    to seed the EMAs meaningfully).

    `force` is reserved for future "skip-if-recent" semantics; the
    legacy runner used an 80-day guard, but the V1 model treats
    every invocation as a full recompute since the runner is cheap
    and idempotent.
    """
    # Validate timeframe.
    try:
        Timeframe(timeframe)
    except ValueError as e:
        raise ValueError(
            f"populate_stock_indicator_state_for_ticker: "
            f"unknown timeframe {timeframe!r}; must be one of "
            f"{[t.value for t in Timeframe]}"
        ) from e

    # Sanity: ticker must exist.
    with db_engine.connect() as conn:
        ticker = TickersRepository(conn).get_by_id(ticker_id)
    if ticker is None:
        raise ValueError(
            f"populate_stock_indicator_state_for_ticker: "
            f"ticker_id={ticker_id} does not exist"
        )

    # Read OHLCV bars.
    with db_engine.connect() as conn:
        bars = _fetch_bars_at_timeframe(conn, ticker_id, timeframe)
    if len(bars) < min_bars:
        raise ValueError(
            f"populate_stock_indicator_state_for_ticker: "
            f"ticker_id={ticker_id} tf={timeframe} has only "
            f"{len(bars)} bars (need at least {min_bars} to seed EMAs)"
        )

    # Build pandas Series for the compute layer.
    open_s = pd.Series([b.open for b in bars], dtype=float)  # noqa: F841
    high_s = pd.Series([b.high for b in bars], dtype=float)
    low_s = pd.Series([b.low for b in bars], dtype=float)
    close_s = pd.Series([b.close for b in bars], dtype=float)
    volume_s = pd.Series([b.volume for b in bars], dtype=float)
    # The bar's UTC timestamp from ohlcv_bars carries through to
    # stock_indicator_state.bar_timestamp 1:1. Both columns are
    # TIMESTAMPTZ so timezone semantics are preserved.
    timestamps = pd.Series([b.timestamp for b in bars])

    # Compute the core state (skips NaN-warmup bars).
    primitives = compute_state_primitives(
        close_s, volume_s, high_s, low_s,
        trade_dates=timestamps,
        synthetic_atr_for_index=False,
    )

    # Convert to dataclass list. computed_at is set on the runner side
    # so all rows of one run share the same audit timestamp.
    now = datetime.now(UTC)
    states = [
        StockIndicatorState(
            ticker_id=ticker_id,
            timeframe=timeframe,
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
            computed_at=now,
        )
        for p in primitives
    ]

    # Atomic write: one transaction, all rows or none.
    with transaction(db_engine) as conn:
        run_id = FundamentalsRunRepository(conn).open(
            phase=PHASE_V1_STOCK_STATE, exchange=ticker.ref.exchange,
            notes=(
                f"v1_stock_indicator_state for ticker_id={ticker_id} "
                f"({ticker.ref.exchange}:{ticker.ref.symbol}) "
                f"tf={timeframe}"
            ),
        )
        StockIndicatorStateRepository(conn).upsert_many(states)
        FundamentalsRunRepository(conn).close(
            run_id, status="SUCCESS",
            succeeded=len(states), failed=0,
            skipped=(len(bars) - len(states)),
        )

    logger.info(
        "populate_stock_indicator_state_for_ticker "
        "ticker_id=%d (%s:%s) tf=%s: emitted=%d skipped=%d bars=%d",
        ticker_id, ticker.ref.exchange, ticker.ref.symbol, timeframe,
        len(states), len(bars) - len(states), len(bars),
    )
    return run_id


def populate_stock_indicator_state_for_exchange(
    db_engine: Engine,
    exchange: str,
    timeframe: str,
    *,
    min_bars: int = 30,
) -> dict:
    """Bulk-compute V1 stock indicator state for every active ticker
    on `exchange`.

    Iterates in arbitrary order. Per-ticker error isolation: a
    failure on ticker X does not abort tickers Y, Z, … — failures
    are collected and returned in the report. Each ticker runs in
    its own transaction.

    Returns a report dict:
        {
          "exchange": str,
          "timeframe": str,
          "succeeded": [(ticker_id, symbol, rows_written), ...],
          "failed":    [(ticker_id, symbol, error_message), ...],
          "skipped":   [(ticker_id, symbol, reason), ...],
        }
    """
    with db_engine.connect() as conn:
        all_active = TickersRepository(conn).find_active()
    exchange_tickers = [t for t in all_active if t.ref.exchange == exchange]
    if not exchange_tickers:
        logger.info(
            "populate_stock_indicator_state_for_exchange %s: "
            "no active tickers on this exchange",
            exchange,
        )

    succeeded: list[tuple[int, str, int]] = []
    failed: list[tuple[int, str, str]] = []
    skipped: list[tuple[int, str, str]] = []

    for ticker in exchange_tickers:
        sym = ticker.ref.symbol
        try:
            run_id = populate_stock_indicator_state_for_ticker(
                db_engine, ticker.ticker_id, timeframe,
                min_bars=min_bars,
            )
            # Best-effort: read back the row count we just wrote. Not
            # strictly required for the report — using run_id as a
            # marker is enough. We use the count_for_ticker repo
            # method to get an accurate post-write count.
            with db_engine.connect() as conn:
                cnt = StockIndicatorStateRepository(
                    conn,
                ).count_for_ticker(ticker.ticker_id, timeframe)
            succeeded.append((ticker.ticker_id, sym, cnt))
            logger.debug(
                "ticker_id=%d (%s) tf=%s OK (run_id=%d, count=%d)",
                ticker.ticker_id, sym, timeframe, run_id, cnt,
            )
        except ValueError as e:
            # ValueError covers both "ticker not found" (defensive)
            # and "insufficient bars". Treat as skipped, not failed
            # — the ticker simply isn't ready for V1 state yet.
            skipped.append((ticker.ticker_id, sym, str(e)))
            logger.info(
                "ticker_id=%d (%s) tf=%s skipped: %s",
                ticker.ticker_id, sym, timeframe, e,
            )
        except Exception as e:
            failed.append((ticker.ticker_id, sym, str(e)))
            logger.warning(
                "ticker_id=%d (%s) tf=%s failed: %s",
                ticker.ticker_id, sym, timeframe, e,
                exc_info=True,
            )

    report = {
        "exchange": exchange,
        "timeframe": timeframe,
        "succeeded": succeeded,
        "failed": failed,
        "skipped": skipped,
    }
    logger.info(
        "populate_stock_indicator_state_for_exchange %s tf=%s: "
        "succeeded=%d failed=%d skipped=%d",
        exchange, timeframe,
        len(succeeded), len(failed), len(skipped),
    )
    return report


# ====================================================================
# Step 5 — stock indicator enrichment populator
# ====================================================================


def populate_stock_indicator_enrichment_for_ticker(
    db_engine: Engine,
    ticker_id: int,
    timeframe: str,
    *,
    force: bool = False,
    min_bars: int = 30,
) -> int:
    """Compute V1 stock indicator enrichment for one (ticker, timeframe).

    Reads OHLCV history, runs `compute_enrichment_primitives`, and
    upserts to `stock_indicator_enrichment` (FK ON DELETE CASCADE
    back to `stock_indicator_state`).

    Requires the matching state row(s) to exist — fails fast with a
    clear ValueError if state has not been populated yet. The FK
    would catch this on insert otherwise but with a less helpful
    error message.

    Returns the fundamentals_run.run_id.
    """
    # Validate timeframe.
    try:
        Timeframe(timeframe)
    except ValueError as e:
        raise ValueError(
            f"populate_stock_indicator_enrichment_for_ticker: "
            f"unknown timeframe {timeframe!r}; must be one of "
            f"{[t.value for t in Timeframe]}"
        ) from e

    # Sanity: ticker must exist.
    with db_engine.connect() as conn:
        ticker = TickersRepository(conn).get_by_id(ticker_id)
    if ticker is None:
        raise ValueError(
            f"populate_stock_indicator_enrichment_for_ticker: "
            f"ticker_id={ticker_id} does not exist"
        )

    # Sanity: state must have been populated first. The FK ON DELETE
    # CASCADE requires a state row per enrichment row; we fail-fast
    # here for a cleaner error than the IntegrityError that would
    # otherwise come from the upsert.
    with db_engine.connect() as conn:
        state_count = StockIndicatorStateRepository(
            conn,
        ).count_for_ticker(ticker_id, timeframe)
    if state_count == 0:
        raise ValueError(
            f"populate_stock_indicator_enrichment_for_ticker: "
            f"ticker_id={ticker_id} tf={timeframe} has no state rows. "
            f"Run the state populator first (Step 4 endpoint)."
        )

    # Read OHLCV bars.
    with db_engine.connect() as conn:
        bars = _fetch_bars_at_timeframe(conn, ticker_id, timeframe)
    if len(bars) < min_bars:
        raise ValueError(
            f"populate_stock_indicator_enrichment_for_ticker: "
            f"ticker_id={ticker_id} tf={timeframe} has only "
            f"{len(bars)} bars (need at least {min_bars} to seed EMAs)"
        )

    # Build pandas Series for the compute layer.
    high_s = pd.Series([b.high for b in bars], dtype=float)
    low_s = pd.Series([b.low for b in bars], dtype=float)
    close_s = pd.Series([b.close for b in bars], dtype=float)
    volume_s = pd.Series([b.volume for b in bars], dtype=float)
    timestamps = pd.Series([b.timestamp for b in bars])

    # Compute enrichment (skips NaN-warmup bars; row-by-row aligned
    # with what compute_state_primitives would emit, so the FK is
    # satisfied for every emitted row).
    primitives = compute_enrichment_primitives(
        close_s, volume_s, high_s, low_s,
        trade_dates=timestamps,
        synthetic_atr_for_index=False,
    )

    # Convert to dataclass list. computed_at is set on the runner side
    # so all rows of one run share the same audit timestamp.
    now = datetime.now(UTC)
    enrichments = [
        StockIndicatorEnrichment(
            ticker_id=ticker_id,
            timeframe=timeframe,
            bar_timestamp=p["bar_timestamp"],
            pierces_upper_mid=p["pierces_upper_mid"],
            pierces_lower_mid=p["pierces_lower_mid"],
            dist_to_med_in_hl_units=p["dist_to_med_in_hl_units"],
            dist_to_long_in_hl_units=p["dist_to_long_in_hl_units"],
            dist_to_longest_in_hl_units=p["dist_to_longest_in_hl_units"],
            mean_dist_to_med_in_hl_units=
                p["mean_dist_to_med_in_hl_units"],
            mean_dist_to_long_in_hl_units=
                p["mean_dist_to_long_in_hl_units"],
            mean_dist_to_longest_in_hl_units=
                p["mean_dist_to_longest_in_hl_units"],
            bars_since_med_turn=p["bars_since_med_turn"],
            bars_since_long_turn=p["bars_since_long_turn"],
            bars_since_longest_turn=p["bars_since_longest_turn"],
            pct_change_since_med_turn=p["pct_change_since_med_turn"],
            pct_change_since_long_turn=p["pct_change_since_long_turn"],
            pct_change_since_longest_turn=
                p["pct_change_since_longest_turn"],
            med_pattern_label=p["med_pattern_label"],
            long_pattern_label=p["long_pattern_label"],
            longest_pattern_label=p["longest_pattern_label"],
            med_turn_buy=p["med_turn_buy"],
            med_turn_sell=p["med_turn_sell"],
            long_turn_buy=p["long_turn_buy"],
            long_turn_sell=p["long_turn_sell"],
            longest_turn_buy=p["longest_turn_buy"],
            longest_turn_sell=p["longest_turn_sell"],
            computed_at=now,
        )
        for p in primitives
    ]

    # Atomic write.
    with transaction(db_engine) as conn:
        run_id = FundamentalsRunRepository(conn).open(
            phase=PHASE_V1_STOCK_ENRICHMENT,
            exchange=ticker.ref.exchange,
            notes=(
                f"v1_stock_indicator_enrichment for ticker_id={ticker_id} "
                f"({ticker.ref.exchange}:{ticker.ref.symbol}) "
                f"tf={timeframe}"
            ),
        )
        StockIndicatorEnrichmentRepository(conn).upsert_many(enrichments)
        FundamentalsRunRepository(conn).close(
            run_id, status="SUCCESS",
            succeeded=len(enrichments), failed=0,
            skipped=(len(bars) - len(enrichments)),
        )

    logger.info(
        "populate_stock_indicator_enrichment_for_ticker "
        "ticker_id=%d (%s:%s) tf=%s: emitted=%d skipped=%d bars=%d",
        ticker_id, ticker.ref.exchange, ticker.ref.symbol, timeframe,
        len(enrichments), len(bars) - len(enrichments), len(bars),
    )
    return run_id


def populate_stock_indicator_enrichment_for_exchange(
    db_engine: Engine,
    exchange: str,
    timeframe: str,
    *,
    min_bars: int = 30,
) -> dict:
    """Bulk-compute V1 stock indicator enrichment for every active
    ticker on `exchange`.

    Per-ticker error isolation. Tickers whose state hasn't been
    populated yet are skipped (with a clear reason in the report)
    rather than failed — same classification as "insufficient bars".

    Returns a report dict with succeeded / failed / skipped lists.
    """
    with db_engine.connect() as conn:
        all_active = TickersRepository(conn).find_active()
    exchange_tickers = [
        t for t in all_active if t.ref.exchange == exchange
    ]
    if not exchange_tickers:
        logger.info(
            "populate_stock_indicator_enrichment_for_exchange %s: "
            "no active tickers on this exchange",
            exchange,
        )

    succeeded: list[tuple[int, str, int]] = []
    failed: list[tuple[int, str, str]] = []
    skipped: list[tuple[int, str, str]] = []

    for ticker in exchange_tickers:
        sym = ticker.ref.symbol
        try:
            run_id = populate_stock_indicator_enrichment_for_ticker(
                db_engine, ticker.ticker_id, timeframe,
                min_bars=min_bars,
            )
            with db_engine.connect() as conn:
                cnt = StockIndicatorEnrichmentRepository(
                    conn,
                ).count_for_ticker(ticker.ticker_id, timeframe)
            succeeded.append((ticker.ticker_id, sym, cnt))
            logger.debug(
                "ticker_id=%d (%s) tf=%s OK (run_id=%d, count=%d)",
                ticker.ticker_id, sym, timeframe, run_id, cnt,
            )
        except ValueError as e:
            skipped.append((ticker.ticker_id, sym, str(e)))
            logger.info(
                "ticker_id=%d (%s) tf=%s skipped: %s",
                ticker.ticker_id, sym, timeframe, e,
            )
        except Exception as e:
            failed.append((ticker.ticker_id, sym, str(e)))
            logger.warning(
                "ticker_id=%d (%s) tf=%s failed: %s",
                ticker.ticker_id, sym, timeframe, e,
                exc_info=True,
            )

    report = {
        "exchange": exchange,
        "timeframe": timeframe,
        "succeeded": succeeded,
        "failed": failed,
        "skipped": skipped,
    }
    logger.info(
        "populate_stock_indicator_enrichment_for_exchange %s tf=%s: "
        "succeeded=%d failed=%d skipped=%d",
        exchange, timeframe,
        len(succeeded), len(failed), len(skipped),
    )
    return report
