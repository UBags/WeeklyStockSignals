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
    db_engine: Engine, run_id: int,
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
    if run.phase != "industry":
        raise ValueError(
            f"Run {run_id} is for phase {run.phase!r}, not 'industry'"
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
