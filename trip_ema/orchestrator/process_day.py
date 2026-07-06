"""Per-day signal processing — the single DB wrapper around the shared
derive core, gated by processing mode.

One entry point for all three drivers (intraday, eod, backfill). The signal
DERIVATION is the shared primitive `derive_signal_for_ticker_this_cycle`; this
module owns only the DB side — which of current_signals / historical_signals
get written, and how — the one thing that differs between the three drivers.

Write matrix (day D, current trading day T):

    mode       historical_signals            current_signals
    ---------- ----------------------------- -----------------------------
    intraday   (never — day not closed)      upsert flips + delete no-flips
    eod        delete day D + insert flips   upsert flips + delete no-flips
    backfill   delete day D + insert flips   only if D == T (final walked day)

Rules encoded:
  - historical is written for eod and every backfilled day; intraday never
    touches it (the day isn't finalized).
  - current is written only when D == today (eod-on-today, intraday-on-today,
    and backfill's final day). It's a full refresh: this run's flips upserted,
    every non-flipping scoped ticker deleted.
  - a signal counts for day D only if its observation date is D
    (ev.signal_date == D). This makes each day idempotent under the
    (ticker_id, timeframe, signal_date) unique key and stops a stale ticker
    from re-emitting its last flip on every subsequent day.

Atomicity: derive + both writes run inside ONE transaction, so a reader never
sees current_signals emptied mid-refresh, and historical's delete-then-insert
for the day is all-or-nothing. Deliberate departure from the legacy per-stage-
transaction cycle.

current_signals refresh uses upsert-flips + delete-no-flips, NOT TRUNCATE:
TRUNCATE takes an ACCESS EXCLUSIVE lock that would block concurrent GUI/API
readers for the whole cycle; row-level upsert/delete preserves MVCC.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal

from sqlalchemy.engine import Engine

from ..persistence import transaction
from ..persistence.current_signals_repo import CurrentSignalsRepository
from ..persistence.historical_signals_repo import HistoricalSignalsRepository
from ..persistence.tickers_repo import TickersRepository
from .derive_signals import derive_signal_for_ticker_this_cycle

logger = logging.getLogger(__name__)

ProcessMode = Literal["intraday", "eod", "backfill"]


@dataclass(frozen=True, slots=True)
class ProcessDayResult:
    day: date
    mode: str
    tickers_scoped: int
    flips: int
    historical_written: int
    current_upserted: int
    current_deleted: int


def process_signals_for_day(
    db_engine: Engine,
    *,
    scoped_tickers: list[int] | None = None,
    day: date,
    today: date,
    mode: ProcessMode,
    as_of: datetime,
    timeframe: str = "1W",
) -> ProcessDayResult:
    """Derive signals for `scoped_tickers` as of `day` and write per `mode`.

    Args:
        scoped_tickers: concrete ticker_ids (caller resolves scope — all-active
            for the live cycle, the backfill scope for backfill). None resolves
            to all active tickers inside the transaction.
        day: observation/processing day D. Historical rows are keyed on this;
            only signals whose signal_date == D are written.
        today: current trading day. current_signals is refreshed only when
            day == today.
        mode: intraday | eod | backfill (drives the write gates).
        as_of: instant capping the state read + price lookup (eod anchor 16:30
            for closed days; 'now' for the live intraday tick).
        timeframe: signal timeframe. '1W' by default.
    """
    write_historical = mode in ("eod", "backfill")
    write_current = day == today

    hist = cur_up = cur_del = 0
    with transaction(db_engine) as conn:
        if scoped_tickers is None:
            # Live cycle default: all active tickers (matches the legacy
            # _stage_derive_and_insert scope via find_active()).
            scoped_tickers = [
                t.ticker_id for t in TickersRepository(conn).find_active()
            ]
        flips: list = []
        no_flip: list[int] = []
        for tid in scoped_tickers:
            try:
                ev = derive_signal_for_ticker_this_cycle(
                    conn, tid, now=as_of, timeframe=timeframe, as_of=as_of,
                )
            except Exception:
                logger.exception(
                    "process_signals_for_day: derive error ticker_id=%d "
                    "day=%s; treating as no-flip", tid, day,
                )
                no_flip.append(tid)
                continue
            # A signal counts for day D only if it was observed on D.
            if ev is not None and ev.signal_date == day:
                flips.append(ev)
            else:
                no_flip.append(tid)

        if write_historical:
            HistoricalSignalsRepository(conn).delete_for_tickers_on_date(
                ticker_ids=scoped_tickers, signal_date=day,
            )
            hist = HistoricalSignalsRepository(conn).insert_many(flips)

        if write_current:
            cur = CurrentSignalsRepository(conn)
            if flips:
                cur_up = cur.insert_many(flips)
            if no_flip:
                cur_del = cur.delete_for_tickers(no_flip)

    logger.info(
        "process_signals_for_day[%s] day=%s scoped=%d flips=%d "
        "hist=%d cur_up=%d cur_del=%d",
        mode, day, len(scoped_tickers), len(flips), hist, cur_up, cur_del,
    )
    return ProcessDayResult(
        day=day, mode=mode, tickers_scoped=len(scoped_tickers),
        flips=len(flips), historical_written=hist,
        current_upserted=cur_up, current_deleted=cur_del,
    )
