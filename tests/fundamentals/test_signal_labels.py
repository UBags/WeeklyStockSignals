"""Unit tests for the pure signal labeller (forward returns / drawdown / rr).

quality_class was retired in 0055 — the great/good/bad target y is
derived downstream from the forward-walk table, not from compute_label.
"""
from trip_ema.fundamentals.signal_labels import compute_label


def test_buy_rally_forward_returns_and_low_drawdown():
    closes = [100 + i * 2.5 for i in range(1, 14)]
    r4, r8, r13, dd, rr = compute_label(100.0, closes, 3.0, "BUY")
    assert abs(r13 - 0.325) < 1e-9
    assert dd < 0.01
    assert rr is not None and rr > 0


def test_buy_drop_negative_return():
    closes = [100 - i for i in range(1, 14)]
    r4, r8, r13, dd, rr = compute_label(100.0, closes, 3.0, "BUY")
    assert r13 < 0


def test_sell_raw_return_stored_as_is():
    closes = [100 - i for i in range(1, 14)]
    r4, r8, r13, dd, rr = compute_label(100.0, closes, 3.0, "SELL")
    assert r13 < 0            # raw close-ratio; direction carried by type
    assert rr is not None and rr > 0   # reward is directional


def test_short_window_yields_partial_returns():
    r4, r8, r13, dd, rr = compute_label(100.0, [101, 102, 103], 3.0, "BUY")
    assert r4 is None and r8 is None and r13 is None
