"""Unit tests for the pure signal labeller (spec §13)."""
from trip_ema.fundamentals.signal_labels import (
    CLASS_BAD,
    CLASS_GOOD,
    CLASS_VERYGOOD,
    compute_label,
)


def test_buy_rally_is_verygood():
    closes = [100 + i * 2.5 for i in range(1, 14)]
    r4, r8, r13, dd, rr, qc = compute_label(100.0, closes, 3.0, "BUY")
    assert abs(r13 - 0.325) < 1e-9
    assert dd < 0.01
    assert qc == CLASS_VERYGOOD


def test_buy_drop_is_bad():
    closes = [100 - i for i in range(1, 14)]
    r4, r8, r13, dd, rr, qc = compute_label(100.0, closes, 3.0, "BUY")
    assert r13 < 0
    assert qc == CLASS_BAD


def test_sell_on_drop_is_verygood_despite_negative_raw_return():
    closes = [100 - i for i in range(1, 14)]
    r4, r8, r13, dd, rr, qc = compute_label(100.0, closes, 3.0, "SELL")
    assert r13 < 0            # raw return stored as-is
    assert qc == CLASS_VERYGOOD   # short profits when price falls


def test_sell_on_rally_is_bad():
    closes = [100 + i * 2.5 for i in range(1, 14)]
    _, _, _, _, _, qc = compute_label(100.0, closes, 3.0, "SELL")
    assert qc == CLASS_BAD


def test_moderate_gain_is_good():
    closes = [100 + i * 0.3 for i in range(1, 14)]
    _, _, _, _, _, qc = compute_label(100.0, closes, 3.0, "BUY")
    assert qc == CLASS_GOOD


def test_short_window_yields_partial_and_null_class():
    r4, r8, r13, dd, rr, qc = compute_label(100.0, [101, 102, 103], 3.0, "BUY")
    assert r4 is None and r8 is None and r13 is None
    assert qc is None


def test_exit_from_buy_on_drop_not_bad():
    closes = [100 - i for i in range(1, 14)]
    _, _, _, _, _, qc = compute_label(100.0, closes, 3.0, "EXIT_FROM_BUY")
    assert qc != CLASS_BAD
