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
            last = FundamentalsRunRepository(conn).last_success(
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
            last = FundamentalsRunRepository(conn).last_success(
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
            last = FundamentalsRunRepository(conn).last_success(
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
            last = FundamentalsRunRepository(conn).last_success(
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
