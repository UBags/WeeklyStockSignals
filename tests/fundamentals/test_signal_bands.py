"""C1 — the band geometry, on its own, with no walk and no DB.

Everything here is arithmetic you can check by hand, which is the point: if
the slabs are wrong, every downstream study of "is the second signal safer
than the first" is wrong in a way no amount of model tuning will reveal.
"""
from __future__ import annotations

import math

import pytest

from trip_ema.fundamentals.signal_bands import (
    ATR_PERIODS,
    BAND_FRACTIONS,
    EMAS,
    N_SLABS,
    POLARITY_DOWN,
    POLARITY_UP,
    band_flags,
    band_key,
    band_keys,
    dist_key,
    polarity_for,
)

ATRS = {10: 5.0, 15: 8.0, 20: 10.0, 30: 20.0, 40: 40.0}


def _flags(price, polarity=POLARITY_UP, *, med=100.0, long=100.0,
           longest=100.0, atrs=None):
    return band_flags(
        price=price, med_ema=med, long_ema=long, longest_ema=longest,
        atr_by_period=ATRS if atrs is None else atrs, polarity=polarity,
    )


def _slab(out, ema, n):
    """Which slab is true for (ema, period), or None if none is."""
    on = [k for k in range(1, N_SLABS + 1) if out[band_key(ema, n, k)]]
    assert len(on) <= 1, f"{ema}/atr{n}: {len(on)} slabs true, must be <= 1"
    return on[0] if on else None


# ---------- shape ----------

class TestShape:

    def test_emits_exactly_the_declared_keys(self):
        out = _flags(105.0)
        assert set(out) == set(band_keys())

    def test_75_booleans_and_15_distances(self):
        out = _flags(105.0)
        bools = [k for k, v in out.items() if isinstance(v, bool)]
        floats = [k for k, v in out.items() if isinstance(v, float)]
        assert len(bools) == 75
        assert len(floats) == 15
        # 90 of the 99 new columns. The other 9: atr10/15/20/30/40,
        # seq_in_week, seq_in_2w, signal_price, band_polarity.
        assert len(out) == 90
        assert len(band_keys()) == 90

    def test_key_names_are_stable(self):
        assert band_key("med", 10, 1) == "band_med_atr10_1"
        assert band_key("longest", 40, 5) == "band_longest_atr40_5"
        assert dist_key("long", 15) == "dist_long_atr15"

    def test_no_duplicate_keys(self):
        keys = band_keys()
        assert len(keys) == len(set(keys))


# ---------- polarity ----------

class TestPolarity:

    def test_exits_invert(self):
        assert polarity_for("BUY") == POLARITY_UP
        assert polarity_for("EXIT_FROM_SELL") == POLARITY_UP
        assert polarity_for("SELL") == POLARITY_DOWN
        assert polarity_for("EXIT_FROM_BUY") == POLARITY_DOWN

    def test_unknown_signal_type_raises(self):
        with pytest.raises(ValueError):
            polarity_for("HOLD")

    def test_bad_polarity_raises(self):
        with pytest.raises(ValueError):
            _flags(105.0, polarity="SIDEWAYS")

    def test_same_bar_scores_differently_by_polarity(self):
        """The whole reason feature dicts cannot be shared across the signal
        types that fired on one day."""
        up = _flags(105.0, POLARITY_UP)
        down = _flags(105.0, POLARITY_DOWN)
        assert _slab(up, "med", 20) == 3         # +0.50 ATR above med
        assert _slab(down, "med", 20) is None    # wrong side for a SELL
        assert up[dist_key("med", 20)] == 0.5
        assert down[dist_key("med", 20)] == -0.5


# ---------- BUY geometry (atr20 = 10 -> cuts at 102.5/105/107.5/110) ----------

class TestBuySlabs:

    @pytest.mark.parametrize("price,slab", [
        (100.0, 1),      # exactly on the EMA -> first slab (lower-inclusive)
        (101.0, 1),
        (102.4999, 1),
        (102.5, 2),      # exactly on a cut -> the HIGHER slab
        (104.9, 2),
        (105.0, 3),
        (107.4, 3),
        (107.5, 4),
        (109.9, 4),
        (110.0, 5),      # exactly on E4 -> the open slab, not slab 4
        (150.0, 5),
    ])
    def test_slab_boundaries(self, price, slab):
        assert _slab(_flags(price), "med", 20) == slab

    def test_below_the_ema_is_all_false(self):
        out = _flags(99.99)
        assert _slab(out, "med", 20) is None
        assert out[dist_key("med", 20)] < 0.0

    def test_exactly_one_slab_true_on_the_signal_side(self):
        for price in (100.0, 103.3, 106.6, 108.8, 999.0):
            out = _flags(price)
            for ema in EMAS:
                for n in ATR_PERIODS:
                    assert _slab(out, ema, n) is not None


# ---------- SELL geometry: the mirror, incl. the flipped last slab ----------

class TestSellSlabs:

    @pytest.mark.parametrize("price,slab", [
        (100.0, 1),
        (97.6, 1),
        (97.5, 2),       # E - 0.25·ATR
        (95.1, 2),
        (95.0, 3),
        (92.5, 4),
        (90.1, 4),
        (90.0, 5),       # "less than E4" — the flipped one
        (10.0, 5),
    ])
    def test_slab_boundaries(self, price, slab):
        assert _slab(_flags(price, POLARITY_DOWN), "med", 20) == slab

    def test_above_the_ema_is_all_false(self):
        out = _flags(100.01, POLARITY_DOWN)
        assert _slab(out, "med", 20) is None
        assert out[dist_key("med", 20)] < 0.0

    def test_mirrors_buy_exactly(self):
        for delta in (0.0, 1.0, 2.5, 7.5, 10.0, 33.0):
            up = _slab(_flags(100.0 + delta, POLARITY_UP), "med", 20)
            dn = _slab(_flags(100.0 - delta, POLARITY_DOWN), "med", 20)
            assert up == dn


# ---------- the three ATR periods are independent ----------

class TestPeriodsAndEmas:

    def test_wider_atr_means_a_lower_slab(self):
        """Same price (+5 over a med of 100), five different ATRs:
        5/5=1.00, 5/8=0.625, 5/10=0.50, 5/20=0.25, 5/40=0.125
        -> slabs 5 / 3 / 3 / 2 / 1. The same bar is 'stretched' under a
        tight ATR and 'nothing happened' under a wide one — which is the
        entire reason for carrying five of them."""
        out = _flags(105.0)
        assert _slab(out, "med", 10) == 5      # exactly 1.00 ATR -> open slab
        assert _slab(out, "med", 15) == 3      # 0.625
        assert _slab(out, "med", 20) == 3      # 0.50
        assert _slab(out, "med", 30) == 2      # 0.25
        assert _slab(out, "med", 40) == 1      # 0.125

    def test_emas_are_scored_independently(self):
        out = _flags(105.0, med=100.0, long=104.0, longest=110.0)
        assert _slab(out, "med", 20) == 3        # +0.50 ATR
        assert _slab(out, "long", 20) == 1       # +0.10 ATR
        assert _slab(out, "longest", 20) is None  # price is BELOW longest
        assert out[dist_key("longest", 20)] == pytest.approx(-0.5)

    def test_distance_is_signed_atr_units(self):
        out = _flags(107.5)
        assert out[dist_key("med", 20)] == pytest.approx(0.75)
        assert out[dist_key("med", 30)] == pytest.approx(0.375)
        assert out[dist_key("med", 40)] == pytest.approx(0.1875)


# ---------- degradation: bad inputs must not poison a row ----------

class TestDegradation:

    def test_missing_atr_for_one_period_only_kills_that_period(self):
        out = _flags(105.0, atrs={**ATRS, 30: None})
        assert _slab(out, "med", 20) == 3
        assert _slab(out, "med", 30) is None
        assert math.isnan(out[dist_key("med", 30)])
        assert _slab(out, "med", 40) == 1

    def test_zero_atr_is_unknown_not_a_divide_by_zero(self):
        out = _flags(105.0, atrs={**ATRS, 20: 0.0})
        assert math.isnan(out[dist_key("med", 20)])
        assert _slab(out, "med", 20) is None

    def test_nan_atr_is_unknown(self):
        out = _flags(105.0, atrs={**ATRS, 20: float("nan")})
        assert math.isnan(out[dist_key("med", 20)])

    def test_an_absent_period_key_is_unknown_not_a_crash(self):
        """A caller that forgets atr15 must get NULLs for atr15, not a
        KeyError that kills the whole ticker's enrichment run."""
        out = _flags(105.0, atrs={10: 5.0, 20: 10.0, 30: 20.0, 40: 40.0})
        assert math.isnan(out[dist_key("med", 15)])
        assert _slab(out, "med", 15) is None
        assert _slab(out, "med", 20) == 3

    def test_missing_price_kills_everything_quietly(self):
        out = _flags(None)
        assert set(out) == set(band_keys())
        assert all(v is False for v in out.values() if isinstance(v, bool))
        assert all(math.isnan(v) for v in out.values()
                   if isinstance(v, float))

    def test_missing_one_ema_kills_only_its_own_flags(self):
        out = _flags(105.0, long=None)
        assert _slab(out, "med", 20) == 3
        assert _slab(out, "long", 20) is None
        assert math.isnan(out[dist_key("long", 20)])
        assert _slab(out, "longest", 20) == 3


# ---------- the spec, restated as a test ----------

def test_the_cuts_are_quarter_atr_multiples():
    """If someone 'tunes' BAND_FRACTIONS, every stored row before that commit
    silently means something else. This is the tripwire."""
    assert BAND_FRACTIONS == (0.25, 0.50, 0.75, 1.00)
    assert ATR_PERIODS == (10, 15, 20, 30, 40)
    assert EMAS == ("med", "long", "longest")
    assert N_SLABS == 5
    assert len(band_keys()) == 90
