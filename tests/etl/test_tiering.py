"""Tests for the tier-assignment algorithm. Pure function; no DB."""

from __future__ import annotations

from trip_ema.etl.tiering import TickerAdv, TierAssignment, assign_tiers
from trip_ema.etl.types import Tier


def _adv(tid, value):
    return TickerAdv(ticker_id=tid, avg_daily_value=value)


class TestTierAssignment:

    def test_top_n_by_value_become_tier_1(self):
        # 10 tickers; tier_1_size=3 → top 3 by ADV are Tier 1
        result = assign_tiers(
            [_adv(i, value=i * 1000) for i in range(1, 11)],
            tier_1_size=3,
            tier_2_size=4,
        )
        # Highest ADV is ticker 10 (10000), then 9, then 8 — those are T1
        by_id = {a.ticker_id: a.tier for a in result}
        assert by_id[10] == Tier.TIER_1
        assert by_id[9] == Tier.TIER_1
        assert by_id[8] == Tier.TIER_1
        # Next 4 are T2
        for tid in [7, 6, 5, 4]:
            assert by_id[tid] == Tier.TIER_2
        # The rest T3
        for tid in [3, 2, 1]:
            assert by_id[tid] == Tier.TIER_3

    def test_zero_adv_always_tier_3(self):
        """A ticker with zero history (new listing) is always Tier 3,
        even if there are fewer than tier_1_size tickers in the
        universe."""
        result = assign_tiers(
            [_adv(1, 0), _adv(2, 0), _adv(3, 0)],
            tier_1_size=5,
            tier_2_size=10,
        )
        for a in result:
            assert a.tier == Tier.TIER_3

    def test_ties_break_by_ticker_id_ascending(self):
        """Two tickers with the same ADV: lower ticker_id ranks higher."""
        result = assign_tiers(
            [_adv(99, 1000.0), _adv(1, 1000.0)],
            tier_1_size=1,
            tier_2_size=1,
        )
        by_id = {a.ticker_id: a.tier for a in result}
        # ticker_id=1 wins the tie → Tier 1; ticker_id=99 → Tier 2
        assert by_id[1] == Tier.TIER_1
        assert by_id[99] == Tier.TIER_2

    def test_empty_input_returns_empty(self):
        assert assign_tiers([]) == []

    def test_smaller_universe_than_t1_size(self):
        """If we have 50 tickers and tier_1_size=200, all liquid ones
        are Tier 1; no Tier 2/3 (except zero-ADV)."""
        result = assign_tiers(
            [_adv(i, value=100.0) for i in range(1, 51)],
            tier_1_size=200,
            tier_2_size=500,
        )
        assert all(a.tier == Tier.TIER_1 for a in result)

    def test_default_sizes(self):
        """Defaults are 200/500. With a 1000-ticker universe, expect
        200 T1, 500 T2, 300 T3."""
        result = assign_tiers(
            [_adv(i, value=(1001 - i) * 100.0) for i in range(1, 1001)],
        )
        counts = {Tier.TIER_1: 0, Tier.TIER_2: 0, Tier.TIER_3: 0}
        for a in result:
            counts[a.tier] += 1
        assert counts[Tier.TIER_1] == 200
        assert counts[Tier.TIER_2] == 500
        assert counts[Tier.TIER_3] == 300
