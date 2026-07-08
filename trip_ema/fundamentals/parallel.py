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
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable, Sequence, TypeVar

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
    progress_cb: "Callable[[str, str], None] | None" = None,
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
    progress_cb: "Callable[[str, str], None] | None",
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
