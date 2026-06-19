"""Phase 1 runner: orchestrates `industry` refresh across an exchange.

Boundaries (same as trip_ema.etl.runner):
  - Doesn't open the DB engine — accepts one.
  - Doesn't decide WHEN to run — CLI, scheduler, or HTTP endpoint does.
  - DOES manage transactions, the fundamentals_run row lifecycle,
    progress checkpointing, and resumability.

Concurrency: deliberately serial. yfinance's Ticker.info is one
HTTP call per ticker with no batched form. Parallelizing inside the
runner would just get us rate-limited faster. The 2-second sleep
between calls is the policy; the runner is the place that enforces it.

Resumability:
  - Every 50 tickers, the runner writes a checkpoint to
    `fundamentals_run.progress_json` with the cursor (last symbol
    processed, count succeeded/failed/skipped, and the original
    ticker list).
  - On crash/SIGINT/rate-limit pause, the operator restarts with
    `--resume RUN_ID`. The runner reads the old `progress_json`,
    finds the cursor symbol in the original list, and picks up at
    the NEXT one. The original ticker list comes from the checkpoint
    rather than re-reading tickers because new tickers added since
    the run started shouldn't suddenly appear mid-resume.

Override handling:
  - Tickers with an `industry_override` row are skipped (count to
    `skipped`, not `failed`).
  - The `instrument_industry` upsert uses `upsert_unless_overridden`
    which has a NOT EXISTS guard at the SQL level — if someone sets
    an override CONCURRENTLY with a fetch, the write is dropped
    cleanly with no torn state.

Exit semantics:
  - status=SUCCESS  if failed==0
  - status=PARTIAL  if some succeeded AND some failed
  - status=FAILED   if everything failed (and at least one was tried)
  - status=RUNNING  is invalid as a close state; the runner closes
    on the way out of every code path including KeyboardInterrupt.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Callable, Optional

from sqlalchemy import Engine, text

from ..persistence import transaction
from ..persistence.fundamentals_repo import (
    FundamentalsRunRepository,
    IndustryOverrideRepository,
    InstrumentIndustryRepository,
    InstrumentSharesRepository,
)

from .industry_fetch import (
    IndustryFetchPermanent,
    IndustryFetchRateLimited,
    IndustryFetchTransient,
    IndustryInfo,
    fetch_industry,
)

# from .backfill_shares_fetch import (
#     HistoricalSharesFetchPermanent,
#     HistoricalSharesFetchRateLimited,
#     HistoricalSharesFetchTransient,
#     fetch_historical_shares,
# )

logger = logging.getLogger(__name__)


# ---------- Tunables ---------- #


# Per-ticker delay between yfinance calls. yfinance has been seen to
# tolerate ~30 requests/minute before rate-limiting; 2s is a
# comfortable margin. Tests override via the `request_delay_s` kwarg
# to avoid making the suite slow.
DEFAULT_REQUEST_DELAY_S = 2.0

# Longer cool-off when yfinance signals rate limit. Yahoo's rate-limit
# windows are short (~1 minute); 60s usually clears it.
RATE_LIMIT_COOLOFF_S = 60.0

# Checkpoint every N tickers. Small enough that a crash near the end
# of a long run doesn't lose hours of work; large enough that the
# checkpoint write isn't itself the bottleneck.
CHECKPOINT_EVERY = 50


# ---------- Public entry point ---------- #


def run_industry_refresh(
    db_engine: Engine,
    exchange: str,
    *,
    resume_run_id: Optional[int] = None,
    force: bool = False,
    request_delay_s: float = DEFAULT_REQUEST_DELAY_S,
    fetch_fn: Callable[[str, str], IndustryInfo] = fetch_industry,
) -> int:
    """Refresh industry classification for every active ticker on `exchange`.

    Returns the run_id of the fundamentals_run row.

    Parameters:
      db_engine — SQLAlchemy engine (caller-owned).
      exchange  — exchange name (must already be validated by caller).
      resume_run_id — if set, resume the named (still-RUNNING) run
                      from its progress_json checkpoint.
      force     — if False and a SUCCESS run for this (phase, exchange)
                  exists within the last 80 days, skip the run and
                  return that run_id. 80 days is "less than a quarter
                  but more than a quarter's worth of error bars".
      request_delay_s — sleep between yfinance calls (tests pass 0).
      fetch_fn  — injected for testability; defaults to the real
                  yfinance fetcher.
    """
    # ----- 0. Skip-if-recent guard ----- #
    if not force and resume_run_id is None:
        with db_engine.connect() as conn:
            last = FundamentalsRunRepository(conn).last_completed(
                phase="industry", exchange=exchange,
            )
        if last is not None and last.completed_at is not None:
            age = (datetime.now(timezone.utc) - last.completed_at).days
            if age < 80:
                logger.info(
                    "Skipping industry refresh for %s: last SUCCESS "
                    "run %d completed %d days ago (use force=True to override)",
                    exchange, last.run_id, age,
                )
                return last.run_id

    # ----- 1. Determine the ticker list ----- #
    if resume_run_id is not None:
        run_id, ticker_list, cursor_index = _resume_from_checkpoint(
            db_engine, resume_run_id,
        )
        logger.info(
            "Resuming industry refresh run %d at index %d/%d",
            run_id, cursor_index, len(ticker_list),
        )
    else:
        ticker_list = _load_active_tickers_for_exchange(db_engine, exchange)
        if not ticker_list:
            logger.warning(
                "No active tickers for exchange %s — skipping refresh",
                exchange,
            )
            with transaction(db_engine) as conn:
                run_id = FundamentalsRunRepository(conn).open(
                    phase="industry", exchange=exchange,
                    notes="No active tickers",
                )
                FundamentalsRunRepository(conn).close(
                    run_id, status="SUCCESS", succeeded=0, failed=0, skipped=0,
                )
            return run_id
        with transaction(db_engine) as conn:
            run_id = FundamentalsRunRepository(conn).open(
                phase="industry", exchange=exchange,
                notes=f"Refresh of {len(ticker_list)} active tickers",
            )
            # Initial checkpoint records the full ticker list so a
            # resumed run uses the SAME list (not whatever's active
            # at resume time).
            FundamentalsRunRepository(conn).update_progress(
                run_id,
                progress_json={
                    "ticker_list": [
                        {"id": t["id"], "symbol": t["symbol"]} for t in ticker_list
                    ],
                    "cursor_index": 0,
                },
            )
        cursor_index = 0

    # ----- 2. Load overrides (set, so the inner-loop check is O(1)) ----- #
    with db_engine.connect() as conn:
        override_ids = {
            ov.ticker_id
            for ov in IndustryOverrideRepository(conn).list_all()
        }

    # ----- 3. Main loop ----- #
    succeeded = 0
    failed = 0
    skipped = 0

    try:
        for idx in range(cursor_index, len(ticker_list)):
            t = ticker_list[idx]
            tid, symbol = t["id"], t["symbol"]
            # instrument_type drives the classification branch. Defaults
            # to "EQ" if a checkpoint from before migration 0007 is
            # being resumed (old checkpoints don't have this field).
            itype = t.get("instrument_type", "EQ")

            if tid in override_ids:
                skipped += 1
                continue

            # Two branches: ETFs get bucketed as 'ETF' regardless of
            # what yfinance returns (we still call yfinance to capture
            # the long name, which is the one useful field). Equities
            # get the full sector+industry classification path.
            if itype == "ETF":
                outcome = _classify_etf(
                    db_engine, tid, symbol, exchange, fetch_fn,
                )
            else:
                outcome = _classify_equity(
                    db_engine, tid, symbol, exchange, fetch_fn,
                    run_id, ticker_list, idx,
                    succeeded, failed, skipped,
                )

            if outcome == "succeeded":
                succeeded += 1
            elif outcome == "failed":
                failed += 1
            elif outcome == "skipped":
                skipped += 1

            # Checkpoint cadence.
            if (idx + 1) % CHECKPOINT_EVERY == 0:
                _checkpoint(db_engine, run_id, ticker_list, idx + 1,
                            succeeded, failed, skipped)
                logger.info(
                    "Checkpoint: %d/%d processed (s=%d f=%d sk=%d) on %s",
                    idx + 1, len(ticker_list),
                    succeeded, failed, skipped, exchange,
                )

            if request_delay_s > 0:
                time.sleep(request_delay_s)

    except KeyboardInterrupt:
        # Operator hit Ctrl-C — close out with current counts as
        # PARTIAL so they can resume. The progress_json was already
        # updated at the last checkpoint, so resume picks up there.
        logger.warning("KeyboardInterrupt — closing run %d as PARTIAL", run_id)
        with transaction(db_engine) as conn:
            FundamentalsRunRepository(conn).close(
                run_id, status="PARTIAL",
                succeeded=succeeded, failed=failed, skipped=skipped,
                notes="Interrupted by KeyboardInterrupt",
            )
        raise

    # ----- 4. Close out ----- #
    status = _terminal_status(succeeded, failed)
    with transaction(db_engine) as conn:
        FundamentalsRunRepository(conn).close(
            run_id, status=status,
            succeeded=succeeded, failed=failed, skipped=skipped,
        )
    logger.info(
        "Industry refresh run %d (%s) finished: status=%s s=%d f=%d sk=%d",
        run_id, exchange, status, succeeded, failed, skipped,
    )
    return run_id


# ---------- Helpers ---------- #


# ---------- Per-ticker classification helpers ---------- #


def _classify_equity(
    db_engine: Engine,
    tid: int,
    symbol: str,
    exchange: str,
    fetch_fn,
    run_id: int,
    ticker_list: list[dict],
    idx: int,
    succeeded: int,
    failed: int,
    skipped: int,
) -> str:
    """Classify one equity by calling yfinance and writing the
    sector+industry result. Returns 'succeeded' or 'failed'.

    Rate-limit handling: catches IndustryFetchRateLimited, persists
    progress to the checkpoint BEFORE the long cool-off (so a SIGKILL
    during sleep doesn't lose state), then retries the ticker once
    after the cool-off. A second rate-limit failure counts as
    'failed' and we move on.
    """
    try:
        info = fetch_fn(symbol, exchange)
        with transaction(db_engine) as conn:
            InstrumentIndustryRepository(conn).upsert_unless_overridden(
                tid,
                sector=info.sector,
                industry=info.industry,
                classification_source="YFINANCE",
            )
        return "succeeded"
    except IndustryFetchRateLimited as e:
        logger.warning(
            "Rate limited on %s (%s): sleeping %.1fs",
            symbol, e, RATE_LIMIT_COOLOFF_S,
        )
        _checkpoint(db_engine, run_id, ticker_list, idx,
                    succeeded, failed, skipped)
        time.sleep(RATE_LIMIT_COOLOFF_S)
        try:
            info = fetch_fn(symbol, exchange)
            with transaction(db_engine) as conn:
                InstrumentIndustryRepository(conn).upsert_unless_overridden(
                    tid,
                    sector=info.sector,
                    industry=info.industry,
                    classification_source="YFINANCE",
                )
            return "succeeded"
        except Exception as e2:
            logger.error(
                "Failed retry after rate-limit cool-off for %s: %s",
                symbol, e2,
            )
            return "failed"
    except (IndustryFetchTransient, IndustryFetchPermanent) as e:
        logger.warning("Failed to fetch %s: %s", symbol, e)
        return "failed"
    except Exception as e:
        # Belt-and-braces: any unexpected exception is logged and
        # counted, never fatal to the run.
        logger.exception("Unexpected error fetching %s: %s", symbol, e)
        return "failed"


def _classify_etf(
    db_engine: Engine,
    tid: int,
    symbol: str,
    exchange: str,
    fetch_fn,
) -> str:
    """Classify one ETF: write industry='ETF', sector='ETF', and
    capture the long_name from yfinance if available. Returns
    'succeeded' on the happy path, 'failed' only if the DB write
    itself fails.

    The yfinance call is best-effort. If it fails (rate limit,
    timeout, ticker unknown), we still write the ETF bucket — just
    without a long_name. ETFs are categorical certainty: we know
    they're ETFs because tickers.instrument_type says so; yfinance
    is just enriching with a human-readable label.

    No rate-limit cool-off and retry like the equity branch: ETFs
    are a small fraction of the universe, the long_name isn't
    critical data, and we don't want a yfinance hiccup to stall
    the whole refresh for 60 seconds per affected ETF.
    """
    long_name = None
    try:
        # We use the same fetch_fn that returns IndustryInfo. For
        # ETFs that resolve, IndustryInfo will typically have empty
        # sector/industry and the long-name needs to be pulled
        # separately. The fetch_fn doesn't currently return longName,
        # so we need a separate path. Lightweight: read yfinance
        # directly here. Falls back to None silently on any error.
        long_name = _try_fetch_etf_long_name(symbol, exchange)
    except Exception as e:
        # Logged but not fatal — we'll still classify as ETF.
        logger.debug("ETF long-name fetch failed for %s: %s", symbol, e)

    try:
        with transaction(db_engine) as conn:
            InstrumentIndustryRepository(conn).upsert_etf(
                tid, etf_long_name=long_name,
            )
        return "succeeded"
    except Exception as e:
        logger.exception("DB write failed for ETF %s: %s", symbol, e)
        return "failed"


def _try_fetch_etf_long_name(symbol: str, exchange: str):
    """Pull the longName field from yfinance for an ETF. Returns
    None if anything goes wrong.

    Isolated function so tests can patch it without going through
    the equity fetch_fn injection point. Lazy-imports the equity
    fetcher's internal _fetch_yfinance_info to share its plumbing
    and exception handling.
    """
    try:
        from .industry_fetch import _fetch_yfinance_info
        from ..etl.exchanges import yfinance_symbol
        info = _fetch_yfinance_info(yfinance_symbol(symbol, exchange))
        ln = (info or {}).get("longName")
        if ln and str(ln).strip():
            return str(ln).strip()
        return None
    except Exception:
        return None


def _load_active_tickers_for_exchange(
    db_engine: Engine, exchange: str,
) -> list[dict]:
    """Return [{id, symbol, instrument_type}, ...] for active tickers
    on `exchange`.

    Returns dicts (not domain objects) because progress_json needs to
    JSON-serialize this list and dicts are JSON-native.

    instrument_type is included so the main loop can branch between
    yfinance fetch (for EQ) and direct ETF classification (for ETF)
    without an extra DB query per ticker.
    """
    with db_engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT ticker_id, symbol, instrument_type
            FROM tickers
            WHERE exchange = :ex AND is_active = TRUE
              AND delisted_at IS NULL
            ORDER BY symbol
        """), {"ex": exchange})
        return [
            {
                "id": int(r[0]),
                "symbol": r[1],
                "instrument_type": r[2] or "EQ",
            }
            for r in rows
        ]


def _resume_from_checkpoint(
    db_engine: Engine, run_id: int, *, expected_phase: str = "industry",
) -> tuple[int, list[dict], int]:
    """Read the previous run's checkpoint and return (run_id,
    ticker_list, cursor_index) to resume from.

    Raises ValueError if the run doesn't exist, is in a terminal
    state, or has no checkpoint.
    """
    with db_engine.connect() as conn:
        run = FundamentalsRunRepository(conn).get(run_id)
    if run is None:
        raise ValueError(f"Run {run_id} does not exist")
    if run.status != "RUNNING" and run.status != "PARTIAL":
        raise ValueError(
            f"Run {run_id} is in terminal status {run.status} — "
            f"can't resume. Start a new run with --force instead."
        )
    if run.phase != expected_phase:
        raise ValueError(
            f"Run {run_id} is for phase {run.phase!r}, "
            f"not {expected_phase!r}"
        )
    pj = run.progress_json or {}
    ticker_list = pj.get("ticker_list")
    cursor_index = int(pj.get("cursor_index", 0))
    if not ticker_list:
        raise ValueError(
            f"Run {run_id} has no checkpoint — was it ever started?"
        )
    # If the resumed run was previously closed as PARTIAL, reopen it.
    # (Otherwise the close() at the end would fail the status check.)
    if run.status == "PARTIAL":
        with transaction(db_engine) as conn:
            conn.execute(
                text("UPDATE fundamentals_run SET status='RUNNING', completed_at=NULL "
                     "WHERE run_id = :rid"),
                {"rid": run_id},
            )
    return run_id, ticker_list, cursor_index


def _checkpoint(
    db_engine: Engine,
    run_id: int,
    ticker_list: list[dict],
    next_index: int,
    succeeded: int,
    failed: int,
    skipped: int,
) -> None:
    """Persist progress. cursor_index points at the NEXT item to
    process — so resume picks up there, not at next_index - 1."""
    with transaction(db_engine) as conn:
        FundamentalsRunRepository(conn).update_progress(
            run_id,
            succeeded=succeeded, failed=failed, skipped=skipped,
            progress_json={
                "ticker_list": ticker_list,
                "cursor_index": next_index,
            },
        )


def _terminal_status(succeeded: int, failed: int) -> str:
    if failed == 0:
        return "SUCCESS"
    if succeeded == 0:
        return "FAILED"
    return "PARTIAL"


# ============================================================
# Phase 2 — shares-outstanding refresh
# ============================================================
#
# Mirrors run_industry_refresh's shape: serial yfinance calls,
# checkpointed every 50 tickers, resumable via progress_json,
# fundamentals_run row tracks the run. The differences from the
# industry runner are:
#
#   - No instrument-type branching. ETFs DO have sharesOutstanding
#     (it's the count of ETF units issued, which is meaningful for
#     AUM analytics); we fetch and store it the same way.
#
#   - No operator-override skip mechanism. There's no
#     "shares_outstanding_override" table because the override path
#     for shares is "insert a row with shares_source='MANUAL_OVERRIDE'"
#     — the time-series shape already accommodates operator-set
#     values without a separate override table.
#
#   - One row per refresh (time series), not upsert-in-place. The
#     `as_of_date` is today's UTC date when the runner is invoked.
#
# Calls the existing _checkpoint / _resume_from_checkpoint /
# _terminal_status helpers from above, with expected_phase='shares'.


def run_shares_refresh(
    db_engine: Engine,
    exchange: str,
    *,
    resume_run_id: Optional[int] = None,
    force: bool = False,
    request_delay_s: float = DEFAULT_REQUEST_DELAY_S,
    fetch_fn=None,
) -> int:
    """Refresh shares-outstanding for every active ticker on `exchange`.

    Inserts ONE new instrument_shares row per ticker with
    `as_of_date = today` (UTC). The composite PK on (ticker_id,
    as_of_date) means re-running on the same day is idempotent
    (UPDATE-on-conflict via the repo's insert_snapshot).

    Returns the run_id of the fundamentals_run row.

    Parameters:
      db_engine, exchange — as run_industry_refresh.
      resume_run_id — resume from a PARTIAL run's checkpoint.
      force — bypass the 80-day skip-if-recent guard.
      request_delay_s — sleep between yfinance calls.
      fetch_fn — injected for testability; defaults to the real
                  fetch_shares from shares_fetch.
    """
    from .shares_fetch import (
        SharesFetchPermanent,
        SharesFetchRateLimited,
        SharesFetchTransient,
        fetch_shares,
    )

    if fetch_fn is None:
        fetch_fn = fetch_shares

    today = datetime.now(timezone.utc).date()

    # ----- 0. Skip-if-recent guard ----- #
    if not force and resume_run_id is None:
        with db_engine.connect() as conn:
            last = FundamentalsRunRepository(conn).last_completed(
                phase="shares", exchange=exchange,
            )
        if last is not None and last.completed_at is not None:
            age = (datetime.now(timezone.utc) - last.completed_at).days
            if age < 80:
                logger.info(
                    "Skipping shares refresh for %s: last SUCCESS "
                    "run %d completed %d days ago (use force=True to override)",
                    exchange, last.run_id, age,
                )
                return last.run_id

    # ----- 1. Determine the ticker list ----- #
    if resume_run_id is not None:
        run_id, ticker_list, cursor_index = _resume_from_checkpoint(
            db_engine, resume_run_id, expected_phase="shares",
        )
        logger.info(
            "Resuming shares refresh run %d at index %d/%d",
            run_id, cursor_index, len(ticker_list),
        )
    else:
        ticker_list = _load_active_tickers_for_exchange(db_engine, exchange)
        if not ticker_list:
            logger.warning(
                "No active tickers for exchange %s — skipping shares refresh",
                exchange,
            )
            with transaction(db_engine) as conn:
                run_id = FundamentalsRunRepository(conn).open(
                    phase="shares", exchange=exchange,
                    notes="No active tickers",
                )
                FundamentalsRunRepository(conn).close(
                    run_id, status="SUCCESS", succeeded=0, failed=0, skipped=0,
                )
            return run_id
        with transaction(db_engine) as conn:
            run_id = FundamentalsRunRepository(conn).open(
                phase="shares", exchange=exchange,
                notes=f"Shares refresh of {len(ticker_list)} active tickers",
            )
            FundamentalsRunRepository(conn).update_progress(
                run_id,
                progress_json={
                    "ticker_list": [
                        {"id": t["id"], "symbol": t["symbol"],
                         "instrument_type": t.get("instrument_type", "EQ")}
                        for t in ticker_list
                    ],
                    "cursor_index": 0,
                },
            )
        cursor_index = 0

    # ----- 2. Main loop ----- #
    succeeded = 0
    failed = 0
    skipped = 0  # always 0 for shares (no override table)

    try:
        for idx in range(cursor_index, len(ticker_list)):
            t = ticker_list[idx]
            tid, symbol = t["id"], t["symbol"]

            try:
                info = fetch_fn(symbol, exchange)
                with transaction(db_engine) as conn:
                    InstrumentSharesRepository(conn).insert_snapshot(
                        tid,
                        as_of_date=today,
                        shares_outstanding=info.shares_outstanding,
                        shares_source=info.source,
                    )
                succeeded += 1
            except SharesFetchRateLimited as e:
                logger.warning(
                    "Rate limited on %s (%s): sleeping %.1fs",
                    symbol, e, RATE_LIMIT_COOLOFF_S,
                )
                _checkpoint(db_engine, run_id, ticker_list, idx,
                            succeeded, failed, skipped)
                time.sleep(RATE_LIMIT_COOLOFF_S)
                # Retry once after cool-off; if it still fails, count
                # as failed and move on.
                try:
                    info = fetch_fn(symbol, exchange)
                    with transaction(db_engine) as conn:
                        InstrumentSharesRepository(conn).insert_snapshot(
                            tid,
                            as_of_date=today,
                            shares_outstanding=info.shares_outstanding,
                            shares_source=info.source,
                        )
                    succeeded += 1
                except Exception as e2:
                    logger.error(
                        "Failed retry after rate-limit cool-off for %s: %s",
                        symbol, e2,
                    )
                    failed += 1
            except (SharesFetchTransient, SharesFetchPermanent) as e:
                logger.warning("Failed to fetch shares for %s: %s", symbol, e)
                failed += 1
            except Exception as e:
                logger.exception("Unexpected error fetching shares for %s: %s", symbol, e)
                failed += 1

            if (idx + 1) % CHECKPOINT_EVERY == 0:
                _checkpoint(db_engine, run_id, ticker_list, idx + 1,
                            succeeded, failed, skipped)
                logger.info(
                    "Checkpoint: %d/%d processed (s=%d f=%d) on %s",
                    idx + 1, len(ticker_list),
                    succeeded, failed, exchange,
                )

            if request_delay_s > 0:
                time.sleep(request_delay_s)

    except KeyboardInterrupt:
        logger.warning("KeyboardInterrupt — closing shares run %d as PARTIAL", run_id)
        with transaction(db_engine) as conn:
            FundamentalsRunRepository(conn).close(
                run_id, status="PARTIAL",
                succeeded=succeeded, failed=failed, skipped=skipped,
                notes="Interrupted by KeyboardInterrupt",
            )
        raise

    # ----- 3. Close out ----- #
    status = _terminal_status(succeeded, failed)
    with transaction(db_engine) as conn:
        FundamentalsRunRepository(conn).close(
            run_id, status=status,
            succeeded=succeeded, failed=failed, skipped=skipped,
        )
    logger.info(
        "Shares refresh run %d (%s) finished: status=%s s=%d f=%d",
        run_id, exchange, status, succeeded, failed,
    )
    return run_id


def run_shares_backfill(
        db_engine,
        exchange: str,
        *,
        start_date,  # date
        resume_run_id=None,  # Optional[int]
        force: bool = False,
        request_delay_s: float = None,  # uses DEFAULT_REQUEST_DELAY_S
        fetch_fn=None,
) -> int:
    """Backfill historical shares-outstanding for every active ticker
    on `exchange`. For each ticker, yfinance's `get_shares_full(start)`
    returns a time series of filing-date snapshots; the runner inserts
    one `instrument_shares` row per snapshot.

    Once this populates the table, `compute-market-cap` (with its
    existing `get_as_of` asof lookup) automatically uses the
    correct historical shares for each trade_date, yielding COMPUTED
    rows over the full backfill range.

    Same exit semantics as run_shares_refresh:
      SUCCESS — all tickers fetched at least one snapshot
      PARTIAL — some succeeded, some failed
      FAILED  — every attempt failed
      RUNNING is never a close state.

    Parameters:
      db_engine, exchange — as run_shares_refresh.
      start_date — earliest date to backfill. yfinance typically
        has snapshots back ~16 months for Indian tickers; older
        dates return whatever's available without erroring.
      resume_run_id — resume from a PARTIAL run's checkpoint.
      force — bypass the 80-day skip-if-recent guard. The guard
        keys off phase='shares_backfill' so it's independent of
        the regular shares-refresh guard.
      request_delay_s — sleep between yfinance calls.
      fetch_fn — injected for testability; defaults to the real
        fetch_historical_shares from backfill_shares_fetch.

    Returns the run_id of the fundamentals_run row.
    """
    from .backfill_shares_fetch import (
        HistoricalSharesFetchPermanent,
        HistoricalSharesFetchRateLimited,
        HistoricalSharesFetchTransient,
        fetch_historical_shares,
    )
    # These are already imported in runners.py at module level:
    #   logger, time, datetime, timezone
    #   FundamentalsRunRepository, InstrumentSharesRepository, transaction
    #   DEFAULT_REQUEST_DELAY_S, RATE_LIMIT_COOLOFF_S, CHECKPOINT_EVERY
    #   _load_active_tickers_for_exchange, _resume_from_checkpoint,
    #   _checkpoint, _terminal_status

    if fetch_fn is None:
        fetch_fn = fetch_historical_shares
    if request_delay_s is None:
        request_delay_s = DEFAULT_REQUEST_DELAY_S  # noqa: F821

    # ----- 0. Skip-if-recent guard ----- #
    if not force and resume_run_id is None:
        with db_engine.connect() as conn:
            last = FundamentalsRunRepository(conn).last_completed(  # noqa: F821
                phase="shares_backfill", exchange=exchange,
            )
        if last is not None and last.completed_at is not None:
            age = (datetime.now(timezone.utc) - last.completed_at).days  # noqa: F821
            if age < 80:
                logger.info(  # noqa: F821
                    "Skipping shares backfill for %s: last SUCCESS "
                    "run %d completed %d days ago (use force=True to override)",
                    exchange, last.run_id, age,
                )
                return last.run_id

    # ----- 1. Determine the ticker list ----- #
    if resume_run_id is not None:
        run_id, ticker_list, cursor_index = _resume_from_checkpoint(  # noqa: F821
            db_engine, resume_run_id, expected_phase="shares_backfill",
        )
        logger.info(  # noqa: F821
            "Resuming shares backfill run %d at index %d/%d",
            run_id, cursor_index, len(ticker_list),
        )
    else:
        ticker_list = _load_active_tickers_for_exchange(db_engine, exchange)  # noqa: F821
        if not ticker_list:
            logger.warning(  # noqa: F821
                "No active tickers for exchange %s — skipping shares backfill",
                exchange,
            )
            with transaction(db_engine) as conn:  # noqa: F821
                run_id = FundamentalsRunRepository(conn).open(  # noqa: F821
                    phase="shares_backfill", exchange=exchange,
                    notes=f"No active tickers (start={start_date.isoformat()})",
                )
                FundamentalsRunRepository(conn).close(  # noqa: F821
                    run_id, status="SUCCESS", succeeded=0, failed=0, skipped=0,
                )
            return run_id
        with transaction(db_engine) as conn:  # noqa: F821
            run_id = FundamentalsRunRepository(conn).open(  # noqa: F821
                phase="shares_backfill", exchange=exchange,
                notes=(
                    f"Shares backfill of {len(ticker_list)} tickers "
                    f"from {start_date.isoformat()}"
                ),
            )
            FundamentalsRunRepository(conn).update_progress(  # noqa: F821
                run_id,
                progress_json={
                    "ticker_list": [
                        {"id": t["id"], "symbol": t["symbol"],
                         "instrument_type": t.get("instrument_type", "EQ")}
                        for t in ticker_list
                    ],
                    "cursor_index": 0,
                    "start_date": start_date.isoformat(),
                },
            )
        cursor_index = 0

    # ----- 2. Main loop ----- #
    succeeded = 0
    failed = 0
    skipped = 0
    snapshots_total = 0  # tracked for the log line

    try:
        for idx in range(cursor_index, len(ticker_list)):
            t = ticker_list[idx]
            tid, symbol = t["id"], t["symbol"]

            try:
                snapshots = fetch_fn(symbol, exchange, start_date)
                # Insert all snapshots in one transaction per ticker.
                # Each upsert is idempotent on (ticker_id, as_of_date)
                # so re-running over the same range is safe.
                with transaction(db_engine) as conn:  # noqa: F821
                    repo = InstrumentSharesRepository(conn)  # noqa: F821
                    for snap in snapshots:
                        repo.insert_snapshot(
                            tid,
                            as_of_date=snap.as_of_date,
                            shares_outstanding=snap.shares_outstanding,
                            shares_source="YFINANCE",
                        )
                succeeded += 1
                snapshots_total += len(snapshots)
            except HistoricalSharesFetchRateLimited as e:
                logger.warning(  # noqa: F821
                    "Rate limited on %s (%s): sleeping %.1fs",
                    symbol, e, RATE_LIMIT_COOLOFF_S,  # noqa: F821
                )
                _checkpoint(db_engine, run_id, ticker_list, idx,  # noqa: F821
                            succeeded, failed, skipped)
                time.sleep(RATE_LIMIT_COOLOFF_S)  # noqa: F821
                # Retry once after cool-off.
                try:
                    snapshots = fetch_fn(symbol, exchange, start_date)
                    with transaction(db_engine) as conn:  # noqa: F821
                        repo = InstrumentSharesRepository(conn)  # noqa: F821
                        for snap in snapshots:
                            repo.insert_snapshot(
                                tid,
                                as_of_date=snap.as_of_date,
                                shares_outstanding=snap.shares_outstanding,
                                shares_source="YFINANCE",
                            )
                    succeeded += 1
                    snapshots_total += len(snapshots)
                except Exception as e2:
                    logger.error(  # noqa: F821
                        "Failed retry after rate-limit cool-off for %s: %s",
                        symbol, e2,
                    )
                    failed += 1
            except (
                    HistoricalSharesFetchTransient,
                    HistoricalSharesFetchPermanent,
            ) as e:
                logger.warning(  # noqa: F821
                    "Failed to backfill shares for %s: %s", symbol, e,
                )
                failed += 1
            except Exception as e:
                logger.exception(  # noqa: F821
                    "Unexpected error backfilling shares for %s: %s",
                    symbol, e,
                )
                failed += 1

            if (idx + 1) % CHECKPOINT_EVERY == 0:  # noqa: F821
                _checkpoint(db_engine, run_id, ticker_list, idx + 1,  # noqa: F821
                            succeeded, failed, skipped)
                logger.info(  # noqa: F821
                    "Checkpoint: %d/%d processed (s=%d f=%d snaps=%d) on %s",
                    idx + 1, len(ticker_list),
                    succeeded, failed, snapshots_total, exchange,
                )

            if request_delay_s > 0:
                time.sleep(request_delay_s)  # noqa: F821

    except KeyboardInterrupt:
        logger.warning(  # noqa: F821
            "KeyboardInterrupt — closing shares backfill run %d as PARTIAL",
            run_id,
        )
        with transaction(db_engine) as conn:  # noqa: F821
            FundamentalsRunRepository(conn).close(  # noqa: F821
                run_id, status="PARTIAL",
                succeeded=succeeded, failed=failed, skipped=skipped,
                notes=f"Interrupted by KeyboardInterrupt; snapshots={snapshots_total}",
            )
        raise

    # ----- 3. Close out ----- #
    status = _terminal_status(succeeded, failed)  # noqa: F821
    with transaction(db_engine) as conn:  # noqa: F821
        FundamentalsRunRepository(conn).close(  # noqa: F821
            run_id, status=status,
            succeeded=succeeded, failed=failed, skipped=skipped,
            notes=f"snapshots_written={snapshots_total}",
        )
    logger.info(  # noqa: F821
        "Shares backfill run %d (%s) finished: status=%s s=%d f=%d snaps=%d",
        run_id, exchange, status, succeeded, failed, snapshots_total,
    )
    return run_id

# ============================================================
# Phase 3 — quarterly financials refresh
# ============================================================
#
# Same shape as run_shares_refresh: serial yfinance calls,
# checkpointed every 50 tickers, resumable via progress_json.
# Differences from shares:
#
#   - Each ticker returns 0-4 quarters (a list), not a scalar. The
#     runner writes one row per quarter for each ticker.
#
#   - "Success" means "the fetcher returned at least one quarter
#     with at least one of revenue/ebitda populated". Quarters with
#     both fields NULL are filtered out by the fetcher; if the
#     fetcher returns the empty list, that's a permanent failure
#     for this refresh of this ticker.
#
#   - ETFs DO get queried. yfinance has financials for ETFs only
#     occasionally (essentially never for Indian ETFs based on the
#     probe), but the runner doesn't skip them — same uniform path
#     for all tickers. ETFs will just produce a high failure rate
#     in the ETF row of fundamentals-status, which is cosmetically
#     ugly but functionally fine. (Same call we made on shares.)


def run_financials_refresh(
    db_engine: Engine,
    exchange: str,
    *,
    resume_run_id: Optional[int] = None,
    force: bool = False,
    request_delay_s: float = DEFAULT_REQUEST_DELAY_S,
    fetch_fn=None,
    max_quarters: int = 4,
) -> int:
    """Refresh quarterly financials for every active ticker on `exchange`.

    For each ticker, fetches up to `max_quarters` of revenue + EBITDA
    from yfinance and inserts one quarterly_financials row per quarter.
    The composite PK (ticker_id, fiscal_quarter, fetched_at) means
    re-running on the same day inserts new rows (not idempotent at
    DB level) — the runner relies on the skip-if-recent guard for
    de-duplication.

    Returns run_id.
    """
    from .financials_fetch import (
        FinancialsFetchPermanent,
        FinancialsFetchRateLimited,
        FinancialsFetchTransient,
        fetch_quarterly_financials,
    )
    from ..persistence.fundamentals_repo import QuarterlyFinancialsRepository

    if fetch_fn is None:
        fetch_fn = fetch_quarterly_financials

    # ----- 0. Skip-if-recent guard ----- #
    if not force and resume_run_id is None:
        with db_engine.connect() as conn:
            last = FundamentalsRunRepository(conn).last_completed(
                phase="financials", exchange=exchange,
            )
        if last is not None and last.completed_at is not None:
            age = (datetime.now(timezone.utc) - last.completed_at).days
            if age < 80:
                logger.info(
                    "Skipping financials refresh for %s: last SUCCESS "
                    "run %d completed %d days ago (use force=True to override)",
                    exchange, last.run_id, age,
                )
                return last.run_id

    # ----- 1. Determine ticker list ----- #
    if resume_run_id is not None:
        run_id, ticker_list, cursor_index = _resume_from_checkpoint(
            db_engine, resume_run_id, expected_phase="financials",
        )
        logger.info(
            "Resuming financials refresh run %d at index %d/%d",
            run_id, cursor_index, len(ticker_list),
        )
    else:
        ticker_list = _load_active_tickers_for_exchange(db_engine, exchange)
        if not ticker_list:
            logger.warning(
                "No active tickers for exchange %s — skipping financials refresh",
                exchange,
            )
            with transaction(db_engine) as conn:
                run_id = FundamentalsRunRepository(conn).open(
                    phase="financials", exchange=exchange,
                    notes="No active tickers",
                )
                FundamentalsRunRepository(conn).close(
                    run_id, status="SUCCESS",
                    succeeded=0, failed=0, skipped=0,
                )
            return run_id
        with transaction(db_engine) as conn:
            run_id = FundamentalsRunRepository(conn).open(
                phase="financials", exchange=exchange,
                notes=f"Financials refresh of {len(ticker_list)} active tickers",
            )
            FundamentalsRunRepository(conn).update_progress(
                run_id,
                progress_json={
                    "ticker_list": [
                        {"id": t["id"], "symbol": t["symbol"],
                         "instrument_type": t.get("instrument_type", "EQ")}
                        for t in ticker_list
                    ],
                    "cursor_index": 0,
                },
            )
        cursor_index = 0

    # ----- 2. Main loop ----- #
    succeeded = 0
    failed = 0
    skipped = 0

    try:
        for idx in range(cursor_index, len(ticker_list)):
            t = ticker_list[idx]
            tid, symbol = t["id"], t["symbol"]

            try:
                quarters = fetch_fn(symbol, exchange, max_quarters=max_quarters)
                with transaction(db_engine) as conn:
                    repo = QuarterlyFinancialsRepository(conn)
                    for q in quarters:
                        repo.insert_snapshot(
                            tid,
                            fiscal_quarter=q.fiscal_quarter,
                            revenue=q.revenue,
                            ebitda=q.ebitda,
                            currency=q.currency,
                            source="YFINANCE",
                        )
                succeeded += 1
            except FinancialsFetchRateLimited as e:
                logger.warning(
                    "Rate limited on %s (%s): sleeping %.1fs",
                    symbol, e, RATE_LIMIT_COOLOFF_S,
                )
                _checkpoint(db_engine, run_id, ticker_list, idx,
                            succeeded, failed, skipped)
                time.sleep(RATE_LIMIT_COOLOFF_S)
                try:
                    quarters = fetch_fn(symbol, exchange, max_quarters=max_quarters)
                    with transaction(db_engine) as conn:
                        repo = QuarterlyFinancialsRepository(conn)
                        for q in quarters:
                            repo.insert_snapshot(
                                tid,
                                fiscal_quarter=q.fiscal_quarter,
                                revenue=q.revenue,
                                ebitda=q.ebitda,
                                currency=q.currency,
                                source="YFINANCE",
                            )
                    succeeded += 1
                except Exception as e2:
                    logger.error(
                        "Failed retry after rate-limit cool-off for %s: %s",
                        symbol, e2,
                    )
                    failed += 1
            except (FinancialsFetchTransient, FinancialsFetchPermanent) as e:
                logger.warning("Failed to fetch financials for %s: %s", symbol, e)
                failed += 1
            except Exception as e:
                logger.exception(
                    "Unexpected error fetching financials for %s: %s",
                    symbol, e,
                )
                failed += 1

            if (idx + 1) % CHECKPOINT_EVERY == 0:
                _checkpoint(db_engine, run_id, ticker_list, idx + 1,
                            succeeded, failed, skipped)
                logger.info(
                    "Checkpoint: %d/%d processed (s=%d f=%d) on %s",
                    idx + 1, len(ticker_list),
                    succeeded, failed, exchange,
                )

            if request_delay_s > 0:
                time.sleep(request_delay_s)

    except KeyboardInterrupt:
        logger.warning(
            "KeyboardInterrupt — closing financials run %d as PARTIAL", run_id,
        )
        with transaction(db_engine) as conn:
            FundamentalsRunRepository(conn).close(
                run_id, status="PARTIAL",
                succeeded=succeeded, failed=failed, skipped=skipped,
                notes="Interrupted by KeyboardInterrupt",
            )
        raise

    # ----- 3. Close out ----- #
    status = _terminal_status(succeeded, failed)
    with transaction(db_engine) as conn:
        FundamentalsRunRepository(conn).close(
            run_id, status=status,
            succeeded=succeeded, failed=failed, skipped=skipped,
        )
    logger.info(
        "Financials refresh run %d (%s) finished: status=%s s=%d f=%d",
        run_id, exchange, status, succeeded, failed,
    )
    return run_id


# ============================================================
# Phase 4A — market cap backfill
# ============================================================
#
# Walks the date range and writes daily_market_cap rows. Two
# distinct paths:
#
#   - For trade_dates within our local data window (i.e. where
#     ohlcv_bars × instrument_shares both have data), uses the
#     market_cap_compute module's compute_market_cap_for_date.
#     No yfinance HTTP needed for these.
#
#   - For trade_dates older than our local data, uses the
#     market_cap_fetch module's fetch_historical_mcap_for_ticker
#     to pull from yfinance. One HTTP per ticker covers the entire
#     historical window for that ticker, so we iterate tickers
#     not dates — much cheaper than per-date yfinance calls.
#
# Per-ticker outer loop on the historical path also means a
# resumed run picks up at "RELIANCE done, TCS half-done" rather
# than mid-date, which matches the resume semantics of the other
# fundamentals runners.


def run_market_cap_backfill(
    db_engine: Engine,
    exchange: str,
    *,
    from_date: "date",
    to_date: "date",
    resume_run_id: Optional[int] = None,
    force: bool = False,
    request_delay_s: float = DEFAULT_REQUEST_DELAY_S,
    hist_fetch_fn=None,
) -> int:
    """Backfill daily_market_cap for [from_date, to_date] on `exchange`.

    Strategy:
      1. Iterate active EQ tickers on `exchange`.
      2. For each ticker, one yfinance HTTP returns all historical
         (close, shares) points in range. Write them to
         daily_market_cap with source=COMPUTED.
      3. The resume cursor advances per-ticker.

    The "local data" path (compute_market_cap_for_date) is NOT
    used here — the backfill path always hits yfinance because
    yfinance has historical shares anyway and the unified path is
    simpler. The local path is reserved for the forward-only
    daily compute (next-session work).

    Returns run_id.
    """
    from .market_cap_fetch import (
        HistoricalMcapFetchPermanent,
        HistoricalMcapFetchRateLimited,
        HistoricalMcapFetchTransient,
        fetch_historical_mcap_for_ticker,
    )
    from ..persistence.fundamentals_repo import DailyMarketCapRepository

    if hist_fetch_fn is None:
        hist_fetch_fn = fetch_historical_mcap_for_ticker

    if from_date > to_date:
        raise ValueError(
            f"from_date {from_date} must be <= to_date {to_date}"
        )

    # ----- 0. Skip-if-recent guard ----- #
    if not force and resume_run_id is None:
        with db_engine.connect() as conn:
            last = FundamentalsRunRepository(conn).last_completed(
                phase="market_cap", exchange=exchange,
            )
        if last is not None and last.completed_at is not None:
            age = (datetime.now(timezone.utc) - last.completed_at).days
            if age < 80:
                logger.info(
                    "Skipping market cap backfill for %s: last SUCCESS "
                    "run %d completed %d days ago (use force=True to override)",
                    exchange, last.run_id, age,
                )
                return last.run_id

    # ----- 1. Determine ticker list ----- #
    if resume_run_id is not None:
        run_id, ticker_list, cursor_index = _resume_from_checkpoint(
            db_engine, resume_run_id, expected_phase="market_cap",
        )
        # Pull the persisted date range from progress_json so a
        # resumed run uses the same range as the original.
        with db_engine.connect() as conn:
            run = FundamentalsRunRepository(conn).get(run_id)
        if run.progress_json and "from_date" in run.progress_json:
            from_date = _parse_iso_date(run.progress_json["from_date"])
            to_date = _parse_iso_date(run.progress_json["to_date"])
        logger.info(
            "Resuming market cap backfill run %d at ticker index %d/%d "
            "(range %s → %s)",
            run_id, cursor_index, len(ticker_list), from_date, to_date,
        )
    else:
        ticker_list = _load_active_tickers_for_exchange(db_engine, exchange)
        # Filter to EQ only — ETFs excluded from market cap layer.
        ticker_list = [
            t for t in ticker_list
            if t.get("instrument_type", "EQ") == "EQ"
        ]
        if not ticker_list:
            logger.warning(
                "No active EQ tickers for exchange %s — skipping market "
                "cap backfill", exchange,
            )
            with transaction(db_engine) as conn:
                run_id = FundamentalsRunRepository(conn).open(
                    phase="market_cap", exchange=exchange,
                    notes="No active EQ tickers",
                )
                FundamentalsRunRepository(conn).close(
                    run_id, status="SUCCESS",
                    succeeded=0, failed=0, skipped=0,
                )
            return run_id
        with transaction(db_engine) as conn:
            run_id = FundamentalsRunRepository(conn).open(
                phase="market_cap", exchange=exchange,
                notes=(
                    f"Market cap backfill {from_date}→{to_date} "
                    f"of {len(ticker_list)} active EQ tickers"
                ),
            )
            FundamentalsRunRepository(conn).update_progress(
                run_id,
                progress_json={
                    "ticker_list": [
                        {"id": t["id"], "symbol": t["symbol"],
                         "instrument_type": t.get("instrument_type", "EQ")}
                        for t in ticker_list
                    ],
                    "cursor_index": 0,
                    "from_date": from_date.isoformat(),
                    "to_date": to_date.isoformat(),
                },
            )
        cursor_index = 0

    # ----- 2. Main per-ticker loop ----- #
    succeeded = 0   # tickers where at least one point was written
    failed = 0      # tickers where the fetch failed
    skipped = 0     # always 0 for this phase

    try:
        for idx in range(cursor_index, len(ticker_list)):
            t = ticker_list[idx]
            tid, symbol = t["id"], t["symbol"]

            try:
                points = hist_fetch_fn(symbol, exchange, from_date, to_date)
                if not points:
                    # Treat empty as failed — the fetcher should have
                    # raised Permanent in that case, but defensive.
                    failed += 1
                else:
                    with transaction(db_engine) as conn:
                        mcap_repo = DailyMarketCapRepository(conn)
                        for p in points:
                            mcap_repo.upsert_snapshot(
                                tid,
                                trade_date=p.trade_date,
                                close_price=p.close_price,
                                shares_used=p.shares_outstanding,
                                market_cap=p.close_price * p.shares_outstanding,
                                source="COMPUTED",
                            )
                    succeeded += 1
            except HistoricalMcapFetchRateLimited as e:
                logger.warning(
                    "Rate limited on %s (%s): sleeping %.1fs",
                    symbol, e, RATE_LIMIT_COOLOFF_S,
                )
                _checkpoint(db_engine, run_id, ticker_list, idx,
                            succeeded, failed, skipped)
                time.sleep(RATE_LIMIT_COOLOFF_S)
                try:
                    points = hist_fetch_fn(symbol, exchange, from_date, to_date)
                    with transaction(db_engine) as conn:
                        mcap_repo = DailyMarketCapRepository(conn)
                        for p in points:
                            mcap_repo.upsert_snapshot(
                                tid,
                                trade_date=p.trade_date,
                                close_price=p.close_price,
                                shares_used=p.shares_outstanding,
                                market_cap=p.close_price * p.shares_outstanding,
                                source="COMPUTED",
                            )
                    succeeded += 1
                except Exception as e2:
                    logger.error(
                        "Retry-after-cool-off failed for %s: %s", symbol, e2,
                    )
                    failed += 1
            except (HistoricalMcapFetchTransient,
                    HistoricalMcapFetchPermanent) as e:
                logger.warning("Failed to fetch hist mcap for %s: %s", symbol, e)
                failed += 1
            except Exception as e:
                logger.exception(
                    "Unexpected error fetching hist mcap for %s: %s",
                    symbol, e,
                )
                failed += 1

            if (idx + 1) % CHECKPOINT_EVERY == 0:
                _checkpoint(db_engine, run_id, ticker_list, idx + 1,
                            succeeded, failed, skipped)
                logger.info(
                    "Checkpoint: %d/%d tickers processed (s=%d f=%d) on %s",
                    idx + 1, len(ticker_list),
                    succeeded, failed, exchange,
                )

            if request_delay_s > 0:
                time.sleep(request_delay_s)

    except KeyboardInterrupt:
        logger.warning(
            "KeyboardInterrupt — closing market cap run %d as PARTIAL",
            run_id,
        )
        with transaction(db_engine) as conn:
            FundamentalsRunRepository(conn).close(
                run_id, status="PARTIAL",
                succeeded=succeeded, failed=failed, skipped=skipped,
                notes="Interrupted by KeyboardInterrupt",
            )
        raise

    # ----- 3. Close out ----- #
    status = _terminal_status(succeeded, failed)
    with transaction(db_engine) as conn:
        FundamentalsRunRepository(conn).close(
            run_id, status=status,
            succeeded=succeeded, failed=failed, skipped=skipped,
        )
    logger.info(
        "Market cap backfill run %d (%s, %s→%s) finished: status=%s s=%d f=%d",
        run_id, exchange, from_date, to_date, status, succeeded, failed,
    )
    return run_id


def _parse_iso_date(s: str) -> "date":
    """Parse a 'YYYY-MM-DD' string to date. Used to restore from
    progress_json on resume."""
    from datetime import date as _date
    return _date.fromisoformat(s)


# ============================================================
# Phase 4B-2 — industry index lifecycle
# ============================================================
#
# Three runners:
#
#   run_rebalance(industry_name, rebalance_date) — single transactional
#     rebalance for one index. Closes prior constituents (REBALANCE_EXIT),
#     picks new top-N, inserts new constituents, writes the
#     rebalance_date's index value. Returns RebalanceResult.
#
#   run_index_value_backfill(industry_name, from_date, to_date) —
#     walks a date range, computes and writes daily index values.
#     fundamentals_run lifecycle, checkpoint-every-N dates, resume.
#
#   run_industry_index_build(exchange, min_constituents=10) —
#     auto-discovery: finds industries with ≥min_constituents active
#     EQ tickers, creates industry_index rows (idempotent), kicks
#     off the initial rebalance for each one.
#
# All three are pure orchestration over the picker+computer from
# index_compute.py — they don't recompute math, they just sequence
# DB writes.


# ---------- helpers ---------- #


def _next_semiannual_rebalance_date(db_engine, after_date: "date") -> Optional["date"]:
    """Find the first trading day (≥ after_date) that's also a
    1st-of-April or 1st-of-October target.

    The semantics:
      - Target dates are April 1 and October 1 each year.
      - If the target date isn't a trading day (weekend / holiday),
        we walk forward to the next available trading day.
      - "Trading day" = any date with at least one row in
        daily_market_cap. Cheaper than reading ohlcv_bars and
        captures exactly the dates we can index.
      - Returns None if no qualifying date is found within the
        next 2 years (defensive bound; shouldn't happen for a
        live data set).

    For Phase 4B, this is called to find the FIRST rebalance date
    for newly-discovered indices. The runner uses it to set
    base_date.
    """
    from datetime import date as _date, timedelta as _td

    # Generate target dates: April 1 and October 1 for the next 2 years.
    candidates: list[_date] = []
    year = after_date.year
    for y in (year, year + 1, year + 2):
        for month in (4, 10):
            target = _date(y, month, 1)
            if target >= after_date:
                candidates.append(target)
    candidates.sort()

    with db_engine.connect() as conn:
        for target in candidates:
            # Walk forward up to 7 days looking for the next
            # trading day at or after target. (NSE has at most
            # a 3-day weekend with Monday/Tuesday holiday;
            # 7 days is comfortable margin.)
            for offset in range(7):
                probe = target + _td(days=offset)
                row = conn.execute(
                    text("""
                        SELECT 1
                        FROM daily_market_cap
                        WHERE trade_date = :td
                        LIMIT 1
                    """),
                    {"td": probe},
                ).fetchone()
                if row is not None:
                    return probe
    return None


def _list_trading_days_in_range(
    db_engine, from_date: "date", to_date: "date",
) -> list["date"]:
    """All distinct trade_dates with daily_market_cap rows in the
    range, ascending. Used by run_index_value_backfill to walk
    only days that have data."""
    with db_engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT DISTINCT trade_date
            FROM daily_market_cap
            WHERE trade_date >= :fd AND trade_date <= :td
            ORDER BY trade_date ASC
        """), {"fd": from_date, "td": to_date}).fetchall()
    return [r[0] for r in rows]


@dataclass(frozen=True, slots=True)
class RebalanceResult:
    """Summary of one rebalance. Used by tests and by
    run_industry_index_build for logging."""
    industry_name: str
    rebalance_date: "date"
    constituents_picked: int
    constituents_closed: int  # how many prior constituents we closed
    rebalance_skipped: bool   # True if min_constituents not met
    skip_reason: Optional[str]


# ---------- run_rebalance ---------- #


def run_rebalance(
    db_engine: Engine,
    industry_name: str,
    rebalance_date: "date",
    *,
    n: int = 10,
    exchange: Optional[str] = None,
    min_constituents: Optional[int] = None,
) -> RebalanceResult:
    """Execute one rebalance for `industry_name` on `rebalance_date`.

    Algorithm:
      1. Look up the index. If it doesn't exist, raise.
      2. Pick top-N constituents via select_top_n_constituents.
      3. If count < min_constituents (taken from index row if not
         overridden), log warning and return without writing.
      4. In a single transaction:
         a. Close any prior active constituents with effective_to
            = rebalance_date - 1 day, reason=REBALANCE_EXIT.
         b. Insert new constituent rows for each picked ticker.
         c. Compute and write the index value for rebalance_date.
            (At the rebalance, value should equal base_value
            because every ratio is 1.0; the write confirms this
            and serves as the anchor for future computations.)

    Returns RebalanceResult. The `rebalance_skipped` field tells
    the caller whether the rebalance was committed; if True, no
    DB state changed.
    """
    from datetime import timedelta as _td

    from .index_compute import (
        compute_index_value_for_date,
        select_top_n_constituents,
    )
    from ..persistence.fundamentals_repo import (
        IndustryIndexConstituentRepository,
        IndustryIndexRepository,
        IndustryIndexValueRepository,
    )

    with db_engine.connect() as conn:
        idx = IndustryIndexRepository(conn).get(industry_name)
    if idx is None:
        raise ValueError(
            f"run_rebalance: index {industry_name!r} does not exist"
        )

    effective_min = (
        min_constituents if min_constituents is not None
        else idx.min_constituents
    )

    # ----- pick new constituents (read-only) ----- #
    picked = select_top_n_constituents(
        db_engine, industry_name, rebalance_date,
        n=n, exchange=exchange,
    )

    if len(picked) < effective_min:
        skip_reason = (
            f"picked {len(picked)} < min_constituents {effective_min}"
        )
        logger.warning(
            "run_rebalance: skipping %s on %s — %s",
            industry_name, rebalance_date, skip_reason,
        )
        return RebalanceResult(
            industry_name=industry_name,
            rebalance_date=rebalance_date,
            constituents_picked=len(picked),
            constituents_closed=0,
            rebalance_skipped=True,
            skip_reason=skip_reason,
        )

    # ----- write phase: single transaction so we can't half-rebalance ----- #
    closed_count = 0
    with transaction(db_engine) as conn:
        cr = IndustryIndexConstituentRepository(conn)
        # Close any prior active constituents. effective_to is
        # the day BEFORE the new rebalance — yesterday's close
        # was the last day they were "in" the index.
        prior = cr.list_active_for_index(industry_name)
        for p in prior:
            cr.close_period(
                industry_name, ticker_id=p.ticker_id,
                effective_to=rebalance_date - _td(days=1),
                removed_reason="REBALANCE_EXIT",
            )
            closed_count += 1
        # Insert new constituents.
        for sc in picked:
            cr.insert_constituent(
                industry_name, ticker_id=sc.ticker_id,
                rebalance_date=rebalance_date,
                weight_at_rebalance=sc.weight,
                market_cap_at_rebalance=sc.market_cap,
                close_at_rebalance=sc.close_price,
            )

    # ----- write the rebalance day's index value ----- #
    # Done in a separate transaction so the constituent inserts are
    # committed before the value computation reads them.
    cv = compute_index_value_for_date(
        db_engine, industry_name, rebalance_date,
    )
    if cv is not None:
        with transaction(db_engine) as conn:
            IndustryIndexValueRepository(conn).upsert_value(
                industry_name,
                trade_date=rebalance_date,
                value=cv.value,
                constituents_used=cv.constituents_used,
                constituents_missing=cv.constituents_missing,
                renormalized=cv.renormalized,
                effective_rebalance_date=cv.effective_rebalance_date,
            )

    logger.info(
        "run_rebalance %s on %s: picked=%d closed_prior=%d",
        industry_name, rebalance_date, len(picked), closed_count,
    )
    return RebalanceResult(
        industry_name=industry_name,
        rebalance_date=rebalance_date,
        constituents_picked=len(picked),
        constituents_closed=closed_count,
        rebalance_skipped=False,
        skip_reason=None,
    )


# ---------- run_index_value_backfill ---------- #


def run_index_value_backfill(
    db_engine: Engine,
    industry_name: str,
    *,
    from_date: "date",
    to_date: "date",
    resume_run_id: Optional[int] = None,
    force: bool = False,
) -> int:
    """Walk trading days in [from_date, to_date], computing and
    writing daily index values for `industry_name`.

    Trading days come from daily_market_cap.trade_date — any day
    we have mcap data is a candidate. Pre-base_date days return
    None from compute_index_value_for_date and are skipped without
    error.

    Same fundamentals_run lifecycle as the Phase 1-3 runners.
    The progress_json carries the date list (not ticker list) for
    resume.

    Returns run_id.
    """
    from .index_compute import compute_index_value_for_date
    from ..persistence.fundamentals_repo import (
        IndustryIndexRepository,
        IndustryIndexValueRepository,
    )

    if from_date > to_date:
        raise ValueError(
            f"from_date {from_date} must be <= to_date {to_date}"
        )

    # Sanity check: index must exist.
    with db_engine.connect() as conn:
        idx = IndustryIndexRepository(conn).get(industry_name)
    if idx is None:
        raise ValueError(
            f"run_index_value_backfill: index {industry_name!r} "
            f"does not exist"
        )

    # Skip-if-recent guard: use the value table's latest entry,
    # not the run-record (which uses a fixed-width exchange column
    # that doesn't accommodate per-industry granularity). If the
    # latest value row is within 80 days of today, skip unless
    # forced.
    phase = "index_value"
    if not force and resume_run_id is None:
        with db_engine.connect() as conn:
            latest = IndustryIndexValueRepository(conn).get_latest(
                industry_name,
            )
        if latest is not None:
            age = (date.today() - latest.trade_date).days
            if age < 80:
                logger.info(
                    "Skipping index value backfill for %s: latest "
                    "value is from %s (%d days ago)",
                    industry_name, latest.trade_date, age,
                )
                # Return a synthetic "no-op success" run id by
                # opening and closing one. Different from the
                # mcap pattern but the skip-if-recent contract
                # requires we return an int.
                with transaction(db_engine) as conn:
                    run_id = FundamentalsRunRepository(conn).open(
                        phase=phase, exchange="ALL",
                        notes=f"Skipped (recent values exist): {industry_name}",
                    )
                    FundamentalsRunRepository(conn).close(
                        run_id, status="SUCCESS",
                        succeeded=0, failed=0, skipped=0,
                    )
                return run_id

    # ----- resume path ----- #
    if resume_run_id is not None:
        with db_engine.connect() as conn:
            run = FundamentalsRunRepository(conn).get(resume_run_id)
        if run is None:
            raise ValueError(f"No run with id {resume_run_id}")
        if run.phase != phase:
            raise ValueError(
                f"Cannot resume: run {resume_run_id} has phase "
                f"{run.phase!r}, expected {phase!r}"
            )
        progress = run.progress_json or {}
        date_list_iso = progress.get("date_list", [])
        cursor_index = progress.get("cursor_index", 0)
        date_list = [_parse_iso_date(s) for s in date_list_iso]
        run_id = resume_run_id
        logger.info(
            "Resuming index value backfill run %d at date index %d/%d",
            run_id, cursor_index, len(date_list),
        )
        # Also restore the range — even if the caller passed a
        # different one, the resumed run uses the original.
        if "from_date" in progress:
            from_date = _parse_iso_date(progress["from_date"])
            to_date = _parse_iso_date(progress["to_date"])
    else:
        # Fresh run.
        date_list = _list_trading_days_in_range(
            db_engine, from_date, to_date,
        )
        if not date_list:
            logger.warning(
                "No trading days in [%s, %s] for index %s — nothing to do",
                from_date, to_date, industry_name,
            )
            with transaction(db_engine) as conn:
                run_id = FundamentalsRunRepository(conn).open(
                    phase=phase, exchange="ALL",
                    notes=f"No trading days in range for {industry_name}",
                )
                FundamentalsRunRepository(conn).close(
                    run_id, status="SUCCESS",
                    succeeded=0, failed=0, skipped=0,
                )
            return run_id
        with transaction(db_engine) as conn:
            run_id = FundamentalsRunRepository(conn).open(
                phase=phase, exchange="ALL",
                notes=(
                    f"Index value backfill for {industry_name} "
                    f"{from_date}→{to_date}, {len(date_list)} trading days"
                ),
            )
            FundamentalsRunRepository(conn).update_progress(
                run_id,
                progress_json={
                    "date_list": [d.isoformat() for d in date_list],
                    "cursor_index": 0,
                    "from_date": from_date.isoformat(),
                    "to_date": to_date.isoformat(),
                    "industry_name": industry_name,
                },
            )
        cursor_index = 0

    # ----- main loop ----- #
    succeeded = 0  # days we wrote a value for
    skipped = 0    # days we skipped (pre-base, no constituents, etc.)
    failed = 0     # days that errored

    try:
        for idx_pos in range(cursor_index, len(date_list)):
            td = date_list[idx_pos]
            try:
                cv = compute_index_value_for_date(
                    db_engine, industry_name, td,
                )
                if cv is None:
                    skipped += 1
                else:
                    with transaction(db_engine) as conn:
                        IndustryIndexValueRepository(conn).upsert_value(
                            industry_name,
                            trade_date=cv.trade_date,
                            value=cv.value,
                            constituents_used=cv.constituents_used,
                            constituents_missing=cv.constituents_missing,
                            renormalized=cv.renormalized,
                            effective_rebalance_date=cv.effective_rebalance_date,
                        )
                    succeeded += 1
            except Exception as e:
                logger.exception(
                    "Failed computing value for %s on %s: %s",
                    industry_name, td, e,
                )
                failed += 1

            if (idx_pos + 1) % CHECKPOINT_EVERY == 0:
                # Build a checkpoint snapshot — date_list re-encoded
                # so the progress_json is self-contained.
                with transaction(db_engine) as conn:
                    FundamentalsRunRepository(conn).update_progress(
                        run_id,
                        succeeded=succeeded,
                        failed=failed,
                        skipped=skipped,
                        progress_json={
                            "date_list": [d.isoformat() for d in date_list],
                            "cursor_index": idx_pos + 1,
                            "from_date": from_date.isoformat(),
                            "to_date": to_date.isoformat(),
                            "industry_name": industry_name,
                        },
                    )
                logger.info(
                    "Checkpoint: %d/%d dates (s=%d sk=%d f=%d) for %s",
                    idx_pos + 1, len(date_list),
                    succeeded, skipped, failed, industry_name,
                )

    except KeyboardInterrupt:
        logger.warning(
            "KeyboardInterrupt — closing index value run %d as PARTIAL",
            run_id,
        )
        with transaction(db_engine) as conn:
            FundamentalsRunRepository(conn).close(
                run_id, status="PARTIAL",
                succeeded=succeeded, failed=failed, skipped=skipped,
                notes="Interrupted by KeyboardInterrupt",
            )
        raise

    status = _terminal_status(succeeded, failed)
    with transaction(db_engine) as conn:
        FundamentalsRunRepository(conn).close(
            run_id, status=status,
            succeeded=succeeded, failed=failed, skipped=skipped,
        )
    logger.info(
        "Index value backfill run %d for %s finished: status=%s s=%d sk=%d f=%d",
        run_id, industry_name, status, succeeded, skipped, failed,
    )
    return run_id


# ---------- run_industry_index_build ---------- #


def run_industry_index_build(
    db_engine: Engine,
    exchange: str,
    *,
    min_constituents: int = 10,
    base_value: float = 1000.0,
    n: int = 10,
) -> dict:
    """Auto-discover qualifying industries and create indices for them.

    Algorithm:
      1. Query instrument_industry for active EQ tickers on the
         exchange, grouped by effective industry (industry_override
         beats auto-classified).
      2. For each industry with ≥min_constituents tickers:
         a. Determine first rebalance date: the next semiannual
            April/October trading day at or after the earliest
            daily_market_cap date.
         b. Upsert an industry_index row (idempotent) with that
            base_date.
         c. Call run_rebalance for that date.
      3. Industries with fewer than min_constituents are skipped
         and reported in the return dict.

    Returns a dict with per-industry outcome lists:
      {
        'qualified': [(industry, sector, count), ...],
        'unqualified': [(industry, sector, count), ...],
        'indexed': [(industry, base_date, picked), ...],
        'failed': [(industry, error), ...],
      }
    """
    # ----- 1. Find candidate industries ----- #
    with db_engine.connect() as conn:
        rows = conn.execute(text("""
            WITH effective_industry AS (
                SELECT
                    t.ticker_id,
                    COALESCE(io.industry, ii.industry) AS industry,
                    COALESCE(io.sector, ii.sector) AS sector
                FROM tickers t
                LEFT JOIN industry_override io
                  ON io.ticker_id = t.ticker_id
                LEFT JOIN instrument_industry ii
                  ON ii.ticker_id = t.ticker_id
                WHERE t.is_active = TRUE
                  AND t.instrument_type = 'EQ'
                  AND t.exchange = :ex
            )
            SELECT
                ei.industry,
                MAX(ei.sector) AS sector,  -- modal-ish; differences are rare
                COUNT(*) AS ticker_count
            FROM effective_industry ei
            WHERE ei.industry IS NOT NULL
            GROUP BY ei.industry
            ORDER BY ei.industry
        """), {"ex": exchange}).fetchall()

    candidates = [
        (r[0], r[1] or "Unknown", int(r[2]))
        for r in rows
    ]

    qualified: list[tuple[str, str, int]] = []
    unqualified: list[tuple[str, str, int]] = []
    for industry, sector, count in candidates:
        if count >= min_constituents:
            qualified.append((industry, sector, count))
        else:
            unqualified.append((industry, sector, count))

    logger.info(
        "run_industry_index_build %s: %d qualified, %d unqualified "
        "industries",
        exchange, len(qualified), len(unqualified),
    )

    # ----- 2. Find the earliest mcap date for this exchange ----- #
    with db_engine.connect() as conn:
        earliest_row = conn.execute(text("""
            SELECT MIN(mc.trade_date)
            FROM daily_market_cap mc
            JOIN tickers t ON t.ticker_id = mc.ticker_id
            WHERE t.exchange = :ex
        """), {"ex": exchange}).fetchone()
    earliest_mcap_date = earliest_row[0] if earliest_row else None
    if earliest_mcap_date is None:
        logger.warning(
            "No mcap data for exchange %s — cannot determine "
            "base_date for indices", exchange,
        )
        return {
            "qualified": qualified,
            "unqualified": unqualified,
            "indexed": [],
            "failed": [(i, "no mcap data") for i, _, _ in qualified],
        }

    # ----- 3. Determine the first rebalance date (shared across industries) ----- #
    # This is the next semiannual trading day at or after the
    # earliest mcap date. All newly-discovered indices get the
    # same base_date if they're being built in this run.
    first_rebalance = _next_semiannual_rebalance_date(
        db_engine, earliest_mcap_date,
    )
    if first_rebalance is None:
        logger.warning(
            "Could not find a semiannual trading day in the next "
            "2 years from %s — aborting build", earliest_mcap_date,
        )
        return {
            "qualified": qualified,
            "unqualified": unqualified,
            "indexed": [],
            "failed": [
                (i, "no semiannual trading day found")
                for i, _, _ in qualified
            ],
        }

    # ----- 4. For each qualified industry: upsert index + rebalance ----- #
    from ..persistence.fundamentals_repo import IndustryIndexRepository

    indexed: list[tuple[str, "date", int]] = []
    failed: list[tuple[str, str]] = []

    for industry, sector, count in qualified:
        try:
            # Idempotent upsert. If the index already exists, the
            # base_date and base_value are preserved (we re-write
            # them but to the same value typically; the runner
            # caller can override by passing base_value).
            with db_engine.connect() as conn:
                existing = IndustryIndexRepository(conn).get(industry)
            with transaction(db_engine) as conn:
                IndustryIndexRepository(conn).upsert(
                    industry,
                    sector_name=sector,
                    base_date=(existing.base_date if existing
                               else first_rebalance),
                    base_value=(existing.base_value if existing
                                else base_value),
                    min_constituents=min_constituents,
                )
            # Run the rebalance — but only if this is a brand-new
            # index, OR the index has no constituents yet. We
            # don't want to rebalance every existing index on
            # every build call.
            with db_engine.connect() as conn:
                from ..persistence.fundamentals_repo import (
                    IndustryIndexConstituentRepository,
                )
                current_constituents = (
                    IndustryIndexConstituentRepository(conn)
                    .list_active_for_index(industry)
                )
            if current_constituents:
                logger.debug(
                    "Skipping initial rebalance for %s — already has "
                    "%d active constituents", industry,
                    len(current_constituents),
                )
                continue
            result = run_rebalance(
                db_engine, industry, first_rebalance,
                n=n, exchange=exchange,
                min_constituents=min_constituents,
            )
            if result.rebalance_skipped:
                failed.append((industry, result.skip_reason or "skipped"))
            else:
                indexed.append(
                    (industry, first_rebalance, result.constituents_picked)
                )
        except Exception as e:
            logger.exception(
                "Failed to build index for %s: %s", industry, e,
            )
            failed.append((industry, str(e)))

    logger.info(
        "run_industry_index_build %s: indexed=%d failed=%d",
        exchange, len(indexed), len(failed),
    )
    return {
        "qualified": qualified,
        "unqualified": unqualified,
        "indexed": indexed,
        "failed": failed,
    }


# ============================================================
# Phase 5-4: indicator state runners
# ============================================================
#
# Different from the Phase 1-3 runners: no yfinance, no checkpoint
# resume, no per-bar serialization. The compute is a single all-or-
# nothing pass over the full bar history (because the regime state
# machine is sequential — bar N depends on bar N-1).
#
# Single-industry path: atomic — succeed completely or raise.
# Bulk path: per-industry error isolation, the fundamentals_run row
# tracks the per-industry SUCCESS/FAIL count.


def run_indicator_state_for_industry(
    db_engine: Engine,
    industry_name: str,
    *,
    force: bool = False,
) -> int:
    """Compute and upsert indicator state for one industry index.

    Reads the full industry_index_value history for the industry,
    computes the indicator state Series + per-bar dicts via
    Phase 5-2/5-3 primitives, and upserts each bar into
    indicator_state.

    The compute is atomic: either all bars succeed and get written,
    or an exception propagates (no partial writes — the upserts
    are batched in a single transaction).

    Skip-if-recent guard: if the indicator_state table has rows for
    this industry with a latest trade_date within 80 days of the
    industry index's latest value date, skip (unless force=True).
    This prevents accidental thrashing of bulk operations.

    Returns run_id of the recorded fundamentals_run row.
    Raises ValueError if the industry has no value history at all.
    """
    import pandas as pd
    from ..persistence.fundamentals_repo import (
        IndicatorStateRepository,
        IndustryIndexRepository,
        IndustryIndexValueRepository,
    )
    from ..fundamentals import IndicatorState
    from .indicator_primitives import (
        compute_bar_states,
        compute_indicator_series,
        INDICATOR_DEFAULTS,
    )
    from datetime import datetime, timezone

    # Sanity: industry must exist as an index.
    with db_engine.connect() as conn:
        idx = IndustryIndexRepository(conn).get(industry_name)
    if idx is None:
        raise ValueError(
            f"run_indicator_state_for_industry: index {industry_name!r} "
            f"does not exist"
        )

    phase = "index_indicators"
    if not force:
        with db_engine.connect() as conn:
            latest = IndicatorStateRepository(conn).get_latest_for_industry(
                industry_name,
            )
        if latest is not None:
            age = (date.today() - latest.trade_date).days
            if age < 80:
                logger.info(
                    "Skipping indicator_state for %s: latest=%s "
                    "(age=%d days < 80). Use force=True to override.",
                    industry_name, latest.trade_date, age,
                )
                with transaction(db_engine) as conn:
                    run_id = FundamentalsRunRepository(conn).open(
                        phase=phase, exchange="ALL",
                    )
                    FundamentalsRunRepository(conn).close(
                        run_id, status="SUCCESS",
                        succeeded=0, failed=0, skipped=1,
                    )
                return run_id

    # Read the full value history. We need it ordered oldest-first
    # for the indicator walk; get_history returns newest-first.
    with db_engine.connect() as conn:
        history = IndustryIndexValueRepository(conn).get_history(
            industry_name,
        )
    if not history:
        raise ValueError(
            f"run_indicator_state_for_industry: industry "
            f"{industry_name!r} has no value rows. Run "
            f"compute-index-values first."
        )
    history.reverse()  # oldest-first

    # Build pandas Series from the history.
    values = pd.Series([h.value for h in history], dtype=float)
    vol = pd.Series([1.0] * len(history), dtype=float)
    dates = pd.Series([h.trade_date for h in history])

    # Compute indicators + regime walk.
    series = compute_indicator_series(
        values, vol, values, values,
        params=INDICATOR_DEFAULTS,
        synthetic_atr_for_index=True,
    )
    bar_states = compute_bar_states(
        values, vol, values, values,
        trade_dates=dates, series=series,
        params=INDICATOR_DEFAULTS,
        # open_ defaults to values.shift(1) — the index synthesis.
    )

    # Open the run, upsert all bars, close. Single transaction for
    # atomicity: either every bar's indicator_state is written, or
    # none of them is (rollback on exception).
    succeeded = 0
    failed = 0
    skipped = 0
    with transaction(db_engine) as conn:
        run_id = FundamentalsRunRepository(conn).open(
            phase=phase, exchange="ALL",
        )
        repo = IndicatorStateRepository(conn)
        for bs in bar_states:
            # Skip bars where required indicators are NaN (early
            # bars before EMAs stabilize). These shouldn't go into
            # indicator_state — they have no analytic meaning.
            if (bs.get("med_ema") is None or pd.isna(bs.get("med_ema"))
                or bs.get("long_ema") is None or pd.isna(bs.get("long_ema"))
                or bs.get("longest_ema") is None or pd.isna(bs.get("longest_ema"))
                or bs.get("atr") is None or pd.isna(bs.get("atr"))):
                skipped += 1
                continue

            state = IndicatorState(
                ticker_id=None, timeframe=None,
                industry_name=industry_name,
                trade_date=bs["trade_date"],
                med_ema=float(bs["med_ema"]),
                long_ema=float(bs["long_ema"]),
                longest_ema=float(bs["longest_ema"]),
                atr=float(bs["atr"]),
                upper_inner_band=float(bs["upper_inner_band"]),
                lower_inner_band=float(bs["lower_inner_band"]),
                upper_mid_band=float(bs["upper_mid_band"]),
                lower_mid_band=float(bs["lower_mid_band"]),
                upper_outer_band=float(bs["upper_outer_band"]),
                lower_outer_band=float(bs["lower_outer_band"]),
                core_regime=bs["core_regime"],
                display_regime=bs["display_regime"],
                pierces_upper_mid=bs["pierces_upper_mid"],
                pierces_lower_mid=bs["pierces_lower_mid"],
                dist_to_med_in_hl_units=bs["dist_to_med_in_hl_units"],
                dist_to_long_in_hl_units=bs["dist_to_long_in_hl_units"],
                dist_to_longest_in_hl_units=bs["dist_to_longest_in_hl_units"],
                mean_dist_to_med_in_hl_units=bs["mean_dist_to_med_in_hl_units"],
                mean_dist_to_long_in_hl_units=bs["mean_dist_to_long_in_hl_units"],
                mean_dist_to_longest_in_hl_units=bs["mean_dist_to_longest_in_hl_units"],
                bars_since_med_turn=bs["bars_since_med_turn"],
                bars_since_long_turn=bs["bars_since_long_turn"],
                bars_since_longest_turn=bs["bars_since_longest_turn"],
                pct_change_since_med_turn=bs["pct_change_since_med_turn"],
                pct_change_since_long_turn=bs["pct_change_since_long_turn"],
                pct_change_since_longest_turn=bs["pct_change_since_longest_turn"],
                med_pattern_label=bs["med_pattern_label"],
                long_pattern_label=bs["long_pattern_label"],
                longest_pattern_label=bs["longest_pattern_label"],
                med_turn_buy=bs["med_turn_buy"],
                med_turn_sell=bs["med_turn_sell"],
                long_turn_buy=bs["long_turn_buy"],
                long_turn_sell=bs["long_turn_sell"],
                longest_turn_buy=bs["longest_turn_buy"],
                longest_turn_sell=bs["longest_turn_sell"],
                computed_at=datetime.now(timezone.utc),
            )
            repo.upsert_for_industry(state)
            succeeded += 1

        FundamentalsRunRepository(conn).close(
            run_id, status="SUCCESS",
            succeeded=succeeded, failed=0, skipped=skipped,
        )

    logger.info(
        "run_indicator_state_for_industry %s: succeeded=%d skipped=%d",
        industry_name, succeeded, skipped,
    )
    return run_id


def run_indicator_state_for_all_industries(
    db_engine: Engine,
    *,
    force: bool = False,
) -> dict:
    """Compute indicator state for every active industry index that
    has value rows.

    Iterates over all active industries (from industry_index).
    For each, calls run_indicator_state_for_industry in isolation —
    if one industry raises, the next is still attempted. Returns a
    report dict with the per-industry outcomes.

    Industries with no value history are reported as skipped (not
    failed). Industries with no `indicator_state`-relevant bars
    after NaN filtering are reported as a successful empty run.

    Returns a dict:
      {
        "succeeded": list[(industry_name, succeeded, skipped)],
        "skipped":   list[(industry_name, reason)],
        "failed":    list[(industry_name, error_message)],
      }
    """
    from ..persistence.fundamentals_repo import (
        IndustryIndexRepository,
        IndustryIndexValueRepository,
    )

    with db_engine.connect() as conn:
        indices = IndustryIndexRepository(conn).list_active()
        value_repo = IndustryIndexValueRepository(conn)
        # Pre-filter: industries with at least one value row.
        with_values = set()
        for name, count, _earliest, _latest in value_repo.coverage_summary():
            if count > 0:
                with_values.add(name)

    succeeded: list[tuple[str, int, int]] = []
    skipped: list[tuple[str, str]] = []
    failed: list[tuple[str, str]] = []

    for idx in indices:
        name = idx.industry_name
        if name not in with_values:
            skipped.append((name, "no value rows"))
            continue
        try:
            run_id = run_indicator_state_for_industry(
                db_engine, name, force=force,
            )
            # Read the run row to get the counts.
            with db_engine.connect() as conn:
                r = FundamentalsRunRepository(conn).get(run_id)
            if r is not None:
                succeeded.append((name, r.succeeded, r.skipped))
            else:
                succeeded.append((name, 0, 0))
        except Exception as e:
            logger.exception(
                "run_indicator_state_for_all_industries: failed for %s",
                name,
            )
            failed.append((name, str(e)))

    logger.info(
        "run_indicator_state_for_all_industries: succeeded=%d "
        "skipped=%d failed=%d",
        len(succeeded), len(skipped), len(failed),
    )
    return {
        "succeeded": succeeded,
        "skipped": skipped,
        "failed": failed,
    }


# ============================================================
# Phase 6-1: signal event runners
# ============================================================
#
# Same shape as Phase 5-4: whole-history per industry, atomic-or-
# raise. Signal events are derived from indicator_state which is
# already path-dependent, so partial backfill makes no sense here.
#
# Special behavior: full delete-then-insert per industry instead of
# upsert. This is because signals are sparse (most bars produce no
# rows) and changing strategy parameters could shift which bars
# emit events. A pure upsert would leave stale rows from old
# computes; delete-then-insert guarantees the table reflects the
# current compute exactly.


def run_signal_events_for_industry(
    db_engine: Engine,
    industry_name: str,
    *,
    force: bool = False,
) -> int:
    """Compute and persist signal events for one industry.

    Reads the full indicator_state history for the industry,
    derives BUY/SELL/EXIT_BUY/EXIT_SELL events from display_regime
    transitions, and writes them to signal_event.

    Behavior:
      - Reads industry_index_value to get close_at_event values.
      - Walks indicator_state oldest-first.
      - Wipes existing signal_event rows for this industry before
        inserting new ones (delete-then-insert, atomic in single
        transaction).
      - Skip-if-recent: if signal_event already has a row for this
        industry from less than 80 days ago, skip unless force=True.

    Returns run_id.
    Raises ValueError if the industry has no indicator_state rows.
    """
    from ..persistence.fundamentals_repo import (
        IndicatorStateRepository,
        IndustryIndexRepository,
        IndustryIndexValueRepository,
        SignalEventRepository,
    )
    from .signal_compute import compute_signal_events_from_states

    # Sanity: industry must exist.
    with db_engine.connect() as conn:
        idx = IndustryIndexRepository(conn).get(industry_name)
    if idx is None:
        raise ValueError(
            f"run_signal_events_for_industry: index {industry_name!r} "
            f"does not exist"
        )

    phase = "index_indicators"
    if not force:
        with db_engine.connect() as conn:
            recent = SignalEventRepository(conn).get_for_industry(
                industry_name, limit=1,
            )
        if recent:
            age = (date.today() - recent[0].trade_date).days
            if age < 80:
                logger.info(
                    "Skipping signal events for %s: latest=%s "
                    "(age=%d days < 80). Use force=True to override.",
                    industry_name, recent[0].trade_date, age,
                )
                with transaction(db_engine) as conn:
                    run_id = FundamentalsRunRepository(conn).open(
                        phase=phase, exchange="ALL",
                    )
                    FundamentalsRunRepository(conn).close(
                        run_id, status="SUCCESS",
                        succeeded=0, failed=0, skipped=1,
                    )
                return run_id

    # Read the indicator_state time series (oldest-first for the
    # walk). get_history_for_industry returns newest-first.
    with db_engine.connect() as conn:
        states = IndicatorStateRepository(conn).get_history_for_industry(
            industry_name,
        )
        value_rows = IndustryIndexValueRepository(conn).get_history(
            industry_name,
        )

    if not states:
        raise ValueError(
            f"run_signal_events_for_industry: industry {industry_name!r} "
            f"has no indicator_state rows. Run compute-indicator-state "
            f"first."
        )

    # Reverse to oldest-first.
    states.reverse()

    # Build close lookup from value rows.
    close_by_date = {row.trade_date: float(row.value) for row in value_rows}

    def close_lookup(td):
        # If a state row exists for a date but no value row, that
        # would be a data integrity bug — but defensive fallback to
        # the med_ema avoids crashing.
        if td in close_by_date:
            return close_by_date[td]
        # Fallback (shouldn't happen in practice): use whichever
        # state row matches.
        for st in states:
            if st.trade_date == td:
                return float(st.med_ema)
        return 0.0  # Defensive — caller will see close=0 which is
                    # an obvious "something went wrong" marker.

    # Compute events.
    events = compute_signal_events_from_states(states, close_lookup)

    # Atomic delete-then-insert in a single transaction.
    with transaction(db_engine) as conn:
        run_id = FundamentalsRunRepository(conn).open(
            phase=phase, exchange="ALL",
        )
        repo = SignalEventRepository(conn)
        deleted = repo.delete_for_industry(industry_name)
        repo.batch_upsert(events)
        FundamentalsRunRepository(conn).close(
            run_id, status="SUCCESS",
            succeeded=len(events), failed=0, skipped=0,
        )

    logger.info(
        "run_signal_events_for_industry %s: emitted=%d (deleted prior=%d)",
        industry_name, len(events), deleted,
    )
    return run_id


def run_signal_events_for_all_industries(
    db_engine: Engine,
    *,
    force: bool = False,
) -> dict:
    """Compute signal events for every active industry that has
    indicator_state rows.

    Per-industry error isolation. Returns a report dict:
      {
        "succeeded": list[(industry_name, events_count)],
        "skipped":   list[(industry_name, reason)],
        "failed":    list[(industry_name, error_message)],
      }
    """
    from ..persistence.fundamentals_repo import (
        IndicatorStateRepository,
        IndustryIndexRepository,
    )

    with db_engine.connect() as conn:
        indices = IndustryIndexRepository(conn).list_active()
        # Pre-filter: industries with at least one indicator_state row.
        with_indicators = set()
        for name, count, _e, _l in (
            IndicatorStateRepository(conn).coverage_summary_for_industries()
        ):
            if count > 0:
                with_indicators.add(name)

    succeeded: list[tuple[str, int]] = []
    skipped: list[tuple[str, str]] = []
    failed: list[tuple[str, str]] = []

    for idx in indices:
        name = idx.industry_name
        if name not in with_indicators:
            skipped.append((name, "no indicator_state rows"))
            continue
        try:
            run_id = run_signal_events_for_industry(
                db_engine, name, force=force,
            )
            with db_engine.connect() as conn:
                r = FundamentalsRunRepository(conn).get(run_id)
            if r is not None:
                succeeded.append((name, r.succeeded))
            else:
                succeeded.append((name, 0))
        except Exception as e:
            logger.exception(
                "run_signal_events_for_all_industries: failed for %s",
                name,
            )
            failed.append((name, str(e)))

    logger.info(
        "run_signal_events_for_all_industries: succeeded=%d "
        "skipped=%d failed=%d",
        len(succeeded), len(skipped), len(failed),
    )
    return {
        "succeeded": succeeded,
        "skipped": skipped,
        "failed": failed,
    }


# ============================================================
# Phase 7: per-ticker indicator state + signal events
# ============================================================
#
# The schema already supports per-ticker rows (indicator_state and
# signal_event both have nullable (ticker_id, timeframe) slots with
# the XOR CHECK). Phase 5-4 / 6-1 wrote per-industry rows; Phase 7
# adds the per-ticker variants.
#
# Compute is identical mechanically: read OHLCV history, run the
# indicator pipeline, walk the regime, write indicator_state rows
# keyed on (ticker_id, timeframe). Then for signals: walk the
# indicator_state rows and emit signal_event rows.
#
# Differences from per-industry:
#   - source data is real OHLC with distinct open/high/low/close
#     and real volume (not the index synthesis); so we pass
#     synthetic_atr_for_index=False.
#   - close_lookup for signals uses real close from ohlcv_bars,
#     not industry_index_value.
#   - bulk runner iterates over tickers (potentially thousands).
#     Per-ticker error isolation, sequential — no need for parallel
#     since the compute is in-process and fast.


def run_indicator_state_for_ticker(
    db_engine: Engine,
    ticker_id: int,
    timeframe: str,
    *,
    force: bool = False,
    min_bars: int = 30,
) -> int:
    """Compute and upsert indicator state for one ticker+timeframe.

    Reads the full OHLCV history for this ticker+timeframe, runs
    the indicator pipeline (3 EMAs, ATR, bands, regime), and
    upserts each indicator-ready bar into indicator_state.

    Returns run_id. Raises ValueError if the ticker doesn't exist
    or has insufficient bars (fewer than `min_bars` to seed the
    EMAs meaningfully).

    Skip-if-recent guard: 80-day threshold same as the per-industry
    runner.
    """
    import pandas as pd
    from ..persistence.bars_repo import BarsRepository
    from ..persistence.fundamentals_repo import (
        IndicatorStateRepository,
    )
    from ..persistence.tickers_repo import TickersRepository
    from ..domain import Timeframe
    from ..fundamentals import IndicatorState
    from .indicator_primitives import (
        compute_bar_states,
        compute_indicator_series,
        INDICATOR_DEFAULTS,
    )
    from datetime import datetime, timezone

    # Coerce timeframe string to Timeframe enum.
    try:
        tf_enum = Timeframe(timeframe)
    except ValueError:
        raise ValueError(
            f"run_indicator_state_for_ticker: unknown timeframe "
            f"{timeframe!r}; must be one of "
            f"{[t.value for t in Timeframe]}"
        )

    # Sanity: ticker must exist.
    with db_engine.connect() as conn:
        ticker = TickersRepository(conn).get_by_id(ticker_id)
    if ticker is None:
        raise ValueError(
            f"run_indicator_state_for_ticker: ticker_id={ticker_id} "
            f"does not exist"
        )

    phase = "index_indicators"
    if not force:
        with db_engine.connect() as conn:
            latest = IndicatorStateRepository(conn).get_latest_for_ticker(
                ticker_id, timeframe,
            )
        if latest is not None:
            age = (date.today() - latest.trade_date).days
            if age < 80:
                logger.info(
                    "Skipping indicator_state for ticker_id=%d tf=%s: "
                    "latest=%s (age=%d days < 80).",
                    ticker_id, timeframe, latest.trade_date, age,
                )
                with transaction(db_engine) as conn:
                    run_id = FundamentalsRunRepository(conn).open(
                        phase=phase, exchange="ALL",
                    )
                    FundamentalsRunRepository(conn).close(
                        run_id, status="SUCCESS",
                        succeeded=0, failed=0, skipped=1,
                    )
                return run_id

    # Read the full OHLCV history (chronological order from fetch_history).
    with db_engine.connect() as conn:
        # Use a large limit — most tickers have <500 weekly bars even
        # over multi-year history. 10000 is a safe upper bound.
        bars = BarsRepository(conn).fetch_history(
            ticker_id, tf_enum, limit=10000,
        )
    if len(bars) < min_bars:
        raise ValueError(
            f"run_indicator_state_for_ticker: ticker_id={ticker_id} "
            f"tf={timeframe} has only {len(bars)} bars (need at least "
            f"{min_bars} to seed EMAs)"
        )

    # Build pandas Series from the bars. ohlcv_bars stores
    # bar_timestamp as TIMESTAMPTZ; we project to trade_date.
    open_s = pd.Series([b.open for b in bars], dtype=float)
    high_s = pd.Series([b.high for b in bars], dtype=float)
    low_s = pd.Series([b.low for b in bars], dtype=float)
    close_s = pd.Series([b.close for b in bars], dtype=float)
    volume_s = pd.Series([b.volume for b in bars], dtype=float)
    # bar_timestamp.date() — weekly bars typically have the same
    # weekday across a series (e.g. all Mondays), so date() is
    # unambiguous. For intraday timeframes this loses time-of-day,
    # but indicator_state.trade_date is DATE anyway so any intraday
    # multiplicity within a day collapses to the last bar of that
    # day. For 1W/1D this is exact.
    dates = pd.Series([b.timestamp.date() for b in bars])

    series = compute_indicator_series(
        close_s, volume_s, high_s, low_s,
        params=INDICATOR_DEFAULTS,
        synthetic_atr_for_index=False,
    )
    bar_states = compute_bar_states(
        close_s, volume_s, high_s, low_s,
        trade_dates=dates, series=series,
        params=INDICATOR_DEFAULTS,
        open_=open_s,
    )

    # Atomic write: one transaction, all bars or none.
    succeeded = 0
    skipped = 0
    with transaction(db_engine) as conn:
        run_id = FundamentalsRunRepository(conn).open(
            phase=phase, exchange="ALL",
        )
        repo = IndicatorStateRepository(conn)
        for bs in bar_states:
            if (bs.get("med_ema") is None or pd.isna(bs.get("med_ema"))
                or bs.get("long_ema") is None or pd.isna(bs.get("long_ema"))
                or bs.get("longest_ema") is None
                or pd.isna(bs.get("longest_ema"))
                or bs.get("atr") is None or pd.isna(bs.get("atr"))):
                skipped += 1
                continue
            state = IndicatorState(
                ticker_id=ticker_id, timeframe=timeframe,
                industry_name=None,
                trade_date=bs["trade_date"],
                med_ema=float(bs["med_ema"]),
                long_ema=float(bs["long_ema"]),
                longest_ema=float(bs["longest_ema"]),
                atr=float(bs["atr"]),
                upper_inner_band=float(bs["upper_inner_band"]),
                lower_inner_band=float(bs["lower_inner_band"]),
                upper_mid_band=float(bs["upper_mid_band"]),
                lower_mid_band=float(bs["lower_mid_band"]),
                upper_outer_band=float(bs["upper_outer_band"]),
                lower_outer_band=float(bs["lower_outer_band"]),
                core_regime=bs["core_regime"],
                display_regime=bs["display_regime"],
                pierces_upper_mid=bs["pierces_upper_mid"],
                pierces_lower_mid=bs["pierces_lower_mid"],
                dist_to_med_in_hl_units=bs["dist_to_med_in_hl_units"],
                dist_to_long_in_hl_units=bs["dist_to_long_in_hl_units"],
                dist_to_longest_in_hl_units=bs["dist_to_longest_in_hl_units"],
                mean_dist_to_med_in_hl_units=bs["mean_dist_to_med_in_hl_units"],
                mean_dist_to_long_in_hl_units=bs["mean_dist_to_long_in_hl_units"],
                mean_dist_to_longest_in_hl_units=bs["mean_dist_to_longest_in_hl_units"],
                bars_since_med_turn=bs["bars_since_med_turn"],
                bars_since_long_turn=bs["bars_since_long_turn"],
                bars_since_longest_turn=bs["bars_since_longest_turn"],
                pct_change_since_med_turn=bs["pct_change_since_med_turn"],
                pct_change_since_long_turn=bs["pct_change_since_long_turn"],
                pct_change_since_longest_turn=bs["pct_change_since_longest_turn"],
                med_pattern_label=bs["med_pattern_label"],
                long_pattern_label=bs["long_pattern_label"],
                longest_pattern_label=bs["longest_pattern_label"],
                med_turn_buy=bs["med_turn_buy"],
                med_turn_sell=bs["med_turn_sell"],
                long_turn_buy=bs["long_turn_buy"],
                long_turn_sell=bs["long_turn_sell"],
                longest_turn_buy=bs["longest_turn_buy"],
                longest_turn_sell=bs["longest_turn_sell"],
                computed_at=datetime.now(timezone.utc),
            )
            repo.upsert_for_ticker(state)
            succeeded += 1

        FundamentalsRunRepository(conn).close(
            run_id, status="SUCCESS",
            succeeded=succeeded, failed=0, skipped=skipped,
        )

    logger.info(
        "run_indicator_state_for_ticker ticker_id=%d tf=%s: "
        "succeeded=%d skipped=%d",
        ticker_id, timeframe, succeeded, skipped,
    )
    return run_id


def run_indicator_state_for_all_tickers(
    db_engine: Engine,
    timeframe: str,
    *,
    exchange: Optional[str] = None,
    force: bool = False,
    min_bars: int = 30,
) -> dict:
    """Compute indicator state for every ticker that has at least
    `min_bars` of OHLCV history for the given timeframe.

    Args:
      timeframe: which timeframe to compute (e.g. "1W", "1D").
      exchange: optional filter — only process tickers on this
        exchange. None means all exchanges.
      force: bypass skip-if-recent guard.
      min_bars: skip tickers with fewer than this many bars
        (counted into the skipped bucket, not failed).

    Returns:
      {"succeeded": list[(ticker_id, symbol, written, skipped)],
       "skipped":   list[(ticker_id, symbol, reason)],
       "failed":    list[(ticker_id, symbol, error_message)]}
    """
    from ..persistence.bars_repo import BarsRepository
    from ..persistence.tickers_repo import TickersRepository
    from ..domain import Timeframe

    # Validate timeframe up-front.
    try:
        tf_enum = Timeframe(timeframe)
    except ValueError:
        raise ValueError(
            f"unknown timeframe {timeframe!r}; must be one of "
            f"{[t.value for t in Timeframe]}"
        )

    # Enumerate tickers. Only ACTIVE tickers — retired/delisted
    # ones shouldn't get fresh indicator state.
    with db_engine.connect() as conn:
        repo = TickersRepository(conn)
        tickers = repo.find_active()
    if exchange is not None:
        tickers = [t for t in tickers if t.ref.exchange == exchange]

    succeeded: list[tuple[int, str, int, int]] = []
    skipped: list[tuple[int, str, str]] = []
    failed: list[tuple[int, str, str]] = []

    for ticker in tickers:
        try:
            run_id = run_indicator_state_for_ticker(
                db_engine, ticker.ticker_id, timeframe,
                force=force, min_bars=min_bars,
            )
            with db_engine.connect() as conn:
                r = FundamentalsRunRepository(conn).get(run_id)
            if r is not None:
                succeeded.append(
                    (ticker.ticker_id, ticker.ref.symbol,
                     r.succeeded, r.skipped)
                )
            else:
                succeeded.append(
                    (ticker.ticker_id, ticker.ref.symbol, 0, 0)
                )
        except ValueError as e:
            # Insufficient bars / unknown timeframe / ticker missing —
            # these are skip conditions, not failures.
            skipped.append((ticker.ticker_id, ticker.ref.symbol, str(e)))
        except Exception as e:
            logger.exception(
                "run_indicator_state_for_all_tickers: failed for "
                "ticker_id=%d symbol=%s",
                ticker.ticker_id, ticker.ref.symbol,
            )
            failed.append((ticker.ticker_id, ticker.ref.symbol, str(e)))

    logger.info(
        "run_indicator_state_for_all_tickers tf=%s: succeeded=%d "
        "skipped=%d failed=%d",
        timeframe, len(succeeded), len(skipped), len(failed),
    )
    return {
        "succeeded": succeeded,
        "skipped": skipped,
        "failed": failed,
    }


def run_signal_events_for_ticker(
    db_engine: Engine,
    ticker_id: int,
    timeframe: str,
    *,
    force: bool = False,
) -> int:
    """Compute and persist signal events for one ticker+timeframe.

    Same delete-then-insert semantics as the per-industry variant.
    Reads indicator_state rows for this ticker, reads ohlcv_bars for
    real close values, emits BUY/SELL/EXIT_BUY/EXIT_SELL events.

    Returns run_id.
    Raises ValueError if the ticker doesn't exist or has no
    indicator_state rows.
    """
    from ..domain import Timeframe
    from ..persistence.bars_repo import BarsRepository
    from ..persistence.fundamentals_repo import (
        IndicatorStateRepository,
        SignalEventRepository,
    )
    from ..persistence.tickers_repo import TickersRepository
    from .signal_compute import compute_signal_events_from_states

    try:
        tf_enum = Timeframe(timeframe)
    except ValueError:
        raise ValueError(
            f"unknown timeframe {timeframe!r}; must be one of "
            f"{[t.value for t in Timeframe]}"
        )

    with db_engine.connect() as conn:
        ticker = TickersRepository(conn).get_by_id(ticker_id)
    if ticker is None:
        raise ValueError(
            f"run_signal_events_for_ticker: ticker_id={ticker_id} "
            f"does not exist"
        )

    phase = "index_indicators"
    if not force:
        with db_engine.connect() as conn:
            recent = SignalEventRepository(conn).get_for_ticker(
                ticker_id, timeframe, limit=1,
            )
        if recent:
            age = (date.today() - recent[0].trade_date).days
            if age < 80:
                logger.info(
                    "Skipping signal events for ticker_id=%d tf=%s: "
                    "latest=%s (age=%d days < 80).",
                    ticker_id, timeframe, recent[0].trade_date, age,
                )
                with transaction(db_engine) as conn:
                    run_id = FundamentalsRunRepository(conn).open(
                        phase=phase, exchange="ALL",
                    )
                    FundamentalsRunRepository(conn).close(
                        run_id, status="SUCCESS",
                        succeeded=0, failed=0, skipped=1,
                    )
                return run_id

    # Read indicator_state (oldest-first for the walk) and OHLCV
    # bars (for close lookup).
    with db_engine.connect() as conn:
        states = IndicatorStateRepository(conn).get_history_for_ticker(
            ticker_id, timeframe,
        )
        bars = BarsRepository(conn).fetch_history(
            ticker_id, tf_enum, limit=10000,
        )

    if not states:
        raise ValueError(
            f"run_signal_events_for_ticker: ticker_id={ticker_id} "
            f"tf={timeframe} has no indicator_state rows. Run "
            f"compute-indicator-state-ticker first."
        )

    states.reverse()  # oldest-first
    # Build close lookup from bars (bar_timestamp.date() → close).
    close_by_date = {b.timestamp.date(): float(b.close) for b in bars}

    def close_lookup(td):
        if td in close_by_date:
            return close_by_date[td]
        return 0.0  # Defensive — shouldn't happen if data integrity OK.

    events = compute_signal_events_from_states(states, close_lookup)

    with transaction(db_engine) as conn:
        run_id = FundamentalsRunRepository(conn).open(
            phase=phase, exchange="ALL",
        )
        repo = SignalEventRepository(conn)
        deleted = repo.delete_for_ticker(ticker_id, timeframe)
        repo.batch_upsert(events)
        FundamentalsRunRepository(conn).close(
            run_id, status="SUCCESS",
            succeeded=len(events), failed=0, skipped=0,
        )

    logger.info(
        "run_signal_events_for_ticker ticker_id=%d tf=%s: "
        "emitted=%d (deleted prior=%d)",
        ticker_id, timeframe, len(events), deleted,
    )
    return run_id


def run_signal_events_for_all_tickers(
    db_engine: Engine,
    timeframe: str,
    *,
    exchange: Optional[str] = None,
    force: bool = False,
) -> dict:
    """Bulk-compute signal events for all tickers (optionally
    filtered by exchange) that have indicator_state rows for the
    given timeframe."""
    from ..persistence.fundamentals_repo import IndicatorStateRepository
    from ..persistence.tickers_repo import TickersRepository

    with db_engine.connect() as conn:
        repo = TickersRepository(conn)
        tickers = repo.find_active()
        # Pre-filter to tickers with at least one indicator_state row
        # for this timeframe.
        ind_repo = IndicatorStateRepository(conn)
        with_indicators = {
            (tid, tf) for tid, tf, _count, _earliest, _latest
            in ind_repo.coverage_summary_for_tickers()
        }
    if exchange is not None:
        tickers = [t for t in tickers if t.ref.exchange == exchange]

    succeeded: list[tuple[int, str, int]] = []
    skipped: list[tuple[int, str, str]] = []
    failed: list[tuple[int, str, str]] = []

    for ticker in tickers:
        key = (ticker.ticker_id, timeframe)
        if key not in with_indicators:
            skipped.append((
                ticker.ticker_id, ticker.ref.symbol,
                "no indicator_state rows for this timeframe",
            ))
            continue
        try:
            run_id = run_signal_events_for_ticker(
                db_engine, ticker.ticker_id, timeframe, force=force,
            )
            with db_engine.connect() as conn:
                r = FundamentalsRunRepository(conn).get(run_id)
            if r is not None:
                succeeded.append(
                    (ticker.ticker_id, ticker.ref.symbol, r.succeeded)
                )
            else:
                succeeded.append(
                    (ticker.ticker_id, ticker.ref.symbol, 0)
                )
        except Exception as e:
            logger.exception(
                "run_signal_events_for_all_tickers: failed for "
                "ticker_id=%d symbol=%s",
                ticker.ticker_id, ticker.ref.symbol,
            )
            failed.append(
                (ticker.ticker_id, ticker.ref.symbol, str(e))
            )

    logger.info(
        "run_signal_events_for_all_tickers tf=%s: succeeded=%d "
        "skipped=%d failed=%d",
        timeframe, len(succeeded), len(skipped), len(failed),
    )
    return {
        "succeeded": succeeded,
        "skipped": skipped,
        "failed": failed,
    }
