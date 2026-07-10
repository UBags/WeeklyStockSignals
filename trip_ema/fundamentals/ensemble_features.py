"""Signal-time ensemble features (spec §17.5). Pure.

For a signal in industry I, aggregate I's influential ensemble's CURRENT
regime (each member J's breadth g^J at the signal's week), correlation-
weighted so counter-movers (C<0) flip in naturally. Directions are
correlation-aware: a member's effective lean is sign(C·g^J) — a bearish
counter-mover confirms a bullish signal.
"""
from __future__ import annotations


def compute_ensemble_features(signal_dir, members):
    """signal_dir: +1 (BUY / EXIT_FROM_SELL) or -1 (SELL / EXIT_FROM_BUY).
    members: iterable of (corr, breadth, is_leader) for the ensemble;
             members with breadth is None are dropped (no regime that week).
    Returns (ensemble_score, ensemble_confirms, ensemble_agreement,
    n_leaders_bull, n_leaders_bear) or None if no member has a regime."""
    ms = [(c, b, isl) for (c, b, isl) in members if b is not None]
    if not ms:
        return None
    den = sum(abs(c) for (c, b, _isl) in ms)
    score = sum(c * b for (c, b, _isl) in ms) / den if den > 0 else 0.0
    confirms = bool(score != 0.0 and (score > 0) == (signal_dir > 0))
    aligned = sum(
        1 for (c, b, _isl) in ms
        if c * b != 0.0 and (c * b > 0) == (signal_dir > 0)
    )
    agreement = aligned / len(ms)
    n_leaders_bull = sum(1 for (c, b, isl) in ms if isl and c * b > 0)
    n_leaders_bear = sum(1 for (c, b, isl) in ms if isl and c * b < 0)
    return score, confirms, agreement, n_leaders_bull, n_leaders_bear
