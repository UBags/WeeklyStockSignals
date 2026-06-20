"""Unit tests for `SignalCache` post-Step-9a.

The legacy `CachedSignal` / `put` / `get` / `all_signals` /
`stale_signals` / `__len__` surface is gone. These tests pin the
remaining surface: the snapshot shape, the writer-callback paths,
and the new stale_since machinery the compute scheduler (Step 9c)
will drive.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from trip_ema.cache.signal_cache import (
    CachedSnapshot,
    IndicatorStateSnapshot,
    SignalCache,
    SignalEventSnapshot,
)
from trip_ema.domain import (
    ChangeSignal,
    SignalEvent,
    TickerRef,
    Timeframe,
)


_TICKER = TickerRef(exchange="NSE", symbol="SYNTH", instrument="EQ")


def _ind(trade_date: date, regime: int = 1) -> IndicatorStateSnapshot:
    return IndicatorStateSnapshot(
        trade_date=trade_date,
        med_ema=100.0, long_ema=100.0, longest_ema=100.0, atr=1.5,
        upper_inner_band=100.3, lower_inner_band=99.7,
        upper_mid_band=103.0,   lower_mid_band=97.0,
        upper_outer_band=106.75, lower_outer_band=93.25,
        display_regime=regime,
        computed_at=datetime(2025, 6, 13, 18, 0, tzinfo=timezone.utc),
    )


def _ev(signal: ChangeSignal, *, price: float = 100.0) -> SignalEventSnapshot:
    return SignalEventSnapshot(
        event_id=42,
        bar_timestamp=datetime(2025, 6, 9, tzinfo=timezone.utc),
        signal=signal,
        price_at_signal=price,
        computed_at=datetime(2025, 6, 13, 18, 0, tzinfo=timezone.utc),
    )


def _snap(
    *,
    ticker_id: int = 1806,
    timeframe: Timeframe = Timeframe.WEEKLY,
    ind=None, ev=None,
    stale_since=None,
) -> CachedSnapshot:
    return CachedSnapshot(
        ticker_id=ticker_id,
        ticker_ref=_TICKER,
        timeframe=timeframe,
        latest_indicator_state=ind,
        latest_signal_event=ev,
        snapshot_taken_at=datetime(2025, 6, 20, 12, 0, tzinfo=timezone.utc),
        stale_since=stale_since,
    )


# ---------------------------------------------------------------------------
# put_snapshot / get_snapshot / snapshot_count round-trips
# ---------------------------------------------------------------------------


class TestSnapshotRoundTrip:

    def test_get_returns_none_for_unknown(self):
        cache = SignalCache()
        assert cache.get_snapshot(1806, Timeframe.WEEKLY) is None
        assert cache.snapshot_count() == 0

    def test_put_then_get(self):
        cache = SignalCache()
        snap = _snap(ind=_ind(date(2025, 6, 9)), ev=_ev(ChangeSignal.BUY))
        cache.put_snapshot(snap)
        assert cache.get_snapshot(1806, Timeframe.WEEKLY) == snap
        assert cache.snapshot_count() == 1

    def test_put_replaces(self):
        cache = SignalCache()
        s1 = _snap(ind=_ind(date(2025, 6, 2)))
        s2 = _snap(ind=_ind(date(2025, 6, 9)))
        cache.put_snapshot(s1)
        cache.put_snapshot(s2)
        assert cache.get_snapshot(1806, Timeframe.WEEKLY) == s2

    def test_has_snapshot(self):
        cache = SignalCache()
        assert cache.has_snapshot(1806, Timeframe.WEEKLY) is False
        cache.put_snapshot(_snap(ind=_ind(date(2025, 6, 9))))
        assert cache.has_snapshot(1806, Timeframe.WEEKLY) is True

    def test_separate_timeframes_are_separate_entries(self):
        cache = SignalCache()
        sw = _snap(timeframe=Timeframe.WEEKLY)
        sd = _snap(timeframe=Timeframe.DAILY)
        cache.put_snapshot(sw)
        cache.put_snapshot(sd)
        assert cache.get_snapshot(1806, Timeframe.WEEKLY) == sw
        assert cache.get_snapshot(1806, Timeframe.DAILY) == sd
        assert cache.snapshot_count() == 2

    def test_all_snapshots_returns_all(self):
        cache = SignalCache()
        cache.put_snapshot(_snap(ticker_id=1))
        cache.put_snapshot(_snap(ticker_id=2))
        cache.put_snapshot(_snap(ticker_id=3))
        all_ = cache.all_snapshots()
        assert len(all_) == 3
        assert {s.ticker_id for s in all_} == {1, 2, 3}

    def test_remove_snapshot(self):
        cache = SignalCache()
        cache.put_snapshot(_snap())
        assert cache.remove_snapshot(1806, Timeframe.WEEKLY) is True
        assert cache.get_snapshot(1806, Timeframe.WEEKLY) is None
        # Idempotent.
        assert cache.remove_snapshot(1806, Timeframe.WEEKLY) is False


# ---------------------------------------------------------------------------
# update_signal_event — the Step 6 writer-callback path
# ---------------------------------------------------------------------------


class TestUpdateSignalEvent:

    def _live_event(self, signal: ChangeSignal = ChangeSignal.BUY) -> SignalEvent:
        return SignalEvent(
            ticker=_TICKER,
            timeframe=Timeframe.WEEKLY,
            bar_timestamp=datetime(2025, 6, 9, tzinfo=timezone.utc),
            signal=signal,
            price_at_signal=99.5,
            computed_at=datetime(2025, 6, 13, 18, 0, tzinfo=timezone.utc),
        )

    def test_first_event_creates_snapshot_with_no_indicator(self):
        cache = SignalCache()
        ev = self._live_event()
        cache.update_signal_event(1806, Timeframe.WEEKLY, ev)
        snap = cache.get_snapshot(1806, Timeframe.WEEKLY)
        assert snap is not None
        assert snap.latest_indicator_state is None
        assert snap.latest_signal_event.signal == ChangeSignal.BUY
        assert snap.latest_signal_event.price_at_signal == 99.5
        assert snap.ticker_ref == _TICKER

    def test_event_after_indicator_preserves_indicator(self):
        cache = SignalCache()
        cache.put_snapshot(_snap(ind=_ind(date(2025, 6, 9)), ev=None))
        ev = self._live_event(signal=ChangeSignal.SELL)
        cache.update_signal_event(1806, Timeframe.WEEKLY, ev)
        snap = cache.get_snapshot(1806, Timeframe.WEEKLY)
        assert snap.latest_indicator_state is not None
        assert snap.latest_indicator_state.trade_date == date(2025, 6, 9)
        assert snap.latest_signal_event.signal == ChangeSignal.SELL

    def test_event_replaces_existing_event(self):
        cache = SignalCache()
        cache.put_snapshot(_snap(
            ind=_ind(date(2025, 6, 9)),
            ev=_ev(ChangeSignal.BUY, price=100.0),
        ))
        ev = self._live_event(signal=ChangeSignal.EXIT_FROM_BUY)
        cache.update_signal_event(1806, Timeframe.WEEKLY, ev)
        snap = cache.get_snapshot(1806, Timeframe.WEEKLY)
        assert snap.latest_signal_event.signal == ChangeSignal.EXIT_FROM_BUY
        assert snap.latest_indicator_state is not None

    def test_event_update_preserves_stale_flag(self):
        """The stale flag is a separate axis from event data.
        Updating the event component must NOT clear stale_since —
        that's a separate signal (the gap is still observed)."""
        cache = SignalCache()
        cache.put_snapshot(_snap(
            ind=_ind(date(2025, 6, 9)),
            ev=_ev(ChangeSignal.BUY),
            stale_since=datetime(2025, 6, 14, tzinfo=timezone.utc),
        ))
        cache.update_signal_event(1806, Timeframe.WEEKLY, self._live_event())
        snap = cache.get_snapshot(1806, Timeframe.WEEKLY)
        assert snap.stale_since == datetime(2025, 6, 14, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# update_indicator_state — the Phase 7 wire-in path (Step 9b target)
# ---------------------------------------------------------------------------


class TestUpdateIndicatorState:

    def test_first_indicator_creates_snapshot_with_no_event(self):
        cache = SignalCache()
        cache.update_indicator_state(
            1806, _TICKER, Timeframe.WEEKLY, _ind(date(2025, 6, 9)),
        )
        snap = cache.get_snapshot(1806, Timeframe.WEEKLY)
        assert snap is not None
        assert snap.latest_signal_event is None
        assert snap.latest_indicator_state.trade_date == date(2025, 6, 9)

    def test_indicator_after_event_preserves_event(self):
        cache = SignalCache()
        cache.update_signal_event(1806, Timeframe.WEEKLY, SignalEvent(
            ticker=_TICKER, timeframe=Timeframe.WEEKLY,
            bar_timestamp=datetime(2025, 6, 9, tzinfo=timezone.utc),
            signal=ChangeSignal.BUY, price_at_signal=99.5,
            computed_at=datetime(2025, 6, 13, tzinfo=timezone.utc),
        ))
        cache.update_indicator_state(
            1806, _TICKER, Timeframe.WEEKLY, _ind(date(2025, 6, 9)),
        )
        snap = cache.get_snapshot(1806, Timeframe.WEEKLY)
        assert snap.latest_signal_event.signal == ChangeSignal.BUY
        assert snap.latest_indicator_state.trade_date == date(2025, 6, 9)


# ---------------------------------------------------------------------------
# stale_since machinery — Step 9c target
# ---------------------------------------------------------------------------


class TestStaleMachinery:

    _WHEN = datetime(2025, 6, 14, 12, 0, tzinfo=timezone.utc)
    _LATER = datetime(2025, 6, 15, 12, 0, tzinfo=timezone.utc)

    def test_mark_stale_sets_field(self):
        cache = SignalCache()
        cache.put_snapshot(_snap(ind=_ind(date(2025, 6, 9))))
        cache.mark_stale(1806, Timeframe.WEEKLY, self._WHEN)
        assert cache.get_snapshot(1806, Timeframe.WEEKLY).stale_since == self._WHEN

    def test_mark_stale_records_first_detection_not_latest(self):
        """If a snapshot is already stale, mark_stale must NOT
        overwrite the timestamp — we want to track when the gap was
        FIRST observed, not the most recent observation."""
        cache = SignalCache()
        cache.put_snapshot(_snap(
            ind=_ind(date(2025, 6, 9)),
            stale_since=self._WHEN,
        ))
        cache.mark_stale(1806, Timeframe.WEEKLY, self._LATER)
        assert cache.get_snapshot(1806, Timeframe.WEEKLY).stale_since == self._WHEN

    def test_mark_stale_on_unknown_ticker_is_noop(self):
        cache = SignalCache()
        cache.mark_stale(9999, Timeframe.WEEKLY, self._WHEN)
        assert cache.get_snapshot(9999, Timeframe.WEEKLY) is None

    def test_clear_stale_resets_field(self):
        cache = SignalCache()
        cache.put_snapshot(_snap(
            ind=_ind(date(2025, 6, 9)),
            stale_since=self._WHEN,
        ))
        cache.clear_stale(1806, Timeframe.WEEKLY)
        assert cache.get_snapshot(1806, Timeframe.WEEKLY).stale_since is None

    def test_clear_stale_on_non_stale_is_noop(self):
        cache = SignalCache()
        cache.put_snapshot(_snap(ind=_ind(date(2025, 6, 9))))
        cache.clear_stale(1806, Timeframe.WEEKLY)
        assert cache.get_snapshot(1806, Timeframe.WEEKLY).stale_since is None

    def test_clear_stale_on_unknown_ticker_is_noop(self):
        cache = SignalCache()
        cache.clear_stale(9999, Timeframe.WEEKLY)
        assert cache.get_snapshot(9999, Timeframe.WEEKLY) is None

    def test_all_stale_snapshots_returns_only_stale(self):
        cache = SignalCache()
        cache.put_snapshot(_snap(ticker_id=1))                          # not stale
        cache.put_snapshot(_snap(ticker_id=2, stale_since=self._WHEN))  # stale
        cache.put_snapshot(_snap(ticker_id=3))                          # not stale
        cache.put_snapshot(_snap(ticker_id=4, stale_since=self._LATER)) # stale
        stale = cache.all_stale_snapshots()
        assert {s.ticker_id for s in stale} == {2, 4}
