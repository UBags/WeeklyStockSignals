"""Backfill runner for per-signal labels (spec §13).

Retrospective pass: for every signal_enrichment row, look up the weekly
close series (daily bars -> weekly) and the ATR at the signal, compute
forward returns / drawdown / quality class, and upsert into signal_labels.
Re-runnable — a signal's label finalises (horizon_complete=True) once all
13 forward weeks exist. Per-ticker fan_out, one transactional connection
each, mirroring the enrichment runner.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, time

from sqlalchemy import Engine, text

from ..domain import Timeframe
from ..domain.signal_label import SignalLabel
from ..indicators.aggregation import _monday_of, daily_to_weekly
from ..persistence import transaction
from ..persistence.bars_repo import BarsRepository
from ..persistence.signal_labels_repo import SignalLabelsRepository
from ..persistence.stock_indicator_state_repo import (
    StockIndicatorStateRepository,
)
from ..domain.market_session import market_session_for
from .parallel import fan_out
from .runners import _load_active_tickers_for_exchange
from .signal_labels import compute_label

logger = logging.getLogger(__name__)

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_H = 13


def build_ticker_labels(enr_rows, weekly, atr_by_date, week_of):
    """Pure: fold enrichment rows + weekly closes into SignalLabel rows.

    enr_rows: [(signal_enrichment_id, signal_date, signal_type)]
    weekly:   [(monday_datetime, close)] ordered by week
    week_of:  signal_date -> its week's monday_datetime (injected so the
              tz/_monday_of logic stays out of the pure core)
    """
    monday_idx = {m: i for i, (m, _c) in enumerate(weekly)}
    closes = [c for _m, c in weekly]
    out: list[SignalLabel] = []
    for seid, sdate, stype in enr_rows:
        idx = monday_idx.get(week_of(sdate))
        if idx is None:
            continue
        close_t = closes[idx]
        ahead = closes[idx + 1: idx + 1 + _H]
        r4, r8, r13, dd, rr = compute_label(
            close_t, ahead, atr_by_date.get(sdate), stype,
        )
        out.append(SignalLabel(
            signal_enrichment_id=seid,
            fwd_ret_4w=r4, fwd_ret_8w=r8, fwd_ret_13w=r13,
            fwd_max_dd=dd, fwd_rr=rr,
            horizon_complete=len(ahead) >= _H,
        ))
    return out


def _label_one_ticker(conn, ticker_id, timeframe, session):
    enr = conn.execute(
        text(
            "SELECT signal_enrichment_id, signal_date, signal_type "
            "FROM signal_enrichment "
            "WHERE ticker_id = :t AND timeframe = :tf "
            "ORDER BY signal_date"
        ),
        {"t": ticker_id, "tf": str(timeframe)},
    ).all()
    if not enr:
        return []
    daily = BarsRepository(conn).fetch_since(ticker_id, Timeframe.DAILY,
                                             _EPOCH)
    if not daily:
        return []
    weekly_bars = daily_to_weekly(
        daily, include_partial_last_week=False, tz=session.tz,
    )
    weekly = [(b.timestamp, b.close) for b in weekly_bars]
    states = StockIndicatorStateRepository(conn).get_history_for_ticker(
        ticker_id, str(timeframe),
    )
    atr_by_date = {s.date: s.atr for s in states}

    def week_of(sdate):
        return _monday_of(
            datetime.combine(sdate, time(0, 0), tzinfo=session.zone),
            tz=session.tz,
        )

    return build_ticker_labels(
        [(r.signal_enrichment_id, r.signal_date, r.signal_type)
         for r in enr],
        weekly, atr_by_date, week_of,
    )


def backfill_signal_labels(
    db_engine: Engine,
    exchange: str,
    *,
    scoped_tickers: list[int] | None = None,
    timeframe: str = Timeframe.WEEKLY,
    progress_cb=None,
):
    """Compute + upsert labels for every active ticker on `exchange`
    (or `scoped_tickers`). Idempotent per signal_enrichment_id."""
    session = market_session_for(exchange)
    if scoped_tickers is None:
        scoped_tickers = [
            t["ticker_id"]
            for t in _load_active_tickers_for_exchange(db_engine, exchange)
        ]

    def _one(ticker_id):
        with transaction(db_engine) as conn:
            rows = _label_one_ticker(conn, ticker_id, timeframe, session)
            if rows:
                SignalLabelsRepository(conn).upsert_many(rows)
            return len(rows)

    outcome = fan_out(
        scoped_tickers, _one, label="signal_labels", progress_cb=progress_cb,
    )
    logger.info(
        "backfill_signal_labels %s: %d tickers, %d failed",
        exchange, outcome.done, len(outcome.failed),
    )
    return outcome
