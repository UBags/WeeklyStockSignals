"""Cross-sectional relative-strength rank (spec §11 rs_rank).

Pure. Given the weekly close series of every constituent in an industry
(each with its active [effective_from, effective_to] window), compute,
per week, each ticker's percentile rank of its N-week return among the
constituents active that week. This is the per-industry cross-section the
tier-3 sector_stock_leadlag table materialises; the runner loads it once
per industry, not once per ticker.
"""
from __future__ import annotations

_NAN = float("nan")


def _n_week_returns(closes, window):
    """closes: {monday: close} for one ticker -> {monday: N-week return}.
    Return at week t needs close_t and close_{t-window}."""
    mondays = sorted(closes)
    out = {}
    for i, m in enumerate(mondays):
        if i < window:
            continue
        c_now = closes[m]
        c_then = closes[mondays[i - window]]
        if c_now and c_then and c_then > 0.0:
            out[m] = c_now / c_then - 1.0
    return out


def compute_rs_rank_by_week(constituent_closes, active_windows, window=4):
    """constituent_closes: {ticker_id: {monday: close}}.
    active_windows:      {ticker_id: (eff_from_monday, eff_to_monday|None)}
                         — ticker counted in a week's cross-section only
                         while active (eff_to None = still active).
    Returns {ticker_id: {monday: rs_rank in [0,1]}}.

    rs_rank = fraction of that week's active constituents whose N-week
    return is <= this ticker's (so the strongest performer -> ~1.0).
    """
    rets = {t: _n_week_returns(c, window)
            for t, c in constituent_closes.items()}
    # gather, per week, the active tickers' returns
    per_week: dict = {}
    for t, rmap in rets.items():
        lo, hi = active_windows.get(t, (None, None))
        for m, r in rmap.items():
            if lo is not None and m < lo:
                continue
            if hi is not None and m > hi:
                continue
            per_week.setdefault(m, {})[t] = r
    out: dict = {t: {} for t in constituent_closes}
    for m, tr in per_week.items():
        vals = list(tr.values())
        n = len(vals)
        if n < 2:
            for t in tr:
                out[t][m] = _NAN
            continue
        for t, r in tr.items():
            out[t][m] = sum(1 for v in vals if v <= r) / n
    return out
