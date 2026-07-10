"""Per-ticker signal-enrichment walk (spec §19). Pure — no DB.

Consumes the shared weekly_forming_walk so the §4 series sit on the exact
weekly bars the stored EMAs were computed on. Advances the assembler once
per week on the converged bar; at each signal week it branches off the
week-start snapshot with the forming (week-to-date) bar as of the signal
day, so features are causally correct (no lookahead into the rest of the
signal's week). Inputs are pre-resolved dicts (the DB runner builds them):

  state_by_date     {date: (med, long, longest, atr)}   forming EMAs / day
  conv_by_monday    {monday_dt: (med, long, longest, atr)} converged / week
  signals_by_date   {date: [signal_type, ...]}
"""
from __future__ import annotations

from ..domain.signal_enrichment import SignalEnrichment
from .signal_features import SignalFeatureAssembler
from .weekly_walk import weekly_forming_walk

_NAN = float("nan")


def compute_ticker_signal_enrichments(
    *,
    ticker_id: int,
    timeframe: str,
    daily_bars,
    state_by_date: dict,
    conv_by_monday: dict,
    signals_by_date: dict,
    session,
    now,
    feature_spec_version: str,
    rs_ema_by_monday: dict | None = None,
    industry_id: int | None = None,
) -> list[SignalEnrichment]:
    signal_dates = list(signals_by_date.keys())
    asm = SignalFeatureAssembler()
    rows: list[SignalEnrichment] = []
    cur_rs_ema = _NAN

    for week in weekly_forming_walk(
        daily_bars, session=session, now=now,
        target_days=signal_dates, constant_volume=1.0,
    ):
        snap = asm.snapshot()

        for day in week.days:               # signal days only (filtered)
            st = state_by_date.get(day.date)
            if st is None:
                continue                    # no state yet (warm-up)
            asm.restore(snap)
            asm.update(
                close=day.close, high=day.high, low=day.low,
                med_ema=st[0], long_ema=st[1], longest_ema=st[2],
                atr=st[3], upper_outer=st[4], lower_outer=st[5],
                regime=st[6], volume=day.volume,
            )
            feats = asm.features()
            feats["rs_ema"] = cur_rs_ema
            for stype in signals_by_date[day.date]:
                rows.append(SignalEnrichment(
                    ticker_id=ticker_id, timeframe=timeframe,
                    signal_date=day.date, signal_type=stype,
                    features=feats, feature_spec_version=feature_spec_version,
                    industry_id=industry_id,
                ))

        asm.restore(snap)                   # discard the forming branch
        if week.real is not None:
            conv = conv_by_monday.get(week.monday)
            m, lo, lg, a, uo, lob, rg = conv if conv is not None else (
                _NAN, _NAN, _NAN, _NAN, None, None, None
            )
            asm.update(
                close=week.real.close, high=week.real.high,
                low=week.real.low, med_ema=m, long_ema=lo,
                longest_ema=lg, atr=a, upper_outer=uo,
                lower_outer=lob, regime=rg, volume=week.real.volume,
            )
            if rs_ema_by_monday is not None:
                cur_rs_ema = rs_ema_by_monday.get(week.monday, _NAN)
    return rows
