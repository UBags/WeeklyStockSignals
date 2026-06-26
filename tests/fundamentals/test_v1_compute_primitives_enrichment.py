"""Tests for compute_enrichment_primitives.

Mirrors test_v1_compute_primitives.py's structure for the
enrichment counterpart.
"""
from __future__ import annotations

import math
from datetime import date, timedelta

import pandas as pd
import pytest

from trip_ema.fundamentals.indicator_primitives import (
    INDICATOR_DEFAULTS,
    compute_indicator_series,
)
from trip_ema.fundamentals.v1_compute_primitives import (
    compute_enrichment_primitives,
    compute_state_primitives,
)


# Pinned key set — change this only when the schema actually changes.
EXPECTED_ENRICHMENT_KEYS = {
    "bar_timestamp",
    "pierces_upper_mid", "pierces_lower_mid",
    "dist_to_med_in_hl_units", "dist_to_long_in_hl_units",
    "dist_to_longest_in_hl_units",
    "mean_dist_to_med_in_hl_units", "mean_dist_to_long_in_hl_units",
    "mean_dist_to_longest_in_hl_units",
    "bars_since_med_turn", "bars_since_long_turn",
    "bars_since_longest_turn",
    "pct_change_since_med_turn", "pct_change_since_long_turn",
    "pct_change_since_longest_turn",
    "med_pattern_label", "long_pattern_label", "longest_pattern_label",
    "med_turn_buy", "med_turn_sell",
    "long_turn_buy", "long_turn_sell",
    "longest_turn_buy", "longest_turn_sell",
}


def _synthetic_bars(n: int) -> tuple[pd.Series, ...]:
    close = pd.Series(
        [100.0 + 5.0 * math.sin(i / 4.0) + i * 0.1 for i in range(n)],
        dtype=float,
    )
    high = close + 1.0
    low = close - 1.0
    volume = pd.Series([1_000_000.0] * n, dtype=float)
    return close, high, low, volume


def _dates(n: int) -> pd.Series:
    start = date(2026, 1, 5)
    return pd.Series([start + timedelta(weeks=i) for i in range(n)])


# ============================================================
# Empty input
# ============================================================

def test_empty_input_returns_empty_list():
    empty = pd.Series([], dtype=float)
    out = compute_enrichment_primitives(
        empty, empty, empty, empty,
        trade_dates=pd.Series([], dtype="object"),
    )
    assert out == []


# ============================================================
# Output shape
# ============================================================

def test_output_dicts_have_exactly_expected_keys():
    n = 100
    close, high, low, volume = _synthetic_bars(n)
    dates = _dates(n)
    out = compute_enrichment_primitives(
        close, volume, high, low, trade_dates=dates,
    )
    assert len(out) > 0
    for d in out:
        assert set(d.keys()) == EXPECTED_ENRICHMENT_KEYS, (
            f"Per-bar dict has wrong key set. "
            f"Extra: {set(d.keys()) - EXPECTED_ENRICHMENT_KEYS}, "
            f"Missing: {EXPECTED_ENRICHMENT_KEYS - set(d.keys())}"
        )


def test_output_does_not_contain_core_state_keys():
    """The point of the V1 split — the enrichment output must NOT
    have EMAs/ATR/bands/regime. Those are produced by
    compute_state_primitives."""
    n = 100
    close, high, low, volume = _synthetic_bars(n)
    dates = _dates(n)
    out = compute_enrichment_primitives(
        close, volume, high, low, trade_dates=dates,
    )
    forbidden = {
        "med_ema", "long_ema", "longest_ema",
        "atr",
        "upper_inner_band", "lower_inner_band",
        "upper_mid_band", "lower_mid_band",
        "upper_outer_band", "lower_outer_band",
        "display_regime",
    }
    for d in out:
        for k in forbidden:
            assert k not in d, (
                f"Core state key {k!r} leaked into enrichment "
                f"output. Core lives in compute_state_primitives "
                f"(Step 4)."
            )


# ============================================================
# Row-by-row alignment with compute_state_primitives
# ============================================================

def test_emits_same_bar_count_as_state_primitives(db_engine=None):
    """compute_enrichment_primitives and compute_state_primitives use
    the same NaN-warmup skip condition, so their output lists must
    have the SAME length and SAME bar_timestamp sequence. This is
    what keeps the FK satisfied at upsert time."""
    n = 200
    close, high, low, volume = _synthetic_bars(n)
    dates = _dates(n)

    state_out = compute_state_primitives(
        close, volume, high, low, trade_dates=dates,
    )
    enrichment_out = compute_enrichment_primitives(
        close, volume, high, low, trade_dates=dates,
    )

    assert len(state_out) == len(enrichment_out), (
        f"State emits {len(state_out)} bars, enrichment emits "
        f"{len(enrichment_out)} — skip conditions diverged. "
        f"The FK between stock_indicator_enrichment and "
        f"stock_indicator_state requires same bar set."
    )
    state_timestamps = [d["bar_timestamp"] for d in state_out]
    enrich_timestamps = [d["bar_timestamp"] for d in enrichment_out]
    assert state_timestamps == enrich_timestamps, (
        "Bar timestamp sequences diverged between state and "
        "enrichment outputs."
    )


# ============================================================
# Warmup behavior
# ============================================================

def test_warmup_bars_are_skipped():
    n = 100
    close, high, low, volume = _synthetic_bars(n)
    dates = _dates(n)
    out = compute_enrichment_primitives(
        close, volume, high, low, trade_dates=dates,
    )
    assert len(out) < n


# ============================================================
# Boolean fields are real Python bools (not numpy)
# ============================================================

def test_boolean_fields_are_python_bool():
    """The repo's upsert path uses these as DB BOOLEAN inputs. numpy's
    bool_ type works via psycopg2 too, but pin to native bool for
    consistency."""
    n = 100
    close, high, low, volume = _synthetic_bars(n)
    dates = _dates(n)
    out = compute_enrichment_primitives(
        close, volume, high, low, trade_dates=dates,
    )
    if not out:
        pytest.skip("no enrichment bars emitted")
    sample = out[0]
    for k in ("pierces_upper_mid", "pierces_lower_mid",
              "med_turn_buy", "med_turn_sell",
              "long_turn_buy", "long_turn_sell",
              "longest_turn_buy", "longest_turn_sell"):
        assert isinstance(sample[k], bool), (
            f"{k}: expected Python bool, got {type(sample[k]).__name__}"
        )


# ============================================================
# Pattern labels are within the allowed set
# ============================================================

def test_pattern_labels_are_in_canonical_set():
    from trip_ema.fundamentals import PATTERN_LABELS

    n = 200
    close, high, low, volume = _synthetic_bars(n)
    dates = _dates(n)
    out = compute_enrichment_primitives(
        close, volume, high, low, trade_dates=dates,
    )
    for d in out:
        for k in ("med_pattern_label", "long_pattern_label",
                  "longest_pattern_label"):
            assert d[k] in PATTERN_LABELS, (
                f"{k}={d[k]!r} not in PATTERN_LABELS"
            )


# ============================================================
# Synthetic ATR for industry index path
# ============================================================

def test_synthetic_atr_for_index_produces_output():
    n = 100
    close = pd.Series(
        [1000.0 + i * 2.0 + 50.0 * math.sin(i / 6.0) for i in range(n)],
        dtype=float,
    )
    high = close.copy()
    low = close.copy()
    volume = pd.Series([1.0] * n, dtype=float)
    dates = _dates(n)

    out = compute_enrichment_primitives(
        close, volume, high, low,
        trade_dates=dates,
        synthetic_atr_for_index=True,
    )
    assert len(out) > 0
