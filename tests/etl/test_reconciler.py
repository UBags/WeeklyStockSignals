"""Tests for the reconciler.

Pure logic; no DB, no time. Each test pins `today` and feeds explicit
new/stored bar lists.
"""

from __future__ import annotations

from datetime import date

import pytest

from trip_ema.etl.reconciler import (
    DivergenceFlag,
    ReconcilePlan,
    _StoredBar,
    reconcile,
)
from trip_ema.etl.types import RawBar


def _new(d, *, o=100, h=101, l=99, c=100.5, ac=100.5, v=1000):
    return RawBar(
        trading_date=d, open=o, high=h, low=l, close=c, adj_close=ac, volume=v,
    )


def _stored(d, *, o=100, h=101, l=99, c=100.5, ac=100.5, v=1000):
    return _StoredBar(
        trading_date=d, open=o, high=h, low=l, close=c, adj_close=ac, volume=v,
    )


TODAY = date(2026, 5, 17)


class TestBrandNewBars:

    def test_new_bar_with_no_stored_history_is_written(self):
        plan = reconcile(
            [_new(date(2026, 5, 10))],
            [],
            today=TODAY,
        )
        assert len(plan.writes) == 1
        assert plan.writes[0].is_overwrite is False
        assert plan.divergences == ()
        assert plan.skipped_count == 0

    def test_new_bar_for_date_not_in_stored_set_is_written(self):
        plan = reconcile(
            [_new(date(2026, 5, 14)), _new(date(2026, 5, 15))],
            [_stored(date(2026, 5, 14))],
            today=TODAY,
        )
        # 5/14 already exists and matches → skipped. 5/15 is new → written.
        assert len(plan.writes) == 1
        assert plan.writes[0].trading_date == date(2026, 5, 15)
        assert plan.skipped_count == 1


class TestRollingWindowOverwrite:

    def test_within_window_diverging_value_is_overwritten(self):
        plan = reconcile(
            [_new(date(2026, 5, 15), c=110.0)],
            [_stored(date(2026, 5, 15), c=100.0)],
            today=TODAY,  # cutoff = 2026-05-10; 5/15 is inside window
            rolling_window_days=7,
        )
        assert len(plan.writes) == 1
        assert plan.writes[0].is_overwrite is True
        assert plan.writes[0].close == 110.0
        assert plan.divergences == ()

    def test_at_window_boundary_is_inside_window(self):
        """A bar exactly at the cutoff date is INSIDE the window (we
        use >= for inclusivity)."""
        cutoff = TODAY  # rolling_window_days=0 means cutoff = today
        plan = reconcile(
            [_new(TODAY, c=110.0)],
            [_stored(TODAY, c=100.0)],
            today=TODAY,
            rolling_window_days=0,
        )
        assert len(plan.writes) == 1
        assert plan.writes[0].is_overwrite is True


class TestOldDivergence:

    def test_old_diverging_bar_is_NOT_overwritten_and_flagged(self):
        plan = reconcile(
            [_new(date(2024, 1, 15), c=200.0)],
            [_stored(date(2024, 1, 15), c=100.0)],
            today=TODAY,
            rolling_window_days=7,
        )
        # Old bar, different value → no write, one divergence
        assert plan.writes == ()
        assert len(plan.divergences) == 1
        d = plan.divergences[0]
        assert d.trading_date == date(2024, 1, 15)
        assert d.stored_close == 100.0
        assert d.new_close == 200.0
        # delta_pct = (200-100)/100 = 1.0
        assert abs(d.delta_pct - 1.0) < 1e-9

    def test_old_matching_bar_is_skipped(self):
        plan = reconcile(
            [_new(date(2024, 1, 15), c=100.0)],
            [_stored(date(2024, 1, 15), c=100.0)],
            today=TODAY,
        )
        assert plan.writes == ()
        assert plan.divergences == ()
        assert plan.skipped_count == 1


class TestTolerance:

    def test_subtolerance_close_difference_is_skipped(self):
        """A 0.005 close difference is within 1 cent abs_tol; the bar
        should be skipped, not flagged."""
        plan = reconcile(
            [_new(date(2024, 1, 15), c=100.005)],
            [_stored(date(2024, 1, 15), c=100.000)],
            today=TODAY,
        )
        assert plan.divergences == ()
        assert plan.skipped_count == 1

    def test_subtolerance_volume_difference_skipped(self):
        """Volume tolerance is wider (1.0 abs / 0.1% rel) since yf
        rounds. 1000 vs 1000.5 should match."""
        plan = reconcile(
            [_new(date(2024, 1, 15), v=1000.5)],
            [_stored(date(2024, 1, 15), v=1000.0)],
            today=TODAY,
        )
        assert plan.skipped_count == 1

    def test_relative_tolerance_for_large_values(self):
        """For a ₹50000 stock, 0.01% is ₹5. A 0.005% diff (₹2.5)
        should match."""
        plan = reconcile(
            [_new(date(2024, 1, 15), c=50002.5)],
            [_stored(date(2024, 1, 15), c=50000.0)],
            today=TODAY,
        )
        # diff=2.5 > abs_tol of 0.01, but rel_diff = 2.5/50002.5 = 0.00005
        # < REL_TOL of 0.0001 → equivalent.
        assert plan.skipped_count == 1


class TestMixedBatch:

    def test_mixed_batch_partitions_correctly(self):
        """A realistic batch: some new bars, some skipped matches, one
        in-window overwrite, one old divergence."""
        plan = reconcile(
            [
                _new(date(2026, 5, 16), c=110.0),  # new (no stored)
                _new(date(2026, 5, 15), c=109.0),  # matches stored — skip
                _new(date(2026, 5, 14), c=108.0),  # in-window overwrite
                _new(date(2024, 1, 15), c=200.0),  # old divergence — flag
            ],
            [
                _stored(date(2026, 5, 15), c=109.0),
                _stored(date(2026, 5, 14), c=100.0),
                _stored(date(2024, 1, 15), c=100.0),
            ],
            today=TODAY,
            rolling_window_days=7,
        )
        # 5/16 = new write, 5/14 = overwrite write
        assert len(plan.writes) == 2
        # 5/15 = skipped
        assert plan.skipped_count == 1
        # 2024-01-15 = divergence
        assert len(plan.divergences) == 1
        assert plan.divergences[0].trading_date == date(2024, 1, 15)

        # Verify the is_overwrite flag is correct per-write
        by_date = {w.trading_date: w for w in plan.writes}
        assert by_date[date(2026, 5, 16)].is_overwrite is False
        assert by_date[date(2026, 5, 14)].is_overwrite is True
