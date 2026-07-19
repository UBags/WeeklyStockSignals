"""C3 — causal, per-type, Monday-anchored signal sequence numbers.

The arithmetic is trivial. The failure mode is not: a counter that peeks at
the rest of the week produces numbers that look right, join right, and are
wrong in exactly the way that makes a model look good in backtest and lose
money live. Most of this file exists to hold that line.
"""
from __future__ import annotations

from datetime import date

import pytest

from trip_ema.fundamentals.signal_sequence import (
    SignalSequence,
    sequence_numbers,
    week_start,
)

# 2026-07-13 is a Monday. The week runs Mon 13th -> Sun 19th.
MON = date(2026, 7, 13)
TUE = date(2026, 7, 14)
WED = date(2026, 7, 15)
THU = date(2026, 7, 16)
FRI = date(2026, 7, 17)
# The week before: Mon 6th -> Sun 12th.
PREV_MON = date(2026, 7, 6)
PREV_WED = date(2026, 7, 8)
PREV_FRI = date(2026, 7, 10)
# Two weeks before.
OLD_WED = date(2026, 7, 1)


class TestWeekStart:

    def test_monday_is_its_own_week_start(self):
        assert week_start(MON) == MON

    def test_every_day_of_a_week_maps_to_its_monday(self):
        for d in (MON, TUE, WED, THU, FRI, date(2026, 7, 19)):  # incl. Sunday
            assert week_start(d) == MON

    def test_saturday_and_sunday_belong_to_the_week_that_opened(self):
        """NSE does not trade them, but a date that lands there must not fall
        into next week — a weekend signal_date would silently reset the
        counter."""
        assert week_start(date(2026, 7, 18)) == MON     # Saturday
        assert week_start(date(2026, 7, 19)) == MON     # Sunday
        assert week_start(date(2026, 7, 20)) == date(2026, 7, 20)  # next Mon

    def test_a_week_spanning_a_year_boundary(self):
        """2026-12-31 is a Thursday; its Monday is 2026-12-28. ISO week
        numbering would have opinions here. Calendar arithmetic does not."""
        assert week_start(date(2026, 12, 31)) == date(2026, 12, 28)
        assert week_start(date(2027, 1, 1)) == date(2026, 12, 28)  # Friday


class TestCausality:
    """The reason this module exists as its own file."""

    def test_a_later_signal_does_not_inflate_an_earlier_one(self):
        """Three BUYs in one week. Tuesday's is the 1st — NOT '1 of 3'.
        If this ever reads 3, the feature has learnt the future."""
        seq = sequence_numbers({TUE: ["BUY"], THU: ["BUY"], FRI: ["BUY"]})
        assert seq[(TUE, "BUY")].in_week == 1
        assert seq[(THU, "BUY")].in_week == 2
        assert seq[(FRI, "BUY")].in_week == 3

    def test_counts_are_monotone_in_date(self):
        seq = sequence_numbers({d: ["BUY"] for d in (MON, TUE, WED, THU, FRI)})
        vals = [seq[(d, "BUY")].in_week for d in (MON, TUE, WED, THU, FRI)]
        assert vals == [1, 2, 3, 4, 5]

    def test_input_order_is_irrelevant(self):
        """The walk builds signals_by_date in whatever order it reads rows.
        A counter that depended on dict insertion order would be a bug that
        only shows up when the query plan changes."""
        a = sequence_numbers({FRI: ["BUY"], TUE: ["BUY"], THU: ["BUY"]})
        b = sequence_numbers({TUE: ["BUY"], THU: ["BUY"], FRI: ["BUY"]})
        assert a == b
        assert a[(TUE, "BUY")].in_week == 1


class TestPerType:

    def test_buy_and_sell_have_independent_counters(self):
        seq = sequence_numbers({TUE: ["BUY"], WED: ["SELL"], THU: ["BUY"]})
        assert seq[(TUE, "BUY")].in_week == 1
        assert seq[(WED, "SELL")].in_week == 1      # not 2
        assert seq[(THU, "BUY")].in_week == 2

    def test_the_churn_week(self):
        """The pattern the whole feature exists to measure: a BUY that fires,
        gets exited, and fires again inside one week."""
        seq = sequence_numbers({
            TUE: ["BUY"],
            WED: ["EXIT_FROM_BUY"],
            FRI: ["BUY"],
        })
        assert seq[(TUE, "BUY")].in_week == 1
        assert seq[(WED, "EXIT_FROM_BUY")].in_week == 1
        assert seq[(FRI, "BUY")].in_week == 2       # the second attempt

    def test_all_four_types_coexist_on_one_date(self):
        types = ["BUY", "SELL", "EXIT_FROM_BUY", "EXIT_FROM_SELL"]
        seq = sequence_numbers({WED: types})
        assert len(seq) == 4
        for t in types:
            assert seq[(WED, t)] == SignalSequence(in_week=1, in_consec_weeks=1)

    def test_a_duplicate_type_on_one_date_is_one_signal(self):
        """(ticker, tf, signal_date, signal_type) is the natural key. Two
        BUYs on one date are one row, so they must be one count — otherwise
        the sequence number cannot be joined back to anything."""
        seq = sequence_numbers({TUE: ["BUY", "BUY"], THU: ["BUY"]})
        assert seq[(TUE, "BUY")].in_week == 1
        assert seq[(THU, "BUY")].in_week == 2


class TestConsecutiveWeeks:
    """Run length over contiguous weeks, not a fixed window.

    Week anchors used below: OLD_WED is in the week of 2026-06-29, PREV_* in
    2026-07-06, and MON/TUE/WED/THU/FRI in 2026-07-13 — three CONTIGUOUS
    weeks, which is what makes them useful for testing a run.
    """

    def test_the_week_counter_resets_but_the_run_carries(self):
        seq = sequence_numbers({PREV_WED: ["BUY"], TUE: ["BUY"]})
        assert seq[(PREV_WED, "BUY")] == SignalSequence(in_week=1, in_consec_weeks=1)
        assert seq[(TUE, "BUY")] == SignalSequence(in_week=1, in_consec_weeks=2)

    def test_three_contiguous_weeks_score_three(self):
        """The old two-week window capped this at 2 and dropped OLD_WED. A
        run length has no cap: it grows for as long as the weeks are
        unbroken, which is the whole point of replacing the window."""
        seq = sequence_numbers({OLD_WED: ["BUY"], PREV_WED: ["BUY"],
                                TUE: ["BUY"]})
        assert seq[(OLD_WED, "BUY")] == SignalSequence(
            in_week=1, in_consec_weeks=1)
        assert seq[(PREV_WED, "BUY")] == SignalSequence(
            in_week=1, in_consec_weeks=2)
        assert seq[(TUE, "BUY")] == SignalSequence(
            in_week=1, in_consec_weeks=3)

    def test_a_full_churn_across_both_weeks(self):
        seq = sequence_numbers({
            PREV_WED: ["BUY"], PREV_FRI: ["BUY"],
            MON: ["BUY"], WED: ["BUY"], FRI: ["BUY"],
        })
        # in_week climbs within each week; the run counts WEEKS, so it is
        # 1 across all of the first week and 2 across all of the second. The
        # old window conflated the two by counting signals, which is why its
        # buckets could not separate anything.
        assert seq[(PREV_WED, "BUY")] == SignalSequence(1, 1)
        assert seq[(PREV_FRI, "BUY")] == SignalSequence(2, 1)
        assert seq[(MON, "BUY")] == SignalSequence(1, 2)
        assert seq[(WED, "BUY")] == SignalSequence(2, 2)
        assert seq[(FRI, "BUY")] == SignalSequence(3, 2)

    def test_a_gap_week_resets_the_run(self):
        """Signal in week N, nothing in N+1, signal in N+2. The run restarts
        at 1 — never 0, because the signal's own week always counts."""
        seq = sequence_numbers({OLD_WED: ["BUY"], TUE: ["BUY"]})
        assert seq[(TUE, "BUY")] == SignalSequence(in_week=1, in_consec_weeks=1)

    def test_both_counters_are_at_least_one_and_independent(self):
        """in_week <= in_consec_weeks held for a two-week WINDOW and is false
        for a run length: two signals in one isolated week give in_week=2 and
        a run of 1. The counters measure different things — signals within a
        week, and weeks in a row — so neither bounds the other. What does
        hold is that both are 1-based."""
        seq = sequence_numbers({
            PREV_MON: ["BUY"], PREV_FRI: ["BUY", "SELL"],
            MON: ["BUY"], TUE: ["SELL"], FRI: ["BUY", "EXIT_FROM_BUY"],
        })
        for s in seq.values():
            assert s.in_week >= 1
            assert s.in_consec_weeks >= 1
        # And the case that breaks the old invariant, stated outright.
        churn = sequence_numbers({MON: ["BUY"], FRI: ["BUY"]})
        assert churn[(FRI, "BUY")] == SignalSequence(
            in_week=2, in_consec_weeks=1)


class TestEdges:

    def test_empty_input(self):
        assert sequence_numbers({}) == {}

    def test_a_single_signal_is_one_of_one(self):
        seq = sequence_numbers({WED: ["SELL"]})
        assert seq[(WED, "SELL")] == SignalSequence(in_week=1, in_consec_weeks=1)

    def test_every_input_signal_gets_exactly_one_entry(self):
        """The walk indexes this dict by (date, type) for every row it is
        about to write. A missing key is a KeyError mid-backfill; an extra
        key is a row that does not exist."""
        signals = {
            PREV_WED: ["BUY", "SELL"],
            TUE: ["EXIT_FROM_BUY"],
            FRI: ["BUY", "EXIT_FROM_SELL", "SELL"],
        }
        seq = sequence_numbers(signals)
        expected = {(d, t) for d, ts in signals.items() for t in ts}
        assert set(seq) == expected

    def test_an_unknown_signal_type_gets_its_own_counter(self):
        """This module has no opinion on the signal vocabulary and must not
        acquire one — signal_bands.polarity_for is where a bad type dies."""
        seq = sequence_numbers({TUE: ["HOLD"], THU: ["HOLD"]})
        assert seq[(THU, "HOLD")].in_week == 2

    def test_a_year_boundary_week_is_one_week(self):
        thu = date(2026, 12, 31)
        fri = date(2027, 1, 1)
        seq = sequence_numbers({thu: ["BUY"], fri: ["BUY"]})
        # Both fall in the week beginning Mon 2026-12-28, so the run is 1 for
        # both — as the test's own name says. Only in_week advances.
        assert seq[(thu, "BUY")] == SignalSequence(1, 1)
        assert seq[(fri, "BUY")] == SignalSequence(2, 1)


def test_sequence_is_frozen():
    """It lands in a DB column. Nothing downstream may adjust it in place."""
    s = SignalSequence(in_week=1, in_consec_weeks=2)
    with pytest.raises(Exception):
        s.in_week = 9
