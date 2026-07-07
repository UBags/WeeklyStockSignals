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
from datetime import date, datetime, UTC
from collections.abc import Callable

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
    resume_run_id: int | None = None,
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
            age = (datetime.now(UTC) - last.completed_at).days
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
    resume_run_id: int | None = None,
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

    today = datetime.now(UTC).date()

    # ----- 0. Skip-if-recent guard ----- #
    if not force and resume_run_id is None:
        with db_engine.connect() as conn:
            last = FundamentalsRunRepository(conn).last_completed(
                phase="shares", exchange=exchange,
            )
        if last is not None and last.completed_at is not None:
            age = (datetime.now(UTC) - last.completed_at).days
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
        request_delay_s: float | None = None,  # uses DEFAULT_REQUEST_DELAY_S
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
                phase="shares", exchange=exchange,
            )
        if last is not None and last.completed_at is not None:
            age = (datetime.now(UTC) - last.completed_at).days  # noqa: F821
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
            db_engine, resume_run_id, expected_phase="shares",
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
                    phase="shares", exchange=exchange,
                    notes=f"No active tickers (start={start_date.isoformat()})",
                )
                FundamentalsRunRepository(conn).close(  # noqa: F821
                    run_id, status="SUCCESS", succeeded=0, failed=0, skipped=0,
                )
            return run_id
        with transaction(db_engine) as conn:  # noqa: F821
            run_id = FundamentalsRunRepository(conn).open(  # noqa: F821
                phase="shares", exchange=exchange,
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
    resume_run_id: int | None = None,
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
            age = (datetime.now(UTC) - last.completed_at).days
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
    from_date: date,
    to_date: date,
    resume_run_id: int | None = None,
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
            age = (datetime.now(UTC) - last.completed_at).days
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
        assert run is not None, (
            f"resume run_id {run_id} not found in fundamentals_run"
        )
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


def _parse_iso_date(s: str) -> date:
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


def _next_semiannual_rebalance_date(db_engine, after_date: date) -> date | None:
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


def _annual_april_rebalance_dates(
    db_engine, from_date: date, through: date,
) -> list[date]:
    """Every April rebalance date in [from_date, through], ascending.

    Target = April 1 each year; if not a trading day (no daily_market_cap
    row), walk forward up to 7 days to the next trading day. A year with
    no trading day near April is skipped. Used by the chain driver to walk
    the annual rebalance sequence (ANNUAL_APR schedule).
    """
    from datetime import date as _date, timedelta as _td
    out: list[date] = []
    with db_engine.connect() as conn:
        for y in range(from_date.year, through.year + 1):
            target = _date(y, 4, 1)
            if target < from_date or target > through:
                # allow the holiday-walk to still land in-range
                if target > through:
                    continue
            found = None
            for offset in range(7):
                probe = target + _td(days=offset)
                if probe < from_date or probe > through:
                    continue
                row = conn.execute(text("""
                    SELECT 1 FROM daily_market_cap
                    WHERE trade_date = :td LIMIT 1
                """), {"td": probe}).fetchone()
                if row is not None:
                    found = probe
                    break
            if found is not None:
                out.append(found)
    return out


def _list_trading_days_in_range(
    db_engine, from_date: date, to_date: date,
) -> list[date]:
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
    rebalance_date: date
    constituents_picked: int
    constituents_closed: int  # how many prior constituents we closed
    rebalance_skipped: bool   # True if min_constituents not met
    skip_reason: str | None


# ---------- run_rebalance ---------- #


def run_rebalance(
    db_engine: Engine,
    industry_name: str,
    rebalance_date: date,
    *,
    n: int = 10,
    exchange: str | None = None,
    min_constituents: int | None = None,
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

    # ----- chain-link: value the OLD basket on R (the continuity
    # anchor) BEFORE we close it, then compute + store the splice
    # factor. compute_index_value_for_date is read-only and, at this
    # point, list_as_of(R) still returns the OLD constituents. -----
    from .index_compute import compute_index_value_for_date as _civ
    from ..persistence.fundamentals_repo import (
        IndustryIndexConstituentRepository as _CR,
        IndustryIndexPeriodRepository as _PR,
    )
    with db_engine.connect() as _c0:
        _has_prior = len(_CR(_c0).list_active_for_index(industry_name)) > 0
    if _has_prior:
        _old_cv = _civ(db_engine, industry_name, rebalance_date)
        if _old_cv is not None and _old_cv.value > 0:
            _link_factor = _old_cv.value / idx.base_value
            _prior_value = _old_cv.value
        else:
            logger.warning(
                "run_rebalance: no prior-basket value on %s for %r — "
                "chain breaks here; link_factor=1.0",
                rebalance_date, industry_name,
            )
            _link_factor = 1.0
            _prior_value = None
        _is_base = False
    else:
        _link_factor = 1.0
        _prior_value = None
        _is_base = True
    with transaction(db_engine) as _cw:
        _PR(_cw).upsert_period(
            industry_name, rebalance_date=rebalance_date,
            link_factor=_link_factor, is_base=_is_base,
            prior_value=_prior_value,
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
    from_date: date,
    to_date: date,
    resume_run_id: int | None = None,
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

    indexed: list[tuple[str, date, int]] = []
    failed: list[tuple[str, str]] = []

    for industry, sector, _count in qualified:
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


# === STEP 14A: CLUSTER A — runners.py truncated ===

# === STEP 14A-FIXUP: _fetch_bars_at_timeframe restored ===
# v1_runners.py imports this helper; it was incorrectly deleted in
# the original 14a truncation.

def _fetch_bars_at_timeframe(conn, ticker_id, timeframe):
    """Fetch bars for per-ticker compute, aggregating 1D → 1W on the fly.

    ohlcv_bars stores raw 1D bars from the ETL; 1W is always derived
    via on-demand daily→weekly aggregation. The per-ticker batch
    runners route through this helper instead of asking the repo for
    a timeframe that isn't physically stored.

    Returns a list of OHLCVBar objects (empty if no usable source).
    """
    from sqlalchemy import text as _text
    from ..persistence.bars_repo import BarsRepository
    from ..indicators.aggregation import daily_to_weekly
    from ..domain import Timeframe

    rows = conn.execute(_text("""
        SELECT DISTINCT timeframe FROM ohlcv_bars WHERE ticker_id = :tid
    """), {"tid": ticker_id}).all()
    available = {r.timeframe for r in rows}

    if timeframe in available:
        return BarsRepository(conn).fetch_history(
            ticker_id, Timeframe(timeframe), limit=10000,
        )
    if timeframe == "1W" and "1D" in available:
        tz_row = conn.execute(_text(
            "SELECT COALESCE(tz, 'UTC') AS tz "
            "FROM tickers WHERE ticker_id = :tid"
        ), {"tid": ticker_id}).first()
        tz = tz_row.tz if tz_row else "UTC"
        daily_bars = BarsRepository(conn).fetch_history(
            ticker_id, Timeframe.DAILY, limit=10000,
        )
        return daily_to_weekly(
            daily_bars, include_partial_last_week=True, tz=tz,
        )
    return []


def build_rebalance_chain(
    db_engine: Engine,
    industry_name: str,
    *,
    exchange: str,
    through: date,
    n: int = 12,
    min_constituents: int = 4,
) -> dict:
    """Build the full annual-April rebalance chain for one existing index.

    Walks every April rebalance date from the index's base_date to `through`
    (inclusive) via _annual_april_rebalance_dates, calling run_rebalance for
    each in ascending order. Ascending order matters: each period's continuity
    splice reads the prior period's stored link_factor.

    Returns {'industry': ..., 'rebalances': [(date, picked|None, skipped_reason)],
             'n_built': int, 'n_skipped': int}.
    """
    from ..persistence.fundamentals_repo import IndustryIndexRepository

    with db_engine.connect() as conn:
        idx = IndustryIndexRepository(conn).get(industry_name)
    if idx is None:
        raise ValueError(
            f"build_rebalance_chain: index {industry_name!r} does not exist"
        )

    dates = _annual_april_rebalance_dates(db_engine, idx.base_date, through)
    rebalances: list[tuple] = []
    n_built = 0
    n_skipped = 0
    for rd in dates:
        try:
            result = run_rebalance(
                db_engine, industry_name, rd,
                n=n, exchange=exchange, min_constituents=min_constituents,
            )
            if result.rebalance_skipped:
                rebalances.append((rd, None, result.skip_reason))
                n_skipped += 1
            else:
                rebalances.append((rd, result.constituents_picked, None))
                n_built += 1
        except Exception as e:  # noqa: BLE001
            logger.exception(
                "build_rebalance_chain: %s on %s failed: %s",
                industry_name, rd, e,
            )
            rebalances.append((rd, None, f"error: {e}"))
            n_skipped += 1

    logger.info(
        "build_rebalance_chain %s: %d rebalances built, %d skipped "
        "(base_date=%s through=%s)",
        industry_name, n_built, n_skipped, idx.base_date, through,
    )
    return {
        "industry": industry_name,
        "rebalances": rebalances,
        "n_built": n_built,
        "n_skipped": n_skipped,
    }


def build_all_industry_index_chains(
    db_engine: Engine,
    exchange: str,
    *,
    through: date,
    n: int = 12,
    min_constituents: int = 4,
    base_value: float = 1000.0,
) -> dict:
    """Discover qualifying industries and build each full annual-April chain.

    1. Discover industries with >= min_constituents active EQ tickers (reuses
       run_industry_index_build's discovery query).
    2. For each, find the first April rebalance date at/after the earliest
       mcap date, upsert an ANNUAL_APR index anchored there.
    3. build_rebalance_chain for each.

    Returns {'qualified': [...], 'unqualified': [...], 'chains': [chain_dict],
             'failed': [(industry, err)]}.
    """
    from ..persistence.fundamentals_repo import IndustryIndexRepository

    # ---- 1. discover candidate industries (same query as build) ----
    with db_engine.connect() as conn:
        rows = conn.execute(text("""
            WITH effective_industry AS (
                SELECT
                    t.ticker_id,
                    COALESCE(io.industry, ii.industry) AS industry,
                    COALESCE(io.sector, ii.sector) AS sector
                FROM tickers t
                LEFT JOIN industry_override io ON io.ticker_id = t.ticker_id
                LEFT JOIN instrument_industry ii ON ii.ticker_id = t.ticker_id
                WHERE t.is_active = TRUE
                  AND t.instrument_type = 'EQ'
                  AND t.exchange = :ex
            )
            SELECT ei.industry, MAX(ei.sector) AS sector, COUNT(*) AS c
            FROM effective_industry ei
            WHERE ei.industry IS NOT NULL
            GROUP BY ei.industry
            ORDER BY ei.industry
        """), {"ex": exchange}).fetchall()

    qualified: list[tuple[str, str, int]] = []
    unqualified: list[tuple[str, str, int]] = []
    for industry, sector, c in rows:
        (qualified if int(c) >= min_constituents else unqualified).append(
            (industry, sector or "Unknown", int(c))
        )

    # ---- 2. earliest mcap date → first April rebalance ----
    with db_engine.connect() as conn:
        earliest = conn.execute(text("""
            SELECT MIN(mc.trade_date)
            FROM daily_market_cap mc
            JOIN tickers t ON t.ticker_id = mc.ticker_id
            WHERE t.exchange = :ex
        """), {"ex": exchange}).scalar()

    chains: list[dict] = []
    failed: list[tuple[str, str]] = []
    if earliest is None:
        return {
            "qualified": qualified, "unqualified": unqualified,
            "chains": [], "failed": [(i, "no mcap data") for i, _, _ in qualified],
        }

    first_aprils = _annual_april_rebalance_dates(db_engine, earliest, through)
    if not first_aprils:
        return {
            "qualified": qualified, "unqualified": unqualified,
            "chains": [], "failed": [
                (i, "no April trading day in range") for i, _, _ in qualified
            ],
        }
    base_date = first_aprils[0]

    # ---- 3. per industry: upsert ANNUAL_APR index + build chain ----
    for industry, sector, _c in qualified:
        try:
            with db_engine.connect() as conn:
                existing = IndustryIndexRepository(conn).get(industry)
            with transaction(db_engine) as conn:
                IndustryIndexRepository(conn).upsert(
                    industry,
                    sector_name=sector,
                    base_date=(existing.base_date if existing else base_date),
                    base_value=(existing.base_value if existing else base_value),
                    rebalance_schedule="ANNUAL_APR",
                    min_constituents=min_constituents,
                )
            chain = build_rebalance_chain(
                db_engine, industry,
                exchange=exchange, through=through,
                n=n, min_constituents=min_constituents,
            )
            chains.append(chain)
        except Exception as e:  # noqa: BLE001
            logger.exception(
                "build_all_industry_index_chains: %s failed: %s", industry, e,
            )
            failed.append((industry, str(e)))

    logger.info(
        "build_all_industry_index_chains %s: %d chains built, %d qualified, "
        "%d unqualified, %d failed",
        exchange, len(chains), len(qualified), len(unqualified), len(failed),
    )
    return {
        "qualified": qualified,
        "unqualified": unqualified,
        "chains": chains,
        "failed": failed,
    }


def run_index_value_backfill_all(
    db_engine: Engine,
    *,
    from_date: date,
    to_date: date,
    force: bool = False,
) -> dict:
    """Run the daily index-value backfill for EVERY active index.

    Iterates ACTIVE industry_index rows and calls run_index_value_backfill for
    each over [from_date, to_date]. Each per-industry call walks its own trading
    days (from daily_market_cap), skips pre-base_date days, and writes
    industry_index_value rows using the chain-linked value model.

    Returns {'industries': [(name, run_id|None, error|None)], 'n_ok', 'n_failed'}.
    """
    from ..persistence.fundamentals_repo import IndustryIndexRepository

    with db_engine.connect() as conn:
        indices = IndustryIndexRepository(conn).list_all()
    active = [ix for ix in indices if getattr(ix, "status", "ACTIVE") == "ACTIVE"]

    results: list[tuple] = []
    n_ok = 0
    n_failed = 0
    for ix in active:
        name = ix.industry_name
        try:
            run_id = run_index_value_backfill(
                db_engine, name,
                from_date=from_date, to_date=to_date, force=force,
            )
            results.append((name, run_id, None))
            n_ok += 1
        except Exception as e:  # noqa: BLE001
            logger.exception(
                "run_index_value_backfill_all: %s failed: %s", name, e,
            )
            results.append((name, None, str(e)))
            n_failed += 1

    logger.info(
        "run_index_value_backfill_all %s..%s: %d ok, %d failed (of %d active)",
        from_date, to_date, n_ok, n_failed, len(active),
    )
    return {"industries": results, "n_ok": n_ok, "n_failed": n_failed}
