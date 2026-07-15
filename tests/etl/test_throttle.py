"""Tests for the yfinance throttle gate.

This is the open item the port work was really for: Yahoo's rate-limit was
undetectable on the recovery path, because that path re-fetches ONE ticker at a
time and the old rule needed two symbols in a frame to call it a throttle. The
gate moves the decision off the batch and onto the pass, so three consecutive
empty solo fetches say what one N-symbol empty frame used to say.

Every test here drives an injected clock and sleeper. Nothing waits.
"""

from __future__ import annotations

from trip_ema.etl.throttle import ThrottleConfig, ThrottleGate, looks_rate_limited

from .fakes import SleepingClock


def _gate(sc: SleepingClock, **cfg) -> ThrottleGate:
    """A gate whose backoff is deterministic: jitter pinned to 0.5, which makes
    the multiplier (0.5 + 0.5) == 1.0, so the delay IS base * 2**(level-1)."""
    return ThrottleGate(
        ThrottleConfig(**cfg),
        sleep=sc.sleep,
        clock=sc.clock,
        jitter=lambda: 0.5,
    )


# ---------- rate-limit sniffing ---------- #


class TestLooksRateLimited:

    def test_429_in_message(self):
        assert looks_rate_limited(Exception("HTTP Error 429: Too Many Requests"))

    def test_rate_limit_wording(self):
        assert looks_rate_limited(Exception("YFRateLimitError: rate limited"))

    def test_too_many_requests_wording(self):
        assert looks_rate_limited(RuntimeError("Too Many Requests"))

    def test_ordinary_error_is_not_a_rate_limit(self):
        assert not looks_rate_limited(RuntimeError("connection reset by peer"))

    def test_the_word_rate_alone_is_not_enough(self):
        """'rate' shows up in plenty of innocent messages. Both halves or nothing."""
        assert not looks_rate_limited(RuntimeError("exchange rate unavailable"))


# ---------- the batched rule (unchanged behaviour) ---------- #


class TestUnanimousBatch:

    def test_all_empty_multi_symbol_frame_trips_immediately(self):
        """One equity with zero bars over a multi-year window is plausible. All
        fifty at once is not a mass delisting."""
        sc = SleepingClock()
        g = _gate(sc, base_seconds=30.0)
        assert g.observe_empty(50) is True
        assert g.snapshot()["backoff_level"] == 1

    def test_data_resets_everything(self):
        sc = SleepingClock()
        g = _gate(sc, base_seconds=30.0)
        g.observe_empty(50)
        g.observe_data()
        assert g.snapshot() == {
            "backoff_level": 0, "solo_empty_streak": 0, "backoff_open": False,
        }


# ---------- the solo rule (the fix) ---------- #


class TestSoloStreak:

    def test_one_empty_solo_fetch_is_not_a_throttle(self):
        """A lone empty frame is genuinely ambiguous — delisted, never traded,
        rights entitlement. Do not back off on it."""
        sc = SleepingClock()
        g = _gate(sc, solo_empty_streak_to_trip=3)
        assert g.observe_empty(1) is False
        assert g.snapshot()["backoff_open"] is False

    def test_two_are_not_enough(self):
        sc = SleepingClock()
        g = _gate(sc, solo_empty_streak_to_trip=3)
        assert g.observe_empty(1) is False
        assert g.observe_empty(1) is False
        assert g.snapshot()["backoff_level"] == 0

    def test_three_in_a_row_trips(self):
        """The whole point. No single solo fetch can see this; the pass can."""
        sc = SleepingClock()
        g = _gate(sc, solo_empty_streak_to_trip=3)
        assert [g.observe_empty(1) for _ in range(3)] == [False, False, True]
        assert g.snapshot()["backoff_level"] == 1

    def test_data_in_between_breaks_the_streak(self):
        """Two delisted tickers, then a live one, then two more delisted, is not
        a throttle — and must not be treated as one."""
        sc = SleepingClock()
        g = _gate(sc, solo_empty_streak_to_trip=3)
        g.observe_empty(1)
        g.observe_empty(1)
        g.observe_data()
        assert g.observe_empty(1) is False
        assert g.observe_empty(1) is False
        assert g.snapshot()["backoff_level"] == 0


# ---------- backoff schedule ---------- #


class TestBackoffSchedule:

    def test_wait_sleeps_out_the_window(self):
        sc = SleepingClock()
        g = _gate(sc, base_seconds=30.0)
        g.observe_empty(2)
        assert g.wait() == 30.0          # jitter pinned: base * 2**0 * 1.0
        assert sc.slept == [30.0]

    def test_backoff_is_exponential_across_trips(self):
        sc = SleepingClock()
        g = _gate(sc, base_seconds=30.0, max_backoffs=4)
        for _ in range(3):
            assert g.observe_empty(2) is True
            g.wait()
        assert sc.slept == [30.0, 60.0, 120.0]

    def test_backoff_is_capped(self):
        sc = SleepingClock()
        g = _gate(sc, base_seconds=30.0, max_seconds=45.0, max_backoffs=4)
        for _ in range(3):
            g.observe_empty(2)
            g.wait()
        assert sc.slept == [30.0, 45.0, 45.0]

    def test_gives_up_after_max_backoffs(self):
        """An unbounded backoff is an unbounded hang. Once we have backed off
        max_backoffs times with no data, let the empty outcomes stand and let
        the runner's own bookkeeping take it from there."""
        sc = SleepingClock()
        g = _gate(sc, base_seconds=1.0, max_backoffs=2)
        assert g.observe_empty(2) is True
        g.wait()
        assert g.observe_empty(2) is True
        g.wait()
        assert g.observe_empty(2) is False   # spent
        assert len(sc.slept) == 2

    def test_wait_is_a_noop_when_the_gate_is_closed(self):
        sc = SleepingClock()
        g = _gate(sc)
        assert g.wait() == 0.0
        assert sc.slept == []

    def test_wait_does_not_sleep_twice_for_one_window(self):
        """The window is consumed. A second waiter — the next fetch in the pass
        — must not serve the same sentence again."""
        sc = SleepingClock()
        g = _gate(sc, base_seconds=30.0)
        g.observe_empty(2)
        assert g.wait() == 30.0
        assert g.wait() == 0.0
        assert sc.slept == [30.0]

    def test_jitter_desynchronises_parallel_workers(self):
        """Same base, different jitter, different delay — otherwise every worker
        comes back through the door at the same instant."""
        delays = []
        for j in (0.0, 1.0):
            sc = SleepingClock()
            g = ThrottleGate(
                ThrottleConfig(base_seconds=30.0),
                sleep=sc.sleep, clock=sc.clock, jitter=lambda j=j: j,
            )
            g.observe_empty(2)
            g.wait()
            delays.append(sc.slept[0])
        assert delays == [15.0, 45.0]        # base * (0.5 + j)


# ---------- explicit 429 ---------- #


class TestRateLimitError:

    def test_429_trips_the_long_backoff(self):
        """A 429 is not a flake. The exception path's 2s/4s retry is another
        swing at the same closed door."""
        sc = SleepingClock()
        g = _gate(sc, base_seconds=30.0)
        assert g.observe_rate_limit_error(Exception("429")) is True
        assert g.wait() == 30.0

    def test_429_clears_the_solo_streak(self):
        """We have hard evidence now; the circumstantial kind is redundant."""
        sc = SleepingClock()
        g = _gate(sc, solo_empty_streak_to_trip=3)
        g.observe_empty(1)
        g.observe_empty(1)
        g.observe_rate_limit_error(Exception("429"))
        assert g.snapshot()["solo_empty_streak"] == 0


# ---- D2: trip_on_empty flag ----


def test_observe_empty_returns_false_when_trip_on_empty_disabled():
    """D2: retry cohort / reference-sweep gates disable empty-tripping.
    observe_empty must be a no-op regardless of n_symbols, and the gate
    must remain closed."""
    from trip_ema.etl.throttle import ThrottleConfig, ThrottleGate

    gate = ThrottleGate(ThrottleConfig(trip_on_empty=False))
    assert gate.observe_empty(1) is False
    assert gate.observe_empty(2) is False   # would trip under default
    assert gate.observe_empty(50) is False  # would trip under default
    snap = gate.snapshot()
    assert snap["backoff_open"] is False
    assert snap["backoff_level"] == 0


def test_observe_rate_limit_still_trips_when_trip_on_empty_disabled():
    """D2: even in retry-cohort mode, an explicit 429 must trip the gate.
    trip_on_empty gates only the shape-based inference, not the explicit
    rate-limit signal."""
    from trip_ema.etl.throttle import ThrottleConfig, ThrottleGate

    gate = ThrottleGate(
        ThrottleConfig(trip_on_empty=False, base_seconds=1.0),
        sleep=lambda _s: None,
    )
    tripped = gate.observe_rate_limit_error(RuntimeError("HTTP 429"))
    assert tripped is True
    assert gate.snapshot()["backoff_open"] is True


def test_trip_on_empty_defaults_true_preserves_bulk_behaviour():
    """D2: the default is True — the bulk pass still trips on unanimity,
    exactly as it did before D2. This is the regression guard against
    accidentally flipping the default."""
    from trip_ema.etl.throttle import ThrottleConfig, ThrottleGate

    assert ThrottleConfig().trip_on_empty is True

    gate = ThrottleGate(
        ThrottleConfig(base_seconds=1.0), sleep=lambda _s: None,
    )
    assert gate.observe_empty(2) is True
