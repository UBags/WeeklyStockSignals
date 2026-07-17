"""Forward-walk orchestration (spec 02_signal_forward_walk_labels §4).

Compute is fanned out over a process pool: each child holds ONE gui-role engine,
reads a ticker's inputs, and RETURNS its rows. The parent is the SOLE writer —
it collects returned rows and upserts them in batches through a single
trip_etl_user engine. Clean role split (workers read, main writes), no
cross-process session sharing, re-runnable and idempotent on
signal_enrichment_id.

Deliberately NOT fan_out_processes: that primitive has each worker write its own
disjoint rows and discards return values. Here the two DB roles (gui read, etl
write) live on opposite sides of the process boundary, so the parent must gather
worker output and be the only thing holding an etl connection.

spawn, not fork: forking a parent holding live psycopg sockets would duplicate
those file descriptors into children. spawn re-imports clean and each child
builds its own engine in _child_init.
"""
from __future__ import annotations

import logging
import multiprocessing
import os
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import Engine

from ..config import Config
from ..domain import Timeframe
from ..domain.forward_walk import SignalForwardWalk
from ..persistence.connection import (
    build_db_url_for_role,
    create_engine_for_role,
    transaction,
)
from ..persistence.signal_labels_repo import SignalLabelsRepository
from .forward_walk_runner import load_market_series, walk_one_ticker
from .runners import _load_active_tickers_for_exchange

logger = logging.getLogger(__name__)


@dataclass
class ForwardWalkOutcome:
    tickers: int = 0
    rows_written: int = 0
    failed: list[tuple[int, str]] = field(default_factory=list)


def _resolve_processes(
    requested: int | None = None, *, cpu_logical: int | None = None,
) -> int:
    """Child process count. ``requested`` wins when positive; otherwise
    round(logical / 3), which equals round(2/3 × physical) on a 2×-SMT box
    (= 4 on the NUC's 12 logical / 6 physical cores)."""
    if requested is not None and requested > 0:
        return requested
    cpu = cpu_logical or os.cpu_count() or 4
    return max(1, round(cpu / 3))


def _pool_kwargs(config: Config) -> dict:
    db = config.database
    return {
        "pool_pre_ping": True,
        "pool_size": db.pool_size,
        "max_overflow": db.pool_max_overflow,
        "pool_timeout": db.pool_timeout_seconds,
        "pool_recycle": db.pool_recycle_seconds,
    }


# --------------------------------------------------------------------------- #
# Child process state (spawn re-imports this module clean per child).
# --------------------------------------------------------------------------- #
_CHILD: dict = {}


def _child_init(
    gui_url: str,
    pool_kwargs: dict,
    exchange: str,
    market_series: Sequence[tuple[date, float]],
    signal_timeframe: str,
) -> None:
    global _CHILD
    from sqlalchemy import create_engine

    _CHILD = {
        "engine": create_engine(gui_url, **pool_kwargs),
        "exchange": exchange,
        "market_series": market_series,
        "signal_timeframe": signal_timeframe,
    }


def _walk_worker(ticker_id: int) -> tuple[int, list[SignalForwardWalk]]:
    """Runs in a child process. Reads via the child's gui engine, returns the
    ticker's forward-walk rows. Never writes."""
    st = _CHILD
    with st["engine"].connect() as conn:
        rows = walk_one_ticker(
            conn,
            ticker_id,
            exchange=st["exchange"],
            market_series=st["market_series"],
            signal_timeframe=st["signal_timeframe"],
        )
    return ticker_id, rows


# --------------------------------------------------------------------------- #
# Single-writer batch loop (pure seam — testable without a pool or DB).
# --------------------------------------------------------------------------- #
def _write_in_batches(
    row_groups: Iterable[Sequence[SignalForwardWalk]],
    upsert: Callable[[list[SignalForwardWalk]], int],
    batch_size: int,
) -> int:
    """Accumulate per-ticker row groups and flush via ``upsert`` in batches of
    at least ``batch_size`` rows. Returns the total rows written."""
    buffer: list[SignalForwardWalk] = []
    written = 0
    for rows in row_groups:
        buffer.extend(rows)
        if len(buffer) >= batch_size:
            written += upsert(buffer)
            buffer = []
    if buffer:
        written += upsert(buffer)
    return written


def run_forward_walk(
    config: Config,
    exchange: str,
    *,
    ticker_ids: Sequence[int] | None = None,
    processes: int | None = None,
    batch_size: int = 500,
    signal_timeframe: str = str(Timeframe.WEEKLY),
    progress_cb: Callable[[str, str], None] | None = None,
) -> ForwardWalkOutcome:
    """Compute + upsert forward-walk rows for every active ticker on
    ``exchange`` (or ``ticker_ids``). Processes compute, the parent writes."""
    if ticker_ids is None:
        read_engine = create_engine_for_role(config, "gui")
        try:
            ticker_ids = [
                t["ticker_id"]
                for t in _load_active_tickers_for_exchange(read_engine, exchange)
            ]
        finally:
            read_engine.dispose()
    ticker_ids = sorted(ticker_ids)

    outcome = ForwardWalkOutcome(tickers=len(ticker_ids))
    if not ticker_ids:
        return outcome

    # Market benchmark loaded ONCE (read) and shared read-only with children.
    with create_engine_for_role(config, "gui").connect() as conn:
        market_series = load_market_series(conn, exchange)

    gui_url = build_db_url_for_role(config, "gui")
    pool_kwargs = _pool_kwargs(config)
    procs = _resolve_processes(processes)
    procs = max(1, min(procs, len(ticker_ids)))

    etl_engine: Engine = create_engine_for_role(config, "etl")

    def _upsert(rows: list[SignalForwardWalk]) -> int:
        with transaction(etl_engine) as conn:
            return SignalLabelsRepository(conn).upsert_forward_walk(rows)

    started = time.monotonic()
    total = len(ticker_ids)
    completed = 0
    every = max(1, total // 100)
    ctx = multiprocessing.get_context("spawn")

    logger.info(
        "forward_walk %s: %d tickers on %d processes", exchange, total, procs,
    )

    try:
        with ProcessPoolExecutor(
            max_workers=procs,
            mp_context=ctx,
            initializer=_child_init,
            initargs=(gui_url, pool_kwargs, exchange, market_series,
                      signal_timeframe),
        ) as ex:
            futures = {
                ex.submit(_walk_worker, tid): tid for tid in ticker_ids
            }

            def _results() -> Iterator[list[SignalForwardWalk]]:
                nonlocal completed
                for fut in as_completed(futures):
                    tid = futures[fut]
                    try:
                        _tid, rows = fut.result()
                    except Exception as e:  # noqa: BLE001 — isolation
                        outcome.failed.append((tid, f"{type(e).__name__}: {e}"))
                        logger.warning("forward_walk ticker %s FAILED: %s",
                                       tid, e)
                        rows = []
                    completed += 1
                    if completed % every == 0 or completed == total:
                        _emit(progress_cb, exchange, completed, total, started,
                              len(outcome.failed))
                    yield rows

            outcome.rows_written = _write_in_batches(
                _results(), _upsert, batch_size,
            )
    finally:
        etl_engine.dispose()

    logger.info(
        "forward_walk %s complete: %d rows, %d tickers, %d failed",
        exchange, outcome.rows_written, outcome.tickers, len(outcome.failed),
    )
    return outcome


def _emit(progress_cb, exchange, completed, total, started, n_failed) -> None:
    if progress_cb is None:
        return
    elapsed = time.monotonic() - started
    rate = completed / elapsed if elapsed > 0 else 0.0
    eta = (total - completed) / rate if rate > 0 else 0.0
    msg = f"{completed}/{total} tickers (eta {int(eta)}s)"
    if n_failed:
        msg += f" [{n_failed} failed]"
    try:
        progress_cb(exchange, msg)
    except Exception:  # noqa: BLE001
        logger.debug("progress_cb raised (ignored)", exc_info=True)
