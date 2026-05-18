"""TickerEngine — per-ticker, per-timeframe stateful indicator computation.

Lifecycle:
    1. Construct with (ticker, timeframe, params).
    2. prime(historical_bars)  — one-shot fold through history.
    3. ingest(latest_bar)      — O(1) update per new bar, repeatedly.

Thread safety:
    Not thread-safe. The caller (ComputeScheduler) is the single writer.
    Read accessors return snapshots of immutable state, so concurrent
    *reads* alongside a *single* writer thread are safe.

What it carries between bars (the recurring state):
    - The three VolumeWeightedTripleEMA instances (med/long/longest)
    - The ATR instance
    - The previous EMA values (for slope checks inside core_regime)
    - The previous bar's close (already held by ATR, but also needed here)
    - The previous CoreRegime / display CoreRegime
    - The last N BarState values (for verbose output)
"""

from __future__ import annotations

from collections import deque
from datetime import datetime

from ..domain import (
    BarState,
    ChangeSignal,
    CoreRegime,
    IndicatorParams,
    OHLCVBar,
    TickerRef,
    Timeframe,
)
from ..indicators.atr import ATR
from ..indicators.bands import (
    compute_bands,
    detects_lower_mid_pierce,
    detects_upper_mid_pierce,
)
from ..indicators.vw_ema import VolumeWeightedTripleEMA
from .change_classifier import classify_change
from .core_regime import CoreRegimeInputs, step_core_regime
from .display_regime import DisplayRegimeInputs, step_display_regime


# How many BarStates to retain in the engine for verbose output.
# Matches the user spec of "last 200 candles" but is configurable per engine.
DEFAULT_HISTORY_RETAIN = 200


class TickerEngine:
    """Stateful per-ticker indicator computation."""

    def __init__(
        self,
        ticker: TickerRef,
        timeframe: Timeframe,
        params: IndicatorParams,
        *,
        history_retain: int = DEFAULT_HISTORY_RETAIN,
    ):
        self.ticker = ticker
        self.timeframe = timeframe
        self.params = params

        # Indicator state
        self._med = VolumeWeightedTripleEMA(
            base=params.med_base, s1=params.med_s1,
            s2=params.med_s2, final=params.med_final,
            volume_sma_length=params.volume_sma_length,
        )
        self._long = VolumeWeightedTripleEMA(
            base=params.long_base, s1=params.long_s1,
            s2=params.long_s2, final=params.long_final,
            volume_sma_length=params.volume_sma_length,
        )
        self._longest = VolumeWeightedTripleEMA(
            base=params.longest_base, s1=params.longest_s1,
            s2=params.longest_s2, final=params.longest_final,
            volume_sma_length=params.volume_sma_length,
        )
        self._atr = ATR(length=params.atr_length)

        # Carried between bars for slope and previous-state checks
        self._prev_med: float | None = None
        self._prev_long: float | None = None
        self._prev_core: CoreRegime = CoreRegime.NEUTRAL
        self._prev_display: CoreRegime = CoreRegime.NEUTRAL
        # The most recent bar's close — kept for callers that need to
        # report "the price at the time of the signal" without having to
        # carry the OHLCVBar themselves.
        self._last_close: float | None = None

        # BarState history for verbose output
        self._states: deque[BarState] = deque(maxlen=history_retain)

        # ---------- Snapshot / rollback machinery ----------
        # When ingest_provisional() is called, we capture the engine's
        # state immediately BEFORE applying the bar. If another
        # ingest_provisional() arrives for the same logical bar (e.g., an
        # updated weekly bar at the next hourly tick), we rollback to
        # the snapshot first, then apply the new bar — preventing the
        # EMA recurrence from being applied on top of itself.
        #
        # The snapshot is dropped (committed) when ingest() is called or
        # when commit_provisional() is called explicitly.
        self._provisional_snapshot: dict | None = None

    # -------------------- Lifecycle -------------------- #

    def prime(self, historical_bars: list[OHLCVBar]) -> None:
        """One-shot ingestion of historical bars.

        All primed bars are treated as FINAL (no snapshot kept). If the
        caller wants to mark the last bar as provisional, they should
        call ingest_provisional() with it instead of including it in the
        prime list.
        """
        for bar in historical_bars:
            self.ingest(bar)

    def ingest(self, bar: OHLCVBar) -> BarState:
        """Update all indicators with one new bar, treating it as FINAL.

        Handles three cases relative to any held provisional snapshot:

        1. No snapshot held: apply the bar normally.
        2. Snapshot held for a bar at the SAME timestamp as `bar`: the
           held provisional is the same bar, now arriving as final.
           Rollback to pre-provisional state, then apply `bar`. State
           ends up as if the final version was the only one ever seen.
        3. Snapshot held for a bar at an OLDER timestamp than `bar`:
           A1 erased-bar fix. The held provisional represents a bar
           that has now closed (since a strictly-newer bar exists).
           The right action is to COMMIT (drop the snapshot, keep the
           provisional's state advance), then apply `bar` on top.
           If we instead rolled back, we would silently erase the
           previously-held bar from the EMA recurrences.
        """
        if self._provisional_snapshot is not None:
            held_ts = self._held_provisional_timestamp()
            if held_ts is not None and bar.timestamp > held_ts:
                # Case 3: timestamp advanced. Commit the held bar first.
                self.commit_provisional()
            else:
                # Case 2: same bar, re-apply.
                self._rollback_to_snapshot()
        return self._apply_bar(bar)

    def ingest_provisional(self, bar: OHLCVBar) -> BarState:
        """Update with a still-open (provisional) bar.

        Handles the three cases the same way as ingest(), except the
        snapshot is held (not dropped) after applying.

        1. No snapshot held: take snapshot, apply.
        2. Snapshot held at SAME timestamp: rollback, re-apply, snapshot
           remains valid.
        3. Snapshot held at OLDER timestamp: A1 erased-bar fix.
           Commit the held bar first (it has closed), then take a fresh
           snapshot for the new bar, then apply.

        Call commit_provisional() when the bar closes, or simply call
        ingest() with the final version — both handle the snapshot
        correctly per the cases above.
        """
        if self._provisional_snapshot is not None:
            held_ts = self._held_provisional_timestamp()
            if held_ts is not None and bar.timestamp > held_ts:
                # Case 3: timestamp advanced. The previously-held
                # provisional is now a closed bar — commit it, then
                # snapshot fresh for the new bar.
                self.commit_provisional()
                self._provisional_snapshot = self._take_snapshot()
            else:
                # Case 2: same bar timestamp, updated OHLCV. Rollback
                # to pre-bar state (snapshot remains held for the next
                # re-apply).
                self._rollback_to_snapshot()
                # _rollback_to_snapshot clears _provisional_snapshot;
                # we need to re-take it so subsequent calls can rollback
                # again.
                self._provisional_snapshot = self._take_snapshot()
        else:
            # Case 1: first provisional — snapshot before any state mutation.
            self._provisional_snapshot = self._take_snapshot()
        return self._apply_bar(bar)

    def _held_provisional_timestamp(self) -> datetime | None:
        """Return the timestamp of the bar that the held provisional
        snapshot was taken AROUND — i.e., the timestamp of the most
        recent bar in _states, which by construction is the provisional
        bar's timestamp.

        Returns None if no states exist yet (defensive — would only
        happen if commit_provisional was called on an empty engine,
        but the snapshot mechanism wouldn't be active then either).
        """
        if not self._states:
            return None
        return self._states[-1].timestamp

    def commit_provisional(self) -> None:
        """Mark the held provisional bar as finalized — drop the snapshot.

        After this, the next ingest_provisional() will snapshot fresh.
        Calling commit_provisional() with no snapshot is a no-op (defensive).
        """
        self._provisional_snapshot = None

    def has_provisional(self) -> bool:
        """True if the engine is currently holding a provisional bar."""
        return self._provisional_snapshot is not None

    # -------------------- Snapshot internals -------------------- #

    def _take_snapshot(self) -> dict:
        """Capture every mutable bit of engine state in a dict of plain
        values. The component indicators' snapshots are nested as tuples.

        For `_states`, we capture the full deque contents as a tuple — NOT
        just the length. Capturing length only is broken when the deque is
        at its `maxlen`: appending the provisional bar would silently drop
        the oldest entry, leaving `len(self._states)` unchanged, and the
        rollback's `while len > snap_len` loop would pop nothing — leaving
        the provisional state stuck and the genuine oldest history gone.
        Snapshotting the contents lets rollback restore the deque to its
        exact pre-provisional shape regardless of maxlen.
        """
        return {
            "med": self._med._snapshot(),
            "long": self._long._snapshot(),
            "longest": self._longest._snapshot(),
            "atr": self._atr._snapshot(),
            "prev_med": self._prev_med,
            "prev_long": self._prev_long,
            "prev_core": self._prev_core,
            "prev_display": self._prev_display,
            "last_close": self._last_close,
            "states": tuple(self._states),
        }

    def _rollback_to_snapshot(self) -> None:
        """Restore engine state from `_provisional_snapshot`. Drops the
        snapshot reference afterward (caller will set a new one if
        they're about to re-apply a provisional bar)."""
        snap = self._provisional_snapshot
        if snap is None:
            return
        self._med._restore(snap["med"])
        self._long._restore(snap["long"])
        self._longest._restore(snap["longest"])
        self._atr._restore(snap["atr"])
        self._prev_med = snap["prev_med"]
        self._prev_long = snap["prev_long"]
        self._prev_core = snap["prev_core"]
        self._prev_display = snap["prev_display"]
        self._last_close = snap["last_close"]
        # Restore the deque contents in full. clear() + extend() preserves
        # the deque's maxlen; if the snapshot tuple is longer than maxlen
        # (shouldn't happen given how snapshots are taken, but defensive),
        # extend() will drop from the left as needed.
        self._states.clear()
        self._states.extend(snap["states"])
        self._provisional_snapshot = None

    def _apply_bar(self, bar: OHLCVBar) -> BarState:
        """Core single-bar update path. Used by both ingest() and
        ingest_provisional(). Does NOT touch the snapshot machinery —
        the caller is responsible for snapshot lifecycle."""
        # 1. Update the three EMAs
        med = self._med.update(bar.close, bar.volume)
        long_ = self._long.update(bar.close, bar.volume)
        longest = self._longest.update(bar.close, bar.volume)

        # 2. Update ATR
        atr = self._atr.update(bar.high, bar.low, bar.close)

        # 3. Derive bands (will be NaN until EMAs and ATR are warmed up)
        bands = compute_bands(med, long_, longest, atr, self.params)

        # 4. Pierce flags
        pierces_upper = detects_upper_mid_pierce(bar.high, bar.low, bands.upper_mid)
        pierces_lower = detects_lower_mid_pierce(bar.high, bar.low, bands.lower_mid)

        # 5. Regime stepping — only meaningful once everything has warmed up.
        # During warmup the inputs include NaN, so the comparisons inside
        # step_core_regime yield False, leaving us in NEUTRAL. That's fine.
        core_inputs = CoreRegimeInputs(
            close=bar.close,
            source=bar.close,                  # source==close in Pine default
            med_ema=med,
            med_ema_prev=self._prev_med if self._prev_med is not None else med,
            long_ema=long_,
            long_ema_prev=self._prev_long if self._prev_long is not None else long_,
            longest_ema=longest,
            upper_inner=bands.upper_inner,
            lower_inner=bands.lower_inner,
        )
        new_core = step_core_regime(self._prev_core, core_inputs)

        display_inputs = DisplayRegimeInputs(
            open=bar.open,
            close=bar.close,
            upper_inner=bands.upper_inner,
            lower_inner=bands.lower_inner,
            upper_outer=bands.upper_outer,
            lower_outer=bands.lower_outer,
        )
        new_display = step_display_regime(
            self._prev_display, new_core, display_inputs,
            goldilocks_corridor_on_fresh_entry_only=(
                self.params.goldilocks_corridor_on_fresh_entry_only
            ),
        )

        # 6. Update carried state
        self._prev_med = med
        self._prev_long = long_
        self._prev_core = new_core
        self._prev_display = new_display
        self._last_close = bar.close

        # 7. Assemble BarState
        state = BarState(
            timestamp=bar.timestamp,
            med_ema=med,
            long_ema=long_,
            longest_ema=longest,
            atr=atr,
            upper_inner=bands.upper_inner,
            lower_inner=bands.lower_inner,
            upper_mid=bands.upper_mid,
            lower_mid=bands.lower_mid,
            upper_outer=bands.upper_outer,
            lower_outer=bands.lower_outer,
            pierces_upper_mid=pierces_upper,
            pierces_lower_mid=pierces_lower,
            core_bg=new_core,
            display_bg=new_display,
        )
        self._states.append(state)
        return state

    # -------------------- Accessors -------------------- #

    def latest_state(self) -> BarState | None:
        """Return the most recent BarState, or None if nothing's been
        ingested yet."""
        return self._states[-1] if self._states else None

    def last_close(self) -> float | None:
        """Most recent bar's close, or None if nothing's been ingested."""
        return self._last_close

    def state_history(self, n: int = DEFAULT_HISTORY_RETAIN) -> list[BarState]:
        """Return up to the last `n` BarStates, oldest first. Returns a copy."""
        if n <= 0:
            return []
        states = list(self._states)
        return states[-n:]

    def last_bar_timestamp(self):
        """Convenience for the scheduler — what's the timestamp of the
        last bar we've ingested? Returns None if empty."""
        latest = self.latest_state()
        return latest.timestamp if latest else None

    def detect_change(self, previous_display: CoreRegime) -> ChangeSignal:
        """Classify the change between an externally-remembered previous
        display state and the engine's current state.

        Used by the ComputeScheduler to detect changes between successive
        compute runs (which may span multiple bars within one ingest pass).
        """
        latest = self.latest_state()
        if latest is None:
            return ChangeSignal.NO_CHANGE
        return classify_change(previous_display, latest.display_bg)
