"""Tests for TickerEngine — the full per-ticker pipeline.

These tests verify that all the pieces wire together correctly:
the EMAs feed into the bands, the bands feed into the regime, the
regime feeds into the BarState. Key property: prime(history) must
produce the same final state as ingesting the bars one at a time.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from trip_ema.domain import (
    BarState,
    ChangeSignal,
    CoreRegime,
    IndicatorParams,
    OHLCVBar,
    TickerRef,
    Timeframe,
)
from trip_ema.engine.ticker_engine import TickerEngine


# -------------------- Test helpers -------------------- #

def _ticker() -> TickerRef:
    return TickerRef(exchange="NSE", symbol="NIFTY", instrument="INDEX")


def _params() -> IndicatorParams:
    return IndicatorParams()


def _bars_at(
    start: datetime,
    closes: list[float],
    *,
    bar_seconds: int = 86400,
    volume: float = 1000.0,
    open_offset: float = 0.0,
    high_offset: float = 1.0,
    low_offset: float = -1.0,
) -> list[OHLCVBar]:
    """Build a sequence of OHLCV bars with given closes.
    Open is close + open_offset (default 0 → 'doji-ish'), high and low
    offsets define the bar range relative to close."""
    bars = []
    for i, c in enumerate(closes):
        bars.append(OHLCVBar(
            timestamp=start + timedelta(seconds=bar_seconds * i),
            open=c + open_offset,
            high=c + high_offset,
            low=c + low_offset,
            close=c,
            volume=volume,
        ))
    return bars


# -------------------- Construction and empty state -------------------- #

def test_empty_engine_has_no_state():
    engine = TickerEngine(_ticker(), Timeframe.WEEKLY, _params())
    assert engine.latest_state() is None
    assert engine.state_history() == []
    assert engine.last_bar_timestamp() is None


def test_engine_remembers_ticker_and_timeframe():
    t = _ticker()
    engine = TickerEngine(t, Timeframe.DAILY, _params())
    assert engine.ticker == t
    assert engine.timeframe == Timeframe.DAILY


# -------------------- Single-bar ingest -------------------- #

def test_single_bar_ingest_produces_barstate():
    engine = TickerEngine(_ticker(), Timeframe.WEEKLY, _params())
    bar = OHLCVBar(
        timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc),
        open=100, high=105, low=99, close=103, volume=1000,
    )
    state = engine.ingest(bar)
    assert isinstance(state, BarState)
    assert state.timestamp == bar.timestamp
    # During warmup most computed values are NaN — that's expected and fine.
    # The regime should be NEUTRAL by default.
    assert state.core_bg == CoreRegime.NEUTRAL
    assert state.display_bg == CoreRegime.NEUTRAL


# -------------------- prime vs ingest equivalence -------------------- #

def test_prime_is_equivalent_to_repeated_ingest():
    """Priming with a sequence must produce exactly the same final state
    as ingesting the same sequence one bar at a time.

    This is a fundamental invariant — if it breaks, the engine is no longer
    a pure fold over its input and downstream stateful behavior becomes
    undefined."""
    closes = [100 + i * 0.5 + 5 * (i % 4 - 1.5) for i in range(80)]
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    bars = _bars_at(start, closes)

    primed = TickerEngine(_ticker(), Timeframe.WEEKLY, _params())
    primed.prime(bars)

    one_by_one = TickerEngine(_ticker(), Timeframe.WEEKLY, _params())
    for b in bars:
        one_by_one.ingest(b)

    p_state = primed.latest_state()
    o_state = one_by_one.latest_state()
    assert p_state is not None
    assert o_state is not None
    # Compare every field except timestamp (trivially same)
    for field_name in (
        "med_ema", "long_ema", "longest_ema", "atr",
        "upper_inner", "lower_inner",
        "upper_mid", "lower_mid",
        "upper_outer", "lower_outer",
        "core_bg", "display_bg",
        "pierces_upper_mid", "pierces_lower_mid",
    ):
        p_val = getattr(p_state, field_name)
        o_val = getattr(o_state, field_name)
        # Handle NaN-safe equality for floats
        if isinstance(p_val, float):
            if p_val != p_val and o_val != o_val:   # both NaN
                continue
            assert p_val == pytest.approx(o_val), (
                f"field {field_name}: primed={p_val}, ingested={o_val}"
            )
        else:
            assert p_val == o_val, (
                f"field {field_name}: primed={p_val}, ingested={o_val}"
            )


# -------------------- state history bounds -------------------- #

def test_state_history_is_bounded():
    engine = TickerEngine(
        _ticker(), Timeframe.WEEKLY, _params(),
        history_retain=10,
    )
    bars = _bars_at(
        datetime(2024, 1, 1, tzinfo=timezone.utc),
        [100 + i for i in range(50)],
    )
    engine.prime(bars)
    # Only the last 10 should be retained
    history = engine.state_history()
    assert len(history) == 10
    # Most recent state's timestamp must be the last bar's
    assert history[-1].timestamp == bars[-1].timestamp


def test_state_history_n_smaller_than_retained_returns_last_n():
    engine = TickerEngine(_ticker(), Timeframe.WEEKLY, _params(), history_retain=50)
    bars = _bars_at(datetime(2024, 1, 1, tzinfo=timezone.utc), list(range(30)))
    engine.prime(bars)
    last_5 = engine.state_history(n=5)
    assert len(last_5) == 5
    assert last_5[-1].timestamp == bars[-1].timestamp


def test_state_history_n_zero_returns_empty():
    engine = TickerEngine(_ticker(), Timeframe.WEEKLY, _params())
    engine.ingest(_bars_at(datetime(2024, 1, 1, tzinfo=timezone.utc), [100])[0])
    assert engine.state_history(n=0) == []


# -------------------- detect_change -------------------- #

def test_detect_change_compares_to_provided_previous():
    """detect_change is parameterized — caller passes the previously observed
    display state. The engine itself doesn't track 'previous compute' state."""
    engine = TickerEngine(_ticker(), Timeframe.WEEKLY, _params())
    bar = OHLCVBar(
        timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc),
        open=100, high=105, low=99, close=103, volume=1000,
    )
    engine.ingest(bar)
    # During warmup display is NEUTRAL. If we tell detect_change the
    # previous observed state was also NEUTRAL, change should be NO_CHANGE.
    assert engine.detect_change(CoreRegime.NEUTRAL) == ChangeSignal.NO_CHANGE
    # If we tell detect_change the previous was BULLISH, we should get
    # EXIT_FROM_BUY (BULLISH -> NEUTRAL).
    assert engine.detect_change(CoreRegime.BULLISH) == ChangeSignal.EXIT_FROM_BUY


def test_detect_change_on_empty_engine_returns_no_change():
    engine = TickerEngine(_ticker(), Timeframe.WEEKLY, _params())
    assert engine.detect_change(CoreRegime.BULLISH) == ChangeSignal.NO_CHANGE


# -------------------- Long-running smoke test -------------------- #

def test_long_uptrend_eventually_paints_bullish():
    """A long, steady uptrend should at some point paint a bullish display.
    This is an integration smoke test — not a precise numerical assertion,
    just 'the pipeline produces sensible signals on obvious data'."""
    # 200 weekly bars rising from 100 to 300, with realistic candle bodies
    engine = TickerEngine(_ticker(), Timeframe.WEEKLY, _params())
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    for i in range(200):
        close = 100 + i * 1.0
        open_ = close - 0.5     # green candle
        bar = OHLCVBar(
            timestamp=start + timedelta(days=7 * i),
            open=open_,
            high=close + 0.5,
            low=open_ - 0.5,
            close=close,
            volume=1000.0,
        )
        engine.ingest(bar)

    history = engine.state_history()
    # At least *some* bar in the later portion of history should have been
    # painted bullish. We're not asserting precisely which one — that
    # depends on EMA convergence rates — just that the pipeline reaches
    # bullish at all on a clean uptrend.
    bullish_count = sum(1 for s in history if s.display_bg == CoreRegime.BULLISH)
    assert bullish_count > 0, "expected at least one bullish bar on a steady uptrend"


# ============================================================
# Snapshot / rollback for provisional bars
# ============================================================

class TestProvisionalSnapshot:
    """The critical invariant: ingesting a provisional bar and then either
    committing it or replacing it with a final bar must produce the same
    state as if the engine had only ever seen the final bar."""

    def _build_engine_with_history(self, n: int = 30) -> TickerEngine:
        eng = TickerEngine(_ticker(), Timeframe.WEEKLY, _params())
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        bars = _bars_at(start, [100 + i * 0.5 for i in range(n)])
        eng.prime(bars)
        return eng

    def test_provisional_then_commit_with_same_bar_equals_direct_ingest(self):
        """Sequence A: ingest_provisional(X) then commit_provisional().
        Sequence B: ingest(X).
        State after must be identical."""
        # Engine A — provisional path
        eng_a = self._build_engine_with_history()
        # Engine B — direct path
        eng_b = self._build_engine_with_history()

        next_bar = OHLCVBar(
            timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc) + timedelta(weeks=30),
            open=120, high=122, low=119, close=121, volume=1500.0,
        )

        eng_a.ingest_provisional(next_bar)
        eng_a.commit_provisional()
        eng_b.ingest(next_bar)

        # Compare every field of the latest BarState
        sa, sb = eng_a.latest_state(), eng_b.latest_state()
        assert sa is not None and sb is not None
        for field in ("med_ema", "long_ema", "longest_ema", "atr",
                      "upper_inner", "lower_inner", "upper_outer", "lower_outer",
                      "core_bg", "display_bg"):
            va, vb = getattr(sa, field), getattr(sb, field)
            if isinstance(va, float) and va != va and vb != vb:
                continue  # both NaN
            assert va == pytest.approx(vb) if isinstance(va, float) else va == vb, (
                f"field {field}: provisional+commit={va}, direct={vb}"
            )

    def test_replacing_provisional_does_not_compound_state(self):
        """Sequence A: ingest_provisional(X1), ingest_provisional(X2), commit.
        Sequence B: ingest(X2).
        State after must be identical. This is THE invariant that the
        original (pre-fix) engine violated — X1's update would permanently
        advance state, then X2's update would compound on top."""
        eng_a = self._build_engine_with_history()
        eng_b = self._build_engine_with_history()

        ts = datetime(2024, 1, 1, tzinfo=timezone.utc) + timedelta(weeks=30)
        x1 = OHLCVBar(timestamp=ts, open=120, high=121, low=119, close=120.5, volume=500.0)
        x2 = OHLCVBar(timestamp=ts, open=120, high=125, low=119, close=124, volume=1500.0)

        eng_a.ingest_provisional(x1)
        eng_a.ingest_provisional(x2)   # replaces x1
        eng_a.commit_provisional()
        eng_b.ingest(x2)

        sa, sb = eng_a.latest_state(), eng_b.latest_state()
        assert sa is not None and sb is not None
        for field in ("med_ema", "long_ema", "longest_ema", "atr"):
            va, vb = getattr(sa, field), getattr(sb, field)
            if isinstance(va, float) and va != va and vb != vb:
                continue
            assert va == pytest.approx(vb), (
                f"compounding detected on field {field}: "
                f"provisional×2+commit={va}, direct={vb}"
            )

    def test_provisional_then_final_replaces_correctly(self):
        """Common real-world pattern: ETL writes provisional weekly bar at
        each hourly update, then writes a final version at week close.
        State after ingest_provisional + ingest(final) must equal
        direct ingest(final)."""
        eng_a = self._build_engine_with_history()
        eng_b = self._build_engine_with_history()

        ts = datetime(2024, 1, 1, tzinfo=timezone.utc) + timedelta(weeks=30)
        provisional = OHLCVBar(timestamp=ts, open=120, high=122, low=119, close=121, volume=500.0)
        final = OHLCVBar(timestamp=ts, open=120, high=128, low=118, close=126, volume=5000.0)

        eng_a.ingest_provisional(provisional)
        eng_a.ingest(final)   # finalizes — should rollback then apply final
        eng_b.ingest(final)

        sa, sb = eng_a.latest_state(), eng_b.latest_state()
        for field in ("med_ema", "long_ema", "longest_ema", "atr",
                      "upper_inner", "lower_outer"):
            va, vb = getattr(sa, field), getattr(sb, field)
            if isinstance(va, float) and va != va and vb != vb:
                continue
            assert va == pytest.approx(vb), (
                f"field {field}: provisional+final={va}, direct={vb}"
            )

    def test_has_provisional_tracks_state_correctly(self):
        eng = self._build_engine_with_history()
        assert eng.has_provisional() is False

        ts = datetime(2024, 1, 1, tzinfo=timezone.utc) + timedelta(weeks=30)
        bar = OHLCVBar(timestamp=ts, open=120, high=122, low=119, close=121, volume=500.0)

        eng.ingest_provisional(bar)
        assert eng.has_provisional() is True

        eng.commit_provisional()
        assert eng.has_provisional() is False

        # ingest() while no provisional held should not affect the flag
        next_ts = ts + timedelta(weeks=1)
        eng.ingest(OHLCVBar(timestamp=next_ts, open=121, high=123, low=120, close=122, volume=500.0))
        assert eng.has_provisional() is False

    def test_commit_with_no_provisional_is_no_op(self):
        eng = self._build_engine_with_history()
        # Should not raise
        eng.commit_provisional()
        eng.commit_provisional()
        assert eng.has_provisional() is False

    def test_state_history_correctly_trimmed_on_rollback(self):
        """When ingest_provisional is replaced by another ingest_provisional,
        the appended BarState from the first provisional must be popped."""
        eng = self._build_engine_with_history()
        history_len_before = len(eng.state_history())

        ts = datetime(2024, 1, 1, tzinfo=timezone.utc) + timedelta(weeks=30)
        x1 = OHLCVBar(timestamp=ts, open=120, high=121, low=119, close=120.5, volume=500.0)
        x2 = OHLCVBar(timestamp=ts, open=120, high=125, low=119, close=124, volume=1500.0)

        eng.ingest_provisional(x1)
        assert len(eng.state_history()) == history_len_before + 1

        eng.ingest_provisional(x2)   # rollback x1, apply x2
        # Should still be +1 from baseline, not +2
        assert len(eng.state_history()) == history_len_before + 1
        # And the held state should be x2's, not x1's
        assert eng.latest_state().timestamp == ts
        # Verify by close price — high tells us which bar's calc ran
        # (can't check close directly because BarState doesn't carry it,
        # but last_close() does)
        assert eng.last_close() == 124


# ============================================================
# A1: Erased-bar regression tests
# ============================================================
# Scenario from Gemini Doc 3 #1: the ETL stops updating week N's
# provisional bar and starts sending week N+1's bar. The scheduler
# calls ingest_provisional(week_N+1) while still holding the snapshot
# from week N. Without the A1 fix, ingest_provisional would rollback
# to pre-week-N state and apply week N+1 on top, silently erasing
# week N from the EMA recurrences.


class TestErasedBarA1:
    """Verifies the A1 fix: when a new bar with a STRICTLY NEWER
    timestamp arrives while a provisional is held, the held bar
    must be COMMITTED (not rolled back), so its state advance
    persists."""

    def _build_engine_with_history(self, n: int = 30) -> TickerEngine:
        eng = TickerEngine(_ticker(), Timeframe.WEEKLY, _params())
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        bars = _bars_at(start, [100 + i * 0.5 for i in range(n)])
        eng.prime(bars)
        return eng

    def test_provisional_then_provisional_at_later_ts_commits_first(self):
        """Sequence A: ingest_provisional(week_N), ingest_provisional(week_N+1).
        Sequence B (reference): ingest(week_N), ingest_provisional(week_N+1).

        State after must match. Without A1 fix, sequence A would erase
        week_N's contribution to the EMAs.
        """
        eng_a = self._build_engine_with_history()
        eng_b = self._build_engine_with_history()

        ts_n = datetime(2024, 1, 1, tzinfo=timezone.utc) + timedelta(weeks=30)
        ts_n_plus_1 = ts_n + timedelta(weeks=1)
        week_n = OHLCVBar(timestamp=ts_n, open=120, high=125, low=119,
                          close=124, volume=2000.0)
        week_n_plus_1 = OHLCVBar(timestamp=ts_n_plus_1, open=124, high=127,
                                  low=123, close=126, volume=1500.0)

        # Path A: both provisional. Week N's state must be committed
        # when week N+1 arrives.
        eng_a.ingest_provisional(week_n)
        eng_a.ingest_provisional(week_n_plus_1)

        # Path B: week N final, week N+1 provisional.
        eng_b.ingest(week_n)
        eng_b.ingest_provisional(week_n_plus_1)

        sa, sb = eng_a.latest_state(), eng_b.latest_state()
        for field in ("med_ema", "long_ema", "longest_ema", "atr"):
            va, vb = getattr(sa, field), getattr(sb, field)
            if isinstance(va, float) and va != va and vb != vb:
                continue
            assert va == pytest.approx(vb), (
                f"A1 erased-bar bug on field {field}: prov+prov={va}, "
                f"final+prov={vb}. Week N was likely rolled back instead "
                f"of committed."
            )

    def test_provisional_then_final_at_later_ts_commits_first(self):
        """Same as above but the second call is ingest() not
        ingest_provisional(). Week N must be committed, not rolled back."""
        eng_a = self._build_engine_with_history()
        eng_b = self._build_engine_with_history()

        ts_n = datetime(2024, 1, 1, tzinfo=timezone.utc) + timedelta(weeks=30)
        ts_n_plus_1 = ts_n + timedelta(weeks=1)
        week_n = OHLCVBar(timestamp=ts_n, open=120, high=125, low=119,
                          close=124, volume=2000.0)
        week_n_plus_1 = OHLCVBar(timestamp=ts_n_plus_1, open=124, high=127,
                                  low=123, close=126, volume=1500.0)

        eng_a.ingest_provisional(week_n)
        eng_a.ingest(week_n_plus_1)

        eng_b.ingest(week_n)
        eng_b.ingest(week_n_plus_1)

        sa, sb = eng_a.latest_state(), eng_b.latest_state()
        for field in ("med_ema", "long_ema", "longest_ema", "atr"):
            va, vb = getattr(sa, field), getattr(sb, field)
            if isinstance(va, float) and va != va and vb != vb:
                continue
            assert va == pytest.approx(vb)

    def test_provisional_same_ts_still_rolls_back(self):
        """Regression: the same-timestamp case (within-bar update)
        must continue to ROLLBACK, not commit. This is the case the
        original snapshot mechanism was designed for and we don't want
        to break."""
        eng_a = self._build_engine_with_history()
        eng_b = self._build_engine_with_history()

        ts = datetime(2024, 1, 1, tzinfo=timezone.utc) + timedelta(weeks=30)
        v1 = OHLCVBar(timestamp=ts, open=120, high=121, low=119,
                       close=120.5, volume=500.0)
        v2 = OHLCVBar(timestamp=ts, open=120, high=125, low=119,
                       close=124, volume=2500.0)   # final, larger volume

        eng_a.ingest_provisional(v1)
        eng_a.ingest_provisional(v2)   # same ts → rollback then apply

        eng_b.ingest_provisional(v2)   # direct

        sa, sb = eng_a.latest_state(), eng_b.latest_state()
        for field in ("med_ema", "long_ema", "longest_ema", "atr"):
            va, vb = getattr(sa, field), getattr(sb, field)
            if isinstance(va, float) and va != va and vb != vb:
                continue
            assert va == pytest.approx(vb)

    def test_state_history_grows_when_timestamp_advances(self):
        """After ingest_provisional(N) + ingest_provisional(N+1), the
        state_history should have +2 entries (one per bar), not +1
        as would happen if we rolled back N's BarState."""
        eng = self._build_engine_with_history()
        baseline = len(eng.state_history())

        ts_n = datetime(2024, 1, 1, tzinfo=timezone.utc) + timedelta(weeks=30)
        week_n = OHLCVBar(timestamp=ts_n, open=120, high=125, low=119,
                          close=124, volume=1000.0)
        week_n_plus_1 = OHLCVBar(timestamp=ts_n + timedelta(weeks=1),
                                  open=124, high=127, low=123,
                                  close=126, volume=1000.0)

        eng.ingest_provisional(week_n)
        assert len(eng.state_history()) == baseline + 1

        eng.ingest_provisional(week_n_plus_1)
        # Week N's BarState should still be there + week N+1's
        assert len(eng.state_history()) == baseline + 2

    def test_repeated_same_ts_provisionals_after_advance_still_correct(self):
        """The full real-world pattern: week N gets updated several times
        within the week, then commits, then week N+1 arrives and gets
        updated several times. State must be consistent throughout."""
        eng_a = self._build_engine_with_history()
        eng_b = self._build_engine_with_history()

        ts_n = datetime(2024, 1, 1, tzinfo=timezone.utc) + timedelta(weeks=30)
        n_v1 = OHLCVBar(timestamp=ts_n, open=120, high=121, low=119,
                         close=120.5, volume=200.0)
        n_v2 = OHLCVBar(timestamp=ts_n, open=120, high=125, low=119,
                         close=124, volume=2500.0)
        ts_np1 = ts_n + timedelta(weeks=1)
        np1_v1 = OHLCVBar(timestamp=ts_np1, open=124, high=125, low=123,
                           close=124.5, volume=200.0)
        np1_v2 = OHLCVBar(timestamp=ts_np1, open=124, high=128, low=123,
                           close=127, volume=2500.0)

        # Path A: realistic sequence
        eng_a.ingest_provisional(n_v1)
        eng_a.ingest_provisional(n_v2)   # same ts → rollback
        eng_a.ingest_provisional(np1_v1)  # newer ts → commit n_v2
        eng_a.ingest_provisional(np1_v2)  # same ts → rollback to between

        # Path B: as if we'd only ever seen the final versions, both final
        eng_b.ingest(n_v2)
        eng_b.ingest_provisional(np1_v2)

        sa, sb = eng_a.latest_state(), eng_b.latest_state()
        for field in ("med_ema", "long_ema", "longest_ema", "atr"):
            va, vb = getattr(sa, field), getattr(sb, field)
            if isinstance(va, float) and va != va and vb != vb:
                continue
            assert va == pytest.approx(vb), f"field {field}: a={va}, b={vb}"


# ============================================================
# Phase 9 — A1 regression: bounded-deque rollback
#
# Originally, _take_snapshot stored `states_len = len(self._states)` and
# rollback did `while len(self._states) > snap_len: self._states.pop()`.
# This worked when the deque was below its maxlen, but at maxlen the
# append-on-provisional would silently drop the oldest left-side entry,
# leaving the length unchanged. The rollback loop's `>` condition then
# failed, the provisional state was never removed, and the genuine oldest
# history bar was permanently lost.
#
# The fix snapshots the deque contents (as a tuple) and restores via
# clear() + extend(), which is correct regardless of maxlen.
# ============================================================


class TestStatesDequeRollbackAtMaxlen:
    """Regression: rollback must work correctly when _states is at maxlen."""

    def _build_engine_at_maxlen(self, history_retain: int = 50) -> TickerEngine:
        """Build an engine whose _states deque is full (length == maxlen)."""
        eng = TickerEngine(
            _ticker(), Timeframe.DAILY, _params(),
            history_retain=history_retain,
        )
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        # Need enough bars to (a) warm up the EMAs and (b) fill the deque.
        # Warmup is ~longest_base + s1 + s2 + sqrt(s2) ≈ 40 bars; we add
        # history_retain on top to ensure the deque is fully populated.
        closes = [100.0 + i * 0.5 for i in range(history_retain + 60)]
        eng.prime(_bars_at(start, closes))
        # Sanity: confirm we're actually at maxlen
        assert len(eng.state_history()) == history_retain
        return eng

    def test_provisional_rollback_preserves_oldest_history_at_maxlen(self):
        """The bug: at maxlen, ingesting a provisional bar would drop the
        oldest history bar, and rollback couldn't recover it. After the
        fix, rolling back must restore the deque to its exact pre-
        provisional contents."""
        eng = self._build_engine_at_maxlen(history_retain=50)

        # Capture the timestamp of the oldest bar before the provisional
        oldest_before = eng.state_history()[0].timestamp
        newest_before = eng.state_history()[-1].timestamp
        assert len(eng.state_history()) == 50

        # Ingest a provisional bar at a NEW timestamp (one day after
        # newest). This triggers _take_snapshot + _apply_bar, which
        # appends to _states. At maxlen, the deque would drop the oldest.
        prov_ts = newest_before + timedelta(days=1)
        prov_bar = OHLCVBar(
            timestamp=prov_ts,
            open=200.0, high=201.0, low=199.0, close=200.5, volume=1000.0,
        )
        eng.ingest_provisional(prov_bar)
        assert eng.has_provisional()

        # The deque now contains the provisional state at the right end.
        # Length is still 50; the oldest entry is now what was previously
        # the second-oldest.
        states_with_prov = eng.state_history()
        assert len(states_with_prov) == 50
        assert states_with_prov[-1].timestamp == prov_ts

        # Now: re-ingest the SAME provisional timestamp with an updated bar.
        # This calls _rollback_to_snapshot first, then re-applies. After
        # rollback, the deque must be restored to its pre-provisional state:
        #   - length 50 again
        #   - oldest entry must be `oldest_before` (the genuine oldest)
        prov_bar_v2 = OHLCVBar(
            timestamp=prov_ts,
            open=200.0, high=205.0, low=198.0, close=203.0, volume=1500.0,
        )
        eng.ingest_provisional(prov_bar_v2)

        # The post-rollback-then-reapply deque:
        #   - rollback restored to the pre-provisional 50 bars (oldest_before .. newest_before)
        #   - then the new provisional was appended, dropping oldest_before
        #   - so the deque is now (oldest_before + 1 day) .. newest_before, plus prov_ts
        # The critical assertion: the SECOND-oldest entry from the original
        # history must now be the oldest. The bug would have lost an extra
        # bar (i.e., the new oldest would be (oldest_before + 2 days)).
        states_after = eng.state_history()
        assert len(states_after) == 50
        assert states_after[-1].timestamp == prov_ts
        # The genuine bar at index 1 of the original history should now be at index 0
        expected_oldest = oldest_before + timedelta(days=1)
        assert states_after[0].timestamp == expected_oldest, (
            f"Expected oldest to be {expected_oldest}, got {states_after[0].timestamp}. "
            "If this is `oldest_before + 2 days`, the deque-rollback bug has regressed."
        )

    def test_rollback_at_maxlen_does_not_lose_extra_history(self):
        """Stronger version of the previous test: do many cycles of
        same-timestamp provisional rollbacks at maxlen and verify that
        the deque length stays constant and no extra history is lost.

        With the bug, each cycle would silently shorten the *effective*
        history (provisional stuck, oldest dropped). With the fix, each
        rollback restores the deque cleanly."""
        eng = self._build_engine_at_maxlen(history_retain=30)
        original_history = list(eng.state_history())
        assert len(original_history) == 30

        prov_ts = original_history[-1].timestamp + timedelta(days=1)
        # Apply and rollback-replace 10 times — each cycle is a fresh
        # provisional at the same timestamp with a different close.
        for cycle in range(10):
            prov = OHLCVBar(
                timestamp=prov_ts,
                open=200.0, high=201.0, low=199.0,
                close=200.0 + cycle * 0.1, volume=1000.0,
            )
            eng.ingest_provisional(prov)

        states_after = eng.state_history()
        # Length unchanged
        assert len(states_after) == 30
        # The pre-provisional history (29 of the original 30 — minus the
        # oldest, which was correctly dropped by the deque maxlen on the
        # FIRST provisional append) must be intact.
        for i in range(29):
            assert states_after[i].timestamp == original_history[i + 1].timestamp, (
                f"At cycle 10, position {i} should be original[{i+1}] "
                f"({original_history[i+1].timestamp}), got {states_after[i].timestamp}"
            )
        # And the last entry is the latest provisional
        assert states_after[-1].timestamp == prov_ts
        assert states_after[-1].display_bg in (
            CoreRegime.NEUTRAL, CoreRegime.BULLISH, CoreRegime.BEARISH,
        )

    def test_rollback_below_maxlen_still_works(self):
        """Regression: the fix must not break the previously-working
        case where the deque isn't yet at maxlen."""
        eng = TickerEngine(
            _ticker(), Timeframe.DAILY, _params(),
            history_retain=500,  # well above what we'll fill
        )
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        closes = [100.0 + i * 0.5 for i in range(60)]   # warmup-ish
        eng.prime(_bars_at(start, closes))
        history_before = list(eng.state_history())

        # Ingest provisional
        prov_ts = history_before[-1].timestamp + timedelta(days=1)
        prov = OHLCVBar(
            timestamp=prov_ts, open=200.0, high=201.0, low=199.0,
            close=200.5, volume=1000.0,
        )
        eng.ingest_provisional(prov)
        assert len(eng.state_history()) == len(history_before) + 1

        # Same-ts re-ingest → rollback → re-apply
        prov_v2 = OHLCVBar(
            timestamp=prov_ts, open=200.0, high=205.0, low=198.0,
            close=203.0, volume=1500.0,
        )
        eng.ingest_provisional(prov_v2)
        history_after = list(eng.state_history())

        # Length is what it should be: original + 1 provisional
        assert len(history_after) == len(history_before) + 1
        # The pre-provisional history is intact bar-for-bar
        for i, expected_bar in enumerate(history_before):
            assert history_after[i].timestamp == expected_bar.timestamp
        # Latest is the new provisional
        assert history_after[-1].timestamp == prov_ts


# ============================================================
# is_warmed_up — convenience property on BarState
#
# Contract: True iff every indicator value (EMAs + ATR) is non-NaN.
# Locks in the contract that downstream consumers can use this rather
# than checking each field for NaN themselves.
# ============================================================


class TestIsWarmedUp:

    def test_first_bar_is_not_warmed_up(self):
        """A single bar fed to a fresh engine produces a BarState whose
        EMAs are still in their warmup phase (NaN). is_warmed_up must
        be False."""
        eng = TickerEngine(_ticker(), Timeframe.DAILY, _params())
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        state = eng.ingest(_bars_at(start, [100.0])[0])
        assert state.is_warmed_up is False

    def test_state_is_warmed_up_after_sufficient_history(self):
        """After priming with enough bars to clear every EMA + ATR
        warmup, is_warmed_up must be True."""
        eng = TickerEngine(_ticker(), Timeframe.DAILY, _params())
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        # 100 bars is well past the longest EMA's warmup (default longest=50)
        # and far past ATR's (default length=14)
        closes = [100.0 + i * 0.5 for i in range(100)]
        eng.prime(_bars_at(start, closes))
        state = eng.latest_state()
        assert state is not None
        assert state.is_warmed_up is True

    def test_partially_warmed_state_is_not_warmed_up(self):
        """Between bar 1 and the point where ALL components are non-NaN,
        is_warmed_up must remain False even if some components are
        already producing values."""
        eng = TickerEngine(_ticker(), Timeframe.DAILY, _params())
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        # 5 bars: ATR(14) still NaN, but volume SMA and short EMA may
        # have warmed up
        closes = [100.0 + i for i in range(5)]
        last_state = None
        for bar in _bars_at(start, closes):
            last_state = eng.ingest(bar)
        assert last_state is not None
        # ATR with length=14 needs 14 bars; 5 is well short
        import math as _m
        assert _m.isnan(last_state.atr)
        assert last_state.is_warmed_up is False

