"""Unit tests for §17.5 per-signal ensemble features."""
from trip_ema.fundamentals.ensemble_features import (
    compute_ensemble_features,
)


def test_score_and_confirms_correlation_aware():
    members = [(0.4, 0.6, True), (-0.3, -0.5, False), (0.3, -0.4, True)]
    score, confirms, agree, lb, le = compute_ensemble_features(1, members)
    assert abs(score - 0.27) < 1e-9
    assert confirms is True
    assert abs(agree - 2 / 3) < 1e-9
    assert (lb, le) == (1, 1)


def test_sell_flips_confirmation():
    members = [(0.4, 0.6, True), (-0.3, -0.5, False), (0.3, -0.4, True)]
    _s, confirms, agree, _lb, _le = compute_ensemble_features(-1, members)
    assert confirms is False
    assert abs(agree - 1 / 3) < 1e-9


def test_bearish_counter_mover_confirms_buy():
    _s, confirms, *_ = compute_ensemble_features(1, [(-0.5, -0.6, False)])
    assert confirms is True


def test_no_regime_returns_none():
    assert compute_ensemble_features(1, [(0.4, None, True)]) is None
