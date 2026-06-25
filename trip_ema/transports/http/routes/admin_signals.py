"""Signal-generation admin routes (Phase 15b).

Two endpoints under /v1/admin/signals/* fronted by the bearer-token
admin gate:

  POST /generate    Fire a backtest signal-generation run for an
                    exchange. Returns 202 with the etl_runs.run_id.
                    Concurrent-run guard prevents two in-flight runs
                    for the same exchange.

  GET  /generated   Read metadata about the on-disk JSON file for
                    an exchange. Returns 200 with `exists: false`
                    when the file doesn't exist (NOT a 404 — that's
                    reserved for "exchange not found" cases, and
                    "no file yet" is a normal post-fresh-deploy
                    state).

Lifecycle ownership (per design Finding 4):
  Unlike the existing /v1/admin/etl/* action endpoints, which submit
  to the executor with NO etl_runs row pre-opened (the runner opens
  it from inside the background thread), THIS route opens the
  etl_runs row in the request thread BEFORE submitting to the
  executor. Reasons:
    (1) The concurrent-run guard's SELECT on etl_runs.outcome IS NULL
        wouldn't see a row opened from the background thread until
        AFTER the executor scheduled the task and the runner started
        running. That window is wide enough (potentially seconds) for
        two concurrent POSTs to both pass the guard and both fire.
    (2) The 202 response includes run_id; without pre-opening, we'd
        have to either block on the runner OR return None and force
        the operator to poll. Neither is acceptable.
  The route opens the row via EtlStatusRepository.open_run (which
  Phase 15a updated to take `exchange=`) inside the same transaction
  as the guard SELECT, holding an advisory lock keyed on the
  exchange. The runner is then called with `run_id=<that_id>`,
  which makes generate_historical_signals skip its own open_run
  call and only close the row at completion. The contract is
  locked in by Phase 15a's test_passing_run_id_skips_open_but_still_closes.

Output path resolution (assumption, not config):
  The signal generator writes to `Path.cwd() / "generated_signals" /
  "{exchange}.json"`. The GET endpoint reads from the SAME relative
  path. This works as long as the HTTP service is started from the
  repo root (which is the convention: scripts/restart_service.sh
  does `cd "$REPO_ROOT"` before launching). If a future deployment
  needs a configurable output dir, promote `_OUTPUT_DIR_NAME` to a
  config field — for Phase 15 we keep it constant.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import datetime, UTC
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import Engine, text

from ....config import Config
from ....domain import Timeframe
from ....etl.exchanges import UnsupportedExchangeError, validate_exchange
from ....etl.status_repo import EtlStatusRepository
from ....etl.types import RunKind
from ..dependencies import (
    get_config,
    get_db_engine,
    get_etl_executor,
    require_admin_token,
)
from ..schemas import (
    SignalGenerateAcceptedPayload,
    SignalGeneratedFilePayload,
)

logger = logging.getLogger(__name__)


router = APIRouter(
    prefix="/v1/admin/signals",
    tags=["admin", "signals"],
    dependencies=[Depends(require_admin_token)],
)


# Phase 15a's signal_generator writes here. Both endpoints share this
# constant so the route never disagrees with the writer about the
# path. The cwd is resolved at request time, not import time — so
# tests can chdir to a tmp dir before invoking the route.
_OUTPUT_DIR_NAME = "generated_signals"


def _output_dir() -> Path:
    return Path.cwd() / _OUTPUT_DIR_NAME


def _require_executor(executor):
    """Mirror admin_etl's _require_executor — 503 when the executor
    is None (admin endpoints disabled)."""
    if executor is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Signal-generation admin endpoints are disabled. Ensure "
                "AppState.etl_executor is provided at startup."
            ),
        )


def _validate_exchange_or_400(exchange: str, config: Config) -> None:
    """Convert UnsupportedExchangeError to HTTP 400 with the exception
    message as the detail. Matches admin_etl's pattern."""
    try:
        validate_exchange(exchange, config)
    except UnsupportedExchangeError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        ) from e


def _parse_timeframes_or_400(timeframe_strs: list[str]) -> list[Timeframe]:
    """Convert the repeated `?timeframe=` strings to Timeframe enums,
    rejecting unknown values and unsupported-for-15a values with 400.

    Phase 15a supports only 1D and 1W (per design D3). Intraday
    timeframes are deferred.
    """
    if not timeframe_strs:
        # FastAPI's `Query(default=["1W"])` should prevent this, but
        # defensive in case the route is invoked programmatically.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="At least one --timeframe value is required.",
        )
    parsed: list[Timeframe] = []
    allowed = {Timeframe.DAILY, Timeframe.WEEKLY}
    for tf_str in timeframe_strs:
        try:
            tf = Timeframe(tf_str)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Unknown timeframe: {tf_str!r}. Allowed values: "
                    f"{sorted(t.value for t in allowed)}."
                ),
            ) from None
        if tf not in allowed:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Timeframe {tf.value!r} not supported by Phase 15. "
                    f"Allowed: {sorted(t.value for t in allowed)}. "
                    f"(Intraday is deferred — see design OOS-5.)"
                ),
            )
        parsed.append(tf)
    return parsed


def _exchange_advisory_lock_key(exchange: str) -> int:
    """A stable 32-bit integer derived from the exchange string, for
    use with pg_advisory_xact_lock. PostgreSQL's advisory lock takes
    a bigint key; we derive it via SHA-256 modulo to keep the key
    stable across processes and across restarts.

    Why advisory lock at all: the concurrent-run guard does a SELECT
    on etl_runs followed by an INSERT. Under read-committed isolation
    (the default), two concurrent transactions can each pass the
    SELECT before either commits the INSERT — both then INSERT and
    we end up with two in-flight rows for the same exchange.

    pg_advisory_xact_lock serializes the SELECT-then-INSERT for a
    given exchange. The lock is released automatically at commit
    or rollback; no cleanup needed.
    """
    digest = hashlib.sha256(f"signal_backtest:{exchange}".encode()).digest()
    # Take the first 4 bytes and unpack as a signed 32-bit int. The
    # bigint range covers any 32-bit value with room to spare.
    return int.from_bytes(digest[:4], byteorder="big", signed=True)


# ---------- POST /v1/admin/signals/generate ---------- #


@router.post(
    "/generate",
    response_model=SignalGenerateAcceptedPayload,
    status_code=status.HTTP_202_ACCEPTED,
)
def admin_signals_generate(
    exchange: str = Query(..., min_length=2, max_length=10),
    force_full: bool = Query(False),
    timeframe: list[str] = Query(default=["1W"]),
    db_engine: Engine = Depends(get_db_engine),
    config: Config = Depends(get_config),
    executor=Depends(get_etl_executor),
) -> SignalGenerateAcceptedPayload:
    """Fire a backtest signal-generation run for `exchange`.

    Submits asynchronously; the runner writes the etl_runs row's
    outcome at completion. The 202 response includes the run_id so
    the GUI/operator can correlate to the etl_runs row in
    /v1/admin/etl/status.

    Concurrent-run guard: if there's already an in-flight
    signal_backtest run for this exchange (i.e. an etl_runs row with
    kind='signal_backtest', exchange=:ex, outcome IS NULL), the route
    returns 409 with the existing run_id. Wait for completion or kill
    the worker before retrying.

    `force_full=true` wipes the per-(ticker, timeframe) resume rows
    for this exchange and recomputes from each ticker's earliest bar.
    Use after changing engine params (`ema:`/`atr:` or
    `exchange_overrides:`).
    """
    _require_executor(executor)
    _validate_exchange_or_400(exchange, config)
    tfs = _parse_timeframes_or_400(timeframe)

    logger.info(
        "Admin: signal-generation requested",
        extra={
            "exchange": exchange,
            "timeframes": [t.value for t in tfs],
            "force_full": force_full,
        },
    )

    # Concurrent-run guard + open etl_runs row, serialized per-exchange
    # via advisory lock. One transaction; either we get the row open
    # and return 202, or we 409 with the in-flight run_id.
    correlation_id = str(uuid.uuid4())
    with db_engine.begin() as conn:
        # Hold the lock for the rest of this transaction. Concurrent
        # POSTs for the same exchange wait here.
        conn.execute(
            text("SELECT pg_advisory_xact_lock(:k)"),
            {"k": _exchange_advisory_lock_key(exchange)},
        )
        # Now safely check for in-flight.
        existing = conn.execute(
            text(
                """
                SELECT run_id FROM etl_runs
                WHERE kind = 'signal_backtest'
                  AND exchange = :ex
                  AND outcome IS NULL
                ORDER BY started_at DESC
                LIMIT 1
                """
            ),
            {"ex": exchange},
        ).first()
        if existing is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"A signal_backtest run is already in flight for "
                    f"exchange='{exchange}' (run_id={existing.run_id}). "
                    f"Wait for completion or kill the worker."
                ),
            )
        # Open the row inside the same transaction. The runner will
        # close it at the end via EtlStatusRepository.close_run.
        status_repo = EtlStatusRepository(conn)
        run_id = status_repo.open_run(
            RunKind.SIGNAL_BACKTEST,
            exchange=exchange,
            correlation_id=correlation_id,
            notes=(
                f"force_full={force_full}, "
                f"timeframes={[t.value for t in tfs]}"
            ),
        )

    # Submit the runner. NOT inside the begin() block — we want the
    # row visible to the runner via a fresh connection (it'll re-open
    # the connection inside its own commit-per-ticker loop), so the
    # caller's transaction has to commit first.
    executor.submit(
        _run_signal_generation_safe,
        db_engine,
        config,
        exchange,
        tfs,
        force_full,
        run_id,
    )

    return SignalGenerateAcceptedPayload(
        run_id=run_id,
        exchange=exchange,
        timeframes=[t.value for t in tfs],
        force_full=force_full,
        estimated_output_path=f"{_OUTPUT_DIR_NAME}/{exchange}.json",
        submitted_at=datetime.now(UTC),
    )


# ---------- GET /v1/admin/signals/generated ---------- #


@router.get(
    "/generated",
    response_model=SignalGeneratedFilePayload,
)
def admin_signals_generated(
    exchange: str = Query(..., min_length=2, max_length=10),
    config: Config = Depends(get_config),
) -> SignalGeneratedFilePayload:
    """Read metadata about the on-disk JSON file for `exchange`.

    Returns the file's size, mtime, and the metadata fields from the
    file (schema_version, generated_by_run_id, counts) but NOT the
    full `by_date` payload — that would be too large to return here.

    When the file doesn't exist, returns 200 with `exists: false`.
    That's a normal post-deploy state; 404 is reserved for "exchange
    is not a valid HTTP path component". An unknown/unconfigured
    exchange returns 400.
    """
    _validate_exchange_or_400(exchange, config)

    path = _output_dir() / f"{exchange}.json"
    if not path.is_file():
        return SignalGeneratedFilePayload(
            exchange=exchange,
            exists=False,
        )

    # Pre-read stat for mtime + size. Done BEFORE reading content so
    # the values reflect the file as the operator's request observed
    # it, not whatever a concurrent writer might have produced
    # mid-read.
    try:
        st = path.stat()
    except OSError as e:
        # Extraordinarily unlikely between is_file() and stat() but
        # possible (e.g. concurrent delete). Treat as not-exist
        # rather than 500.
        logger.warning(
            "Stat failed for existing-looking signal file",
            extra={"exchange": exchange, "path": str(path), "error": str(e)},
        )
        return SignalGeneratedFilePayload(
            exchange=exchange,
            exists=False,
        )

    # Read + parse. For Phase 15a-sized files (NSE: ~14MB) this is
    # well under 1s. The design's escape-hatch (use ijson if file
    # sizes ever exceed 200MB) hasn't been triggered yet.
    try:
        with path.open("r", encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        # A corrupt or unreadable file is a real problem — surface it
        # rather than lying with exists=false. A 500 is appropriate
        # because the file IS there but we can't tell the operator
        # what it contains.
        logger.error(
            "Failed to parse signal file",
            extra={"exchange": exchange, "path": str(path), "error": str(e)},
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                f"Signal file for exchange {exchange!r} exists at "
                f"{path} but could not be parsed: {e}"
            ),
        ) from e

    return SignalGeneratedFilePayload(
        exchange=exchange,
        exists=True,
        filename=path.name,
        size_bytes=st.st_size,
        mtime=datetime.fromtimestamp(st.st_mtime, tz=UTC),
        generated_by_run_id=payload.get("generated_by_run_id"),
        schema_version=payload.get("schema_version"),
        counts=payload.get("counts"),
    )


# ---------- Internal runner wrapper (currently 501) ---------- #
#
# Phase 15a's signal_generator.py was removed in the demolition.
# This wrapper exists so the route signature stays stable, but it
# always raises 501. Operators run the equivalent CLI command:
#
#   trip-ema-etl compute-signals --all-tickers --force \
#       --timeframe 1W [--ticker SYMBOL]
#
# Re-wiring the route to the working compute-signals path is
# tracked separately (Phase-15 modernization slice — also covers
# moving the GUI signal_generation_panel off the deprecated JSON
# file output and onto the signal_events DB read endpoints).


def _run_signal_generation_safe(
    db_engine, config, exchange, timeframes, force_full, run_id,
):
    from fastapi import HTTPException
    raise HTTPException(
        status_code=501,
        detail=(
            "Historical signal generation via HTTP is unavailable. "
            "Use the CLI instead: "
            "`trip-ema-etl compute-signals --all-tickers --force "
            "--timeframe 1W`. Phase-15 modernization will re-wire "
            "this route to the working compute-signals path."
        ),
    )
