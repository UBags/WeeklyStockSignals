"""Within-day signal chain — the position state machine.

WHY THIS IS A SEPARATE DERIVE
  derive_signal_for_ticker_this_cycle answers "what is the NET signal for day D"
  by comparing state[D] against state[D-1]. That is the right question for
  historical_signals (one net row per day) and current_signals, and it is left
  untouched.

  It is the wrong question intraday. state[D] is upserted IN PLACE as the
  forming weekly bar moves, so there is no tick-to-tick state history to diff
  against. Re-deriving vs state[D-1] on every tick would re-emit the SAME BUY
  every 15 minutes.

  So the intraday chain compares against the ticker's POSITION — the regime
  implied by the most recent transition already logged today — falling back to
  state[D-1] when the chain is empty (the first tick of the day).

THE RULE (matches the operator's spec)
  position := last logged transition's regime_after, else state[D-1].regime
  current  := state[D].regime  (recomputed this tick)

  current == position  -> suppress (nothing changed since the last transition)
  current != position  -> log classify(position -> current)

WORKED EXAMPLE (previous day closed NEUTRAL)
  10:00  regime BULLISH, position NEUTRAL  -> log BUY            (position=1)
  10:15  regime BULLISH, position BULLISH  -> suppress
  11:30  regime NEUTRAL, position BULLISH  -> log EXIT_FROM_BUY  (position=0)
  14:00  regime BULLISH, position NEUTRAL  -> log BUY            (position=1)

  chain = BUY - EXIT_FROM_BUY - BUY

  Note the 11:30 EXIT: the regime merely returned to the previous day's value,
  so the NET daily derive sees no change and emits nothing — but the position
  was open, so the chain must close it. That transition exists only here.

  Because a transition is logged only when it differs from the current
  position, two adjacent entries can never be the same signal_type: a repeat of
  a type is always separated by an intervening one.
"""
from __future__ import annotations

import logging
import threading
from datetime import date, datetime

from sqlalchemy import Engine

from ..fundamentals.parallel import fan_out
from ..persistence import transaction
from ..persistence.tickers_repo import TickersRepository
from ..persistence.intraday_signal_log_repo import (
    IntradaySignalLogRepository,
    IntradayTransition,
)
from ..persistence.stock_indicator_state_repo import (
    StockIndicatorStateRepository,
)
from .derive_signals import _classify_regime_change, _latest_daily_close

logger = logging.getLogger(__name__)


def log_intraday_transitions(
    db_engine: Engine,
    *,
    scoped_tickers: list[int] | None = None,
    day: date,
    as_of: datetime,
    timeframe: str = "1W",
) -> int:
    """Append this tick's transitions to intraday_signal_log.

    Call on every intraday tick AND on the eod run, after the state recompute
    and before process_signals_for_day. Returns rows appended.

    scoped_tickers=None resolves to all active tickers — matching
    process_signals_for_day's own default, so the live cycle can pass None.

    Read-only per ticker (state + last-logged-position + price), so the derive
    fans out; the appends are collected and written in one transaction.
    """
    if scoped_tickers is None:
        with db_engine.connect() as conn:
            scoped_tickers = [
                t.ticker_id for t in TickersRepository(conn).find_active()
            ]

    pending: list[IntradayTransition] = []
    lock = threading.Lock()

    def _one(tid: int) -> None:
        try:
            with db_engine.connect() as conn:
                states = StockIndicatorStateRepository(
                    conn,
                ).get_history_for_ticker(
                    tid, timeframe, to_date=day, limit=2,
                )
                if len(states) < 2:
                    return
                latest, previous = states[0], states[1]

                # No state row for today yet (pre-open / not yet computed).
                if latest.date != day:
                    return

                logged = IntradaySignalLogRepository(
                    conn,
                ).latest_regime_after(tid, timeframe, day)

                position = (
                    logged if logged is not None
                    else previous.display_regime
                )
                current = latest.display_regime
                if current == position:
                    return

                price = _latest_daily_close(conn, tid, on_or_before=as_of)
                if price is None or price <= 0:
                    logger.warning(
                        "intraday chain: ticker_id=%d transition %d -> %d on "
                        "%s but no valid daily close; skipping",
                        tid, position, current, day,
                    )
                    return

                t = IntradayTransition(
                    ticker_id=tid,
                    timeframe=timeframe,
                    signal_date=day,
                    bar_timestamp=latest.bar_timestamp,
                    signal_type=_classify_regime_change(position, current),
                    regime_before=position,
                    regime_after=current,
                    price_at_signal=price,
                    computed_at=as_of,
                )
        except Exception:
            logger.exception(
                "intraday chain: derive error ticker_id=%d day=%s", tid, day,
            )
            return
        with lock:
            pending.append(t)

    fan_out(scoped_tickers, _one, label="intraday_chain")

    if not pending:
        return 0
    with transaction(db_engine) as conn:
        repo = IntradaySignalLogRepository(conn)
        for t in pending:
            repo.append(t)
    logger.info(
        "intraday chain: day=%s appended=%d transitions", day, len(pending),
    )
    return len(pending)
