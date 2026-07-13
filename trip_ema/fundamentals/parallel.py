"""Shared bounded-parallel fan-out primitive.

The one place where ``for item in items: work(item)`` becomes parallel.
Every long per-item loop in the codebase — backfill's full-history
stages and the live intraday/eod cycle's incremental stages — routes
through :func:`fan_out`, so parallelism, error isolation and progress
logging are written and tuned in exactly one place. Backfill and the
live cycle then differ only in *scope* (full history vs a two-week
window) and *which write-gates fire*, never in how they iterate.

What this gives every caller:

  - **Per-item error isolation.** One item raising never aborts the
    batch; failures are collected and returned.
  - **A progress heartbeat.** Elapsed + ETA emitted via ``progress_cb``
    roughly every 1% of the list, so long stages stop being silent.
  - **A worker count that can't outrun the DB pool.** Derived from CPU
    (~2/3 of logical cores, since DB-round-trip-bound work releases the
    GIL on IO) and hard-capped below the connection-pool ceiling.

The single contract a ``worker_fn`` MUST honour: it opens its *own*
connection from the engine pool. A SQLAlchemy ``Connection`` is not
threadsafe, so nothing here shares one across workers — each worker_fn
receives only the item and does its own ``with db_engine.connect()`` /
``transaction(db_engine)`` inside. Parallelise only over axes whose
writes touch disjoint primary-key ranges (days, tickers, industries),
so there is no row contention between workers.
"""
from __future__ import annotations

import logging
import os
import time
import multiprocessing
from concurrent.futures import (
    ProcessPoolExecutor,
    ThreadPoolExecutor,
    as_completed,
)
from dataclasses import dataclass, field
from typing import TypeVar
from collections.abc import Callable, Sequence

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Fraction of logical CPUs used by default. DB-round-trip-bound work
# spends most of its time waiting on IO (GIL released), so ~2/3 of the
# cores overlaps the wait while keeping the box responsive.
_DEFAULT_CPU_FRACTION = 2.0 / 3.0

# Absolute ceiling on workers regardless of CPU count. The DB
# connection pool is the true limit — pool_size(5) + max_overflow(10)
# = 15 by default — and we must leave connections for the main
# thread's own transactions plus any co-resident service threads.
_HARD_WORKER_CAP = 12


def resolve_max_workers(requested: int | None = None) -> int:
    """Return the worker count to use.

    ``requested`` (e.g. from ``config.compute.max_workers``) wins when
    positive, still clamped to the hard cap. Otherwise default to
    ~2/3 of logical CPUs, floored at 2 and capped at
    :data:`_HARD_WORKER_CAP`.
    """
    if requested is not None and requested > 0:
        return min(requested, _HARD_WORKER_CAP)
    cpu = os.cpu_count() or 4
    return max(2, min(int(cpu * _DEFAULT_CPU_FRACTION), _HARD_WORKER_CAP))


@dataclass
class FanOutOutcome:
    """Aggregate result of a :func:`fan_out` run.

    ``done`` is the count of items whose ``worker_fn`` returned without
    raising. ``failed`` pairs each offending item with its error string
    (``f"{type(e).__name__}: {e}"``), matching the existing
    ``(done, failed)`` contract used by the backfill stage helpers.
    """

    done: int = 0
    failed: list[tuple[object, str]] = field(default_factory=list)

    def as_tuple(self) -> tuple[int, list[tuple[object, str]]]:
        return self.done, self.failed


def _fmt_secs(s: float) -> str:
    s = int(s)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m{sec:02d}s"
    if m:
        return f"{m}m{sec:02d}s"
    return f"{sec}s"


def fan_out(
    items: Sequence[T],
    worker_fn: Callable[[T], object],
    *,
    label: str,
    max_workers: int | None = None,
    progress_cb: Callable[[str, str], None] | None = None,
    heartbeat_pct: int = 1,
) -> FanOutOutcome:
    """Run ``worker_fn(item)`` over ``items`` on a bounded thread pool.

    Args:
        items: the work list. Order of execution is not guaranteed.
        worker_fn: called once per item. MUST open its own DB
            connection (see module docstring). Its return value is
            ignored; only success/failure is tracked.
        label: stage name used in progress messages and logs, e.g.
            ``"stage_2_stock_state"``.
        max_workers: explicit worker count; ``None`` resolves via
            :func:`resolve_max_workers`.
        progress_cb: optional ``callback(label, message)`` invoked with
            an ``"N/total (elapsed, eta …)"`` heartbeat.
        heartbeat_pct: emit a heartbeat roughly every this-many percent
            of the list (default 1%). Always emits on the final item.

    Returns:
        :class:`FanOutOutcome` with the done count and per-item failures.
    """
    total = len(items)
    outcome = FanOutOutcome()
    if total == 0:
        _emit(progress_cb, label, "0/0 (nothing to do)")
        return outcome

    workers = resolve_max_workers(max_workers)
    # No point spinning up more threads than there are items.
    workers = max(1, min(workers, total))
    every = max(1, total * max(1, heartbeat_pct) // 100)

    logger.info(
        "%s: fan_out over %d items on %d workers", label, total, workers,
    )
    started = time.monotonic()
    completed = 0

    with ThreadPoolExecutor(
        max_workers=workers, thread_name_prefix=f"fanout-{label}",
    ) as ex:
        future_to_item = {
            ex.submit(worker_fn, item): item for item in items
        }
        for fut in as_completed(future_to_item):
            item = future_to_item[fut]
            try:
                fut.result()
                outcome.done += 1
            except Exception as e:  # noqa: BLE001 — isolation is the point
                outcome.failed.append(
                    (item, f"{type(e).__name__}: {e}"),
                )
                logger.warning("%s: item %r FAILED: %s", label, item, e)

            completed += 1
            if completed % every == 0 or completed == total:
                elapsed = time.monotonic() - started
                rate = completed / elapsed if elapsed > 0 else 0.0
                eta = (total - completed) / rate if rate > 0 else 0.0
                msg = (
                    f"{completed}/{total} done "
                    f"(elapsed {_fmt_secs(elapsed)}, "
                    f"eta {_fmt_secs(eta)})"
                )
                if outcome.failed:
                    msg += f" [{len(outcome.failed)} failed]"
                _emit(progress_cb, label, msg)

    elapsed = time.monotonic() - started
    logger.info(
        "%s: fan_out complete — %d ok, %d failed in %s",
        label, outcome.done, len(outcome.failed), _fmt_secs(elapsed),
    )
    return outcome


def _emit(
    progress_cb: Callable[[str, str], None] | None,
    label: str,
    message: str,
) -> None:
    """Best-effort progress emit — a broken callback never aborts work."""
    if progress_cb is None:
        return
    try:
        progress_cb(label, message)
    except Exception:  # noqa: BLE001
        logger.debug("%s: progress_cb raised (ignored)", label, exc_info=True)


# ---------------------------------------------------------------------------
# Process-pool fan-out.
#
# fan_out (above) is correct for IO-bound stages: the fetch and the industry
# passes spend their time waiting on yfinance or on Postgres, GIL released, and
# threads overlap that wait. It is the WRONG tool for a stage whose cost is a
# pure-Python compute loop. Measured on stage_2_stock_state: 8 threads produced
# 1.15 cores of work with Postgres sitting idle (1 active backend, 9 idle) and
# the box 52% idle. That is the GIL, and no amount of threads fixes it.
#
# fan_out_processes runs N *processes*, each of which runs the ordinary threaded
# fan_out over a contiguous slice of the work. Each process therefore reproduces
# the single-process behaviour we measured, and N of them multiply it.
# ---------------------------------------------------------------------------

# Default process count. The NUC has 6 physical cores; at ~1.15 cores of Python
# plus ~1.4 cores of Postgres per process, 3 processes lands near the physical
# core count and Postgres becomes the next constraint. More than this queues
# rather than parallelises.
_DEFAULT_PROCESSES = 3

# Set once per child by _child_init. Module-global because ProcessPoolExecutor's
# initializer has nowhere else to put it, and because we want ONE engine (and
# therefore one connection pool) per child process, not one per submitted slice.
_CHILD_ENGINE = None


def engine_spec_from_config(config) -> tuple[str, dict]:
    """Build a picklable engine recipe for child processes.

    Returns ``(url, pool_kwargs)`` — plain strings, ints and bools. Nothing
    here requires the child to unpickle a Config or to resolve the DB password
    from its own environment; the parent resolves everything once, using the
    same ``build_db_url`` and the same pool fields that
    ``create_engine_from_config`` uses, so a child engine is identical to the
    parent's by construction.
    """
    from ..persistence.connection import build_db_url

    db = config.database
    return build_db_url(config), {
        "pool_pre_ping": True,
        "pool_size": db.pool_size,
        "max_overflow": db.pool_max_overflow,
        "pool_timeout": db.pool_timeout_seconds,
        "pool_recycle": db.pool_recycle_seconds,
    }


def _child_init(engine_spec: tuple[str, dict]) -> None:
    """ProcessPoolExecutor initializer: one engine per child process."""
    global _CHILD_ENGINE
    from sqlalchemy import create_engine

    url, pool_kwargs = engine_spec
    _CHILD_ENGINE = create_engine(url, **pool_kwargs)


def _run_slice(
    worker_fn: Callable[[object, T], object],
    slice_items: list,
    threads: int,
    label: str,
) -> tuple[int, list[tuple[object, str]]]:
    """Runs inside a child process: threaded fan_out over one slice.

    ``worker_fn`` is called as ``worker_fn(engine, item)`` with the child's own
    engine, honouring fan_out's contract that each worker opens its own
    connection from the pool.
    """
    outcome = fan_out(
        slice_items,
        lambda item: worker_fn(_CHILD_ENGINE, item),
        label=label,
        max_workers=threads,
        progress_cb=None,  # parent reports at slice granularity
    )
    return outcome.done, outcome.failed


def _slices(items: Sequence[T], n: int) -> list[list]:
    """Chop ``items`` into ``n`` contiguous slices (last may be shorter)."""
    if n <= 0:
        return [list(items)]
    size = max(1, (len(items) + n - 1) // n)
    return [list(items[i:i + size]) for i in range(0, len(items), size)]


def fan_out_processes(
    items: Sequence[T],
    worker_fn: Callable[[object, T], object],
    *,
    engine_spec: tuple[str, dict],
    label: str,
    processes: int | None = None,
    threads_per_process: int | None = None,
    progress_cb: Callable[[str, str], None] | None = None,
    slices_per_process: int = 20,
) -> FanOutOutcome:
    """Run ``worker_fn(engine, item)`` over ``items`` on a process pool.

    Use this ONLY for stages whose cost is Python compute (the state engine).
    For IO-bound stages use :func:`fan_out`; threads are cheaper and correct
    there.

    Args:
        items: the work list. Must be picklable (ticker_ids, ints, strings).
        worker_fn: a MODULE-LEVEL function taking ``(engine, item)``.
            ProcessPoolExecutor pickles callables by qualified name, so a
            closure or a lambda will not work here. Same connection contract
            as fan_out: open your own connection inside.
        engine_spec: from :func:`engine_spec_from_config`. Each child builds
            its own Engine from this — engines and connections are never
            inherited across the process boundary.
        processes: child process count. Default :data:`_DEFAULT_PROCESSES`.
        threads_per_process: threads inside each child. Default resolves via
            :func:`resolve_max_workers`, i.e. the same count fan_out would
            have used on its own.
        slices_per_process: how many slices to cut per process. More slices =
            finer progress reporting and better tail balancing; fewer = less
            IPC. 20 gives ~60 slices over 2,673 tickers (~45 tickers each).

    Returns:
        :class:`FanOutOutcome` — same shape as :func:`fan_out`, so callers can
        swap between the two without changing their result handling.
    """
    total = len(items)
    outcome = FanOutOutcome()
    if total == 0:
        _emit(progress_cb, label, "0/0 (nothing to do)")
        return outcome

    procs = processes if (processes and processes > 0) else _DEFAULT_PROCESSES
    procs = max(1, min(procs, total))
    threads = resolve_max_workers(threads_per_process)

    chunks = _slices(items, procs * max(1, slices_per_process))
    every = max(1, total * 1 // 100)

    logger.info(
        "%s: fan_out_processes over %d items — %d processes x %d threads, "
        "%d slices",
        label, total, procs, threads, len(chunks),
    )
    started = time.monotonic()
    completed = 0
    last_emitted = 0

    # spawn, NOT fork. Forking a parent that holds live psycopg sockets would
    # duplicate those file descriptors into the children — two processes on one
    # Postgres connection. spawn re-imports clean and each child builds its own
    # engine via _child_init.
    ctx = multiprocessing.get_context("spawn")

    with ProcessPoolExecutor(
        max_workers=procs,
        mp_context=ctx,
        initializer=_child_init,
        initargs=(engine_spec,),
    ) as ex:
        future_to_chunk = {
            ex.submit(_run_slice, worker_fn, chunk, threads, label): chunk
            for chunk in chunks
        }
        for fut in as_completed(future_to_chunk):
            chunk = future_to_chunk[fut]
            try:
                done, failed = fut.result()
                outcome.done += done
                outcome.failed.extend(failed)
            except Exception as e:  # noqa: BLE001
                # The child died (OOM, segfault, un-picklable payload). We
                # cannot tell which item killed it, so the whole slice is
                # charged as failed rather than silently vanishing from the
                # counts.
                for item in chunk:
                    outcome.failed.append(
                        (item, f"slice failed: {type(e).__name__}: {e}"),
                    )
                logger.warning(
                    "%s: slice of %d items FAILED wholesale: %s",
                    label, len(chunk), e,
                )

            completed += len(chunk)
            if completed - last_emitted >= every or completed >= total:
                last_emitted = completed
                elapsed = time.monotonic() - started
                rate = completed / elapsed if elapsed > 0 else 0.0
                eta = (total - completed) / rate if rate > 0 else 0.0
                msg = (
                    f"{completed}/{total} done "
                    f"(elapsed {_fmt_secs(elapsed)}, "
                    f"eta {_fmt_secs(eta)})"
                )
                if outcome.failed:
                    msg += f" [{len(outcome.failed)} failed]"
                _emit(progress_cb, label, msg)

    elapsed = time.monotonic() - started
    logger.info(
        "%s: fan_out_processes complete — %d ok, %d failed in %s",
        label, outcome.done, len(outcome.failed), _fmt_secs(elapsed),
    )
    return outcome
