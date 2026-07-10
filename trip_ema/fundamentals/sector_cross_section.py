"""Cross-sectional relative-strength rank (spec §11 rs_rank).

Pure. rs_rank_t = pctrank of a stock's N-week return within the return
distribution of its industry's *constituents* active that week. The set
that gets ranked (all stocks in the industry) is separate from the
reference set that defines the distribution (the index constituents), so
a non-constituent is still scored against its sector's leaders.
"""
from __future__ import annotations

_NAN = float("nan")


def _n_week_returns(closes, window):
    """{monday: close} -> {monday: N-week return} (needs close_{t-window})."""
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


def compute_rs_rank_by_week(
    ranked_closes, reference_closes, active_windows, window=4,
):
    """ranked_closes:    {ticker_id: {monday: close}} — everyone to score.
    reference_closes: {ticker_id: {monday: close}} — the constituent peer
                      set that defines each week's distribution.
    active_windows:   {ticker_id: (from_monday, to_monday|None)} for the
                      REFERENCE set (a constituent counts only while active).
    Returns {ticker_id: {monday: rs_rank in [0,1]}} for ranked tickers.

    rs_rank = fraction of that week's active reference returns <= the
    ticker's own N-week return (strongest -> ~1.0; a non-constituent that
    beats every constituent -> 1.0).
    """
    ref_rets = {t: _n_week_returns(c, window)
                for t, c in reference_closes.items()}
    ref_by_week: dict = {}
    for t, rmap in ref_rets.items():
        lo, hi = active_windows.get(t, (None, None))
        for m, r in rmap.items():
            if lo is not None and m < lo:
                continue
            if hi is not None and m > hi:
                continue
            ref_by_week.setdefault(m, []).append(r)

    ranked_rets = {t: _n_week_returns(c, window)
                   for t, c in ranked_closes.items()}
    out: dict = {t: {} for t in ranked_closes}
    for t, rmap in ranked_rets.items():
        for m, r in rmap.items():
            ref = ref_by_week.get(m)
            if not ref or len(ref) < 2:
                out[t][m] = _NAN
                continue
            out[t][m] = sum(1 for v in ref if v <= r) / len(ref)
    return out
