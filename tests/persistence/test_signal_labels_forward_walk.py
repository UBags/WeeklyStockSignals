"""Tests for the forward-walk upsert flatten (spec §3/§4).

The flatten maps a SignalForwardWalk onto 79 columns; a wrong mapping is a wrong
training target, so the column set, the result mapping, and the censored-defaults
are all pinned — plus a cross-check that the column set equals migration 0055's.
"""
from __future__ import annotations

import importlib.util
from datetime import date
from pathlib import Path

from trip_ema.domain.forward_walk import (
    DATA_END,
    LONG,
    NEXT_SIGNAL,
    SHORT,
    SignalForwardWalk,
    WalkBar,
    walk_signal,
)
from trip_ema.persistence.signal_labels_repo import (
    _FW_COLUMNS,
    _UPSERT_FW,
    _fw_params,
)


def _result_row() -> SignalForwardWalk:
    res = walk_signal(100.0, [WalkBar(100, 110, 98, 105)], LONG,
                      atr20=2.0, industry_ret=0.01, market_ret=0.03)
    return SignalForwardWalk(7, LONG, date(2024, 1, 2), date(2024, 1, 2),
                             NEXT_SIGNAL, 1, False, res)


def test_fw_columns_are_79_and_unique():
    assert len(_FW_COLUMNS) == 79
    assert len(set(_FW_COLUMNS)) == 79


def test_fw_params_keys_exactly_cover_columns():
    p = _fw_params(_result_row())
    assert set(p.keys()) == {"signal_enrichment_id", *_FW_COLUMNS}
    assert len(p) == 80


def test_fw_params_result_mapping():
    row = _result_row()
    r = row.result
    p = _fw_params(row)
    assert p["forward_walk_done"] is True
    assert p["side"] == LONG
    assert p["window_censored"] is False
    assert p["bars_in_window"] == 1
    assert p["entry_open"] == r.entry_open
    assert p["exit_close"] == r.exit_close
    assert p["realized_ret"] == r.realized_ret
    assert p["hit_gain_5_pct"] == r.hit_gain["5"]
    assert p["bars_to_gain_5_pct"] == r.bars_to_gain["5"]
    assert p["hit_loss_10_pct"] == r.hit_loss["10"]
    assert p["bars_to_loss_10_pct"] == r.bars_to_loss["10"]
    assert p["mfe_pct"] == r.mfe_pct and p["mae_bar"] == r.mae_bar
    assert p["excess_ret_market"] == r.excess_ret_market


def test_fw_params_censored_writes_explicit_defaults():
    # result=None (data-quality censored): metrics reset, bookkeeping kept
    row = SignalForwardWalk(7, SHORT, date(2024, 1, 2), date(2024, 1, 3),
                            DATA_END, 4, True, None)
    p = _fw_params(row)
    assert p["window_censored"] is True and p["forward_walk_done"] is True
    assert p["bars_in_window"] == 4 and p["side"] == SHORT
    assert p["entry_open"] is None and p["realized_ret"] is None
    assert p["hit_gain_5_pct"] is False and p["bars_to_gain_5_pct"] == -1
    assert p["hit_loss_20_pct"] is False and p["bars_to_loss_20_pct"] == -1
    assert p["mfe_pct"] is None and p["mfe_bar"] == -1
    assert p["ever_in_profit"] is False and p["time_underwater_bars"] == -1


def test_fw_columns_match_migration_0055():
    repo = Path(__file__).resolve().parents[2]
    path = repo / "alembic_pkg" / "versions" / "0055_signal_label_forward_walk.py"
    spec = importlib.util.spec_from_file_location("mig_0055", path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    assert set(_FW_COLUMNS) == {n for n, _k in m.ALL_COLUMNS}


def test_upsert_sql_covers_every_column():
    sql = str(_UPSERT_FW)
    for c in _FW_COLUMNS:
        assert c in sql, c
    assert "ON CONFLICT (signal_enrichment_id)" in sql
    assert "computed_at = NOW()" in sql
