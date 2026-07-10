"""Cross-entity enrichment (spec §11/§16/§17). Pure — computes features
that depend on OTHER entities (industry index, constituents, ensemble)
on the weekly grain, keyed by week Monday, for the walk to merge into the
per-signal vector. The single-entity SignalFeatureAssembler stays pure;
this layer is where multi-entity data is combined.
"""
from __future__ import annotations


_NAN = float("nan")


def compute_rs_ema_by_week(weekly_closes, index_by_monday, *, span=13):
    """§11 rs_ema per week. rs_t = close_t / industry_index_t, rebased to
    1.0 at the first week both exist; rs_ema = EMA_span(rebased rs).

    weekly_closes:    [(monday_dt, close)] ordered by week.
    index_by_monday:  {monday_dt: index_value} (daily index aggregated to
                      the same weekly grain).
    Returns {monday_dt: rs_ema}; weeks with no index (pre-base) get NaN
    and do not seed the EMA.
    """
    alpha = 2.0 / (span + 1)
    rs0 = None
    ema = None
    out = {}
    for monday, close in weekly_closes:
        idx = index_by_monday.get(monday)
        if idx is None or idx <= 0.0 or close is None or close <= 0.0:
            out[monday] = _NAN
            continue
        rs = close / idx
        if rs0 is None:
            rs0 = rs
        rs_norm = rs / rs0
        ema = rs_norm if ema is None else alpha * rs_norm + (1 - alpha) * ema
        out[monday] = ema
    return out


def index_by_week(index_daily, monday_of):
    """Aggregate a daily industry-index series to the weekly grain: the
    last (latest) value in each week. index_daily must be ordered ascending
    by date so later values overwrite earlier within a week.

    index_daily: [(date, value)] ascending.
    monday_of:   date -> week Monday (injected; keeps tz logic out).
    Returns {monday: value}.
    """
    out = {}
    for d, v in index_daily:
        out[monday_of(d)] = v
    return out
