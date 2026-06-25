"""Lifecycle — application startup orchestration.

# Step 9a — legacy prime path removed

`prime_all_tickers` (the engine-priming startup path) is gone along
with the TickerRegistry / TickerEngine surface it sat on top of. The
only startup prime now is `prime_cache_from_db`: two SELECTs against
`indicator_state` and `signal_events`, plus a ticker-ref hydration
query.

Wire it in `app.py` at startup:

```python
from trip_ema.api.lifecycle import prime_cache_from_db
# ...
result = prime_cache_from_db(db_engine=engine, cache=cache)
logger.info("Prime: %d snapshots populated", result.snapshots_populated)
```

Step 9c updates the admin `/v1/admin/prime-all` route to call this
function in place of the deleted `prime_all_tickers`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, UTC

from sqlalchemy import Engine, text

from ..cache.signal_cache import (
    CachedSnapshot,
    IndicatorStateSnapshot,
    SignalCache,
    SignalEventSnapshot,
)
from ..domain import ChangeSignal, TickerRef, Timeframe

logger = logging.getLogger(__name__)


# ============================================================
# Result type
# ============================================================


@dataclass(frozen=True, slots=True)
class PrimeCacheResult:
    """Summary of one `prime_cache_from_db` invocation."""
    tickers_seen: int
    """Distinct ticker_ids produced from the two SELECTs."""
    snapshots_populated: int
    """Cache entries inserted via `put_snapshot`."""
    indicator_only: int
    """Tickers with an indicator_state row but no signal_event
    yet."""
    signal_event_only: int
    """Tickers with a signal_event but no indicator_state. Unusual
    but possible during regeneration."""
    both: int
    """Tickers with both components populated."""


# ============================================================
# Wire-string → ChangeSignal map
# ============================================================


_STR_TO_SIGNAL: dict[str, ChangeSignal] = {
    "BUY": ChangeSignal.BUY,
    "SELL": ChangeSignal.SELL,
    "EXIT_FROM_BUY": ChangeSignal.EXIT_FROM_BUY,
    "EXIT_FROM_SELL": ChangeSignal.EXIT_FROM_SELL,
}


# ============================================================
# Public entry point
# ============================================================


def prime_cache_from_db(
    *,
    db_engine: Engine,
    cache: SignalCache,
    timeframe: Timeframe = Timeframe.WEEKLY,
) -> PrimeCacheResult:
    """Populate the cache from two SELECTs against the persisted
    producers.

    The two queries:
      1. Latest `indicator_state` row per (ticker_id, timeframe).
         Sourced from a `DISTINCT ON (ticker_id) … ORDER BY trade_date
         DESC` over rows with `ticker_id IS NOT NULL` for the given
         timeframe.
      2. Latest `signal_events` row per (ticker_id, timeframe).
         Same DISTINCT ON pattern, ordered by `(computed_at DESC,
         event_id DESC)` so the tiebreak is deterministic.

    For each ticker that appears in either result we build a
    `CachedSnapshot` carrying whichever components are present and
    `put_snapshot` it onto the cache. Tickers that appear in neither
    result get no entry (no data to cache).

    Args:
        db_engine: SQLAlchemy Engine for the application database.
        cache: SignalCache to seed.
        timeframe: Which timeframe to seed. Defaults to WEEKLY —
            the only timeframe Steps 5 / 6 / 7 currently produce.

    Returns:
        PrimeCacheResult with component-coverage counts. Times in
        production should be well under a second for ~2,600 tickers.
    """
    with db_engine.connect() as conn:
        indicator_rows = _load_latest_indicator_states(
            conn, timeframe=timeframe,
        )
        signal_rows = _load_latest_signal_events(
            conn, timeframe=timeframe,
        )
        ticker_refs = _load_ticker_refs(
            conn, ticker_ids=set(indicator_rows.keys()) | set(signal_rows.keys()),
        )

    snapshot_taken_at = datetime.now(UTC)
    tickers_seen = set(indicator_rows.keys()) | set(signal_rows.keys())

    indicator_only = 0
    signal_event_only = 0
    both = 0
    populated = 0

    for ticker_id in tickers_seen:
        ref = ticker_refs.get(ticker_id)
        if ref is None:
            # Snapshot rows exist but no tickers entry — corrupted
            # state, skip defensively.
            logger.warning(
                "ticker_id %d has snapshot rows but no tickers entry; skipping",
                ticker_id,
            )
            continue

        ind_snap = indicator_rows.get(ticker_id)
        sig_snap = signal_rows.get(ticker_id)
        if ind_snap is not None and sig_snap is not None:
            both += 1
        elif ind_snap is not None:
            indicator_only += 1
        else:
            signal_event_only += 1

        cache.put_snapshot(CachedSnapshot(
            ticker_id=ticker_id,
            ticker_ref=ref,
            timeframe=timeframe,
            latest_indicator_state=ind_snap,
            latest_signal_event=sig_snap,
            snapshot_taken_at=snapshot_taken_at,
        ))
        populated += 1

    logger.info(
        "prime_cache_from_db: %d tickers seen, %d snapshots populated "
        "(%d both, %d indicator-only, %d signal-only)",
        len(tickers_seen), populated, both, indicator_only, signal_event_only,
    )
    return PrimeCacheResult(
        tickers_seen=len(tickers_seen),
        snapshots_populated=populated,
        indicator_only=indicator_only,
        signal_event_only=signal_event_only,
        both=both,
    )


# ============================================================
# Read helpers
# ============================================================


def _load_latest_indicator_states(
    conn, *, timeframe: Timeframe,
) -> dict[int, IndicatorStateSnapshot]:
    """One SELECT: latest indicator_state row per ticker_id."""
    sql = text("""
        SELECT DISTINCT ON (ticker_id)
               ticker_id, trade_date,
               med_ema, long_ema, longest_ema, atr,
               upper_inner_band, lower_inner_band,
               upper_mid_band, lower_mid_band,
               upper_outer_band, lower_outer_band,
               display_regime, computed_at
          FROM indicator_state
         WHERE ticker_id IS NOT NULL
           AND timeframe = :tf
      ORDER BY ticker_id, trade_date DESC
    """)
    rows = conn.execute(sql, {"tf": timeframe.value}).fetchall()
    return {
        r.ticker_id: IndicatorStateSnapshot(
            trade_date=r.trade_date,
            med_ema=r.med_ema, long_ema=r.long_ema,
            longest_ema=r.longest_ema, atr=r.atr,
            upper_inner_band=r.upper_inner_band,
            lower_inner_band=r.lower_inner_band,
            upper_mid_band=r.upper_mid_band,
            lower_mid_band=r.lower_mid_band,
            upper_outer_band=r.upper_outer_band,
            lower_outer_band=r.lower_outer_band,
            display_regime=r.display_regime,
            computed_at=r.computed_at,
        )
        for r in rows
    }


def _load_latest_signal_events(
    conn, *, timeframe: Timeframe,
) -> dict[int, SignalEventSnapshot]:
    """One SELECT: latest signal_events row per ticker_id."""
    sql = text("""
        SELECT DISTINCT ON (ticker_id)
               ticker_id, event_id, bar_timestamp,
               signal_type, price_at_signal, computed_at
          FROM signal_events
         WHERE ticker_id IS NOT NULL
           AND timeframe = :tf
      ORDER BY ticker_id, computed_at DESC, event_id DESC
    """)
    rows = conn.execute(sql, {"tf": timeframe.value}).fetchall()
    out: dict[int, SignalEventSnapshot] = {}
    for r in rows:
        sig = _STR_TO_SIGNAL.get(r.signal_type)
        if sig is None:
            logger.warning(
                "signal_events.signal_type=%r is not in the wire vocabulary; "
                "skipping ticker_id=%d event_id=%d",
                r.signal_type, r.ticker_id, r.event_id,
            )
            continue
        out[r.ticker_id] = SignalEventSnapshot(
            event_id=r.event_id,
            bar_timestamp=r.bar_timestamp,
            signal=sig,
            price_at_signal=r.price_at_signal,
            computed_at=r.computed_at,
        )
    return out


def _load_ticker_refs(
    conn, *, ticker_ids: set[int],
) -> dict[int, TickerRef]:
    """One SELECT (ANY-clause): resolve ticker_id → TickerRef for
    the set of tickers we found snapshot rows for."""
    if not ticker_ids:
        return {}
    sql = text("""
        SELECT ticker_id, exchange, symbol, instrument
          FROM tickers
         WHERE ticker_id = ANY(:tids)
    """)
    rows = conn.execute(sql, {"tids": list(ticker_ids)}).fetchall()
    return {
        r.ticker_id: TickerRef(
            exchange=r.exchange, symbol=r.symbol, instrument=r.instrument,
        )
        for r in rows
    }
