"""SignalCache — read-mostly cache of the latest persisted snapshot per
(ticker_id, timeframe).

# Step 9a — legacy half removed

The legacy `CachedSignal` / `build_cached_signal` / `put` / `get` /
`has` / `all_signals` / `stale_signals` / `__len__` / `remove`
surface (built around the `TickerEngine` + `BarState` world) is
gone. Only the snapshot shape remains. Compute scheduler (Step 9c)
no longer writes the legacy shape; admin_health (Step 9c) no
longer reads it.

# Snapshot shape

Per (ticker_id, timeframe) the cache holds:

  - `latest_indicator_state`: subset of the latest `indicator_state`
    row (EMAs, ATR, six bands, `display_regime`, `computed_at`).
  - `latest_signal_event`:    subset of the latest `signal_events`
    row (`event_id`, `bar_timestamp`, `signal`, `price_at_signal`,
    `computed_at`).
  - `stale_since`: when set, the compute scheduler detected a data
    gap on this ticker on/at the given UTC wall-clock and is
    refusing to advance the snapshot until the ETL backfills.
    Cleared on the next pass that finds the gap closed.

Both components may be None during early lifecycle (a ticker with
indicator_state but no signal yet, or vice versa).

# Concurrency

The orchestrator (Step 6's `generate_live_signal_for_ticker`,
invoked by the compute scheduler) is the single writer to the
signal-event component. Phase 7 (wired in Step 9b) is the single
writer to the indicator-state component. The compute scheduler is
the single writer to the stale flag. Reads are lock-free and rely
on dict-lookup atomicity under the GIL; writes serialise on a
single lock for cross-writer safety even though contention isn't
expected.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, replace
from datetime import date, datetime

from ..domain import (
    ChangeSignal,
    SignalEvent,
    TickerRef,
    Timeframe,
)


# ============================================================
# Snapshot dataclasses
# ============================================================


@dataclass(frozen=True, slots=True)
class IndicatorStateSnapshot:
    """Subset of one `indicator_state` row, mirrored into the cache.

    Carries exactly what the read endpoints and any "current state at
    a glance" reader needs. The full row is still in Postgres; this
    is the latest week's values for sub-millisecond reads.
    """
    trade_date: date
    """The IST-Monday this row covers."""
    med_ema: float
    long_ema: float
    longest_ema: float
    atr: float
    upper_inner_band: float
    lower_inner_band: float
    upper_mid_band: float
    lower_mid_band: float
    upper_outer_band: float
    lower_outer_band: float
    display_regime: int
    """-1 BEARISH / 0 NEUTRAL / +1 BULLISH. Same encoding as the
    indicator_state column."""
    computed_at: datetime
    """When Phase 7 last wrote this row."""


@dataclass(frozen=True, slots=True)
class SignalEventSnapshot:
    """Subset of one `signal_events` row, mirrored into the cache.

    `bar_timestamp` is the weekly bar's IST-Monday anchor; `signal`
    is the ChangeSignal enum value; `computed_at` is when the event
    was emitted (simulated day for backfill rows, NOW() for live
    ticks).
    """
    event_id: int
    bar_timestamp: datetime
    signal: ChangeSignal
    price_at_signal: float
    computed_at: datetime


@dataclass(frozen=True, slots=True)
class CachedSnapshot:
    """Per-(ticker_id, timeframe) cache entry.

    Either component can be None while the other is populated:
      - A ticker may have an `indicator_state` row but no
        `signal_event` yet (the price has stayed neutral / mid-band
        all year). Cache has indicator_state, signal_event=None.
      - A ticker can in principle have a signal_event but no
        indicator_state yet (extremely unusual; would happen only if
        Step 5 backfill ran before Phase 7 finished). Cache has
        signal_event, indicator_state=None.

    Both None means the entry should not have been put — the
    lifecycle prime skips tickers with neither row.

    `stale_since` is set by the compute scheduler when a data gap is
    detected. Readers should treat a non-None `stale_since` as "this
    snapshot is the last good state; new ticks won't land until the
    ETL backfills the gap."
    """
    ticker_id: int
    ticker_ref: TickerRef
    """Mirrored for read-endpoint convenience — saves a tickers lookup."""
    timeframe: Timeframe
    latest_indicator_state: IndicatorStateSnapshot | None
    latest_signal_event: SignalEventSnapshot | None
    snapshot_taken_at: datetime
    """When this cache entry was last refreshed end-to-end (i.e.
    via lifecycle prime or a `put_snapshot`). The component
    `computed_at` fields tell you when each component was produced
    by its respective writer."""
    stale_since: datetime | None = None
    """When the compute scheduler last observed a data gap on this
    ticker. None means no gap. Set by `mark_stale`, cleared by
    `clear_stale`."""


# ============================================================
# The cache class
# ============================================================


class SignalCache:
    """Read-mostly per-(ticker_id, timeframe) cache.

    Reads (`get_snapshot`, `all_snapshots`, `all_stale_snapshots`)
    are lock-free and rely on dict-lookup atomicity under the GIL.

    Writes (`put_snapshot`, `update_signal_event`,
    `update_indicator_state`, `mark_stale`, `clear_stale`,
    `remove_snapshot`) serialise on a single lock — we don't expect
    contention since each writer family has a single producer, but
    the lock is cheap.
    """

    def __init__(self) -> None:
        self._snapshots: dict[tuple[int, Timeframe], CachedSnapshot] = {}
        self._write_lock = threading.Lock()

    # ---------- Reads (lock-free) ---------- #

    def get_snapshot(
        self, ticker_id: int, timeframe: Timeframe,
    ) -> CachedSnapshot | None:
        """O(1) lookup by ticker_id. Returns the immutable snapshot
        or None if not seeded yet."""
        return self._snapshots.get((ticker_id, timeframe))

    def has_snapshot(self, ticker_id: int, timeframe: Timeframe) -> bool:
        return (ticker_id, timeframe) in self._snapshots

    def all_snapshots(self) -> list[CachedSnapshot]:
        """Atomic copy of every cached entry."""
        return list(self._snapshots.values())

    def all_stale_snapshots(self) -> list[CachedSnapshot]:
        """Every cached entry with a non-None `stale_since`. Used by
        the admin `/v1/admin/stale-tickers` route."""
        return [s for s in self._snapshots.values() if s.stale_since is not None]

    def snapshot_count(self) -> int:
        """Number of entries. Replaces the legacy `len(cache)` —
        intentionally a named method rather than `__len__` so the
        rewrite-or-error from any leftover legacy `len()` call is
        loud."""
        return len(self._snapshots)

    # ---------- Writes ---------- #

    def put_snapshot(self, snapshot: CachedSnapshot) -> None:
        """Replace the entry for `(snapshot.ticker_id,
        snapshot.timeframe)`. Used by `prime_cache_from_db` to seed
        every ticker at startup and by tests."""
        key = (snapshot.ticker_id, snapshot.timeframe)
        with self._write_lock:
            self._snapshots[key] = snapshot

    def update_signal_event(
        self,
        ticker_id: int,
        timeframe: Timeframe,
        event: SignalEvent,
    ) -> None:
        """Refresh just the `latest_signal_event` component for a
        `(ticker_id, timeframe)`.

        Matches the signature of `SignalCacheWriter` from Step 4's
        `signals/persist_signals.py`, so Step 6's
        `generate_live_signal_for_ticker(...,
        signal_cache_writer=signal_cache.update_signal_event)`
        wires in directly as a bound method.

        Behaviour when no entry exists yet for `(ticker_id,
        timeframe)`:
          - Creates one with `latest_indicator_state=None` and the
            supplied event as `latest_signal_event`. A subsequent
            indicator_state update will fill in the missing
            component. This lets events land before the startup
            prime finishes.
        """
        new_event_snap = SignalEventSnapshot(
            event_id=0,  # writer doesn't know the DB-assigned id; harmless
            bar_timestamp=event.bar_timestamp,
            signal=event.signal,
            price_at_signal=event.price_at_signal,
            computed_at=event.computed_at,
        )
        with self._write_lock:
            existing = self._snapshots.get((ticker_id, timeframe))
            if existing is None:
                self._snapshots[(ticker_id, timeframe)] = CachedSnapshot(
                    ticker_id=ticker_id,
                    ticker_ref=event.ticker,
                    timeframe=timeframe,
                    latest_indicator_state=None,
                    latest_signal_event=new_event_snap,
                    snapshot_taken_at=event.computed_at,
                    stale_since=None,
                )
            else:
                self._snapshots[(ticker_id, timeframe)] = replace(
                    existing,
                    latest_signal_event=new_event_snap,
                    snapshot_taken_at=event.computed_at,
                )

    def update_indicator_state(
        self,
        ticker_id: int,
        ticker_ref: TickerRef,
        timeframe: Timeframe,
        state: IndicatorStateSnapshot,
    ) -> None:
        """Refresh just the `latest_indicator_state` component.

        Called by Phase 7 (wired in Step 9b) whenever a new weekly
        indicator_state row is written. `ticker_ref` is required
        because, in the first-write-for-this-ticker case, we have
        to construct a new snapshot and need the readable identity.
        """
        with self._write_lock:
            existing = self._snapshots.get((ticker_id, timeframe))
            if existing is None:
                self._snapshots[(ticker_id, timeframe)] = CachedSnapshot(
                    ticker_id=ticker_id,
                    ticker_ref=ticker_ref,
                    timeframe=timeframe,
                    latest_indicator_state=state,
                    latest_signal_event=None,
                    snapshot_taken_at=state.computed_at,
                    stale_since=None,
                )
            else:
                self._snapshots[(ticker_id, timeframe)] = replace(
                    existing,
                    latest_indicator_state=state,
                    snapshot_taken_at=state.computed_at,
                )

    def mark_stale(
        self,
        ticker_id: int,
        timeframe: Timeframe,
        when: datetime,
    ) -> None:
        """Flag a snapshot as stale due to an observed data gap.

        Called by the compute scheduler when it detects missing
        bars on a ticker. The snapshot itself is left untouched —
        the indicator_state and signal_event components keep their
        last-known-good values. The `stale_since` field is set if
        not already, or left at its existing value (we record the
        time of FIRST detection, not the most recent observation).

        No-op when the cache has no entry for the pair.
        """
        with self._write_lock:
            existing = self._snapshots.get((ticker_id, timeframe))
            if existing is None:
                return
            if existing.stale_since is not None:
                return  # don't overwrite the first-detection time
            self._snapshots[(ticker_id, timeframe)] = replace(
                existing, stale_since=when,
            )

    def clear_stale(self, ticker_id: int, timeframe: Timeframe) -> None:
        """Clear the stale flag. Called by the compute scheduler on
        a pass that finds the gap filled.

        No-op when the cache has no entry, or when the entry is
        already non-stale.
        """
        with self._write_lock:
            existing = self._snapshots.get((ticker_id, timeframe))
            if existing is None or existing.stale_since is None:
                return
            self._snapshots[(ticker_id, timeframe)] = replace(
                existing, stale_since=None,
            )

    def remove_snapshot(self, ticker_id: int, timeframe: Timeframe) -> bool:
        """Drop an entry. Returns True iff something was removed."""
        with self._write_lock:
            return self._snapshots.pop((ticker_id, timeframe), None) is not None

    # ---------- Test helpers ---------- #

    def clear(self) -> None:
        with self._write_lock:
            self._snapshots.clear()
