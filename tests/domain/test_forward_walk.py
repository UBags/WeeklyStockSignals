"""Unit tests for the pure forward-walk core (spec 02_signal_forward_walk).

The geometry here is the correctness heart of the labeler — wrong numbers here
become wrong training targets — so every branch is pinned: long & short,
entry-bar (distance 0) vs later touch, misses (-1), single-bar windows,
MAE/MFE bars + ordering, capture ratio, ATR normalization, time underwater,
in-window slope/vol/Sharpe, excess returns, and input validation.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from trip_ema.domain.forward_walk import (
    GAIN_RUNGS,
    LOSS_RUNGS,
    LONG,
    SHORT,
    WalkBar,
    walk_signal,
)

B = WalkBar


# --------------------------------------------------------------------------- #
# LONG
# --------------------------------------------------------------------------- #
def test_long_basic_returns_rungs_and_excursions():
    bars = [
        B(100, 102, 99, 101),   # entry: +2% high, -1% low
        B(101, 106, 100, 105),  # +6% high
        B(105, 103, 101, 102),  # exit close 102
    ]
    r = walk_signal(100.0, bars, LONG)
    assert r.side == LONG and r.bars_in_window == 3
    assert abs(r.realized_ret - 0.02) < 1e-12

    assert r.hit_gain["1"] and r.bars_to_gain["1"] == 0   # entry-bar high 102
    assert r.hit_gain["3"] and r.bars_to_gain["3"] == 1
    assert r.hit_gain["5"] and r.bars_to_gain["5"] == 1
    assert not r.hit_gain["7"] and r.bars_to_gain["7"] == -1
    assert all(not r.hit_loss[k] and r.bars_to_loss[k] == -1
               for k, _ in LOSS_RUNGS)

    assert abs(r.mfe_pct - 0.06) < 1e-12 and r.mfe_bar == 1
    assert abs(r.mae_pct - 0.01) < 1e-12 and r.mae_bar == 0
    assert r.mae_before_mfe is True
    assert r.ever_in_profit is True
    assert r.time_underwater_bars == 0
    assert abs(r.capture_ratio - (0.02 / 0.06)) < 1e-12


# --------------------------------------------------------------------------- #
# SHORT (mirror)
# --------------------------------------------------------------------------- #
def test_short_basic_is_mirrored():
    bars = [
        B(100, 101, 98, 99),   # entry: -2% low (short +2%), +1% high (short loss)
        B(99, 100, 94, 95),    # -6% low
        B(95, 97, 93, 96),     # exit 96
    ]
    r = walk_signal(100.0, bars, SHORT)
    assert abs(r.realized_ret - (100.0 / 96.0 - 1.0)) < 1e-12

    assert r.bars_to_gain["1"] == 0            # entry low 98 → short +2%
    assert r.bars_to_gain["3"] == 1
    assert r.bars_to_gain["5"] == 1
    assert r.bars_to_gain["7"] == 2
    assert not r.hit_gain["9"] and r.bars_to_gain["9"] == -1
    assert all(not r.hit_loss[k] for k, _ in LOSS_RUNGS)   # never adverse enough

    assert abs(r.mfe_pct - 0.07) < 1e-12 and r.mfe_bar == 2
    assert abs(r.mae_pct - 0.01) < 1e-12 and r.mae_bar == 0
    assert r.mae_before_mfe is True
    assert r.time_underwater_bars == 0


# --------------------------------------------------------------------------- #
# Bar indexing (locked): entry = bar 1 → distance 0; "all hit on bar 2" → 1
# --------------------------------------------------------------------------- #
def test_all_gain_rungs_hit_on_bar2_have_distance_1():
    bars = [
        B(100, 100.5, 99.5, 100.2),   # entry high 100.5 < 101 → no +1% here
        B(100.2, 109.5, 100, 109),    # +9% (=109) → 1,3,5,7,9 all hit at index 1
    ]
    r = walk_signal(100.0, bars, LONG)
    for label in ("1", "3", "5", "7", "9"):
        assert r.hit_gain[label] and r.bars_to_gain[label] == 1
    assert not r.hit_gain["11"] and r.bars_to_gain["11"] == -1


def test_entry_bar_gap_up_gives_distance_0():
    bars = [B(100, 103, 100, 102)]     # gap: entry-bar high 103
    r = walk_signal(100.0, bars, LONG)
    assert r.bars_to_gain["1"] == 0 and r.bars_to_gain["3"] == 0
    assert not r.hit_gain["5"]


def test_unreached_rungs_are_minus_one():
    bars = [B(100, 101, 99, 100), B(100, 102, 98.5, 100)]
    r = walk_signal(100.0, bars, LONG)
    assert r.hit_gain["1"] and r.bars_to_gain["1"] == 0
    assert not r.hit_gain["3"] and r.bars_to_gain["3"] == -1
    assert not r.hit_loss["5"] and r.bars_to_loss["5"] == -1


# --------------------------------------------------------------------------- #
# Single-bar window
# --------------------------------------------------------------------------- #
def test_single_bar_window():
    r = walk_signal(100.0, [B(100, 105, 98, 103)], LONG)
    assert r.bars_in_window == 1
    assert abs(r.realized_ret - 0.03) < 1e-12
    assert r.bars_to_gain["5"] == 0 and not r.hit_gain["7"]
    assert abs(r.mfe_pct - 0.05) < 1e-12 and r.mfe_bar == 0
    assert abs(r.mae_pct - 0.02) < 1e-12 and r.mae_bar == 0
    assert r.mae_before_mfe is False           # same bar → not strictly before
    assert r.realized_vol is None and r.sharpe_in_window is None
    assert r.slope_per_bar is None             # < 2 closes
    assert abs(r.capture_ratio - 0.6) < 1e-12


# --------------------------------------------------------------------------- #
# MAE / MFE ordering
# --------------------------------------------------------------------------- #
def test_mfe_before_mae_sets_flag_false():
    bars = [B(100, 106, 100, 105), B(105, 105, 94, 95)]   # run up, then heat
    r = walk_signal(100.0, bars, LONG)
    assert r.mfe_bar == 0 and r.mae_bar == 1
    assert r.mae_before_mfe is False


# --------------------------------------------------------------------------- #
# Capture ratio
# --------------------------------------------------------------------------- #
def test_capture_ratio_is_none_when_mfe_zero():
    bars = [B(100, 100, 98, 99), B(99, 99.5, 97, 98)]     # never above entry
    r = walk_signal(100.0, bars, LONG)
    assert r.mfe_pct == 0.0
    assert r.ever_in_profit is False
    assert r.capture_ratio is None


# --------------------------------------------------------------------------- #
# Excess returns
# --------------------------------------------------------------------------- #
def test_excess_returns():
    bars = [B(100, 105, 98, 103)]
    r = walk_signal(100.0, bars, LONG, industry_ret=0.01, market_ret=0.03)
    assert abs(r.excess_ret_industry - (0.03 - 0.01)) < 1e-12
    assert abs(r.excess_ret_market - (0.03 - 0.03)) < 1e-12


def test_excess_returns_none_passthrough():
    r = walk_signal(100.0, [B(100, 105, 98, 103)], LONG)
    assert r.excess_ret_industry is None and r.excess_ret_market is None


# --------------------------------------------------------------------------- #
# ATR-normalized excursions
# --------------------------------------------------------------------------- #
def test_atr20_normalized_excursions():
    r = walk_signal(100.0, [B(100, 105, 98, 103)], LONG, atr20=2.0)
    assert abs(r.mfe_atr20 - (0.05 * 100 / 2.0)) < 1e-12   # 2.5
    assert abs(r.mae_atr20 - (0.02 * 100 / 2.0)) < 1e-12   # 1.0


def test_atr20_missing_gives_none():
    r = walk_signal(100.0, [B(100, 105, 98, 103)], LONG, atr20=None)
    assert r.mfe_atr20 is None and r.mae_atr20 is None
    r0 = walk_signal(100.0, [B(100, 105, 98, 103)], LONG, atr20=0.0)
    assert r0.mfe_atr20 is None and r0.mae_atr20 is None


# --------------------------------------------------------------------------- #
# Time underwater
# --------------------------------------------------------------------------- #
def test_time_underwater_counts_adverse_closes():
    bars = [B(100, 101, 99, 98), B(98, 100, 97, 101), B(101, 102, 100, 99)]
    r = walk_signal(100.0, bars, LONG)
    assert r.time_underwater_bars == 2


# --------------------------------------------------------------------------- #
# Path statistics
# --------------------------------------------------------------------------- #
def test_slope_vol_sharpe_on_monotonic_up():
    bars = [B(100, 100, 100, 100), B(101, 101, 101, 101),
            B(102, 102, 102, 102), B(103, 103, 103, 103)]
    r = walk_signal(100.0, bars, LONG)
    assert abs(r.slope_per_bar - 0.01) < 1e-12     # OLS slope 1.0 / entry 100
    assert r.realized_vol is not None and r.realized_vol > 0
    assert r.sharpe_in_window is not None and r.sharpe_in_window > 0


# --------------------------------------------------------------------------- #
# Loss rungs, direction-aware
# --------------------------------------------------------------------------- #
def test_long_loss_rungs_on_low():
    r = walk_signal(100.0, [B(100, 100, 90, 92)], LONG)
    assert r.hit_loss["5"] and r.bars_to_loss["5"] == 0
    assert r.hit_loss["10"] and r.bars_to_loss["10"] == 0
    assert not r.hit_loss["15"] and r.bars_to_loss["15"] == -1


def test_short_loss_rungs_on_high():
    r = walk_signal(100.0, [B(100, 110, 100, 108)], SHORT)
    assert r.hit_loss["5"] and r.bars_to_loss["5"] == 0
    assert r.hit_loss["10"] and r.bars_to_loss["10"] == 0
    assert not r.hit_loss["15"]


# --------------------------------------------------------------------------- #
# Input validation (fail loud)
# --------------------------------------------------------------------------- #
def test_bad_side_raises():
    with pytest.raises(ValueError):
        walk_signal(100.0, [B(100, 101, 99, 100)], "BUY")


def test_nonpositive_entry_raises():
    with pytest.raises(ValueError):
        walk_signal(0.0, [B(100, 101, 99, 100)], LONG)


def test_empty_bars_raises():
    with pytest.raises(ValueError):
        walk_signal(100.0, [], LONG)


# --------------------------------------------------------------------------- #
# Rung definitions must match migration 0055 (code-vs-migration tripwire)
# --------------------------------------------------------------------------- #
def _load_migration():
    repo = Path(__file__).resolve().parents[2]
    path = repo / "alembic_pkg" / "versions" / "0055_signal_label_forward_walk.py"
    spec = importlib.util.spec_from_file_location("mig_0055", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_gain_rungs_count_and_values():
    assert len(GAIN_RUNGS) == 21
    labels = [lbl for lbl, _f in GAIN_RUNGS]
    assert labels[0] == "1" and labels[-1] == "100"
    for lbl, frac in GAIN_RUNGS:
        assert abs(frac - int(lbl) / 100.0) < 1e-12


def test_loss_rungs_count_and_fractions():
    assert len(LOSS_RUNGS) == 6
    d = dict(LOSS_RUNGS)
    assert abs(d["7_and_half"] - 0.075) < 1e-12
    assert abs(d["12_and_half"] - 0.125) < 1e-12


def test_rung_labels_match_migration():
    m = _load_migration()
    assert [lbl for lbl, _ in GAIN_RUNGS] == [str(n) for n in m.GAIN_RUNGS]
    assert [lbl for lbl, _ in LOSS_RUNGS] == list(m.LOSS_RUNGS)


def test_exact_touch_hits_but_near_miss_does_not():
    # exact round-number touch registers (the float-representability fix)
    assert walk_signal(100.0, [B(100, 103, 99, 101)], LONG).hit_gain["3"]
    # a real near-miss just below the rung must NOT register
    r = walk_signal(100.0, [B(100, 102.9999, 99, 101)], LONG)
    assert not r.hit_gain["3"] and r.bars_to_gain["3"] == -1
