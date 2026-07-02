"""Truth table for the single source of truth: DetermineSignal.

Covers the four actionable transitions, the two direct-reversal
tie-break rows (previously untested anywhere), the NO_CHANGE sink for
equal / out-of-range input (never raises), and the enum<->DB round trip.
"""
import pytest

from trip_ema.domain import DetermineSignal
from trip_ema.domain.state import ChangeSignal, CoreRegime


class TestDetermineSignalTruthTable:
    @pytest.mark.parametrize("before,after,expected", [
        # through-NEUTRAL transitions
        (0, 1, ChangeSignal.BUY),
        (0, -1, ChangeSignal.SELL),
        (1, 0, ChangeSignal.EXIT_FROM_BUY),
        (-1, 0, ChangeSignal.EXIT_FROM_SELL),
        # direct reversals -- the tie-break rows
        (-1, 1, ChangeSignal.BUY),    # BEARISH->BULLISH: BUY beats EXIT_FROM_SELL
        (1, -1, ChangeSignal.SELL),   # BULLISH->BEARISH: SELL beats EXIT_FROM_BUY
        # equal regimes -> NO_CHANGE
        (0, 0, ChangeSignal.NO_CHANGE),
        (1, 1, ChangeSignal.NO_CHANGE),
        (-1, -1, ChangeSignal.NO_CHANGE),
    ])
    def test_truth_table(self, before, after, expected):
        assert DetermineSignal.determine_signal(before, after) is expected

    def test_accepts_coreregime_members(self):
        assert DetermineSignal.determine_signal(
            CoreRegime.BEARISH, CoreRegime.BULLISH
        ) is ChangeSignal.BUY
        assert DetermineSignal.determine_signal(
            CoreRegime.BULLISH, CoreRegime.NEUTRAL
        ) is ChangeSignal.EXIT_FROM_BUY

    @pytest.mark.parametrize("before,after", [
        (2, 3), (5, 0), (0, 7), (-2, 2),
    ])
    def test_out_of_range_is_no_change_never_raises(self, before, after):
        # Uddi's rule: not one of the four -> NO_CHANGE. Period. No raise.
        assert DetermineSignal.determine_signal(before, after) is ChangeSignal.NO_CHANGE


class TestDetermineSignalDbConversion:
    @pytest.mark.parametrize("member,db", [
        (ChangeSignal.BUY, "BUY"),
        (ChangeSignal.SELL, "SELL"),
        (ChangeSignal.EXIT_FROM_BUY, "EXIT_FROM_BUY"),
        (ChangeSignal.EXIT_FROM_SELL, "EXIT_FROM_SELL"),
    ])
    def test_round_trip(self, member, db):
        assert DetermineSignal.to_db_str(member) == db
        assert DetermineSignal.from_db_str(db) is member

    def test_no_change_has_no_db_string(self):
        assert DetermineSignal.to_db_str(ChangeSignal.NO_CHANGE) is None

    def test_unknown_db_string_is_no_change(self):
        assert DetermineSignal.from_db_str("WOBBLE") is ChangeSignal.NO_CHANGE
