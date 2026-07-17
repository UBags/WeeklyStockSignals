"""Per-ticker forward-walk runner (spec 02_signal_forward_walk_labels §4).

Reads a ticker's signals + corrected daily bars + context series, pairs
consecutive signals into windows, and calls the pure ``walk_signal`` core.
READS ONLY (via ``trip_gui_user``); the orchestrator (Phase 4) is the single
writer.

The pairing core (``compute_ticker_walks``) is pure and heavily tested — it is
where the historical ``bar_timestamp`` week-anchor trap once silently dropped
79.5% of signals. Everything keys on ``signal_date`` and on each bar's local
trading date (``bar_timestamp AT TIME ZONE`` the exchange zone), never on the
shared IST-Monday ``bar_timestamp``.

Corrected bars: ``BarsRepository`` returns the self-adjusted daily series (the
OHLCV corrector rewrites ``ohlcv_bars`` in place). If adjusted OHLC ever moves
to separate columns, only ``_load_dated_bars`` changes.
"""
from __future__ import annotations

import bisect
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..domain import Timeframe
from ..domain.forward_walk import (
    DATA_END,
    LONG,
    NEXT_SIGNAL,
    SHORT,
    SignalForwardWalk,
    WalkBar,
    walk_signal,
)
from ..domain.market_session import market_session_for
from ..persistence.bars_repo import BarsRepository
from ..persistence import transaction
from ..persistence.signal_labels_repo import SignalLabelsRepository
from .parallel import FanOutOutcome, fan_out
from .runners import _load_active_tickers_for_exchange

logger = logging.getLogger(__name__)

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MARKET_SYMBOL = "NIFTYBEES"          # market benchmark proxy (see spec §3a)
_WALK_SIDES = {"BUY": LONG, "SELL": SHORT}


@dataclass(frozen=True, slots=True)
class _Signal:
    signal_enrichment_id: int
    signal_date: date
    signal_type: str
    atr20: float | None


@dataclass(frozen=True, slots=True)
class _DatedBar:
    trading_date: date
    bar: WalkBar


# --------------------------------------------------------------------------- #
# asof-backward series lookup + total return
# --------------------------------------------------------------------------- #
def asof_backward(series: Sequence[tuple[date, float]], d: date) -> float | None:
    """Value at the latest series date <= d, or None if none exists.
    ``series`` must be sorted ascending by date."""
    if not series:
        return None
    dates = [dt for dt, _v in series]
    i = bisect.bisect_right(dates, d) - 1
    if i < 0:
        return None
    return series[i][1]


def series_total_return(
    series: Sequence[tuple[date, float]],
    entry_date: date,
    exit_date: date,
) -> float | None:
    """close_last/close_first - 1 over [entry_date, exit_date], asof-backward.
    None if either endpoint has no covering value or the first is non-positive."""
    v0 = asof_backward(series, entry_date)
    v1 = asof_backward(series, exit_date)
    if v0 is None or v1 is None or v0 <= 0:
        return None
    return v1 / v0 - 1.0


# --------------------------------------------------------------------------- #
# Pure window pairing + walk
# --------------------------------------------------------------------------- #
def compute_ticker_walks(
    signals: Sequence[_Signal],
    dated_bars: Sequence[_DatedBar],
    *,
    market_series: Sequence[tuple[date, float]],
    industry_series: Sequence[tuple[date, float]] | None,
    censored: Callable[[date, date], bool] = lambda _e, _x: False,
) -> list[SignalForwardWalk]:
    """Pair consecutive signals into windows and walk each BUY/SELL.

    ``signals`` ordered by (signal_date, signal_enrichment_id). ``dated_bars``
    ordered ascending by trading_date. Only BUY/SELL get rows; EXIT_FROM_* act
    solely as window terminators. The last BUY/SELL runs to data end and is
    right-censored.
    """
    if not dated_bars:
        return []
    bar_dates = [db.trading_date for db in dated_bars]
    bars = [db.bar for db in dated_bars]
    last_bar_date = bar_dates[-1]

    out: list[SignalForwardWalk] = []
    for i, sig in enumerate(signals):
        side = _WALK_SIDES.get(sig.signal_type)
        if side is None:                       # EXIT_FROM_* — terminator only
            continue

        # entry = first bar STRICTLY AFTER signal_date (no lookahead)
        entry_idx = bisect.bisect_right(bar_dates, sig.signal_date)
        if entry_idx >= len(bars):
            continue                           # signal at/after data end: skip

        if i + 1 < len(signals):
            n_date = signals[i + 1].signal_date
            exit_reason = NEXT_SIGNAL
            right_censored = False
        else:
            n_date = last_bar_date
            exit_reason = DATA_END
            right_censored = True

        # exit = last bar on-or-before N (N is a trading day for this ticker
        # when it's a next signal; for DATA_END it's the last bar itself)
        exit_idx = bisect.bisect_right(bar_dates, n_date) - 1
        if exit_idx < entry_idx:
            continue                           # degenerate/empty window: skip

        window = bars[entry_idx:exit_idx + 1]
        entry_date = bar_dates[entry_idx]
        exit_date = bar_dates[exit_idx]

        if censored(entry_date, exit_date):
            out.append(SignalForwardWalk(
                signal_enrichment_id=sig.signal_enrichment_id,
                side=side,
                entry_date=entry_date,
                exit_date=exit_date,
                exit_reason=exit_reason,
                bars_in_window=len(window),
                window_censored=True,
                result=None,
            ))
            continue

        market_ret = series_total_return(market_series, entry_date, exit_date)
        ind_ret = (
            series_total_return(industry_series, entry_date, exit_date)
            if industry_series
            else None
        )
        # fall back to the market series when the stock has no covered
        # industry index over the window (spec §3a)
        industry_ret = ind_ret if ind_ret is not None else market_ret

        result = walk_signal(
            window[0].open,
            window,
            side,
            atr20=sig.atr20,
            industry_ret=industry_ret,
            market_ret=market_ret,
        )
        out.append(SignalForwardWalk(
            signal_enrichment_id=sig.signal_enrichment_id,
            side=side,
            entry_date=entry_date,
            exit_date=exit_date,
            exit_reason=exit_reason,
            bars_in_window=len(window),
            window_censored=right_censored,
            result=result,
        ))
    return out


# --------------------------------------------------------------------------- #
# I/O loaders (reads via the caller's connection; no writes)
# --------------------------------------------------------------------------- #
def _load_signals(
    conn: Connection, ticker_id: int, signal_timeframe: str,
) -> list[_Signal]:
    rows = conn.execute(
        text(
            "SELECT signal_enrichment_id, signal_date, signal_type, atr20 "
            "FROM signal_enrichment "
            "WHERE ticker_id = :t AND timeframe = :tf "
            "ORDER BY signal_date, signal_enrichment_id"
        ),
        {"t": ticker_id, "tf": signal_timeframe},
    ).all()
    return [
        _Signal(r.signal_enrichment_id, r.signal_date, r.signal_type, r.atr20)
        for r in rows
    ]


def _load_dated_bars(conn: Connection, ticker_id: int, zone) -> list[_DatedBar]:
    """Corrected daily bars, each tagged with its local trading date."""
    bars = BarsRepository(conn).fetch_since(ticker_id, Timeframe.DAILY, _EPOCH)
    out: list[_DatedBar] = []
    for b in bars:
        trading_date = b.timestamp.astimezone(zone).date()
        out.append(_DatedBar(
            trading_date,
            WalkBar(open=b.open, high=b.high, low=b.low, close=b.close),
        ))
    return out


def _load_industry_name(conn: Connection, ticker_id: int) -> str | None:
    return conn.execute(
        text("SELECT industry FROM instrument_industry WHERE ticker_id = :t"),
        {"t": ticker_id},
    ).scalar()


def _load_industry_series(
    conn: Connection, industry_name: str,
) -> list[tuple[date, float]]:
    rows = conn.execute(
        text(
            "SELECT trade_date, value FROM industry_index_value "
            "WHERE industry_name = :ind ORDER BY trade_date"
        ),
        {"ind": industry_name},
    ).all()
    return [(r.trade_date, r.value) for r in rows]


def load_market_series(
    conn: Connection, exchange: str,
) -> list[tuple[date, float]]:
    """Daily close series of the market benchmark (NIFTYBEES) for `exchange`,
    keyed by local trading date. Loaded ONCE per run and shared read-only."""
    session = market_session_for(exchange)
    ticker_id = conn.execute(
        text(
            "SELECT ticker_id FROM tickers "
            "WHERE exchange = :ex AND symbol = :sym"
        ),
        {"ex": exchange, "sym": _MARKET_SYMBOL},
    ).scalar()
    if ticker_id is None:
        logger.warning(
            "market benchmark %s not found on %s; excess-vs-market will be NULL",
            _MARKET_SYMBOL, exchange,
        )
        return []
    bars = BarsRepository(conn).fetch_since(ticker_id, Timeframe.DAILY, _EPOCH)
    return [(b.timestamp.astimezone(session.zone).date(), b.close) for b in bars]


def walk_one_ticker(
    conn: Connection,
    ticker_id: int,
    *,
    exchange: str,
    market_series: Sequence[tuple[date, float]],
    signal_timeframe: str = str(Timeframe.WEEKLY),
    censored: Callable[[date, date], bool] = lambda _e, _x: False,
) -> list[SignalForwardWalk]:
    """Load one ticker's inputs and return its forward-walk rows. No writes."""
    session = market_session_for(exchange)
    signals = _load_signals(conn, ticker_id, signal_timeframe)
    if not signals:
        return []
    dated_bars = _load_dated_bars(conn, ticker_id, session.zone)
    if not dated_bars:
        return []

    industry_name = _load_industry_name(conn, ticker_id)
    industry_series = (
        _load_industry_series(conn, industry_name) if industry_name else None
    )

    return compute_ticker_walks(
        signals,
        dated_bars,
        market_series=market_series,
        industry_series=industry_series,
        censored=censored,
    )


def backfill_forward_walk(
    db_engine,
    exchange: str,
    *,
    scoped_tickers: Sequence[int] | None = None,
    progress_cb: Callable[[str, str], None] | None = None,
) -> FanOutOutcome:
    """Compute + upsert forward-walk rows for every active ticker on
    ``exchange`` (or ``scoped_tickers``).

    Threaded fan_out, the codebase's IO-bound convention: each worker opens its
    OWN connection, reads the ticker's signals + corrected daily bars + industry
    series, walks every BUY/SELL, and upserts the rows — reads and writes on the
    same connection. Workers touch disjoint signal_enrichment_ids (one ticker
    each), so there is no row contention. The market benchmark series is loaded
    ONCE and shared read-only across threads. Idempotent on
    signal_enrichment_id, so re-runs are safe.
    """
    if scoped_tickers is None:
        scoped_tickers = [
            t["ticker_id"]
            for t in _load_active_tickers_for_exchange(db_engine, exchange)
        ]
    if not scoped_tickers:
        logger.info("forward_walk %s: no tickers in scope", exchange)
        return FanOutOutcome()

    with db_engine.connect() as conn:
        market_series = load_market_series(conn, exchange)

    def _one(ticker_id: int) -> int:
        with transaction(db_engine) as conn:
            rows = walk_one_ticker(
                conn, ticker_id,
                exchange=exchange, market_series=market_series,
            )
            SignalLabelsRepository(conn).upsert_forward_walk(rows)
        return len(rows)

    outcome = fan_out(
        list(scoped_tickers), _one,
        label="signal_forward_walk", progress_cb=progress_cb,
    )
    logger.info(
        "backfill_forward_walk %s: %d tickers ok, %d failed",
        exchange, outcome.done, len(outcome.failed),
    )
    return outcome
