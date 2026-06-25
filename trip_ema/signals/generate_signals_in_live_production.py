"""Live tick orchestrator: compute regime + signal for the current
week's provisional bar for one ticker, persist if a signal fires.

This is the per-tick, per-ticker entry point that the ComputeScheduler
(after Step 9 demolition) invokes once per tick per active ticker. It
replaces the signal-emitting portion of `fundamentals/signal_compute.py`
(Phase 6-1) and `fundamentals/signal_generator.py` (Phase 15a) for the
live path.

The scheduler-level orchestration (tick cadence, ticker selection,
parallelism, market-hours gating) stays where it lives today. Step 6
only provides the per-ticker primitive.

The difference from Step 5 (backfill) is small but important:
  - Step 5 is a forward walk over history. One run per ticker per
    regeneration. `computed_at` = the simulated day at IST midnight.
  - Step 6 is a single computation for "right now". One run per
    ticker per scheduler tick. `computed_at` = `now()`.
  - Step 6 also drives the signal_cache write-through (when Step 8's
    cache is wired up); Step 5 leaves the cache untouched.

Both use the same pure rules (regime/compute_regime, signals/
generate_signals) and the same persistence layer
(persist_signal_events).

Algorithm per tick:
  1. Load daily bars for [now - history_window_days, now] from
     ohlcv_bars.
  2. Aggregate to weekly via `daily_to_weekly` with
     include_partial_last_week=True. Last bar is the provisional
     current week.
  3. Compute the full indicator series via
     compute_indicator_series.
  4. Walk the weekly bars sequentially with compute_regime to derive
     the regime trajectory. ~100 bars for a 2-year window — sub-ms.
  5. The last bar's regime is "current regime". The second-to-last
     bar's outputs feed `generate_signal`'s `previous_*` inputs.
  6. Resolve `previous_state` from the latest signal_events row for
     this (ticker_id, timeframe). No row → PositionState.FLAT.
  7. Call generate_signal.
  8. If signal_kind != NO_CHANGE: persist with computed_at = now()
     via Step 4's persist_signal_events. Optional cache write-through.

Idempotency: the partial unique index on (ticker_id, timeframe,
bar_timestamp, signal_type) from migration 0016 means the same
signal type firing twice on the same bar (e.g., BUY detected on
Tuesday's tick and again on Wednesday's tick when the bar's close
still meets the trigger) collapses to one row. The first tick wins
on computed_at; subsequent ticks return the existing event_id
without inserting a new row.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, UTC
from zoneinfo import ZoneInfo

import pandas as pd
from sqlalchemy import text
from sqlalchemy.engine import Connection

from trip_ema.domain import (
    ChangeSignal,
    SignalEvent,
    TickerRef,
    Timeframe,
)
from trip_ema.fundamentals.indicator_primitives import (
    INDICATOR_DEFAULTS,
    EngineParams,
    compute_indicator_series,
)
from trip_ema.indicators.aggregation import daily_to_weekly
from trip_ema.regime import BarInputs, Regime, compute_regime
from trip_ema.signals.generate_and_backfill_signals import (
    _load_daily_bars,
    date_minus_days,
)
from trip_ema.signals.generate_signals import (
    PositionState,
    generate_signal,
)
from trip_ema.signals.persist_signals import (
    SignalCacheWriter,
    persist_signal_events,
)


# ============================================================
# Result type
# ============================================================


@dataclass(frozen=True, slots=True)
class LiveTickResult:
    """Outcome of one live tick for one (ticker, timeframe)."""
    ticker_id: int
    timeframe: Timeframe
    emitted: bool
    """True iff a SignalEvent was persisted (or matched an existing
    row via ON CONFLICT DO NOTHING)."""
    signal_kind: ChangeSignal
    """The signal that fired. ChangeSignal.NO_CHANGE if no signal."""
    event_id: int | None
    """The event_id of the persisted (or existing) row. None if no
    signal fired."""
    current_regime: Regime
    """The regime computed for the current provisional bar.
    Regime.NEUTRAL if we couldn't compute (warmup, no bars, etc.)."""
    new_state: PositionState
    """The position state after applying this tick's signal."""
    skipped_reason: str | None = None
    """If we didn't compute (no bars, warmup, etc.), why. None when
    the tick ran to completion regardless of whether a signal fired."""


# ============================================================
# Public entry point
# ============================================================


def generate_live_signal_for_ticker(
    conn: Connection,
    ticker_id: int,
    ticker_ref: TickerRef,
    timeframe: Timeframe,
    *,
    now: datetime | None = None,
    indicator_params: EngineParams = INDICATOR_DEFAULTS,
    history_window_days: int = 730,
    week_anchor_tz: str = "Asia/Kolkata",
    signal_cache_writer: SignalCacheWriter | None = None,
) -> LiveTickResult:
    """Run one live tick: compute current regime/signal, persist if needed.

    Args:
        conn: SQLAlchemy Connection inside the caller's transaction.
        ticker_id, ticker_ref: The target ticker.
        timeframe: Currently must be Timeframe.WEEKLY.
        now: The wall-clock now() for this tick. Defaults to
            datetime.now(timezone.utc). Pass explicitly in tests.
            Used as both the `computed_at` for any emitted event and
            the upper bound on bars to load.
        indicator_params: Override defaults if needed.
        history_window_days: Daily-bar history window. 730 (2 years)
            comfortably warms a 156-week LONG EMA.
        week_anchor_tz: Timezone the weekly buckets anchor on. NSE
            default; pass differently for NYSE/LSE/etc.
        signal_cache_writer: Optional write-through callback. Step 8
            will wire the real signal_cache writer here; until then,
            pass None.

    Returns:
        LiveTickResult. Inspect `.emitted` to know if a row was
        actually persisted. Inspect `.skipped_reason` to know why
        we didn't compute.
    """
    if timeframe != Timeframe.WEEKLY:
        raise NotImplementedError(
            f"Only WEEKLY is supported in this step; got {timeframe}"
        )

    if now is None:
        now = datetime.now(UTC)

    # ---------- 1. Load daily bars ----------
    today = now.astimezone(ZoneInfo(week_anchor_tz)).date()
    daily_bars = _load_daily_bars(
        conn,
        ticker_id=ticker_id,
        start=date_minus_days(today, history_window_days),
        end=today,
    )
    if not daily_bars:
        return _skipped(ticker_id, timeframe, "no_bars")

    # ---------- 2. Aggregate to weekly ----------
    weekly_bars = daily_to_weekly(
        daily_bars,
        include_partial_last_week=True,
        tz=week_anchor_tz,
    )
    if len(weekly_bars) < 2:
        # Need at least previous-week + current-week to compute a
        # signal — the first leg of the spec rules references
        # `previous_regime`, which doesn't exist without a prior bar.
        return _skipped(ticker_id, timeframe, "insufficient_history")

    # ---------- 3. Compute indicators ----------
    closes = pd.Series([b.close for b in weekly_bars])
    volumes = pd.Series([b.volume for b in weekly_bars])
    highs = pd.Series([b.high for b in weekly_bars])
    lows = pd.Series([b.low for b in weekly_bars])
    series = compute_indicator_series(
        closes, volumes, highs, lows,
        params=indicator_params,
    )

    # ---------- 4. Walk the weekly bars to derive the regime trajectory ----------
    # Regime is recursive: today's depends on yesterday's. We need to
    # walk from the start of the indicator-mature window to "now" to
    # know the current regime. ~100 iterations for a 2-year window —
    # sub-millisecond overhead per ticker.
    regimes: list[Regime | None] = []
    bar_inputs_list: list[BarInputs | None] = []

    prev_regime: Regime = Regime.NEUTRAL
    prev_close: float | None = None
    prev_lower_env: float | None = None
    prev_upper_env: float | None = None

    for i in range(len(weekly_bars)):
        med = series.emas.med.iloc[i]
        long_ = series.emas.long.iloc[i]
        longest = series.emas.longest.iloc[i]
        atr_val = series.atr.iloc[i]

        if any(math.isnan(v) or not math.isfinite(v)
               for v in (med, long_, longest, atr_val)):
            regimes.append(None)
            bar_inputs_list.append(None)
            continue

        upper_env = float(max(med, long_, longest))
        lower_env = float(min(med, long_, longest))
        bar_inputs = BarInputs(
            close=weekly_bars[i].close,
            upper_inner=float(series.bands.upper_inner.iloc[i]),
            lower_inner=float(series.bands.lower_inner.iloc[i]),
            upper_outer=float(series.bands.upper_outer.iloc[i]),
            lower_outer=float(series.bands.lower_outer.iloc[i]),
            upper_envelope=upper_env,
            lower_envelope=lower_env,
        )
        current_regime = compute_regime(
            previous_regime=prev_regime,
            previous_close=prev_close,
            previous_lower_envelope=prev_lower_env,
            previous_upper_envelope=prev_upper_env,
            bar=bar_inputs,
        )
        regimes.append(current_regime)
        bar_inputs_list.append(bar_inputs)

        prev_regime = current_regime
        prev_close = bar_inputs.close
        prev_lower_env = lower_env
        prev_upper_env = upper_env

    # ---------- 5. Pull the current + previous-completed slots ----------
    current_regime = regimes[-1]
    current_bar_inputs = bar_inputs_list[-1]
    if current_regime is None or current_bar_inputs is None:
        return _skipped(ticker_id, timeframe, "warmup")

    # Walk backward to find the most recent mature bar BEFORE the
    # current one. Normally this is regimes[-2]; if that's None
    # (warmup), keep going. If we never find one, treat as first-bar.
    prev_completed_regime: Regime = Regime.NEUTRAL
    prev_completed_close: float | None = None
    prev_completed_lower_env: float | None = None
    prev_completed_upper_env: float | None = None
    for j in range(len(regimes) - 2, -1, -1):
        # Bind indexed access to locals so pyright narrows via flow
        # analysis. Indexed access (regimes[j]) doesn't get narrowed by
        # 'if regimes[j] is not None' — the index expression is
        # re-evaluated on each access.
        regime_j = regimes[j]
        bar_j = bar_inputs_list[j]
        if regime_j is not None and bar_j is not None:
            prev_completed_regime = regime_j
            prev_completed_close = bar_j.close
            break

    # ---------- 6. Resolve previous state from signal_events ----------
    prev_state = _resolve_previous_state(conn, ticker_id, timeframe)

    # ---------- 7. Generate signal ----------
    decision = generate_signal(
        previous_regime=prev_completed_regime,
        previous_close=prev_completed_close,
        previous_state=prev_state,
        current_regime=current_regime,
        bar=current_bar_inputs,
    )

    # ---------- 8. Persist if signal fired ----------
    if decision.signal_kind == ChangeSignal.NO_CHANGE:
        return LiveTickResult(
            ticker_id=ticker_id,
            timeframe=timeframe,
            emitted=False,
            signal_kind=ChangeSignal.NO_CHANGE,
            event_id=None,
            current_regime=current_regime,
            new_state=decision.new_state,
        )

    event = SignalEvent(
        ticker=ticker_ref,
        timeframe=timeframe,
        bar_timestamp=weekly_bars[-1].timestamp,
        signal=decision.signal_kind,
        price_at_signal=current_bar_inputs.close,
        computed_at=now,  # NOW() — the live policy
    )
    event_ids = persist_signal_events(
        conn,
        ticker_id=ticker_id,
        events=[event],
        signal_cache_writer=signal_cache_writer,
    )
    return LiveTickResult(
        ticker_id=ticker_id,
        timeframe=timeframe,
        emitted=True,
        signal_kind=decision.signal_kind,
        event_id=event_ids[0] if event_ids else None,
        current_regime=current_regime,
        new_state=decision.new_state,
    )


# ============================================================
# Helpers
# ============================================================


def _skipped(
    ticker_id: int, timeframe: Timeframe, reason: str,
) -> LiveTickResult:
    """Build a skipped result with NEUTRAL/FLAT defaults."""
    return LiveTickResult(
        ticker_id=ticker_id,
        timeframe=timeframe,
        emitted=False,
        signal_kind=ChangeSignal.NO_CHANGE,
        event_id=None,
        current_regime=Regime.NEUTRAL,
        new_state=PositionState.FLAT,
        skipped_reason=reason,
    )


# Map signal_type wire strings → the position state that signal
# leaves the (ticker, timeframe) in.
_SIGNAL_TYPE_TO_STATE: dict[str, PositionState] = {
    "BUY": PositionState.BUY,
    "SELL": PositionState.SELL,
    "EXIT_FROM_BUY": PositionState.FLAT,
    "EXIT_FROM_SELL": PositionState.FLAT,
}


def _resolve_previous_state(
    conn: Connection,
    ticker_id: int,
    timeframe: Timeframe,
) -> PositionState:
    """Derive the position state for (ticker_id, timeframe) from the
    last signal_events row.

    The state machine is determined by the most recent emitted signal:
    a BUY leaves it in BUY; an EXIT leaves it FLAT; etc. No prior
    signals → FLAT (initial state).

    Reading from signal_events (rather than a per-(ticker, timeframe)
    state cache) means we don't depend on auxiliary caches for
    correctness. Step 8 will add a cache that mirrors this value for
    sub-millisecond reads, but cache-vs-DB consistency is a Step 8
    concern; this step stays correct without it.
    """
    sql = text("""
        SELECT signal_type
          FROM signal_events
         WHERE ticker_id = :tid AND timeframe = :tf
      ORDER BY computed_at DESC, event_id DESC
         LIMIT 1
    """)
    row = conn.execute(sql, {
        "tid": ticker_id, "tf": timeframe.value,
    }).fetchone()
    if row is None:
        return PositionState.FLAT
    return _SIGNAL_TYPE_TO_STATE.get(row.signal_type, PositionState.FLAT)
