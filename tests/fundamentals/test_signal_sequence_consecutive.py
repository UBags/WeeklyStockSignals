"""seq_in_consec_weeks is a RUN LENGTH, and a gap resets it.

The distinction that matters: a fixed window asks "how many in the last N
weeks", a run length asks "how many weeks in a row". Two signals in one
fortnight is not two consecutive weeks of signals, and the old seq_in_2w
could not tell them apart -- which is why its buckets measured nothing.

Causality is preserved: weeks before this signal's own week lie entirely in
the past, and the current week counts only signals on or before this one.
"""
from __future__ import annotations

from datetime import date, timedelta

from trip_ema.fundamentals.signal_sequence import sequence_numbers, week_start

MON = date(2026, 1, 5)          # a Monday
WEEK = timedelta(days=7)


def _mon(n: int) -> date:
    return MON + n * WEEK


def _seq(*pairs):
    # Accumulate, do not overwrite: a dict comprehension keyed on the date
    # silently drops the BUY when a SELL shares that date, which is exactly
    # the per-type case these tests exist to check.
    by_date: dict = {}
    for d, t in pairs:
        by_date.setdefault(d, []).append(t)
    return sequence_numbers(by_date)


def test_a_lone_signal_has_a_run_of_one():
    got = _seq((_mon(0), "BUY"))
    assert got[(_mon(0), "BUY")].in_consec_weeks == 1


def test_two_signals_in_the_same_week_are_still_a_run_of_one():
    """The old two-week window scored this 2. One week is one week."""
    got = _seq((_mon(0), "BUY"), (_mon(0) + timedelta(days=3), "BUY"))
    assert got[(_mon(0), "BUY")].in_consec_weeks == 1
    assert got[(_mon(0) + timedelta(days=3), "BUY")].in_consec_weeks == 1
    assert got[(_mon(0) + timedelta(days=3), "BUY")].in_week == 2


def test_the_run_grows_over_contiguous_weeks():
    got = _seq(*[(_mon(i), "BUY") for i in range(5)])
    assert [got[(_mon(i), "BUY")].in_consec_weeks for i in range(5)] == \
        [1, 2, 3, 4, 5]


def test_a_missed_week_resets_the_run():
    got = _seq((_mon(0), "BUY"), (_mon(1), "BUY"), (_mon(3), "BUY"),
               (_mon(4), "BUY"))
    assert got[(_mon(1), "BUY")].in_consec_weeks == 2
    assert got[(_mon(3), "BUY")].in_consec_weeks == 1   # week 2 was empty
    assert got[(_mon(4), "BUY")].in_consec_weeks == 2


def test_the_run_never_returns_zero():
    """A gap resets to 1, not 0 -- the signal's own week always counts."""
    got = _seq((_mon(0), "BUY"), (_mon(9), "BUY"))
    assert got[(_mon(9), "BUY")].in_consec_weeks == 1


def test_runs_are_counted_per_signal_type():
    got = _seq((_mon(0), "BUY"), (_mon(1), "BUY"),
               (_mon(1), "SELL"), (_mon(2), "SELL"))
    assert got[(_mon(1), "BUY")].in_consec_weeks == 2
    assert got[(_mon(1), "SELL")].in_consec_weeks == 1
    assert got[(_mon(2), "SELL")].in_consec_weeks == 2


def test_a_later_signal_does_not_inflate_an_earlier_one():
    """Causality. The whole feature is worthless if it can see forward."""
    early = _seq((_mon(0), "BUY"))[(_mon(0), "BUY")].in_consec_weeks
    later = _seq((_mon(0), "BUY"), (_mon(1), "BUY"),
                 (_mon(2), "BUY"))[(_mon(0), "BUY")].in_consec_weeks
    assert early == later == 1


def test_a_mid_week_signal_counts_its_own_week():
    """Weeks are Monday-anchored; a Thursday signal is in Monday's week."""
    thu = _mon(1) + timedelta(days=3)
    got = _seq((_mon(0), "BUY"), (thu, "BUY"))
    assert week_start(thu) == _mon(1)
    assert got[(thu, "BUY")].in_consec_weeks == 2


def test_many_signals_within_each_week_do_not_inflate_the_run():
    pairs = []
    for i in range(3):
        for day in (0, 2, 4):
            pairs.append((_mon(i) + timedelta(days=day), "BUY"))
    got = _seq(*pairs)
    last = _mon(2) + timedelta(days=4)
    assert got[(last, "BUY")].in_consec_weeks == 3
    assert got[(last, "BUY")].in_week == 3
