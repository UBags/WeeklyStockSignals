"""D7: the listing-date guard on _coverage_shortfall_days, locked in.

This behaviour already exists (a genuinely-late listing like ELCID, whose .NS
history correctly begins on its 2026 listing date, must NOT be flagged as
truncated). This test is a regression guard so a future edit can't quietly
drop the listing_date check and start re-chasing correctly-short histories.
"""

from __future__ import annotations

from datetime import date

from trip_ema.etl.runner import _TickerInfo, _coverage_shortfall_days
from trip_ema.etl.types import Tier


class _Bar:
    def __init__(self, d):
        self.trading_date = d


def _ticker(listing_date):
    return _TickerInfo(
        ticker_id=1, symbol="X", tier=Tier.TIER_1, listing_date=listing_date,
    )


class TestCoverageShortfallListingDate:

    def test_late_listing_is_not_flagged(self):
        """ELCID case: requested 2006, but the stock listed 2026-04-20 and its
        history correctly starts there. expected_first = max(req, listing) =
        the listing date, which the bars satisfy -> 0 shortfall."""
        t = _ticker(date(2026, 4, 20))
        bars = [_Bar(date(2026, 4, 20)), _Bar(date(2026, 4, 21))]
        assert _coverage_shortfall_days(t, bars, date(2006, 4, 1)) == 0

    def test_genuine_truncation_is_flagged(self):
        """A ticker listed long ago whose first returned bar is far later than
        requested IS truncated -> positive shortfall."""
        t = _ticker(date(1999, 1, 1))
        bars = [_Bar(date(2026, 7, 1))]
        assert _coverage_shortfall_days(t, bars, date(2006, 4, 1)) > 0

    def test_null_listing_date_never_false_alarms(self):
        """No listing_date -> no ground truth -> never a false alarm (0)."""
        t = _ticker(None)
        bars = [_Bar(date(2026, 7, 1))]
        assert _coverage_shortfall_days(t, bars, date(2006, 4, 1)) == 0

    def test_no_bars_is_zero(self):
        t = _ticker(date(1999, 1, 1))
        assert _coverage_shortfall_days(t, [], date(2006, 4, 1)) == 0
