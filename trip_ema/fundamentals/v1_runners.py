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
from datetime import date, datetime, time, UTC
from zoneinfo import ZoneInfo

import pandas as pd
from sqlalchemy.engine import Engine

from ..domain import OHLCVBar, Timeframe
from ..domain.industry_indicator_enrichment import (
    IndustryIndicatorEnrichment,
)
from ..domain.industry_indicator_state import IndustryIndicatorState
from ..domain.stock_indicator_enrichment import StockIndicatorEnrichment
from ..domain.stock_indicator_state import StockIndicatorState
from ..indicators.aggregation import daily_to_weekly
from ..persistence.fundamentals_repo import (
    FundamentalsRunRepository,
    IndustryIndexRepository,
    IndustryIndexValueRepository,
)
from ..persistence.industry_indicator_enrichment_repo import (
    IndustryIndicatorEnrichmentRepository,
)
from ..persistence.industry_indicator_state_repo import (
    IndustryIndicatorStateRepository,
)
from ..persistence.stock_indicator_enrichment_repo import (
    StockIndicatorEnrichmentRepository,
)
from ..persistence.stock_indicator_state_repo import (
    StockIndicatorStateRepository,
)
from ..persistence.tickers_repo import TickersRepository
from ..persistence import transaction
from .index_compute import compute_index_value_for_date
from .runners import (
    _fetch_bars_at_timeframe,
    _list_trading_days_in_range,
    _parse_iso_date,
)
from .v1_compute_primitives import (
    compute_enrichment_primitives,
    compute_state_primitives,
)

logger = logging.getLogger(__name__)

# fundamentals_run.phase for V1 stock indicator state runs. Added to
# the RUN_PHASES tuple in fundamentals/__init__.py.
PHASE_V1_STOCK_STATE = "v1_stock_indicator_state"
PHASE_V1_STOCK_ENRICHMENT = "v1_stock_indicator_enrichment"
PHASE_V1_INDUSTRY_INDEX_VALUE = "v1_industry_index_value"
PHASE_V1_INDUSTRY_INDICATOR_STATE = "v1_industry_indicator_state"
PHASE_V1_INDUSTRY_INDICATOR_ENRICHMENT = (
    "v1_industry_indicator_enrichment"
)

# IST is used for the industry weekly-bar Monday anchor — see Phase 1B
# unification (commit 607a2a5). Computed once at module load.
_IST = ZoneInfo("Asia/Kolkata")


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


# ====================================================================
# Step 6 — industry index value populator
# ====================================================================
#
# Unlike the stock state/enrichment runners, this one is resumable.
# Industry index value backfills span ~250+ trading days × 60+
# industries, so a power or network outage mid-run could lose a lot
# of work. The runner persists its date-list cursor to
# fundamentals_run.progress_json and accepts a resume_run_id to pick
# up from where it left off.
#
# Architectural notes:
#   - No skip-if-recent guard. Every invocation is a full upsert
#     recompute of the requested range. (The legacy
#     run_index_value_backfill had an 80-day guard that was the
#     suspected cause of stale-data symptoms — dropped here.)
#   - One transaction per date for the upsert; progress_json is
#     updated every `checkpoint_every` dates (default 50).
#   - Uses exchange="ALL" in the audit row because industry phases
#     don't map to a single exchange (legacy convention).


def populate_industry_index_value_for_industry(
    db_engine: Engine,
    industry_name: str,
    *,
    from_date: date | None = None,
    to_date: date | None = None,
    resume_run_id: int | None = None,
    checkpoint_every: int = 50,
) -> int:
    """Compute V1 industry index values for one industry across a
    date range. Resumable.

    Args:
        industry_name: The index to compute.
        from_date: Start of date range (inclusive). Defaults to
            the index's `base_date` (the earliest meaningful date).
        to_date: End of date range (inclusive). Defaults to
            today (UTC).
        resume_run_id: If set, resume an existing RUNNING run from
            where it left off. The runner restores the date list,
            cursor, and counters from progress_json. The caller's
            from_date / to_date are ignored — the resumed run uses
            the range from the original invocation.
        checkpoint_every: Update progress_json every N dates.

    Returns:
        fundamentals_run.run_id (either the new run's id or the
        resumed one).

    Raises:
        ValueError: industry_name unknown, from_date > to_date, or
            resume_run_id points to a non-existent/wrong-phase/
            non-RUNNING run.
    """
    # --- resume path ---
    if resume_run_id is not None:
        with db_engine.connect() as conn:
            run = FundamentalsRunRepository(conn).get(resume_run_id)
        if run is None:
            raise ValueError(
                f"populate_industry_index_value_for_industry: "
                f"no fundamentals_run with id={resume_run_id}"
            )
        if run.phase != PHASE_V1_INDUSTRY_INDEX_VALUE:
            raise ValueError(
                f"populate_industry_index_value_for_industry: "
                f"resume run {resume_run_id} has phase {run.phase!r}, "
                f"expected {PHASE_V1_INDUSTRY_INDEX_VALUE!r}"
            )
        if run.status != "RUNNING":
            raise ValueError(
                f"populate_industry_index_value_for_industry: "
                f"resume run {resume_run_id} has status "
                f"{run.status!r}, can only resume RUNNING runs"
            )
        progress = run.progress_json or {}
        resumed_industry = progress.get("industry_name")
        if resumed_industry != industry_name:
            raise ValueError(
                f"populate_industry_index_value_for_industry: "
                f"resume run {resume_run_id} is for industry "
                f"{resumed_industry!r}, not {industry_name!r}"
            )
        date_list = [
            _parse_iso_date(s) for s in progress.get("date_list", [])
        ]
        cursor_index = int(progress.get("cursor_index", 0))
        from_date = _parse_iso_date(progress["from_date"])
        to_date = _parse_iso_date(progress["to_date"])
        succeeded = run.succeeded
        failed = run.failed
        skipped = run.skipped
        run_id = resume_run_id
        logger.info(
            "Resuming v1_industry_index_value run %d for %s at "
            "date %d/%d (from=%s to=%s)",
            run_id, industry_name, cursor_index, len(date_list),
            from_date, to_date,
        )
    else:
        # --- fresh run ---
        with db_engine.connect() as conn:
            idx = IndustryIndexRepository(conn).get(industry_name)
        if idx is None:
            raise ValueError(
                f"populate_industry_index_value_for_industry: "
                f"industry {industry_name!r} does not exist"
            )

        if from_date is None:
            from_date = idx.base_date
        if to_date is None:
            to_date = datetime.now(UTC).date()
        if from_date > to_date:
            raise ValueError(
                f"populate_industry_index_value_for_industry: "
                f"from_date {from_date} must be <= to_date {to_date}"
            )

        date_list = _list_trading_days_in_range(
            db_engine, from_date, to_date,
        )

        if not date_list:
            # Open + close a zero-row SUCCESS run for audit
            # consistency. No work to do but we still want the
            # operator to see the empty result in fundamentals_run.
            with transaction(db_engine) as conn:
                run_id = FundamentalsRunRepository(conn).open(
                    phase=PHASE_V1_INDUSTRY_INDEX_VALUE,
                    exchange="ALL",
                    notes=(
                        f"v1_industry_index_value for {industry_name}: "
                        f"no trading days in [{from_date}, {to_date}]"
                    ),
                )
                FundamentalsRunRepository(conn).close(
                    run_id, status="SUCCESS",
                    succeeded=0, failed=0, skipped=0,
                )
            logger.info(
                "populate_industry_index_value_for_industry %s: "
                "no trading days in [%s, %s] — no-op run %d",
                industry_name, from_date, to_date, run_id,
            )
            return run_id

        cursor_index = 0
        succeeded = 0
        failed = 0
        skipped = 0
        with transaction(db_engine) as conn:
            run_id = FundamentalsRunRepository(conn).open(
                phase=PHASE_V1_INDUSTRY_INDEX_VALUE,
                exchange="ALL",
                notes=(
                    f"v1_industry_index_value for {industry_name}: "
                    f"{len(date_list)} trading days in "
                    f"[{from_date}, {to_date}]"
                ),
            )
            FundamentalsRunRepository(conn).update_progress(
                run_id,
                progress_json={
                    "industry_name": industry_name,
                    "date_list": [d.isoformat() for d in date_list],
                    "cursor_index": 0,
                    "from_date": from_date.isoformat(),
                    "to_date": to_date.isoformat(),
                },
            )

    # --- walk dates ---
    for i in range(cursor_index, len(date_list)):
        d = date_list[i]
        try:
            computed = compute_index_value_for_date(
                db_engine, industry_name, d,
            )
            if computed is None:
                # Pre-base date, or empty rebalance, or missing
                # mcap — None is "nothing to write", not an error.
                skipped += 1
            else:
                with transaction(db_engine) as conn:
                    IndustryIndexValueRepository(conn).upsert_value(
                        industry_name,
                        trade_date=computed.trade_date,
                        value=computed.value,
                        constituents_used=computed.constituents_used,
                        constituents_missing=
                            computed.constituents_missing,
                        renormalized=computed.renormalized,
                        effective_rebalance_date=
                            computed.effective_rebalance_date,
                    )
                succeeded += 1
        except Exception as e:
            failed += 1
            logger.warning(
                "v1_industry_index_value %s date=%s failed: %s",
                industry_name, d, e, exc_info=True,
            )

        # Checkpoint progress every N dates so a crash here doesn't
        # lose more than `checkpoint_every` dates worth of work.
        if (i + 1) % checkpoint_every == 0:
            with transaction(db_engine) as conn:
                FundamentalsRunRepository(conn).update_progress(
                    run_id,
                    succeeded=succeeded, failed=failed, skipped=skipped,
                    progress_json={
                        "industry_name": industry_name,
                        "date_list": [d.isoformat() for d in date_list],
                        "cursor_index": i + 1,
                        "from_date": from_date.isoformat(),
                        "to_date": to_date.isoformat(),
                    },
                )

    # --- close the run ---
    status = "SUCCESS" if failed == 0 else "PARTIAL"
    with transaction(db_engine) as conn:
        FundamentalsRunRepository(conn).close(
            run_id, status=status,
            succeeded=succeeded, failed=failed, skipped=skipped,
        )

    logger.info(
        "populate_industry_index_value_for_industry %s [%s, %s]: "
        "succeeded=%d failed=%d skipped=%d (run_id=%d, status=%s)",
        industry_name, from_date, to_date,
        succeeded, failed, skipped, run_id, status,
    )
    return run_id


def populate_industry_index_value_for_all(
    db_engine: Engine,
    *,
    from_date: date | None = None,
    to_date: date | None = None,
    checkpoint_every: int = 50,
) -> dict:
    """Bulk-compute V1 industry index values for every ACTIVE industry.

    Iterates in alphabetical order (via IndustryIndexRepository.
    list_active). Per-industry error isolation: a failure on one
    industry doesn't abort the others.

    NOTE: This bulk variant does NOT support resume. If interrupted
    mid-way, the operator can look at fundamentals_run for any
    RUNNING v1_industry_index_value rows and call
    populate_industry_index_value_for_industry with resume_run_id
    for each. The upserts are idempotent so re-running the bulk
    variant after interruption is also safe (just wasteful — it
    will recompute already-done industries).

    Returns a report dict:
        {
          "from_date": str | None,
          "to_date": str | None,
          "succeeded": [(industry_name, run_id, num_value_rows), ...],
          "failed":    [(industry_name, error_message), ...],
          "skipped":   [(industry_name, reason), ...],
        }
    """
    with db_engine.connect() as conn:
        indices = IndustryIndexRepository(conn).list_active()

    succeeded: list[tuple[str, int, int]] = []
    failed: list[tuple[str, str]] = []
    skipped: list[tuple[str, str]] = []

    for idx in indices:
        try:
            run_id = populate_industry_index_value_for_industry(
                db_engine, idx.industry_name,
                from_date=from_date, to_date=to_date,
                checkpoint_every=checkpoint_every,
            )
            with db_engine.connect() as conn:
                cnt = len(
                    IndustryIndexValueRepository(conn).get_history(
                        idx.industry_name,
                    )
                )
            succeeded.append((idx.industry_name, run_id, cnt))
            logger.debug(
                "industry=%s OK (run_id=%d, total_rows=%d)",
                idx.industry_name, run_id, cnt,
            )
        except ValueError as e:
            skipped.append((idx.industry_name, str(e)))
            logger.info(
                "industry=%s skipped: %s", idx.industry_name, e,
            )
        except Exception as e:
            failed.append((idx.industry_name, str(e)))
            logger.warning(
                "industry=%s failed: %s",
                idx.industry_name, e, exc_info=True,
            )

    report = {
        "from_date":
            from_date.isoformat() if from_date is not None else None,
        "to_date":
            to_date.isoformat() if to_date is not None else None,
        "succeeded": succeeded,
        "failed": failed,
        "skipped": skipped,
    }
    logger.info(
        "populate_industry_index_value_for_all [%s, %s]: "
        "succeeded=%d failed=%d skipped=%d",
        from_date, to_date,
        len(succeeded), len(failed), len(skipped),
    )
    return report


# ====================================================================
# Step 7 — industry indicator state populator
# ====================================================================
#
# Reads industry_index_value daily rows, aggregates to IST-Monday
# weekly bars via the same daily_to_weekly helper used for stocks
# (Phase 1B unification), runs compute_state_primitives with
# synthetic_atr_for_index=False (true-range ATR), and upserts the
# resulting rows to industry_indicator_state.
#
# The aggregation is needed because industry_index_value rows are
# daily (one per trading day) but the indicator math runs on weekly
# bars. The legacy `run_indicator_state_for_industry` used the same
# pattern.
#
# No skip-if-recent guard. V1 treats every invocation as a full
# idempotent recompute.


def _build_industry_weekly_bars(
    history: list,
) -> list[OHLCVBar]:
    """Convert industry_index_value rows to IST-Monday-anchored
    weekly bars.

    Args:
        history: List of IndustryIndexValue records (oldest-first).
            Each has `.trade_date` and `.value`.

    Returns:
        Weekly OHLCVBar list. Open = first day's value in the week,
        high/low = max/min daily values that week, close = last
        day's value. Volume = bar count (synthetic).

    Empty input → empty output.
    """
    if not history:
        return []
    # Build pseudo-daily bars: H=L=O=C=value (no intra-day OHLC on
    # IIV). The daily_to_weekly aggregator will compute meaningful
    # weekly H/L as max/min across the days in each week.
    pseudo_daily_bars = [
        OHLCVBar(
            timestamp=datetime.combine(
                h.trade_date, time.min, tzinfo=_IST,
            ),
            open=float(h.value),
            high=float(h.value),
            low=float(h.value),
            close=float(h.value),
            volume=1.0,
        )
        for h in history
    ]
    return daily_to_weekly(
        pseudo_daily_bars,
        include_partial_last_week=True,
        tz="Asia/Kolkata",
    )


def populate_industry_indicator_state_for_industry(
    db_engine: Engine,
    industry_name: str,
    *,
    timeframe: str = "Weekly",
    min_bars: int = 30,
) -> int:
    """Compute V1 industry indicator state for one industry.

    Reads the full industry_index_value history, aggregates to weekly
    bars (IST Monday-anchored), runs compute_state_primitives, and
    upserts to industry_indicator_state.

    Args:
        industry_name: The index to compute.
        timeframe: Always 'Weekly' in V1 (column kept for future
            multi-timeframe support). Pass anything else and you
            get a ValueError.
        min_bars: Minimum weekly bars required to seed EMAs. Bars
            shorter than this raise ValueError.

    Returns:
        fundamentals_run.run_id.

    Raises:
        ValueError: industry doesn't exist, has no IIV rows, or
            has fewer than min_bars weekly bars after aggregation.
    """
    # Validate timeframe. V1 only supports Weekly for industry but
    # the column is kept for future expansion.
    if timeframe != "Weekly":
        raise ValueError(
            f"populate_industry_indicator_state_for_industry: "
            f"only 'Weekly' is supported for industry indices in V1, "
            f"got {timeframe!r}"
        )

    # Sanity: industry must exist as an index.
    with db_engine.connect() as conn:
        idx = IndustryIndexRepository(conn).get(industry_name)
    if idx is None:
        raise ValueError(
            f"populate_industry_indicator_state_for_industry: "
            f"industry {industry_name!r} does not exist"
        )

    # Read the full value history (oldest-first for the walk).
    with db_engine.connect() as conn:
        history = IndustryIndexValueRepository(conn).get_history(
            industry_name,
        )
    if not history:
        raise ValueError(
            f"populate_industry_indicator_state_for_industry: "
            f"industry {industry_name!r} has no value rows. Run "
            f"v1_industry_index_value first."
        )
    # get_history returns newest-first; the indicator walk needs
    # oldest-first.
    history = list(reversed(history))

    # Aggregate to weekly bars.
    weekly_bars = _build_industry_weekly_bars(history)
    if len(weekly_bars) < min_bars:
        raise ValueError(
            f"populate_industry_indicator_state_for_industry: "
            f"industry {industry_name!r} has only {len(weekly_bars)} "
            f"weekly bars after aggregation (need at least "
            f"{min_bars} to seed EMAs)"
        )

    # Build pandas Series for the compute layer.
    open_s = pd.Series(  # noqa: F841  (kept for parity / readability)
        [b.open for b in weekly_bars], dtype=float,
    )
    high_s = pd.Series([b.high for b in weekly_bars], dtype=float)
    low_s = pd.Series([b.low for b in weekly_bars], dtype=float)
    close_s = pd.Series([b.close for b in weekly_bars], dtype=float)
    volume_s = pd.Series(
        [b.volume for b in weekly_bars], dtype=float,
    )
    # The weekly bar's IST-Monday timestamp carries through to
    # industry_indicator_state.bar_timestamp 1:1. Both columns are
    # TIMESTAMPTZ.
    timestamps = pd.Series([b.timestamp for b in weekly_bars])

    # Compute core state — true-range ATR (Phase 1B unification).
    primitives = compute_state_primitives(
        close_s, volume_s, high_s, low_s,
        trade_dates=timestamps,
        synthetic_atr_for_index=False,
    )

    # Convert to dataclass list. computed_at is set on the runner
    # side so all rows of one run share the same audit timestamp.
    now = datetime.now(UTC)
    states = [
        IndustryIndicatorState(
            industry_name=industry_name,
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
            phase=PHASE_V1_INDUSTRY_INDICATOR_STATE,
            exchange="ALL",
            notes=(
                f"v1_industry_indicator_state for {industry_name} "
                f"tf={timeframe}"
            ),
        )
        IndustryIndicatorStateRepository(conn).upsert_many(states)
        FundamentalsRunRepository(conn).close(
            run_id, status="SUCCESS",
            succeeded=len(states), failed=0,
            skipped=(len(weekly_bars) - len(states)),
        )

    logger.info(
        "populate_industry_indicator_state_for_industry %s tf=%s: "
        "emitted=%d skipped=%d weekly_bars=%d daily_iiv_rows=%d",
        industry_name, timeframe,
        len(states), len(weekly_bars) - len(states),
        len(weekly_bars), len(history),
    )
    return run_id


def populate_industry_indicator_state_for_all(
    db_engine: Engine,
    *,
    timeframe: str = "Weekly",
    min_bars: int = 30,
) -> dict:
    """Bulk-compute V1 industry indicator state for every ACTIVE
    industry.

    Iterates in alphabetical order (IndustryIndexRepository.
    list_active). Per-industry error isolation: a failure on one
    industry doesn't abort the others.

    Industries with no IIV rows or insufficient weekly bars after
    aggregation are reported as `skipped` (not failed).

    Returns a report dict:
        {
          "timeframe": str,
          "succeeded": [(industry_name, run_id, num_rows), ...],
          "failed":    [(industry_name, error_message), ...],
          "skipped":   [(industry_name, reason), ...],
        }
    """
    with db_engine.connect() as conn:
        indices = IndustryIndexRepository(conn).list_active()

    succeeded: list[tuple[str, int, int]] = []
    failed: list[tuple[str, str]] = []
    skipped: list[tuple[str, str]] = []

    for idx in indices:
        try:
            run_id = populate_industry_indicator_state_for_industry(
                db_engine, idx.industry_name,
                timeframe=timeframe, min_bars=min_bars,
            )
            with db_engine.connect() as conn:
                cnt = IndustryIndicatorStateRepository(
                    conn,
                ).count_for_industry(idx.industry_name, timeframe)
            succeeded.append((idx.industry_name, run_id, cnt))
            logger.debug(
                "industry=%s tf=%s OK (run_id=%d, count=%d)",
                idx.industry_name, timeframe, run_id, cnt,
            )
        except ValueError as e:
            # ValueError covers "no IIV rows", "insufficient bars",
            # "industry doesn't exist". Treat as skipped, not failed
            # — the industry simply isn't ready yet.
            skipped.append((idx.industry_name, str(e)))
            logger.info(
                "industry=%s tf=%s skipped: %s",
                idx.industry_name, timeframe, e,
            )
        except Exception as e:
            failed.append((idx.industry_name, str(e)))
            logger.warning(
                "industry=%s tf=%s failed: %s",
                idx.industry_name, timeframe, e, exc_info=True,
            )

    report = {
        "timeframe": timeframe,
        "succeeded": succeeded,
        "failed": failed,
        "skipped": skipped,
    }
    logger.info(
        "populate_industry_indicator_state_for_all tf=%s: "
        "succeeded=%d failed=%d skipped=%d",
        timeframe,
        len(succeeded), len(failed), len(skipped),
    )
    return report


# ====================================================================
# Step 8 — industry indicator enrichment populator
# ====================================================================
#
# V1 industry-side mirror of Step 5. Reads industry_index_value daily
# rows, aggregates to IST-Monday weekly bars via
# `_build_industry_weekly_bars` (REUSED from Step 7), runs
# `compute_enrichment_primitives` with synthetic_atr_for_index=False
# (true-range ATR — matching Step 7), and upserts to
# industry_indicator_enrichment.
#
# Sequencing: industry_indicator_state must exist first (FK ON DELETE
# CASCADE from enrichment → state). The runner fail-fasts with a
# clear error if state has not been populated.
#
# Closes the V1 refactor.


def populate_industry_indicator_enrichment_for_industry(
    db_engine: Engine,
    industry_name: str,
    *,
    timeframe: str = "Weekly",
    min_bars: int = 30,
) -> int:
    """Compute V1 industry indicator enrichment for one industry.

    Reads industry_index_value history, aggregates to weekly bars,
    runs compute_enrichment_primitives, and upserts to
    industry_indicator_enrichment.

    Returns the fundamentals_run.run_id.

    Raises:
        ValueError: industry doesn't exist, has no IIV rows, has no
            matching state rows (Step 7 not yet run), or has fewer
            than min_bars weekly bars.
    """
    # Validate timeframe — V1 only supports Weekly.
    if timeframe != "Weekly":
        raise ValueError(
            f"populate_industry_indicator_enrichment_for_industry: "
            f"only 'Weekly' is supported for industry indices in V1, "
            f"got {timeframe!r}"
        )

    # Sanity: industry must exist.
    with db_engine.connect() as conn:
        idx = IndustryIndexRepository(conn).get(industry_name)
    if idx is None:
        raise ValueError(
            f"populate_industry_indicator_enrichment_for_industry: "
            f"industry {industry_name!r} does not exist"
        )

    # Sanity: state must have been populated first. The FK ON DELETE
    # CASCADE requires a state row per enrichment row; fail-fast for
    # a cleaner error than the IntegrityError that would otherwise
    # come from the upsert.
    with db_engine.connect() as conn:
        state_count = IndustryIndicatorStateRepository(
            conn,
        ).count_for_industry(industry_name, timeframe)
    if state_count == 0:
        raise ValueError(
            f"populate_industry_indicator_enrichment_for_industry: "
            f"industry {industry_name!r} tf={timeframe} has no "
            f"state rows. Run the state populator first (Step 7 "
            f"endpoint)."
        )

    # Read the full value history.
    with db_engine.connect() as conn:
        history = IndustryIndexValueRepository(conn).get_history(
            industry_name,
        )
    if not history:
        raise ValueError(
            f"populate_industry_indicator_enrichment_for_industry: "
            f"industry {industry_name!r} has no value rows. Run "
            f"v1_industry_index_value first."
        )
    history = list(reversed(history))  # oldest-first for the walk

    # Aggregate to weekly bars (reuse Step 7 helper).
    weekly_bars = _build_industry_weekly_bars(history)
    if len(weekly_bars) < min_bars:
        raise ValueError(
            f"populate_industry_indicator_enrichment_for_industry: "
            f"industry {industry_name!r} has only {len(weekly_bars)} "
            f"weekly bars after aggregation (need at least "
            f"{min_bars})"
        )

    # Build pandas Series for the compute layer.
    high_s = pd.Series([b.high for b in weekly_bars], dtype=float)
    low_s = pd.Series([b.low for b in weekly_bars], dtype=float)
    close_s = pd.Series([b.close for b in weekly_bars], dtype=float)
    volume_s = pd.Series(
        [b.volume for b in weekly_bars], dtype=float,
    )
    timestamps = pd.Series([b.timestamp for b in weekly_bars])

    # Compute enrichment (skips NaN-warmup bars with the EXACT same
    # condition as compute_state_primitives, so the FK is satisfied
    # for every emitted enrichment row).
    primitives = compute_enrichment_primitives(
        close_s, volume_s, high_s, low_s,
        trade_dates=timestamps,
        synthetic_atr_for_index=False,
    )

    # Convert to dataclass list. computed_at is set on the runner
    # side so all rows of one run share the same audit timestamp.
    now = datetime.now(UTC)
    enrichments = [
        IndustryIndicatorEnrichment(
            industry_name=industry_name,
            timeframe=timeframe,
            bar_timestamp=p["bar_timestamp"],
            pierces_upper_mid=p["pierces_upper_mid"],
            pierces_lower_mid=p["pierces_lower_mid"],
            dist_to_med_in_hl_units=p["dist_to_med_in_hl_units"],
            dist_to_long_in_hl_units=p["dist_to_long_in_hl_units"],
            dist_to_longest_in_hl_units=(
                p["dist_to_longest_in_hl_units"]
            ),
            mean_dist_to_med_in_hl_units=(
                p["mean_dist_to_med_in_hl_units"]
            ),
            mean_dist_to_long_in_hl_units=(
                p["mean_dist_to_long_in_hl_units"]
            ),
            mean_dist_to_longest_in_hl_units=(
                p["mean_dist_to_longest_in_hl_units"]
            ),
            bars_since_med_turn=p["bars_since_med_turn"],
            bars_since_long_turn=p["bars_since_long_turn"],
            bars_since_longest_turn=p["bars_since_longest_turn"],
            pct_change_since_med_turn=(
                p["pct_change_since_med_turn"]
            ),
            pct_change_since_long_turn=(
                p["pct_change_since_long_turn"]
            ),
            pct_change_since_longest_turn=(
                p["pct_change_since_longest_turn"]
            ),
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

    # Atomic write: one transaction, all rows or none.
    with transaction(db_engine) as conn:
        run_id = FundamentalsRunRepository(conn).open(
            phase=PHASE_V1_INDUSTRY_INDICATOR_ENRICHMENT,
            exchange="ALL",
            notes=(
                f"v1_industry_indicator_enrichment for "
                f"{industry_name} tf={timeframe}"
            ),
        )
        IndustryIndicatorEnrichmentRepository(conn).upsert_many(
            enrichments,
        )
        FundamentalsRunRepository(conn).close(
            run_id, status="SUCCESS",
            succeeded=len(enrichments), failed=0,
            skipped=(len(weekly_bars) - len(enrichments)),
        )

    logger.info(
        "populate_industry_indicator_enrichment_for_industry %s "
        "tf=%s: emitted=%d skipped=%d weekly_bars=%d "
        "daily_iiv_rows=%d",
        industry_name, timeframe,
        len(enrichments), len(weekly_bars) - len(enrichments),
        len(weekly_bars), len(history),
    )
    return run_id


def populate_industry_indicator_enrichment_for_all(
    db_engine: Engine,
    *,
    timeframe: str = "Weekly",
    min_bars: int = 30,
) -> dict:
    """Bulk-compute V1 industry indicator enrichment for every
    ACTIVE industry.

    Iterates in alphabetical order. Per-industry error isolation:
    a failure on one industry doesn't abort the others.

    Industries with no IIV rows, no state rows, or insufficient
    weekly bars after aggregation are reported as `skipped`
    (not `failed`).

    Returns a report dict:
        {
          "timeframe": str,
          "succeeded": [(industry_name, run_id, num_rows), ...],
          "failed":    [(industry_name, error_message), ...],
          "skipped":   [(industry_name, reason), ...],
        }
    """
    with db_engine.connect() as conn:
        indices = IndustryIndexRepository(conn).list_active()

    succeeded: list[tuple[str, int, int]] = []
    failed: list[tuple[str, str]] = []
    skipped: list[tuple[str, str]] = []

    for idx in indices:
        try:
            run_id = (
                populate_industry_indicator_enrichment_for_industry(
                    db_engine, idx.industry_name,
                    timeframe=timeframe, min_bars=min_bars,
                )
            )
            with db_engine.connect() as conn:
                cnt = IndustryIndicatorEnrichmentRepository(
                    conn,
                ).count_for_industry(idx.industry_name, timeframe)
            succeeded.append((idx.industry_name, run_id, cnt))
            logger.debug(
                "industry=%s tf=%s OK (run_id=%d, count=%d)",
                idx.industry_name, timeframe, run_id, cnt,
            )
        except ValueError as e:
            # ValueError covers "no state rows", "no IIV rows",
            # "insufficient bars", "industry doesn't exist". Treat
            # as skipped, not failed — the industry simply isn't
            # ready yet (likely Step 7 not yet run for it).
            skipped.append((idx.industry_name, str(e)))
            logger.info(
                "industry=%s tf=%s skipped: %s",
                idx.industry_name, timeframe, e,
            )
        except Exception as e:
            failed.append((idx.industry_name, str(e)))
            logger.warning(
                "industry=%s tf=%s failed: %s",
                idx.industry_name, timeframe, e, exc_info=True,
            )

    report = {
        "timeframe": timeframe,
        "succeeded": succeeded,
        "failed": failed,
        "skipped": skipped,
    }
    logger.info(
        "populate_industry_indicator_enrichment_for_all tf=%s: "
        "succeeded=%d failed=%d skipped=%d",
        timeframe,
        len(succeeded), len(failed), len(skipped),
    )
    return report
