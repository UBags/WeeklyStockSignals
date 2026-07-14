"""Signal sequence numbering (spec §21). Pure — no DB, no I/O, no clock.

A weekly signal is generated off the FORMING weekly bar, which means it is
re-evaluated every day of the week. A BUY can print on Tuesday, retract on
Wednesday, print again on Friday. Whether the one you are holding is the
first, second or fourth attempt of the week is a fact about the signal, and
it is a fact you can know when it prints — so it is a feature.

Two counters, both 1-based, both per (ticker, timeframe, signal_type):

  seq_in_week   this signal's ordinal among signals of the SAME TYPE whose
                signal_date falls in the same Monday-anchored week, counting
                only those on or before this one.

  seq_in_2w     the same, over the two-week window [previous Monday, this
                signal's date].

CAUSALITY IS THE POINT. Both counters look BACKWARD only. A Tuesday BUY is
the 1st BUY of its week even if two more BUYs print on Thursday and Friday —
because on Tuesday, that is the truth, and the truth on Tuesday is the only
thing a Tuesday decision can be made from. Counting the whole week and then
labelling Tuesday's signal "1 of 3" would leak the future into the feature.
It would also be undetectable: the numbers look identical in a spreadsheet.
test_causality_a_later_signal_does_not_inflate_an_earlier_one is what stands
between this file and that.

PER TYPE, not per direction and not pooled. A BUY and a SELL in one week are
each the 1st of their kind. An EXIT_FROM_BUY has its own counter too — the
exits are what a retraction looks like, and counting them is how you tell a
clean run from a churn.

WEEKS ARE MONDAY-ANCHORED, by calendar arithmetic on the exchange-local date.
No timezone appears in this module and that is deliberate: signal_date is
ALREADY an exchange-local calendar date. Lifting it to a timestamp to find
its Monday would reintroduce the exact instant-vs-date confusion that caused
every timezone bug this codebase has had. `date - timedelta(days=weekday())`
is the same Monday `_monday_of` computes, without the round trip.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, timedelta

WEEK = timedelta(days=7)


@dataclass(frozen=True, slots=True)
class SignalSequence:
    """1-based, causal, per signal type."""

    in_week: int
    in_2w: int


def week_start(d: date) -> date:
    """The Monday of d's week. Monday returns itself."""
    return d - timedelta(days=d.weekday())


def sequence_numbers(
    signals_by_date: Mapping[date, Iterable[str]],
) -> dict[tuple[date, str], SignalSequence]:
    """{(signal_date, signal_type): SignalSequence} for one ticker+timeframe.

    Input is the same dict the enrichment walk already builds. Signal types
    are not validated here — an unknown type simply gets its own counter,
    because this module has no opinion about the signal vocabulary and
    should not acquire one.

    Duplicate types on one date are collapsed: the natural key on
    signal_enrichment is (ticker, timeframe, signal_date, signal_type), so
    two BUYs on one date are one BUY, and counting them twice would produce
    a sequence number that cannot be joined back to a row.
    """
    by_type: dict[str, list[date]] = {}
    for d, types in signals_by_date.items():
        for t in set(types):                    # collapse duplicates
            by_type.setdefault(t, []).append(d)

    out: dict[tuple[date, str], SignalSequence] = {}
    for t, dates in by_type.items():
        dates.sort()
        for i, d in enumerate(dates):
            wk = week_start(d)
            prev_wk = wk - WEEK
            # dates[:i + 1] is every signal of this type ON OR BEFORE d.
            # Everything after i is the future and is not consulted.
            history = dates[: i + 1]
            out[(d, t)] = SignalSequence(
                in_week=sum(1 for x in history if week_start(x) == wk),
                in_2w=sum(1 for x in history if week_start(x) >= prev_wk),
            )
    return out
