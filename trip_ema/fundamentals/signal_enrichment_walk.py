"""Per-ticker signal-enrichment walk (spec §19/§20/§21). Pure — no DB.

Consumes the shared weekly_forming_walk so the §4 series sit on the exact
weekly bars the stored EMAs were computed on. Advances the assembler once
per week on the converged bar; at each signal week it branches off the
week-start snapshot with the forming (week-to-date) bar as of the signal
day, so features are causally correct (no lookahead into the rest of the
signal's week). Inputs are pre-resolved dicts (the DB runner builds them):

  state_by_date     {date: (med, long, longest, atr, uo, lo, regime)} / day
  conv_by_monday    {monday_dt: (...same...)}            converged / week
  signals_by_date   {date: [signal_type, ...]}

§20 — THE BANDS ARE CUT AGAINST THE FORMING-DAY EMAs.

`st` here is state_by_date[day], the EMAs as they stood on the signal day —
the very numbers the signal fired from. NOT conv_by_monday, which holds the
same week's converged (Friday) EMAs. On Tuesday, Friday's EMAs do not exist.
Using them would be a lookahead so quiet that nothing downstream could
detect it: the rows would be complete, plausible, and systematically wrong
in the direction that flatters the signal.

§20 — POLARITY IS PER ROW, SO THE ROWS CANNOT SHARE.

BUY and EXIT_FROM_SELL are scored with upward geometry; SELL and
EXIT_FROM_BUY with downward. Four types can print on one bar, at one price,
against one set of EMAs, and carry four different sets of flags. Each row
therefore gets its own band dict and its own copy of `features` — an object
shared across rows is an aliasing bug waiting for the first caller who
mutates one.

§21 — THE SEQUENCE COUNTS SIGNALS, NOT ROWS.

sequence_numbers() is fed every signal in signals_by_date, including any the
walk then declines to enrich for want of indicator state (warm-up). That is
deliberate: the ordinal describes the signal's place in the week's churn,
which is a fact about the market, not about our ability to compute features
for it.
"""
from __future__ import annotations

from ..domain.signal_enrichment import SignalEnrichment
from .signal_bands import band_flags, polarity_for
from .signal_features import SignalFeatureAssembler
from .signal_sequence import sequence_numbers
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
    # Causal, per signal type. Computed once, up front, from the full signal
    # history for this ticker — the counters look backward only, so this is
    # not a lookahead (see signal_sequence).
    seq = sequence_numbers(signals_by_date)
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
            atrs = asm.atrs()               # {10: .., 15: .., 20: .., ..}
            price = day.close               # the close the signal fired from
            bars = asm.bars_seen()          # gates the long-period ATRs
            dow = day.date.isoweekday()     # Mon=1 .. Sun=7

            for stype in signals_by_date[day.date]:
                # Raises on an unknown type. Better to fail this ticker loudly
                # than to write 40k rows scored with the wrong geometry.
                polarity = polarity_for(stype)
                s = seq[(day.date, stype)]
                rows.append(SignalEnrichment(
                    ticker_id=ticker_id, timeframe=timeframe,
                    signal_date=day.date, signal_type=stype,
                    # dict(feats): one object per row. See the module
                    # docstring — rows must not alias each other.
                    features=dict(feats),
                    feature_spec_version=feature_spec_version,
                    industry_id=industry_id,
                    bands=band_flags(
                        price=price,
                        # st[0:3] is the FORMING-day state. Not conv_by_monday.
                        med_ema=st[0], long_ema=st[1], longest_ema=st[2],
                        atr_by_period=atrs, polarity=polarity,
                    ),
                    atrs=dict(atrs),
                    seq_in_week=s.in_week, seq_in_consec_weeks=s.in_consec_weeks,
                    signal_price=price,
                    band_polarity=polarity,
                    bars_available=bars,
                    day_of_week=dow,
                    # data_is_valid is set by the repo from the final
                    # NULL-mapped row — not here. See the repo docstring.
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
