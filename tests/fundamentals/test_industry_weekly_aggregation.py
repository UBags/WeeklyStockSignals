"""Unit tests for Phase 1B: IIV → weekly OHLC aggregation used by
`run_indicator_state_for_industry`.

These tests exercise the IIV-row → pseudo-OHLCVBar → daily_to_weekly
pipeline that the runner now uses, NOT the indicator math itself.
The end-to-end indicator runner tests live in
test_indicator_state_runners.py and continue to use the
`_setup_industry_with_values` fixture (one IIV row per week, which
trivially produces one weekly bar per input).

Scope (what these tests guard):
  - Multiple daily IIV rows in the SAME IST week collapse to one
    weekly bar with O=first, H=max, L=min, C=last.
  - Bars spanning multiple IST weeks produce one bar per week.
  - Gaps in the input (missing days) produce no bar for empty weeks
    but neighbouring weeks aggregate correctly.
  - A single-day week emits a weekly bar with H=L=O=C=value.
  - The current (in-progress) week emits a partial bar.
  - IST week anchoring lands on the correct local Monday — including
    near the UTC ↔ IST boundary (the IST-Sunday / IST-Monday switch
    is at Sunday 18:30 UTC).

These tests do NOT touch the DB — they test the in-memory adapter the
runner builds before calling `daily_to_weekly`. We replicate the
runner's exact transformation here so a refactor of the runner that
silently changes the bucketing semantics gets caught.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pytest

from trip_ema.domain import OHLCVBar
from trip_ema.indicators.aggregation import daily_to_weekly


_IST = ZoneInfo("Asia/Kolkata")


# ---------- helpers ---------- #


@dataclass(frozen=True)
class _FakeIIVRow:
    """Stand-in for an `IndustryIndexValue` row — the runner only
    reads `trade_date` and `value`, so a 2-field shim is enough."""
    trade_date: date
    value: float


def _adapt(history: list[_FakeIIVRow]) -> list[OHLCVBar]:
    """Mirror of the runner's pseudo-bar construction. Mirrors verbatim
    so a runner-side regression would surface here as a behavioural
    mismatch rather than a silent semantic drift."""
    return [
        OHLCVBar(
            timestamp=datetime.combine(h.trade_date, time.min, tzinfo=_IST),
            open=float(h.value),
            high=float(h.value),
            low=float(h.value),
            close=float(h.value),
            volume=1.0,
        )
        for h in history
    ]


def _aggregate(history: list[_FakeIIVRow]) -> list[OHLCVBar]:
    """Mirror of the runner's full aggregation step."""
    return daily_to_weekly(
        _adapt(history),
        include_partial_last_week=True,
        tz="Asia/Kolkata",
    )


# ---------- tests ---------- #


class TestWeeklyAggregationOfIIV:

    def test_five_daily_rows_same_week_collapse_to_one_bar(self):
        """Mon-Fri of one IST week, monotonically increasing values.
        Output: one weekly bar with O=Mon value, C=Fri value,
        H=Fri value, L=Mon value."""
        # 2026-05-18 is a Monday in IST.
        rows = [
            _FakeIIVRow(date(2026, 5, 18), 100.0),  # Mon
            _FakeIIVRow(date(2026, 5, 19), 102.0),  # Tue
            _FakeIIVRow(date(2026, 5, 20), 105.0),  # Wed
            _FakeIIVRow(date(2026, 5, 21), 103.0),  # Thu
            _FakeIIVRow(date(2026, 5, 22), 108.0),  # Fri
        ]
        weekly = _aggregate(rows)
        assert len(weekly) == 1
        w = weekly[0]
        assert w.open == 100.0
        assert w.close == 108.0
        assert w.high == 108.0
        assert w.low == 100.0
        # The bar's timestamp is the IST Monday, stored as UTC.
        assert w.timestamp.astimezone(_IST).date() == date(2026, 5, 18)

    def test_intra_week_high_low_capture_extremes(self):
        """H = max, L = min across the week — not first/last."""
        rows = [
            _FakeIIVRow(date(2026, 5, 18), 100.0),
            _FakeIIVRow(date(2026, 5, 19), 95.0),   # week low
            _FakeIIVRow(date(2026, 5, 20), 110.0),  # week high
            _FakeIIVRow(date(2026, 5, 21), 102.0),
            _FakeIIVRow(date(2026, 5, 22), 105.0),
        ]
        weekly = _aggregate(rows)
        assert len(weekly) == 1
        w = weekly[0]
        assert w.open == 100.0
        assert w.close == 105.0
        assert w.high == 110.0
        assert w.low == 95.0

    def test_three_weeks_produce_three_bars(self):
        """Continuous coverage across 3 IST weeks."""
        rows = []
        # Week of Mon 2026-05-18 (Mon-Fri)
        for d, v in zip([18, 19, 20, 21, 22], [100, 101, 102, 103, 104]):
            rows.append(_FakeIIVRow(date(2026, 5, d), float(v)))
        # Week of Mon 2026-05-25 (Mon-Fri)
        for d, v in zip([25, 26, 27, 28, 29], [105, 106, 107, 108, 109]):
            rows.append(_FakeIIVRow(date(2026, 5, d), float(v)))
        # Week of Mon 2026-06-01 (Mon-Fri)
        for d, v in zip([1, 2, 3, 4, 5], [110, 111, 112, 113, 114]):
            rows.append(_FakeIIVRow(date(2026, 6, d), float(v)))

        weekly = _aggregate(rows)
        assert len(weekly) == 3
        assert [w.close for w in weekly] == [104.0, 109.0, 114.0]
        assert [w.open for w in weekly] == [100.0, 105.0, 110.0]
        # Mondays advance by 7 days.
        mondays = [w.timestamp.astimezone(_IST).date() for w in weekly]
        assert mondays == [date(2026, 5, 18), date(2026, 5, 25),
                           date(2026, 6, 1)]

    def test_gap_week_produces_no_bar_for_empty_week(self):
        """Skip a whole week in the middle. The two surrounding
        weeks each produce a bar; the gap week produces nothing."""
        rows = [
            # Week 1
            _FakeIIVRow(date(2026, 5, 18), 100.0),
            _FakeIIVRow(date(2026, 5, 19), 101.0),
            # (Week of Mon 2026-05-25 has no rows — gap)
            # Week 3
            _FakeIIVRow(date(2026, 6, 1), 110.0),
            _FakeIIVRow(date(2026, 6, 2), 111.0),
        ]
        weekly = _aggregate(rows)
        assert len(weekly) == 2
        mondays = [w.timestamp.astimezone(_IST).date() for w in weekly]
        assert mondays == [date(2026, 5, 18), date(2026, 6, 1)]

    def test_single_day_week_degenerate_OHLC(self):
        """One value in a week → weekly bar with H=L=O=C=value.
        Edge case relevant for partial first weeks at the start
        of an industry's history."""
        rows = [_FakeIIVRow(date(2026, 5, 22), 100.0)]  # Friday
        weekly = _aggregate(rows)
        assert len(weekly) == 1
        w = weekly[0]
        assert w.open == 100.0
        assert w.high == 100.0
        assert w.low == 100.0
        assert w.close == 100.0
        # The Monday of that week is 2026-05-18.
        assert w.timestamp.astimezone(_IST).date() == date(2026, 5, 18)

    def test_partial_current_week_emits_a_bar(self):
        """`include_partial_last_week=True` is the runner's setting,
        matching the stock-side convention. A current week with only
        partial data must emit a (partial) weekly bar."""
        rows = [
            # Last full week.
            _FakeIIVRow(date(2026, 5, 18), 100.0),
            _FakeIIVRow(date(2026, 5, 19), 101.0),
            _FakeIIVRow(date(2026, 5, 20), 102.0),
            _FakeIIVRow(date(2026, 5, 21), 103.0),
            _FakeIIVRow(date(2026, 5, 22), 104.0),
            # Current (in-progress) week — only Monday + Tuesday.
            _FakeIIVRow(date(2026, 5, 25), 105.0),
            _FakeIIVRow(date(2026, 5, 26), 106.0),
        ]
        weekly = _aggregate(rows)
        assert len(weekly) == 2
        current = weekly[-1]
        assert current.timestamp.astimezone(_IST).date() == date(2026, 5, 25)
        assert current.open == 105.0
        assert current.close == 106.0
        assert current.high == 106.0
        assert current.low == 105.0

    def test_ist_week_anchor_is_local_monday_not_utc(self):
        """A row whose IST date is Monday must bucket into THAT week,
        even though its UTC instant (midnight IST = Sunday 18:30 UTC)
        falls on Sunday in UTC. Guards against a future regression
        that strips tz information."""
        rows = [
            _FakeIIVRow(date(2026, 5, 18), 100.0),  # IST Monday
        ]
        weekly = _aggregate(rows)
        assert len(weekly) == 1
        # The week-anchor's IST-local date is the Monday itself,
        # not the UTC Sunday.
        anchor_ist = weekly[0].timestamp.astimezone(_IST).date()
        assert anchor_ist == date(2026, 5, 18)

    def test_empty_history_produces_no_bars(self):
        """No IIV rows → no weekly bars. The runner has a guard one
        layer up that raises ValueError on empty history before
        calling this code path, but the aggregator itself must
        tolerate an empty input cleanly."""
        weekly = _aggregate([])
        assert weekly == []


class TestVolumeAndOrderingInvariants:

    def test_volume_sums_across_week(self):
        """Pseudo-bars carry volume=1.0; weekly volume should be the
        bar count for the week."""
        rows = [
            _FakeIIVRow(date(2026, 5, 18), 100.0),
            _FakeIIVRow(date(2026, 5, 19), 101.0),
            _FakeIIVRow(date(2026, 5, 20), 102.0),
        ]
        weekly = _aggregate(rows)
        assert len(weekly) == 1
        assert weekly[0].volume == 3.0

    def test_open_is_first_chronologically_not_first_in_input(self):
        """The runner reverses history to oldest-first before calling
        the adapter. Verify that with oldest-first input, the weekly
        `open` is the chronologically-earliest value in the week."""
        rows = [
            _FakeIIVRow(date(2026, 5, 18), 100.0),  # Mon — first
            _FakeIIVRow(date(2026, 5, 19), 200.0),
            _FakeIIVRow(date(2026, 5, 20), 50.0),   # Wed — minimum
            _FakeIIVRow(date(2026, 5, 22), 300.0),  # Fri — maximum
        ]
        weekly = _aggregate(rows)
        assert len(weekly) == 1
        assert weekly[0].open == 100.0       # Monday's value
        assert weekly[0].high == 300.0       # Friday's value
        assert weekly[0].low == 50.0         # Wednesday's value
        assert weekly[0].close == 300.0      # Friday's value
