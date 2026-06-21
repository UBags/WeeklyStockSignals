"""Day-by-day forward-walk orchestrator for historical signal regeneration.

Replaces `fundamentals/signal_compute.py` (Phase 6-1) and
`fundamentals/signal_generator.py` (Phase 15a) for the per-ticker
path. Re-uses, without modification:

  - `indicators/aggregation.daily_to_weekly` for IST-Monday-anchored
    weekly aggregation.
  - `fundamentals/indicator_primitives.compute_indicator_series` for
    EMAs / ATR / bands.
  - `regime.compute_regime` (Step 1) for the per-bar regime
    classification.
  - `signals.generate_signal` (Step 2) for signal emission.
  - `signals.persist_signal_events` (Step 4) for the actual writes.

This file owns the loop, the state threading, and the
`computed_at = simulated day` policy.

The architecture is "pure core + I/O shell":

  - `walk_and_generate_events(daily_bars, …) → list[SignalEvent]`
    is pure. Takes daily bars in, returns signal events out. No DB.

  - `run_backfill_for_ticker(conn, …) → BackfillResult` is the I/O
    shell. Loads daily bars from the DB, calls the walker, persists
    the resulting events via Step 4's `persist_signal_events`.

The split lets us unit-test the walk logic against synthetic bars
without a database, and integration-test the I/O shell end-to-end
against a real Postgres.

`computed_at` policy: every emitted SignalEvent has
`computed_at = the simulated day at 00:00 IST` (the spec). The live
orchestrator (Step 6) will stamp `NOW()` instead — same persist
layer, different policy at the source.

What this file deliberately does NOT do:
  - Write `indicator_state` rows. Phase 7 (the existing EoD
    compute-indicator-state pass) owns that table. After Step 9
    demolition updates Phase 7 to call the new `compute_regime`,
    `indicator_state` and `signal_events` are independent producers
    over the same `ohlcv_bars` input.
  - Handle industry-level signals. The current `SignalEvent` domain
    type models per-ticker only.
  - Open or close transactions. The caller (the regenerate-signals
    CLI in Step 10) decides transaction shape — typically one
    transaction per ticker.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Optional
from zoneinfo import ZoneInfo

import pandas as pd
from sqlalchemy import text
from sqlalchemy.engine import Connection

from trip_ema.domain import (
    ChangeSignal,
    OHLCVBar,
    SignalEvent,
    TickerRef,
    Timeframe,
)
from trip_ema.fundamentals.indicator_primitives import (
    INDICATOR_DEFAULTS,
    EngineParams,
    compute_indicator_series,
)
from trip_ema.indicators.aggregation import _monday_of, daily_to_weekly
from trip_ema.regime import BarInputs, Regime, compute_regime
from trip_ema.signals.generate_signals import (
    PositionState,
    generate_signal,
)
from trip_ema.signals.persist_signals import persist_signal_events


# ============================================================
# Result + walker output types
# ============================================================


@dataclass(frozen=True, slots=True)
class BackfillResult:
    """Summary of one backfill run for one (ticker, timeframe)."""
    ticker_id: int
    timeframe: Timeframe
    start_date: date
    end_date: date
    days_walked: int
    """Days where we computed a regime (post-warmup, weekly bar present)."""
    skipped_warmup_days: int
    """Days where the indicator series was still NaN."""
    events_emitted: int
    """SignalEvents inserted or already-present (ON CONFLICT DO NOTHING)."""


# ============================================================
# Pure walker — no I/O
# ============================================================


def walk_and_generate_events(
    daily_bars: list[OHLCVBar],
    ticker_ref: TickerRef,
    timeframe: Timeframe,
    start_date: date,
    end_date: date,
    *,
    indicator_params: EngineParams = INDICATOR_DEFAULTS,
    week_anchor_tz: str = "Asia/Kolkata",
) -> tuple[list[SignalEvent], BackfillResult]:
    """Pure day-by-day forward-walk over daily bars.

    Returns (events, result). No I/O. Suitable for direct unit testing
    with synthetic bars.

    Algorithm:
      1. Iterate the daily bars in chronological order.
      2. Maintain an incremental running weekly aggregation: each
         daily bar either extends the current in-progress week or
         finalises it and starts a new one.
      3. On every daily bar at-or-after start_date, treat the
         provisional weekly bar as "today's view of this week" and:
           a. Compute the full weekly indicator series (EMAs, ATR,
              bands) via `compute_indicator_series`. Cheap — the
              weekly series is short.
           b. If the last bar's indicators are still NaN, count as a
              warmup day and skip.
           c. Build `BarInputs` for the provisional weekly bar.
           d. Detect week transitions vs yesterday; snapshot
              yesterday's outputs as the "previous completed bar" for
              the new week.
           e. Call `compute_regime` and `generate_signal` with the
              snapshotted previous-bar state.
           f. If signal_kind != NO_CHANGE, emit a SignalEvent with
              `computed_at = the simulated day at 00:00 IST`.
           g. Thread `prev_state` (PositionState) forward across days
              within the same week — it can change intra-week
              (e.g., Tuesday BUY → state = BUY for the rest of the
              week).

    Args:
        daily_bars: All daily OHLCV bars for the ticker, including
            enough warmup (default ~2 years before start_date).
            Sorted chronologically. The walker doesn't reload from
            DB — it works with whatever you hand it.
        ticker_ref: For populating SignalEvent.ticker.
        timeframe: For populating SignalEvent.timeframe. The only
            currently supported timeframe is Timeframe.WEEKLY; the
            arg is here for forward-compat with future per-timeframe
            backfills.
        start_date: First day on which we emit signals. Days before
            this are still walked (warmup) but no signals are emitted.
        end_date: Last day on which we emit signals (inclusive).
        indicator_params: Override defaults if you want non-standard
            EMA / ATR lengths or band multipliers.
        week_anchor_tz: Timezone the weekly buckets anchor on.
            Defaults to NSE convention; pass a different tz for NYSE,
            LSE, etc.

    Returns:
        (events, result). `events` are in chronological order by
        the day the signal fired.
    """
    if timeframe != Timeframe.WEEKLY:
        raise NotImplementedError(
            f"Only WEEKLY is supported in this step; got {timeframe}"
        )

    tz = ZoneInfo(week_anchor_tz)
    events: list[SignalEvent] = []

    # State threaded across days.
    prev_completed_regime: Regime = Regime.NEUTRAL
    prev_completed_close: Optional[float] = None
    prev_completed_lower_env: Optional[float] = None
    prev_completed_upper_env: Optional[float] = None
    prev_state: PositionState = PositionState.FLAT

    # End-of-yesterday snapshots (used to populate prev_completed_* on
    # week transitions).
    yesterday_week_start: Optional[datetime] = None
    yesterday_regime: Optional[Regime] = None
    yesterday_bar_inputs: Optional[BarInputs] = None

    # Incremental weekly aggregation state.
    completed_weeks: list[OHLCVBar] = []
    cur_week_start: Optional[datetime] = None
    cur_open: Optional[float] = None
    cur_high: float = float("-inf")
    cur_low: float = float("inf")
    cur_close: Optional[float] = None
    cur_volume: float = 0.0

    days_walked = 0
    skipped_warmup_days = 0

    for daily_bar in daily_bars:
        bar_week_start = _monday_of(daily_bar.timestamp, week_anchor_tz)

        # ---- Update incremental weekly aggregation ----
        if cur_week_start is None:
            # First bar.
            cur_week_start = bar_week_start
            cur_open = daily_bar.open
            cur_high = daily_bar.high
            cur_low = daily_bar.low
            cur_close = daily_bar.close
            cur_volume = daily_bar.volume
        elif bar_week_start != cur_week_start:
            # Week transition. Finalize the previous week.
            completed_weeks.append(OHLCVBar(
                timestamp=cur_week_start,
                open=cur_open,
                high=cur_high,
                low=cur_low,
                close=cur_close,
                volume=cur_volume,
            ))
            cur_week_start = bar_week_start
            cur_open = daily_bar.open
            cur_high = daily_bar.high
            cur_low = daily_bar.low
            cur_close = daily_bar.close
            cur_volume = daily_bar.volume
        else:
            # Same week — extend.
            cur_high = max(cur_high, daily_bar.high)
            cur_low = min(cur_low, daily_bar.low)
            cur_close = daily_bar.close
            cur_volume += daily_bar.volume

        # The current (provisional) weekly bar.
        provisional = OHLCVBar(
            timestamp=cur_week_start,
            open=cur_open,
            high=cur_high,
            low=cur_low,
            close=cur_close,
            volume=cur_volume,
        )
        weekly_series = completed_weeks + [provisional]

        # The day we're simulating.
        sim_day = daily_bar.timestamp.astimezone(tz).date()

        # Skip days before start_date — they're walked for warmup
        # only, but we don't emit signals.
        if sim_day < start_date:
            continue
        if sim_day > end_date:
            break

        # ---- Compute indicator series on the full weekly series ----
        closes = pd.Series([b.close for b in weekly_series])
        volumes = pd.Series([b.volume for b in weekly_series])
        highs = pd.Series([b.high for b in weekly_series])
        lows = pd.Series([b.low for b in weekly_series])
        series = compute_indicator_series(
            closes, volumes, highs, lows,
            params=indicator_params,
        )

        # Last bar's indicators (the provisional one).
        i = len(weekly_series) - 1
        med = series.emas.med.iloc[i]
        long_ = series.emas.long.iloc[i]
        longest = series.emas.longest.iloc[i]
        atr_val = series.atr.iloc[i]

        # Warmup guard.
        if any(math.isnan(v) or not math.isfinite(v)
               for v in (med, long_, longest, atr_val)):
            skipped_warmup_days += 1
            continue

        upper_env = max(med, long_, longest)
        lower_env = min(med, long_, longest)
        bar_inputs = BarInputs(
            close=provisional.close,
            upper_inner=float(series.bands.upper_inner.iloc[i]),
            lower_inner=float(series.bands.lower_inner.iloc[i]),
            upper_outer=float(series.bands.upper_outer.iloc[i]),
            lower_outer=float(series.bands.lower_outer.iloc[i]),
            upper_envelope=float(upper_env),
            lower_envelope=float(lower_env),
        )

        # ---- Detect week transition: snapshot yesterday's outputs ----
        # If yesterday was in a different (earlier) week than today,
        # then yesterday's outputs are the FINAL values of the
        # now-completed previous week. Those become our "previous bar"
        # for regime+signal computation today.
        if (
            yesterday_week_start is not None
            and yesterday_week_start != cur_week_start
        ):
            assert yesterday_regime is not None
            assert yesterday_bar_inputs is not None
            prev_completed_regime = yesterday_regime
            prev_completed_close = yesterday_bar_inputs.close
            prev_completed_lower_env = yesterday_bar_inputs.lower_envelope
            prev_completed_upper_env = yesterday_bar_inputs.upper_envelope

        # ---- Regime + signal ----
        current_regime = compute_regime(
            previous_regime=prev_completed_regime,
            previous_close=prev_completed_close,
            previous_lower_envelope=prev_completed_lower_env,
            previous_upper_envelope=prev_completed_upper_env,
            bar=bar_inputs,
        )
        decision = generate_signal(
            previous_regime=prev_completed_regime,
            previous_close=prev_completed_close,
            previous_state=prev_state,
            current_regime=current_regime,
            bar=bar_inputs,
        )

        if decision.signal_kind != ChangeSignal.NO_CHANGE:
            # The simulated day, at 00:00 IST — the spec's
            # "computed_at = bar date" convention.
            computed_at = datetime.combine(sim_day, time.min, tzinfo=tz)
            events.append(SignalEvent(
                ticker=ticker_ref,
                timeframe=timeframe,
                bar_timestamp=cur_week_start,
                signal=decision.signal_kind,
                price_at_signal=bar_inputs.close,
                computed_at=computed_at,
            ))

        # ---- Thread state ----
        prev_state = decision.new_state
        yesterday_regime = current_regime
        yesterday_bar_inputs = bar_inputs
        yesterday_week_start = cur_week_start
        days_walked += 1

    result = BackfillResult(
        ticker_id=-1,  # filled in by the orchestrator
        timeframe=timeframe,
        start_date=start_date,
        end_date=end_date,
        days_walked=days_walked,
        skipped_warmup_days=skipped_warmup_days,
        events_emitted=len(events),
    )
    return events, result


# ============================================================
# I/O shell — loads bars, calls walker, persists
# ============================================================


def run_backfill_for_ticker(
    conn: Connection,
    ticker_id: int,
    ticker_ref: TickerRef,
    timeframe: Timeframe,
    start_date: date,
    end_date: date,
    *,
    indicator_params: EngineParams = INDICATOR_DEFAULTS,
    warmup_days: int = 730,
    week_anchor_tz: str = "Asia/Kolkata",
) -> BackfillResult:
    """Orchestrator: load bars, walk, persist.

    Loads daily bars for `[start_date - warmup_days, end_date]` from
    `ohlcv_bars`, runs the pure walker, persists the resulting
    SignalEvents via `persist_signal_events`.

    The transaction shape is the caller's call — `conn` is used
    directly. Typical caller: the regenerate-signals CLI, opening one
    transaction per ticker (so a mid-batch failure rolls back just
    that ticker; ON CONFLICT DO NOTHING handles the re-run).

    Args:
        conn: SQLAlchemy `Connection` already inside the caller's
            transaction.
        ticker_id, ticker_ref: The target ticker.
        timeframe: Currently must be Timeframe.WEEKLY.
        start_date, end_date: Inclusive backfill window.
        indicator_params: Override defaults if needed.
        warmup_days: Days of daily bars to load BEFORE start_date so
            the EMA / ATR series have settled by the time start_date
            arrives. 730 (2 years) is comfortable for a 156-week LONG
            EMA.
        week_anchor_tz: Timezone for week-bucket anchoring.

    Returns:
        BackfillResult with ticker_id populated.
    """
    # Load daily bars for the window.
    daily_bars = _load_daily_bars(
        conn,
        ticker_id=ticker_id,
        start=date_minus_days(start_date, warmup_days),
        end=end_date,
    )
    if not daily_bars:
        return BackfillResult(
            ticker_id=ticker_id,
            timeframe=timeframe,
            start_date=start_date,
            end_date=end_date,
            days_walked=0,
            skipped_warmup_days=0,
            events_emitted=0,
        )

    events, walker_result = walk_and_generate_events(
        daily_bars,
        ticker_ref=ticker_ref,
        timeframe=timeframe,
        start_date=start_date,
        end_date=end_date,
        indicator_params=indicator_params,
        week_anchor_tz=week_anchor_tz,
    )

    # Persist all events in one go. The walker emits them in chrono
    # order; persist_signal_events preserves that order in its
    # returned event_ids and respects each event's computed_at field
    # verbatim (= the simulated day).
    if events:
        persist_signal_events(conn, ticker_id, events)

    return BackfillResult(
        ticker_id=ticker_id,
        timeframe=walker_result.timeframe,
        start_date=walker_result.start_date,
        end_date=walker_result.end_date,
        days_walked=walker_result.days_walked,
        skipped_warmup_days=walker_result.skipped_warmup_days,
        events_emitted=walker_result.events_emitted,
    )


# ============================================================
# Small I/O helpers
# ============================================================


def date_minus_days(d: date, n: int) -> date:
    """Return d minus n days as a date. Trivial; named for clarity at
    the call site where the intent ("subtract the warmup window") is
    more important than the arithmetic."""
    from datetime import timedelta
    return d - timedelta(days=n)


def _load_daily_bars(
    conn: Connection,
    *,
    ticker_id: int,
    start: date,
    end: date,
) -> list[OHLCVBar]:
    """Fetch daily bars for ticker_id in [start, end], chronologically.

    Lives here (rather than in `bars_repo.py`) because:
      - `bars_repo` has no date-range fetcher today, only
        `fetch_history(limit)` and `fetch_since(timestamp_exclusive)`.
      - Adding a range method to the persistence layer is a real
        touch we don't need yet.
      - The query is tiny and self-explanatory.

    If this method gets reused elsewhere later, promote it to
    `bars_repo.fetch_range_inclusive` then.
    """
    sql = text("""
        SELECT bar_timestamp, open, high, low, close, volume
          FROM ohlcv_bars
         WHERE ticker_id = :tid
           AND timeframe = '1D'
           AND bar_timestamp::date BETWEEN :start AND :end
      ORDER BY bar_timestamp
    """)
    rows = conn.execute(sql, {
        "tid": ticker_id,
        "start": start,
        "end": end,
    }).fetchall()
    return [
        OHLCVBar(
            timestamp=r.bar_timestamp,
            open=r.open, high=r.high, low=r.low, close=r.close,
            volume=r.volume,
        )
        for r in rows
    ]
