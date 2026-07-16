"""Regime-based inputs for the §16 sector lead/lag compute (design §4.2).

The original sector lead/lag correlated weekly RETURNS. The design switches
the inputs to REGIME:
  r_s  = the stock's weekly display_regime series
  r_I  = the industry BREADTH series (equal-weight mean of constituent
         display_regime, the same series the industry x industry matrix uses)

Self-weight debiasing (leave-one-out)
-------------------------------------
compute_sector_leadlag debiases the industry series against the stock so a
large constituent does not correlate with itself:

    r_Is = (r_I - w_s * r_s) / (1 - w_s)

That formula is the market-cap leave-one-out for a CAP-WEIGHTED index. But
breadth is an EQUAL-WEIGHT mean over the N active constituents in the week:

    breadth = (1/N) * sum_k regime_k

Removing this stock from an equal-weight mean is:

    (N * breadth - regime_s) / (N - 1)
      = (breadth - (1/N) * regime_s) / (1 - 1/N)

which is exactly the core formula with **w_s = 1/N**. So the correct weight to
feed for a constituent week is 1/N (N = constituents contributing to that
week's breadth), NOT the cap weight. For a non-constituent week the stock is
not in the breadth at all, so w_s = 0 (no debias). For N <= 1 the leave-one-out
is undefined (removing the only constituent leaves nothing), so w_s = 0.

Getting w_s wrong here does not error — it silently biases every lead/lag on
big constituents. The stock's regime value fed as r_s MUST be the same weekly
value that went into that week's breadth (latest daily display_regime in the
week, timeframe 1W) or the subtraction is only approximate.
"""
from __future__ import annotations

from collections.abc import Hashable


def build_regime_leadlag_inputs(
    stock_regime: dict,
    breadth: dict,
    n_by_week: dict,
    constituent_weeks: set,
):
    """Align a stock's regime series with the industry breadth series.

    Parameters
    ----------
    stock_regime : {week: display_regime}   the stock's weekly regime.
    breadth      : {week: breadth}           the industry breadth series.
    n_by_week    : {week: N}                 constituents contributing to
                                             that week's breadth.
    constituent_weeks : set[week]            weeks in which this stock is an
                                             active breadth constituent.

    `week` keys must be identical objects across the dicts (e.g. the same
    bar_timestamp) so alignment is exact.

    Returns (weeks, r_s, r_I, w_s, is_constituent), all aligned ascending
    over the weeks present in BOTH stock_regime and breadth. w_s = 1/N on
    constituent weeks with N > 1, else 0.0.
    """
    weeks: list[Hashable] = sorted(w for w in stock_regime if w in breadth)
    r_s: list[float] = []
    r_I: list[float] = []
    w_s: list[float] = []
    is_constituent: list[bool] = []
    for w in weeks:
        r_s.append(float(stock_regime[w]))
        r_I.append(float(breadth[w]))
        if w in constituent_weeks:
            n = n_by_week.get(w, 0)
            w_s.append(1.0 / n if n > 1 else 0.0)
            is_constituent.append(True)
        else:
            w_s.append(0.0)
            is_constituent.append(False)
    return weeks, r_s, r_I, w_s, is_constituent
