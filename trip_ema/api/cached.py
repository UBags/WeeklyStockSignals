"""Cached read API — sub-millisecond read of the latest persisted
state per (ticker_id, timeframe).

# Step 8 note

The cache now holds a single `CachedSnapshot` per ticker — the
latest `indicator_state` row and the latest `signal_event` row,
both already on Postgres. This module is the read surface over that
snapshot, adapting it into the existing `TerseResponse` /
`VerboseResponse` shapes the rest of the system already understands.

The OLD "Verbose returns the last 200 BarStates of history" contract
is unavoidably narrowed: the cache only carries one bar of derived
state, so `VerboseResponse.bars` is a one-tuple containing the
latest bar's `BarState`. Anyone wanting the full series goes through
the `/v1/signals/{exchange}/{symbol}/history` endpoint (Step 7),
which reads `indicator_state` + `signal_events` + aggregated
`ohlcv_bars` directly.

The split is intentional: cached reads stay O(1) memory; history
reads accept the DB cost.

`TickerNotPrimedError` is still raised when the cache lacks an
entry. The caller decides whether to fall back to the history
endpoint, queue a prime, or surface the error.

The OLD function signatures took `ticker: TickerRef`. The new ones
take `ticker_id: int` to match the new cache key. The caller
(typically an HTTP route handler) already resolves the ticker_id
from path params via `TickersRepository.get_by_ref` before calling.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

from ..cache.signal_cache import (
    CachedSnapshot,
    IndicatorStateSnapshot,
    SignalEventSnapshot,
    SignalCache,
)
from ..domain import BarState, ChangeSignal, CoreRegime, Timeframe
from .responses import (
    TerseResponse,
    TickerNotPrimedError,
    VerboseResponse,
)


def query_cached_verbose(
    *,
    ticker_id: int,
    timeframe: Timeframe,
    cache: SignalCache,
) -> VerboseResponse:
    """Read the cached snapshot for `(ticker_id, timeframe)`.

    Returns a `VerboseResponse` whose `bars` field is a single-entry
    tuple — the BarState reconstructed from the latest
    `indicator_state` row. `latest_change` is the latest signal_event's
    `signal`, or `NO_CHANGE` if no event has fired yet.

    Raises `TickerNotPrimedError` if the cache has no entry for
    `(ticker_id, timeframe)`.
    """
    snap = cache.get_snapshot(ticker_id, timeframe)
    if snap is None:
        raise TickerNotPrimedError(
            f"No cached snapshot for ticker_id={ticker_id} @ "
            f"{timeframe.value}. Either the ticker is not in the DB "
            "or lifecycle prime has not run yet."
        )
    return _verbose_from_snapshot(snap)


def query_cached_terse(
    *,
    ticker_id: int,
    timeframe: Timeframe,
    cache: SignalCache,
) -> TerseResponse:
    """Read just the latest-bar-and-change summary for `(ticker_id,
    timeframe)`."""
    snap = cache.get_snapshot(ticker_id, timeframe)
    if snap is None:
        raise TickerNotPrimedError(
            f"No cached snapshot for ticker_id={ticker_id} @ "
            f"{timeframe.value}."
        )
    return _terse_from_snapshot(snap)


# ============================================================
# Snapshot → response adapters
# ============================================================


def _verbose_from_snapshot(snap: CachedSnapshot) -> VerboseResponse:
    """Map a CachedSnapshot into the existing VerboseResponse shape.

    The OLD VerboseResponse carries `bars: tuple[BarState, ...]`. We
    fill it with at most one entry — the BarState reconstructed from
    the latest `indicator_state` row (if present). Empty tuple if
    no indicator_state has landed yet.
    """
    bars: tuple[BarState, ...] = ()
    if snap.latest_indicator_state is not None:
        bars = (_bar_state_from_indicator(
            snap.latest_indicator_state,
            snapshot_taken_at=snap.snapshot_taken_at,
        ),)

    latest_change = (
        snap.latest_signal_event.signal
        if snap.latest_signal_event is not None
        else ChangeSignal.NO_CHANGE
    )

    return VerboseResponse(
        ticker=snap.ticker_ref,
        timeframe=snap.timeframe,
        bars=bars,
        latest_change=latest_change,
        computed_at=snap.snapshot_taken_at,
    )


def _terse_from_snapshot(snap: CachedSnapshot) -> TerseResponse:
    """Map a CachedSnapshot into the existing TerseResponse shape."""
    ev = snap.latest_signal_event
    ind = snap.latest_indicator_state

    # latest_bar_timestamp: prefer the signal_event's bar_timestamp
    # if present; fall back to a timestamp derived from the
    # indicator_state's trade_date (IST Monday at 00:00 UTC-stored).
    # If neither component is present we shouldn't have a snapshot at
    # all, but be defensive — return epoch.
    if ev is not None:
        latest_bar_timestamp = ev.bar_timestamp
    elif ind is not None:
        latest_bar_timestamp = datetime.combine(
            ind.trade_date, datetime.min.time(), tzinfo=timezone.utc,
        )
    else:
        latest_bar_timestamp = datetime.fromtimestamp(0, tz=timezone.utc)

    return TerseResponse(
        ticker=snap.ticker_ref,
        timeframe=snap.timeframe,
        latest_bar_timestamp=latest_bar_timestamp,
        latest_change=ev.signal if ev is not None else ChangeSignal.NO_CHANGE,
        latest_price=ev.price_at_signal if ev is not None else float("nan"),
        computed_at=snap.snapshot_taken_at,
    )


def _bar_state_from_indicator(
    ind: IndicatorStateSnapshot,
    *,
    snapshot_taken_at: datetime,
) -> BarState:
    """Reconstruct a BarState from an IndicatorStateSnapshot.

    Pierces are recomputed from the bands and snapshot is set to
    False (we don't have the high/low here — the indicator_state
    table doesn't carry them). The OLD pierce flags were
    "did this bar's range touch the mid bands?", which under the
    new model isn't a state-driving feature; callers that need
    pierces should go through the history endpoint.

    Under the unified regime model `core_bg == display_bg` at the
    wire boundary.
    """
    bar_ts = datetime.combine(
        ind.trade_date, datetime.min.time(), tzinfo=timezone.utc,
    )
    regime = CoreRegime(ind.display_regime)
    return BarState(
        timestamp=bar_ts,
        med_ema=ind.med_ema,
        long_ema=ind.long_ema,
        longest_ema=ind.longest_ema,
        atr=ind.atr,
        upper_inner=ind.upper_inner_band,
        lower_inner=ind.lower_inner_band,
        upper_mid=ind.upper_mid_band,
        lower_mid=ind.lower_mid_band,
        upper_outer=ind.upper_outer_band,
        lower_outer=ind.lower_outer_band,
        pierces_upper_mid=False,
        pierces_lower_mid=False,
        core_bg=regime,
        display_bg=regime,
    )
