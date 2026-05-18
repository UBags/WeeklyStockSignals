"""Tests for SignalCache — the read-mostly per-ticker cache."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pytest

from trip_ema.cache import CachedSignal, SignalCache, build_cached_signal
from trip_ema.domain import BarState, ChangeSignal, CoreRegime, TickerRef, Timeframe


def _state(t: datetime, display: CoreRegime = CoreRegime.NEUTRAL) -> BarState:
    return BarState(
        timestamp=t,
        med_ema=100.0, long_ema=100.0, longest_ema=100.0,
        atr=2.0,
        upper_inner=102, lower_inner=98,
        upper_mid=105, lower_mid=95,
        upper_outer=110, lower_outer=90,
        pierces_upper_mid=False, pierces_lower_mid=False,
        core_bg=display, display_bg=display,
    )


def _signal(
    ticker: TickerRef,
    *,
    tf: Timeframe = Timeframe.WEEKLY,
    display: CoreRegime = CoreRegime.NEUTRAL,
    last_change: ChangeSignal = ChangeSignal.NO_CHANGE,
    close: float = 100.0,
) -> CachedSignal:
    t = datetime(2024, 1, 1, tzinfo=timezone.utc)
    return build_cached_signal(
        ticker=ticker,
        timeframe=tf,
        latest_state=_state(t, display),
        latest_close=close,
        state_history=[_state(t, display)],
        last_change=last_change,
        last_change_bar=t if last_change != ChangeSignal.NO_CHANGE else None,
        last_change_computed_at=t if last_change != ChangeSignal.NO_CHANGE else None,
        computed_at=t,
    )


NIFTY = TickerRef("NSE", "NIFTY", "INDEX")
RELIANCE = TickerRef("NSE", "RELIANCE", "EQ")


# ---------- Reads on empty cache ---------- #

def test_empty_cache_returns_none():
    cache = SignalCache()
    assert cache.get(NIFTY, Timeframe.WEEKLY) is None
    assert cache.has(NIFTY, Timeframe.WEEKLY) is False
    assert len(cache) == 0
    assert cache.all_signals() == []


# ---------- Basic put / get ---------- #

def test_put_and_get_roundtrip():
    cache = SignalCache()
    signal = _signal(NIFTY)
    cache.put(signal)
    assert cache.get(NIFTY, Timeframe.WEEKLY) is signal
    assert cache.has(NIFTY, Timeframe.WEEKLY)
    assert len(cache) == 1


def test_put_replaces_existing_entry():
    cache = SignalCache()
    cache.put(_signal(NIFTY, last_change=ChangeSignal.NO_CHANGE))
    cache.put(_signal(NIFTY, last_change=ChangeSignal.BUY))
    got = cache.get(NIFTY, Timeframe.WEEKLY)
    assert got.last_change == ChangeSignal.BUY
    assert len(cache) == 1   # replaced, not duplicated


def test_distinct_tickers_kept_separate():
    cache = SignalCache()
    cache.put(_signal(NIFTY, last_change=ChangeSignal.BUY))
    cache.put(_signal(RELIANCE, last_change=ChangeSignal.SELL))
    assert cache.get(NIFTY, Timeframe.WEEKLY).last_change == ChangeSignal.BUY
    assert cache.get(RELIANCE, Timeframe.WEEKLY).last_change == ChangeSignal.SELL
    assert len(cache) == 2


def test_distinct_timeframes_kept_separate():
    cache = SignalCache()
    cache.put(_signal(NIFTY, tf=Timeframe.WEEKLY, last_change=ChangeSignal.BUY))
    cache.put(_signal(NIFTY, tf=Timeframe.DAILY, last_change=ChangeSignal.SELL))
    assert cache.get(NIFTY, Timeframe.WEEKLY).last_change == ChangeSignal.BUY
    assert cache.get(NIFTY, Timeframe.DAILY).last_change == ChangeSignal.SELL


# ---------- Mutation isolation ---------- #

def test_cached_signal_is_frozen():
    """CachedSignal is a frozen dataclass — direct mutation should raise."""
    signal = _signal(NIFTY)
    with pytest.raises(Exception):     # FrozenInstanceError
        signal.latest_close = 999.0


def test_state_history_is_tuple_not_list():
    """state_history must be a tuple so callers can't mutate it."""
    signal = _signal(NIFTY)
    assert isinstance(signal.state_history, tuple)


# ---------- Remove and clear ---------- #

def test_remove_returns_true_when_present():
    cache = SignalCache()
    cache.put(_signal(NIFTY))
    assert cache.remove(NIFTY, Timeframe.WEEKLY) is True
    assert cache.get(NIFTY, Timeframe.WEEKLY) is None


def test_remove_returns_false_when_absent():
    cache = SignalCache()
    assert cache.remove(NIFTY, Timeframe.WEEKLY) is False


def test_clear_empties_cache():
    cache = SignalCache()
    cache.put(_signal(NIFTY))
    cache.put(_signal(RELIANCE))
    cache.clear()
    assert len(cache) == 0


# ---------- Concurrent access smoke test ---------- #

def test_concurrent_reads_during_writes_do_not_crash():
    """Smoke test: a single writer thread and several reader threads can
    coexist without exceptions. Not a strict correctness test (Python's
    GIL + our locking already guarantees that) — just verifies no obvious
    deadlock or torn-state bug."""
    cache = SignalCache()
    stop = threading.Event()

    def writer():
        for i in range(500):
            change = ChangeSignal.BUY if i % 2 == 0 else ChangeSignal.SELL
            cache.put(_signal(NIFTY, last_change=change, close=100 + i))
        stop.set()

    def reader():
        seen_buy = seen_sell = False
        while not stop.is_set():
            sig = cache.get(NIFTY, Timeframe.WEEKLY)
            if sig is None:
                continue
            if sig.last_change == ChangeSignal.BUY:
                seen_buy = True
            elif sig.last_change == ChangeSignal.SELL:
                seen_sell = True
        # If the writer ran enough iterations, both states were observed.
        return seen_buy or seen_sell

    threads = [threading.Thread(target=reader) for _ in range(4)]
    for t in threads:
        t.start()
    writer()
    for t in threads:
        t.join(timeout=5.0)
        assert not t.is_alive(), "reader thread did not finish"
