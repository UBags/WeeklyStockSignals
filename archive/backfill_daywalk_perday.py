"""ARCHIVED — the per-day backfill daywalk. Not imported by anything.

Superseded by trip_ema/backfill/bulk_daywalk.py. Kept as the reference
implementation the bulk path was proved against.

WHY IT WAS REPLACED
  This walked every trading day and, for each, fanned out over every scoped
  ticker — each opening a connection for two point queries. On a full NSE
  rebuild: 4,982 days x 2,673 tickers = ~13.3M derive calls / ~26M queries,
  ~3 hours.

  The walked-day set IS the set of distinct stock_indicator_state dates, and
  derive compares state[D] against state[D-1] — the preceding ROW in that
  ticker's own series. So the whole walk collapses to "for each ticker, emit on
  every consecutive state-row pair whose regimes differ": ~2 queries per ticker,
  ~3 minutes.

  Parity-verified against this implementation before the swap:
  0 missing / 0 extra / 0 mismatch over 19,743 signals.

HISTORICAL NOTE
  While this was live, the state read was bounded on bar_timestamp (the
  IST-Monday WEEK anchor) instead of date, so on any day D it returned the
  week's LAST two rows rather than (D, D-1). Only the week's final trading day
  could emit, and 79.5% of all signals were silently lost (45,161 stored vs
  220,132 real). Fixed in the state repo, not here — but if you ever resurrect
  this code, that is the trap.

DO NOT re-import this into the pipeline without re-running
parity_check_signals.py.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from sqlalchemy import text

from trip_ema.orchestrator.process_day import process_signals_for_day

_IST = ZoneInfo("Asia/Kolkata")


def _emit_progress(progress_cb, stage: str, msg: str) -> None:
    if progress_cb is not None:
        progress_cb(stage, msg)


def _walk_trading_days(
    db_engine, *, scoped_tickers: list[int],
    date_from: date, date_to: date,
) -> list[date]:
    """Trading days in [date_from, date_to] for the scoped tickers = the
    distinct stock_indicator_state dates in range (populated by the state
    stage earlier in this pipeline). Only days with state can produce
    signals, so this is both correct and the minimal set to walk."""
    with db_engine.connect() as conn:
        rows = conn.execute(
            text("""
                SELECT DISTINCT date
                  FROM stock_indicator_state
                 WHERE date BETWEEN :f AND :t
                   AND timeframe = :tf
                   AND ticker_id = ANY(:ids)
                 ORDER BY date
            """),
            {"f": date_from, "t": date_to, "tf": "1W", "ids": scoped_tickers},
        ).all()
    return [r[0] for r in rows]


def _stage_backfill_daywalk(
    db_engine, *, scoped_tickers: list[int],
    date_from: date, date_to: date, today: date,
    progress_cb: Callable[[str, str], None] | None = None,
) -> tuple[int, int, int, int, bool]:
    """Stages 8+9 unified: walk each trading day in range and run
    process_signals_for_day in backfill mode. Per-day historical
    (delete day + insert); current only on the day that == today.

    Returns (days_walked, historical_inserted, current_upserted,
    current_deleted, today_walked). today_walked is True iff today's
    trading day was in the walked range (drives the notify gate)."""
    days = _walk_trading_days(
        db_engine, scoped_tickers=scoped_tickers,
        date_from=date_from, date_to=date_to,
    )
    hist_ins = cur_up = cur_del = 0
    total = len(days)
    every = max(1, total // 100)
    for i, d in enumerate(days, 1):
        as_of_dt = datetime.combine(d, time(16, 30), tzinfo=_IST)
        pd = process_signals_for_day(
            db_engine,
            scoped_tickers=scoped_tickers,
            day=d,
            today=today,
            mode="backfill",
            as_of=as_of_dt,
        )
        hist_ins += pd.historical_written
        cur_up += pd.current_upserted
        cur_del += pd.current_deleted
        if i % every == 0 or i == total:
            _emit_progress(
                progress_cb, "stage_8_walk_days",
                f"{i}/{total} days walked",
            )
    return len(days), hist_ins, cur_up, cur_del, (today in days)
