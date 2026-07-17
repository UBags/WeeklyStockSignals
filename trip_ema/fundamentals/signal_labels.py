"""Offline labelling for signal_enrichment (spec §13). Pure — no DB.

For each signal, forward returns at 4/8/13 weekly bars, the max drawdown
of the trade's directional equity over the horizon, and a Bad/Good/
VeryGood quality class. Labels use FUTURE bars, so they are computed
retrospectively and are never part of the live feature vector.

Direction: a SELL is favourable when price falls, so the quality class is
judged on the direction-adjusted return. fwd_ret_Nw is stored raw
(close_{t+N}/close_t - 1); signal_type carries the direction for the model.
"""
from __future__ import annotations

_DIR = {"BUY": 1, "SELL": -1, "EXIT_FROM_BUY": -1, "EXIT_FROM_SELL": 1}

_HORIZON = 13


def _fwd_ret(close_t, closes_ahead, n):
    if close_t in (None, 0) or len(closes_ahead) < n:
        return None
    c = closes_ahead[n - 1]
    if c in (None, 0):
        return None
    return c / close_t - 1.0


def _max_drawdown(equity):
    peak = equity[0]
    mdd = 0.0
    for e in equity:
        if e > peak:
            peak = e
        if peak > 0:
            mdd = max(mdd, (peak - e) / peak)
    return mdd


def compute_label(close_t, closes_ahead, atr_t, signal_type):
    """Return (fwd_ret_4w, fwd_ret_8w, fwd_ret_13w, fwd_max_dd, fwd_rr).
    Any of the forward returns / rr may be None when there aren't enough
    future bars yet."""
    d = _DIR.get(signal_type, 1)
    r4 = _fwd_ret(close_t, closes_ahead, 4)
    r8 = _fwd_ret(close_t, closes_ahead, 8)
    r13 = _fwd_ret(close_t, closes_ahead, 13)

    horizon = [c for c in closes_ahead[:_HORIZON] if c not in (None, 0)]
    if close_t not in (None, 0) and horizon:
        if d > 0:
            eq = [1.0] + [c / close_t for c in horizon]
        else:
            eq = [1.0] + [close_t / c for c in horizon]
        fwd_max_dd = _max_drawdown(eq)
    else:
        fwd_max_dd = None

    # reward in ATR units (directional), on the longest available horizon
    prim = r13 if r13 is not None else (r8 if r8 is not None else r4)
    if prim is None:
        return r4, r8, r13, fwd_max_dd, None
    n = 13 if r13 is not None else (8 if r8 is not None else 4)
    move = closes_ahead[n - 1] - close_t
    rr = (move * d / atr_t) if (atr_t not in (None, 0)) else None
    return r4, r8, r13, fwd_max_dd, rr
