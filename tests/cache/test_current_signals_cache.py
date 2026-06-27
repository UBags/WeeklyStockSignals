"""Unit tests for `trip_ema.cache.current_signals_cache.CurrentSignalsCache`.

Pure-Python — no DB. The cache is an in-memory dict guarded by an
atomic-swap discipline.
"""
from __future__ import annotations

import threading
from datetime import datetime, UTC

import pytest

from trip_ema.cache.current_signals_cache import CurrentSignalsCache
from trip_ema.domain.signal_event import SignalEvent


# ============================================================
# Helpers
# ============================================================

def _signal(
    ticker_id: int,
    *,
    timeframe: str = "1W",
    signal_type: str = "BUY",
    regime_before: int = 0,
    regime_after: int = 1,
    price_at_signal: float = 100.0,
    bar_timestamp: datetime | None = None,
    computed_at: datetime | None = None,
) -> SignalEvent:
    return SignalEvent(
        ticker_id=ticker_id,
        timeframe=timeframe,
        bar_timestamp=(
            bar_timestamp or datetime(2025, 10, 6, tzinfo=UTC)
        ),
        signal_type=signal_type,
        regime_before=regime_before,
        regime_after=regime_after,
        price_at_signal=price_at_signal,
        computed_at=computed_at or datetime.now(UTC),
    )


# ============================================================
# Empty cache
# ============================================================

class TestEmpty:

    def test_empty_signal_count_is_zero(self):
        cache = CurrentSignalsCache()
        assert cache.signal_count() == 0

    def test_empty_get_returns_none(self):
        cache = CurrentSignalsCache()
        assert cache.get_signal(1, "1W") is None

    def test_empty_all_signals_is_empty_list(self):
        cache = CurrentSignalsCache()
        assert cache.all_signals() == []


# ============================================================
# rebuild_from basic semantics
# ============================================================

class TestRebuildFrom:

    def test_rebuild_populates_dict(self):
        cache = CurrentSignalsCache()
        signals = [_signal(1), _signal(2), _signal(3)]
        cache.rebuild_from(signals)
        assert cache.signal_count() == 3
        assert cache.get_signal(1, "1W") is not None
        assert cache.get_signal(2, "1W") is not None
        assert cache.get_signal(3, "1W") is not None

    def test_rebuild_from_empty_empties_cache(self):
        cache = CurrentSignalsCache()
        cache.rebuild_from([_signal(1), _signal(2)])
        assert cache.signal_count() == 2

        cache.rebuild_from([])
        assert cache.signal_count() == 0
        assert cache.get_signal(1, "1W") is None
        assert cache.get_signal(2, "1W") is None

    def test_second_rebuild_replaces_first(self):
        """rebuild_from doesn't accumulate — it REPLACES."""
        cache = CurrentSignalsCache()
        cache.rebuild_from([_signal(1), _signal(2)])
        # Second rebuild with completely different tickers
        cache.rebuild_from([_signal(10), _signal(20)])
        assert cache.signal_count() == 2
        assert cache.get_signal(1, "1W") is None
        assert cache.get_signal(2, "1W") is None
        assert cache.get_signal(10, "1W") is not None
        assert cache.get_signal(20, "1W") is not None

    def test_rebuild_overwrites_same_ticker(self):
        cache = CurrentSignalsCache()
        cache.rebuild_from([_signal(
            1, signal_type="BUY",
            regime_before=0, regime_after=1,
            price_at_signal=100.0,
        )])
        # Second rebuild with same ticker, different signal
        cache.rebuild_from([_signal(
            1, signal_type="SELL",
            regime_before=0, regime_after=-1,
            price_at_signal=200.0,
        )])
        sig = cache.get_signal(1, "1W")
        assert sig is not None
        assert sig.signal_type == "SELL"
        assert sig.price_at_signal == 200.0

    def test_drops_tickers_not_in_new_snapshot(self):
        """If ticker T was in the old cache but not in the new
        signals list, it's dropped. NO stale_since flag — they're
        just gone."""
        cache = CurrentSignalsCache()
        cache.rebuild_from([_signal(1), _signal(2), _signal(3)])

        # Second rebuild without ticker 2
        cache.rebuild_from([_signal(1), _signal(3)])
        assert cache.signal_count() == 2
        assert cache.get_signal(2, "1W") is None
        assert cache.get_signal(1, "1W") is not None
        assert cache.get_signal(3, "1W") is not None


# ============================================================
# Key shape
# ============================================================

class TestKeyShape:

    def test_keyed_by_ticker_and_timeframe(self):
        """The cache key is (ticker_id, timeframe) — same ticker
        with different timeframes coexist."""
        cache = CurrentSignalsCache()
        cache.rebuild_from([
            _signal(1, timeframe="1W"),
            _signal(1, timeframe="1D"),
        ])
        assert cache.signal_count() == 2
        assert cache.get_signal(1, "1W") is not None
        assert cache.get_signal(1, "1D") is not None

    def test_timeframe_mismatch_returns_none(self):
        cache = CurrentSignalsCache()
        cache.rebuild_from([_signal(1, timeframe="1W")])
        assert cache.get_signal(1, "1W") is not None
        assert cache.get_signal(1, "1D") is None


# ============================================================
# all_signals
# ============================================================

class TestAllSignals:

    def test_returns_every_entry(self):
        cache = CurrentSignalsCache()
        sigs = [_signal(1), _signal(2), _signal(3)]
        cache.rebuild_from(sigs)
        out = cache.all_signals()
        assert len(out) == 3
        # Returns the SignalEvent objects (not their (k, v) pairs)
        ids = {s.ticker_id for s in out}
        assert ids == {1, 2, 3}

    def test_caller_mutation_doesnt_affect_cache(self):
        """`all_signals` returns a fresh list — mutating it MUST
        NOT affect the cache."""
        cache = CurrentSignalsCache()
        cache.rebuild_from([_signal(1), _signal(2)])

        out = cache.all_signals()
        out.clear()
        out.append(_signal(99))

        # Cache unchanged
        assert cache.signal_count() == 2
        assert cache.get_signal(1, "1W") is not None
        assert cache.get_signal(99, "1W") is None


# ============================================================
# Atomic swap under concurrent reads
# ============================================================

class TestAtomicSwap:
    """The rebuild_from contract: readers in flight during a rebuild
    see EITHER the old dict OR the new dict — never a torn
    intermediate. Verified via 100 reader threads × 10 rebuilds."""

    def test_concurrent_reads_never_see_torn_state(self):
        cache = CurrentSignalsCache()

        # Two clean snapshots — readers MUST see one or the other,
        # never a partial mix.
        snap_a = [
            _signal(i, signal_type="BUY",
                    regime_before=0, regime_after=1)
            for i in range(1, 51)
        ]
        snap_b = [
            _signal(i, signal_type="SELL",
                    regime_before=0, regime_after=-1)
            for i in range(101, 151)
        ]

        # Seed with snap_a
        cache.rebuild_from(snap_a)

        # Each reader takes a `signal_count` snapshot; the cache
        # should never be in a state where signal_count is neither
        # 50 (snap_a) nor 50 (snap_b) (both happen to be 50 here,
        # so we instead check that get_signal results are
        # internally consistent within a single read).
        torn_state = []

        def reader():
            for _ in range(200):
                sigs = cache.all_signals()
                if not sigs:
                    continue
                # All sigs in a single all_signals() call should
                # come from the SAME snapshot — either all BUYs (A)
                # or all SELLs (B). A torn state would mix them.
                types = {s.signal_type for s in sigs}
                if len(types) > 1:
                    torn_state.append(types)

        def writer():
            for i in range(50):
                cache.rebuild_from(
                    snap_a if i % 2 == 0 else snap_b,
                )

        readers = [
            threading.Thread(target=reader) for _ in range(20)
        ]
        writer_thread = threading.Thread(target=writer)

        for r in readers:
            r.start()
        writer_thread.start()
        for r in readers:
            r.join()
        writer_thread.join()

        assert torn_state == [], (
            f"Saw {len(torn_state)} torn states — atomic-swap "
            f"discipline was violated. Sample: {torn_state[:3]}"
        )
