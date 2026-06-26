"""V1 signal derivation primitive.

Reads the latest two `stock_indicator_state` rows for a ticker. If
their `display_regime` differs, emits a `SignalEvent` representing
that regime change. Otherwise returns None.

O(1) per ticker — at most three small index lookups per call
(latest two state rows + latest daily close for price_at_signal).

Signal kind precedence (mirrors the legacy `generate_signals.py`):
    regime_after == +1                    → BUY
        (takes precedence over EXIT_FROM_SELL on -1→+1)
    regime_after == -1                    → SELL
        (takes precedence over EXIT_FROM_BUY on +1→-1)
    regime_before == +1, regime_after == 0 → EXIT_FROM_BUY
    regime_before == -1, regime_after == 0 → EXIT_FROM_SELL

Same-bar re-detection is by design — if cycle N detected a regime
change at bar B (still mid-week, open), cycle N+1 will detect the
SAME regime change at bar B because nothing has finalized the bar.
Both cycles emit a signal at B. Rotation (Step 4) carries cycle N's
signal into historical between the two; historical accumulates a
copy per cycle until the bar closes.

Price source — weekly close gotcha:

`ohlcv_bars` stores ONLY daily bars (`timeframe='1D'`); the weekly
timeframe is synthesized in-memory from daily bars via
`indicators.aggregation.daily_to_weekly`. So `price_at_signal` for
a Weekly bar = close of the most recent daily bar (which IS the
right edge of the current weekly bucket). The `on_or_before`
parameter constrains this lookup for backfill use (Step 11);
the live cycle leaves it as None.
"""
from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..domain import Timeframe
from ..domain.signal_event import SignalEvent
from ..persistence.stock_indicator_state_repo import (
    StockIndicatorStateRepository,
)
from ..persistence.tickers_repo import TickersRepository

logger = logging.getLogger(__name__)


def _classify_regime_change(
    regime_before: int, regime_after: int,
) -> str:
    """Map a (before, after) regime pair to one of the four signal_types.

    Callers must ensure regime_before != regime_after (a signal IS a
    regime change by definition) and both values are in {-1, 0, +1}.
    """
    if regime_after == 1:
        return "BUY"
    if regime_after == -1:
        return "SELL"
    if regime_before == 1:
        return "EXIT_FROM_BUY"
    if regime_before == -1:
        return "EXIT_FROM_SELL"
    # Unreachable for valid inputs (the only case left is
    # regime_before == 0 and regime_after == 0, which is not a
    # regime change).
    raise ValueError(
        f"cannot classify regime change "
        f"(before={regime_before}, after={regime_after}) — "
        f"caller must ensure they differ and are in {{-1, 0, +1}}"
    )


def _latest_daily_close(
    conn: Connection,
    ticker_id: int,
    *,
    on_or_before: datetime | None = None,
) -> float | None:
    """Most recent daily ohlcv close for this ticker.

    Returns None if the ticker has no daily bars.

    `on_or_before` constrains the lookup for backfill (Step 11) —
    None means "take the absolute latest available."
    """
    if on_or_before is None:
        sql = (
            "SELECT close FROM ohlcv_bars "
            "WHERE ticker_id = :tid AND timeframe = '1D' "
            "ORDER BY bar_timestamp DESC LIMIT 1"
        )
        params = {"tid": ticker_id}
    else:
        sql = (
            "SELECT close FROM ohlcv_bars "
            "WHERE ticker_id = :tid AND timeframe = '1D' "
            "  AND bar_timestamp <= :ob "
            "ORDER BY bar_timestamp DESC LIMIT 1"
        )
        params = {"tid": ticker_id, "ob": on_or_before}
    row = conn.execute(text(sql), params).fetchone()
    return float(row[0]) if row is not None else None


def derive_signal_for_ticker_this_cycle(
    conn: Connection,
    ticker_id: int,
    *,
    now: datetime,
    timeframe: str = Timeframe.WEEKLY,
    on_or_before: datetime | None = None,
) -> SignalEvent | None:
    """Compare the latest two stock_indicator_state rows. Emit a
    SignalEvent if their display_regime differs, else None.

    Returns None when:
      - the ticker has fewer than 2 state rows (warmup / new ticker)
      - the two latest rows have the same display_regime (no change)
      - no daily ohlcv bar exists to source price_at_signal from

    Args:
        conn: SQLAlchemy Connection. Caller controls the transaction.
        ticker_id: target ticker.
        now: stamped into the emitted signal's `computed_at`.
        timeframe: 'Weekly' (= '1W') by default. Daily is Phase 16.
        on_or_before: optional cutoff on the daily-close lookup.
            For backfill use; leave None for live cycle.
    """
    repo = StockIndicatorStateRepository(conn)
    states = repo.get_history_for_ticker(
        ticker_id, str(timeframe), limit=2,
    )
    if len(states) < 2:
        return None

    latest, previous = states[0], states[1]   # newest-first ordering
    if latest.display_regime == previous.display_regime:
        return None

    price = _latest_daily_close(
        conn, ticker_id, on_or_before=on_or_before,
    )
    if price is None or price <= 0:
        # No valid price → can't emit a signal. The DB CHECK on
        # price_at_signal > 0 would reject it anyway.
        logger.warning(
            "derive_signal_for_ticker_this_cycle: ticker_id=%d "
            "regime changed (%d → %d) but no valid daily close "
            "available; skipping signal",
            ticker_id, previous.display_regime, latest.display_regime,
        )
        return None

    return SignalEvent(
        ticker_id=ticker_id,
        timeframe=str(timeframe),
        bar_timestamp=latest.bar_timestamp,
        signal_type=_classify_regime_change(
            previous.display_regime, latest.display_regime,
        ),
        regime_before=previous.display_regime,
        regime_after=latest.display_regime,
        price_at_signal=price,
        computed_at=now,
    )


def derive_signals_for_all_active_stocks(
    conn: Connection,
    *,
    now: datetime,
    timeframe: str = Timeframe.WEEKLY,
    on_or_before: datetime | None = None,
) -> list[SignalEvent]:
    """Iterate active tickers; emit signals for any that changed
    regime in this cycle.

    Returns a list ordered by ticker_id (stable for testing and
    deterministic in `current_signals` PK insertion order). Per-ticker
    errors are logged and the iteration continues — one corrupted
    ticker doesn't break the whole derive stage.

    The caller (Step 6's `recompute_market`) feeds this list directly
    into `CurrentSignalsRepository.insert_many`.
    """
    tickers = TickersRepository(conn).find_active()
    tickers.sort(key=lambda t: t.ticker_id)

    out: list[SignalEvent] = []
    for ticker in tickers:
        try:
            signal = derive_signal_for_ticker_this_cycle(
                conn, ticker.ticker_id,
                now=now, timeframe=timeframe,
                on_or_before=on_or_before,
            )
        except Exception:
            logger.exception(
                "derive_signals_for_all_active_stocks: error on "
                "ticker_id=%d symbol=%s; skipping",
                ticker.ticker_id, ticker.ref.symbol,
            )
            continue
        if signal is not None:
            out.append(signal)
    return out
