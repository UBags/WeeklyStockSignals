"""Bulk backfill daywalk — same signals, ~2 queries per ticker.

WHY
  The per-day daywalk calls process_signals_for_day once per trading day, which
  fans out over every scoped ticker, and each ticker opens a connection and runs
  two point queries (state rows + daily close). Over a full NSE rebuild that is
  4,982 days x 2,673 tickers = ~13.3M derive calls / ~26M queries, and it takes
  hours.

  It is all recomputing the same thing. The walked-day set IS the set of distinct
  stock_indicator_state dates (see _walk_trading_days), and derive compares the
  two most recent state rows as of day D. With the daily-grain date bound in
  place, those two rows are exactly state[D] and state[D-1] — the immediately
  preceding ROW in that ticker's own series.

  So the entire daywalk is equivalent to:

      for each ticker:
          load its state series once (ascending by date)
          load its daily closes once
          for each consecutive pair (prev, cur):
              if prev.display_regime != cur.display_regime and cur.date in range:
                  emit a signal dated cur.date

  ~2 queries per ticker instead of ~26M. The arithmetic is unchanged: this module
  calls the SAME _classify_regime_change and reproduces the SAME price lookup
  (latest daily bar with bar_timestamp <= D 16:30 IST, with reach-back), so the
  emitted SignalEvent is field-for-field identical to the per-day path.

PARITY
  Verify with parity_check_signals.py before wiring this in. It runs this module
  in dry-run and diffs the result against the historical_signals rows produced by
  the per-day walk, on
  (ticker_id, signal_date, signal_type, regime_before, regime_after,
   price_at_signal, bar_timestamp).
"""
from __future__ import annotations

import bisect
import logging
import threading
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from sqlalchemy import Engine, text

from ..domain.signal_event import SignalEvent
from ..fundamentals.parallel import fan_out
from ..orchestrator.derive_signals import _classify_regime_change
from ..persistence import transaction
from ..persistence.historical_signals_repo import HistoricalSignalsRepository

logger = logging.getLogger(__name__)

_IST = ZoneInfo("Asia/Kolkata")


def _as_of(d: date) -> datetime:
    """The per-day walk's observation instant: D 16:30 IST. Must match
    _stage_backfill_daywalk exactly — it lands in computed_at, which the
    bounded stage-7 delete filters on."""
    return datetime.combine(d, time(16, 30), tzinfo=_IST)


def _load_state_series(conn, ticker_id: int, timeframe: str, date_to: date):
    """Full state series ascending by date, capped at date_to.

    Ascending (the repo method is newest-first) and unbounded below: the row
    BEFORE date_from is required to establish the baseline for the first
    in-range day, exactly as the per-day walk gets it via `limit=2`.
    """
    rows = conn.execute(
        text("""
            SELECT date, bar_timestamp, display_regime
              FROM stock_indicator_state
             WHERE ticker_id = :tid AND timeframe = :tf
               AND date <= :to_date
             ORDER BY date ASC
        """),
        {"tid": ticker_id, "tf": timeframe, "to_date": date_to},
    ).all()
    return rows


def _load_closes(conn, ticker_id: int, date_to: date):
    """Daily bars ascending. Capped at date_to's close so a signal can never be
    priced off a future bar."""
    rows = conn.execute(
        text("""
            SELECT bar_timestamp, close
              FROM ohlcv_bars
             WHERE ticker_id = :tid AND timeframe = '1D'
               AND bar_timestamp <= :ob
             ORDER BY bar_timestamp ASC
        """),
        {"tid": ticker_id, "ob": _as_of(date_to)},
    ).all()
    return [r[0] for r in rows], [float(r[1]) for r in rows]


def _price_on_or_before(ts_list, close_list, as_of_dt) -> float | None:
    """Close of the most recent daily bar with bar_timestamp <= as_of_dt.

    Mirrors _latest_daily_close(on_or_before=...) — including its REACH-BACK:
    when the signal day has no bar of its own, the previous bar's close is used.
    """
    i = bisect.bisect_right(ts_list, as_of_dt)
    if i == 0:
        return None
    return close_list[i - 1]


def derive_signals_for_ticker_bulk(
    conn,
    ticker_id: int,
    *,
    timeframe: str,
    date_from: date,
    date_to: date,
) -> list[SignalEvent]:
    """Every signal for one ticker over [date_from, date_to], in two queries."""
    states = _load_state_series(conn, ticker_id, timeframe, date_to)
    if len(states) < 2:
        return None or []

    ts_list, close_list = _load_closes(conn, ticker_id, date_to)
    if not ts_list:
        return []

    out: list[SignalEvent] = []
    for i in range(1, len(states)):
        prev_row = states[i - 1]
        cur_row = states[i]

        # The per-day walk emits on day D only when a state row exists ON D
        # (signal_date == day). Walking consecutive rows gives exactly that.
        d = cur_row.date
        if d < date_from or d > date_to:
            continue
        if prev_row.display_regime == cur_row.display_regime:
            continue

        as_of_dt = _as_of(d)
        price = _price_on_or_before(ts_list, close_list, as_of_dt)
        if price is None or price <= 0:
            # Same guard as derive_signal_for_ticker_this_cycle: no valid price
            # -> no signal (the DB CHECK price_at_signal > 0 would reject it).
            logger.warning(
                "bulk daywalk: ticker_id=%d regime changed (%d -> %d) on %s "
                "but no valid daily close; skipping signal",
                ticker_id, prev_row.display_regime, cur_row.display_regime, d,
            )
            continue

        out.append(SignalEvent(
            ticker_id=ticker_id,
            timeframe=timeframe,
            bar_timestamp=cur_row.bar_timestamp,
            signal_type=_classify_regime_change(
                prev_row.display_regime, cur_row.display_regime,
            ),
            regime_before=prev_row.display_regime,
            regime_after=cur_row.display_regime,
            price_at_signal=price,
            computed_at=as_of_dt,
            signal_date=d,
        ))
    return out


def compute_all_signals(
    db_engine: Engine,
    *,
    scoped_tickers: list[int],
    date_from: date,
    date_to: date,
    timeframe: str = "1W",
    progress_cb=None,
) -> list[SignalEvent]:
    """Derive (no writes) every signal for every scoped ticker. Read-only, so
    each ticker runs on its own pooled connection under the existing fan_out."""
    all_events: list[SignalEvent] = []
    lock = threading.Lock()

    def _one(tid: int) -> None:
        try:
            with db_engine.connect() as conn:
                evs = derive_signals_for_ticker_bulk(
                    conn, tid, timeframe=timeframe,
                    date_from=date_from, date_to=date_to,
                )
        except Exception:
            logger.exception("bulk daywalk: derive error ticker_id=%d", tid)
            return
        if evs:
            with lock:
                all_events.extend(evs)

    fan_out(scoped_tickers, _one, label="signals_derive_bulk")
    return all_events


def backfill_daywalk_bulk(
    db_engine: Engine,
    *,
    scoped_tickers: list[int],
    date_from: date,
    date_to: date,
    today: date,
    timeframe: str = "1W",
    progress_cb=None,
) -> tuple[int, int, int, int, bool]:
    """Bulk replacement for _stage_backfill_daywalk. Same 5-tuple contract:
    (days_walked, historical_inserted, current_upserted, current_deleted,
     today_walked).

    History is written in bulk. current_signals is NOT derivable in bulk — the
    per-day walk only touches it when day == today, and its semantics include
    DELETING the rows of tickers that did not flip today. So when today is in
    the walked set we defer that single day to process_signals_for_day, which
    is one day's work (cheap) and keeps current_signals byte-identical.
    """
    events = compute_all_signals(
        db_engine, scoped_tickers=scoped_tickers,
        date_from=date_from, date_to=date_to,
        timeframe=timeframe, progress_cb=progress_cb,
    )

    with transaction(db_engine) as conn:
        # Range delete == the per-day walk's per-day delete_for_tickers_on_date
        # applied across the walked range: keeps a re-run self-correcting.
        conn.execute(
            text("""
                DELETE FROM historical_signals
                 WHERE ticker_id = ANY(:ids)
                   AND signal_date BETWEEN :f AND :t
            """),
            {"ids": scoped_tickers, "f": date_from, "t": date_to},
        )
        written = HistoricalSignalsRepository(conn).insert_many(events)

        walked = [
            r[0] for r in conn.execute(
                text("""
                    SELECT DISTINCT date FROM stock_indicator_state
                     WHERE date BETWEEN :f AND :t
                       AND timeframe = :tf AND ticker_id = ANY(:ids)
                     ORDER BY date
                """),
                {"f": date_from, "t": date_to, "tf": timeframe,
                 "ids": scoped_tickers},
            ).all()
        ]

    days_walked = len(walked)
    today_walked = today in walked

    cur_up = cur_del = 0
    if today_walked:
        # Single day, via the original path: this is the only writer of
        # current_signals (upsert flips, delete non-flips). It re-deletes and
        # re-inserts today's historical rows too — idempotent, same values.
        from ..orchestrator.process_day import process_signals_for_day
        pd = process_signals_for_day(
            db_engine,
            scoped_tickers=scoped_tickers,
            day=today,
            today=today,
            mode="backfill",
            as_of=_as_of(today),
            timeframe=timeframe,
        )
        cur_up, cur_del = pd.current_upserted, pd.current_deleted

    logger.info(
        "bulk daywalk: tickers=%d days=%d signals=%d cur_up=%d cur_del=%d",
        len(scoped_tickers), days_walked, written, cur_up, cur_del,
    )
    return days_walked, written, cur_up, cur_del, today_walked
