"""Backfill runner for per-signal enrichment (spec §19).

Materialises causal signal-quality features for every historical signal,
one row per (ticker, signal_date, signal_type), into signal_enrichment.
Per-ticker isolation via fan_out (each worker owns a transactional
connection): read stored state/bars/signals, fold them into the pure
computer's dict inputs, compute, upsert.

Alignment (diagnosed against the domain + signal write path):
  - SignalEvent.signal_date is the flip day (daily date) -> keys
    signals_by_date and the walk's forming days.
  - SignalEvent.bar_timestamp / StockIndicatorState.bar_timestamp is the
    week's Monday -> keys conv_by_monday.
  - StockIndicatorState.date is the daily date -> forming EMAs; the
    converged EMAs for a week are its last (max-date) state row.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, time

from sqlalchemy import Engine, text

from ..domain import Timeframe
from ..indicators.aggregation import _monday_of, daily_to_weekly
from ..persistence import transaction
from ..persistence.bars_repo import BarsRepository
from ..persistence.signal_enrichment_repo import SignalEnrichmentRepository
from ..persistence.stock_indicator_state_repo import (
    StockIndicatorStateRepository,
)
from ..domain.market_session import market_session_for
from .parallel import fan_out
from .runners import _load_active_tickers_for_exchange
from .signal_cross_entity import compute_rs_ema_by_week, index_by_week
from .signal_enrichment_walk import compute_ticker_signal_enrichments

logger = logging.getLogger(__name__)

# v0.5 — §20 bands + §21 sequence numbers, as COLUMNS beside the jsonb.
# Rows written at v0.4 and earlier have every band column NULL; that is a
# real distinction (an all-FALSE band row means the price was on the wrong
# side of the EMA, which is a computed result, not an absence). Nothing
# reads the new columns until the recompute has run the whole history
# through this version.
DEFAULT_SPEC_VERSION = "v0.5"
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _fold_inputs(states, signals):
    """Pure: fold state + signal rows into the computer's dict inputs.

    Returns (state_by_date, conv_by_monday, signals_by_date).
    """
    state_by_date = {
        s.date: (s.med_ema, s.long_ema, s.longest_ema, s.atr,
                 s.upper_outer_band, s.lower_outer_band, s.display_regime)
        for s in states
    }
    # converged EMAs per week = its last (max-date) state row; explicit
    # max-date keeps this independent of row ordering.
    conv_tmp: dict = {}
    for s in states:
        m = s.bar_timestamp
        cur = conv_tmp.get(m)
        if cur is None or s.date > cur[0]:
            conv_tmp[m] = (s.date,
                           (s.med_ema, s.long_ema, s.longest_ema, s.atr,
                            s.upper_outer_band, s.lower_outer_band,
                            s.display_regime))
    conv_by_monday = {m: v[1] for m, v in conv_tmp.items()}

    signals_by_date: dict = {}
    for ev in signals:
        if ev.signal_date is None:
            continue
        types = signals_by_date.setdefault(ev.signal_date, [])
        if ev.signal_type not in types:
            types.append(ev.signal_type)

    return state_by_date, conv_by_monday, signals_by_date


def _resolve_ticker_inputs(conn, ticker_id, timeframe):
    """Read stored state/signals/bars for one ticker. Returns
    (daily_bars, state_by_date, conv_by_monday, signals_by_date), or None
    when there's nothing to enrich."""
    states = StockIndicatorStateRepository(conn).get_history_for_ticker(
        ticker_id, str(timeframe),
    )
    if len(states) < 2:
        return None
    # historical_signals_repo.get_for_ticker does not surface signal_date
    # (curated _SELECT_COLS); read the flip-day + type directly. One row
    # per (ticker, tf, signal_date) by the unique constraint.
    signal_rows = conn.execute(
        text(
            "SELECT DISTINCT signal_date, signal_type "
            "FROM historical_signals "
            "WHERE ticker_id = :tid AND timeframe = :tf "
            "  AND signal_date IS NOT NULL"
        ),
        {"tid": ticker_id, "tf": str(timeframe)},
    ).all()
    if not signal_rows:
        return None
    daily = BarsRepository(conn).fetch_since(ticker_id, Timeframe.DAILY,
                                             _EPOCH)
    if not daily:
        return None
    state_by_date, conv_by_monday, signals_by_date = _fold_inputs(
        states, signal_rows,
    )
    return daily, state_by_date, conv_by_monday, signals_by_date


def _resolve_rs_ema_by_monday(conn, ticker_id, daily, session):
    """§11 rs_ema per week (stock vs its industry index), or None if the
    stock has no industry mapping or the index has no values."""
    industry = conn.execute(
        text("SELECT industry FROM instrument_industry "
             "WHERE ticker_id = :t"),
        {"t": ticker_id},
    ).scalar()
    if not industry:
        return None
    idx_rows = conn.execute(
        text("SELECT trade_date, value FROM industry_index_value "
             "WHERE industry_name = :ind ORDER BY trade_date"),
        {"ind": industry},
    ).all()
    if not idx_rows:
        return None

    def week_of(d):
        return _monday_of(
            datetime.combine(d, time(0, 0), tzinfo=session.zone),
            tz=session.tz,
        )

    idx_wk = index_by_week(
        [(r.trade_date, r.value) for r in idx_rows], week_of,
    )
    weekly = daily_to_weekly(
        daily, include_partial_last_week=False, tz=session.tz,
    )
    weekly_closes = [(b.timestamp, b.close) for b in weekly]
    return compute_rs_ema_by_week(weekly_closes, idx_wk)


def backfill_signal_enrichment(
    db_engine: Engine,
    exchange: str,
    *,
    scoped_tickers: list[int] | None = None,
    timeframe: str = Timeframe.WEEKLY,
    feature_spec_version: str = DEFAULT_SPEC_VERSION,
    now: datetime | None = None,
    progress_cb=None,
):
    """Compute + upsert signal enrichment for every active ticker on
    `exchange` (or `scoped_tickers`). Idempotent per (ticker, signal_date,
    signal_type). Returns the FanOutOutcome (done, failed)."""
    session = market_session_for(exchange)
    if now is None:
        now = datetime.now(session.zone)
    if scoped_tickers is None:
        scoped_tickers = [
            t["ticker_id"]
            for t in _load_active_tickers_for_exchange(db_engine, exchange)
        ]

    def _one_ticker(ticker_id):
        with transaction(db_engine) as conn:
            resolved = _resolve_ticker_inputs(conn, ticker_id, timeframe)
            if resolved is None:
                return 0
            daily, state_by_date, conv_by_monday, signals_by_date = resolved
            rs_ema_by_monday = _resolve_rs_ema_by_monday(
                conn, ticker_id, daily, session,
            )
            rows = compute_ticker_signal_enrichments(
                ticker_id=ticker_id, timeframe=str(timeframe),
                daily_bars=daily, state_by_date=state_by_date,
                conv_by_monday=conv_by_monday,
                signals_by_date=signals_by_date, session=session, now=now,
                feature_spec_version=feature_spec_version,
                rs_ema_by_monday=rs_ema_by_monday,
            )
            if rows:
                SignalEnrichmentRepository(conn).upsert_many(rows)
            return len(rows)

    outcome = fan_out(
        scoped_tickers, _one_ticker,
        label="signal_enrichment", progress_cb=progress_cb,
    )
    logger.info(
        "backfill_signal_enrichment %s: %d tickers processed, %d failed",
        exchange, outcome.done, len(outcome.failed),
    )
    return outcome
