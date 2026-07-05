"""State-carry indicator engine — O(weeks+days) daily-grain compute.

A single stateful engine that carries every between-bar quantity, so the
weekly indicators can be sampled once per trading day without recomputing
the whole history each day. Replaces the pandas recompute under
`compute_daily_grain_states` / `_enrichment`; the pandas layer moves to
`archive/` and remains the parity oracle.

Correctness: this is a bar-faithful port of the pandas path
(`compute_indicator_series` + `compute_bar_states` + `_compute_regime_
series`). The numeric layer reuses the already-validated stateful
`VolumeWeightedTripleEMA`; ATR/HLRA/mean-dist/turns are simple carried
recurrences; the regime + pattern rules are the SHARED functions
(`..regime.compute_regime`, `pattern_rules`), so business logic cannot
drift. The only replicated thing is arithmetic, gated by a parity harness.

Walk per ticker (the O(weeks+days) win):

    for W in weeks:
        snap = engine.snapshot()
        for D in trading_days(W):
            engine.restore(snap)
            emit(engine.apply(forming_bar(W, as_of=D)))   # prorated volume
        engine.restore(snap)
        engine.apply(real_week_bar(W))                    # advance past W

Each day branches off the same snapshot (not chained), matching the
pandas array `[completed weeks < W] + [forming(D)]`.
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from ..domain import OHLCVBar
from ..indicators.aggregation import _monday_of, daily_to_weekly
from ..indicators.vw_ema import VolumeWeightedTripleEMA
from ..regime import BarInputs, Regime, compute_regime
from .market_session import MarketSession
from .pattern_rules import bars_and_pct_since_turn, pattern_label

_LEGS = ("med", "long", "longest")


def _isnan(x) -> bool:
    return x is None or (isinstance(x, float) and math.isnan(x))


@dataclass
class _Track:
    """Per-EMA pattern/turn tracking state (mirror of _PerEMATrackState)."""
    last_buy_value: float | None = None
    second_last_buy_value: float | None = None
    last_sell_value: float | None = None
    second_last_sell_value: float | None = None
    last_buy_bar_idx: int | None = None
    last_sell_bar_idx: int | None = None
    last_buy_label: str = "NA"
    last_sell_label: str = "NA"

    def snapshot(self) -> tuple:
        return (self.last_buy_value, self.second_last_buy_value,
                self.last_sell_value, self.second_last_sell_value,
                self.last_buy_bar_idx, self.last_sell_bar_idx,
                self.last_buy_label, self.last_sell_label)

    def restore(self, s: tuple) -> None:
        (self.last_buy_value, self.second_last_buy_value,
         self.last_sell_value, self.second_last_sell_value,
         self.last_buy_bar_idx, self.last_sell_bar_idx,
         self.last_buy_label, self.last_sell_label) = s


class StatefulIndicatorEngine:
    """Carries all between-bar state; `apply(bar)` advances one bar and
    returns (state_dict, enrichment_dict) for that bar."""

    def __init__(self, params, *, synthetic_atr_for_index: bool = False):
        legp = {"med": params.med, "long": params.long,
                "longest": params.longest}
        self._ema = {
            leg: VolumeWeightedTripleEMA(
                base=p.base_len, s1=p.first_smoothing, s2=p.hull_n,
                final=p.final_smoothing,
                volume_sma_length=params.volume_smoothing,
            )
            for leg, p in legp.items()
        }
        self._atr_len = params.atr_length
        self._inner = params.atr_bands.inner_multiplier
        self._mid = params.atr_bands.mid_multiplier
        self._outer = params.atr_bands.outer_multiplier
        self._hlra_len = params.hl_range_avg_length
        self._synth = synthetic_atr_for_index

        # ATR (Wilder RMA of true range)
        self._atr_rma: float | None = None
        self._atr_prev_close: float | None = None
        # HLRA ring
        self._hlra: deque[float] = deque(maxlen=self._hlra_len)
        # mean-dist rings (52) per leg
        self._dist: dict[str, deque] = {
            leg: deque(maxlen=52) for leg in _LEGS
        }
        # turn trackers: last two EMA values per leg
        self._ema_p1: dict[str, float | None] = {leg: None for leg in _LEGS}
        self._ema_p2: dict[str, float | None] = {leg: None for leg in _LEGS}
        # pattern/turn tracking per leg
        self._trk: dict[str, _Track] = {leg: _Track() for leg in _LEGS}
        # regime state
        self._regime: Regime = Regime.NEUTRAL
        self._r_prev_close: float | None = None
        self._r_prev_low_env: float | None = None
        self._r_prev_up_env: float | None = None
        # bar index (== forming bar's position; matches pandas `i`)
        self._i = 0

    # -------------------- snapshot / restore -------------------- #

    def snapshot(self) -> tuple:
        return (
            {leg: self._ema[leg]._snapshot() for leg in _LEGS},
            self._atr_rma, self._atr_prev_close,
            tuple(self._hlra),
            {leg: tuple(self._dist[leg]) for leg in _LEGS},
            dict(self._ema_p1), dict(self._ema_p2),
            {leg: self._trk[leg].snapshot() for leg in _LEGS},
            self._regime, self._r_prev_close,
            self._r_prev_low_env, self._r_prev_up_env,
            self._i,
        )

    def restore(self, s: tuple) -> None:
        (emas, self._atr_rma, self._atr_prev_close, hlra, dist,
         p1, p2, trks, self._regime, self._r_prev_close,
         self._r_prev_low_env, self._r_prev_up_env, self._i) = s
        for leg in _LEGS:
            self._ema[leg]._restore(emas[leg])
            self._hlra.clear(); self._hlra.extend(hlra)
            self._dist[leg].clear(); self._dist[leg].extend(dist[leg])
            self._trk[leg].restore(trks[leg])
        self._ema_p1 = dict(p1)
        self._ema_p2 = dict(p2)

    # -------------------- per-bar advance -------------------- #

    def _true_range(self, high: float, low: float, close: float) -> float:
        if self._synth:
            if self._atr_prev_close is None:
                return float("nan")
            return abs(close - self._atr_prev_close) * math.sqrt(2.0)
        if self._atr_prev_close is None:
            return high - low  # first bar: prev_close undefined
        pc = self._atr_prev_close
        return max(high - low, abs(high - pc), abs(low - pc))

    def apply(self, high: float, low: float, close: float,
              volume: float) -> tuple[dict, dict]:
        i = self._i

        # --- EMAs (stateful, already parity-matched to pandas) ---
        ema_v = {leg: self._ema[leg].update(close, volume) for leg in _LEGS}

        # --- ATR = RMA(true_range, atr_len), alpha = 1/n ---
        tr = self._true_range(high, low, close)
        if self._atr_rma is None:
            if not _isnan(tr):
                self._atr_rma = tr
        elif not _isnan(tr):
            a = 1.0 / self._atr_len
            self._atr_rma = a * tr + (1.0 - a) * self._atr_rma
        atr_v = self._atr_rma if self._atr_rma is not None else float("nan")
        self._atr_prev_close = close

        # --- HLRA = SMA(high-low, 26) ---
        self._hlra.append(high - low)
        hlra_v = (sum(self._hlra) / self._hlra_len
                  if len(self._hlra) == self._hlra_len else float("nan"))

        # --- bands (max/min of EMAs +/- mult*ATR) ---
        emas_ok = all(not _isnan(ema_v[leg]) for leg in _LEGS)
        if emas_ok and not _isnan(atr_v):
            hi = max(ema_v[leg] for leg in _LEGS)
            lo = min(ema_v[leg] for leg in _LEGS)
            bands = {
                "upper_inner": hi + self._inner * atr_v,
                "lower_inner": lo - self._inner * atr_v,
                "upper_mid": hi + self._mid * atr_v,
                "lower_mid": lo - self._mid * atr_v,
                "upper_outer": hi + self._outer * atr_v,
                "lower_outer": lo - self._outer * atr_v,
            }
        else:
            bands = {k: float("nan") for k in (
                "upper_inner", "lower_inner", "upper_mid",
                "lower_mid", "upper_outer", "lower_outer")}

        # --- dist + mean-dist (52) per leg ---
        dist_v: dict[str, float] = {}
        mean_dist_v: dict[str, float] = {}
        for leg in _LEGS:
            if (not _isnan(hlra_v) and hlra_v > 0
                    and not _isnan(ema_v[leg])):
                d = abs(close - ema_v[leg]) / hlra_v
            else:
                d = float("nan")
            dist_v[leg] = d
            self._dist[leg].append(d)
            ring = self._dist[leg]
            if len(ring) == 52 and not any(_isnan(x) for x in ring):
                mean_dist_v[leg] = sum(ring) / 52.0
            else:
                mean_dist_v[leg] = float("nan")

        # --- turns: buy = ema>ema[1] and ema[1]<ema[2] ---
        buy: dict[str, bool] = {}
        sell: dict[str, bool] = {}
        for leg in _LEGS:
            p1, p2 = self._ema_p1[leg], self._ema_p2[leg]
            e = ema_v[leg]
            if p1 is None or p2 is None:
                buy[leg] = sell[leg] = False
            else:
                buy[leg] = (e > p1) and (p1 < p2)
                sell[leg] = (e < p1) and (p1 > p2)

        # --- pattern labels + bars/pct since turn (PRE-update reads) ---
        labels: dict[str, str] = {}
        bars_since: dict[str, int | None] = {}
        pct_since: dict[str, float | None] = {}
        for leg in _LEGS:
            trk = self._trk[leg]
            bs_buy = (i - trk.last_buy_bar_idx
                      if trk.last_buy_bar_idx is not None else None)
            bs_sell = (i - trk.last_sell_bar_idx
                       if trk.last_sell_bar_idx is not None else None)
            labels[leg] = pattern_label(
                buy[leg], sell[leg], close,
                trk.last_buy_value, trk.second_last_buy_value,
                trk.last_sell_value, trk.second_last_sell_value,
                trk.last_buy_label, trk.last_sell_label,
                bars_since_last_buy=bs_buy, bars_since_last_sell=bs_sell,
            )
            bars_since[leg], pct_since[leg] = bars_and_pct_since_turn(
                ema_now=ema_v[leg], prev_ema=self._ema_p1[leg],
                current_idx=i, current_close=close,
                last_buy_bar_idx=trk.last_buy_bar_idx,
                last_buy_close=trk.last_buy_value,
                last_sell_bar_idx=trk.last_sell_bar_idx,
                last_sell_close=trk.last_sell_value,
            )

        # --- regime (sticky on NaN; matches _compute_regime_series) ---
        need_nan = (_isnan(close) or not emas_ok
                    or any(_isnan(bands[k]) for k in
                           ("upper_inner", "lower_inner",
                            "upper_outer", "lower_outer")))
        if need_nan:
            regime_out = int(self._regime)
        else:
            up_env = max(ema_v[leg] for leg in _LEGS)
            low_env = min(ema_v[leg] for leg in _LEGS)
            cur = compute_regime(
                previous_regime=self._regime,
                previous_close=self._r_prev_close,
                previous_lower_envelope=self._r_prev_low_env,
                previous_upper_envelope=self._r_prev_up_env,
                bar=BarInputs(
                    close=close,
                    upper_inner=bands["upper_inner"],
                    lower_inner=bands["lower_inner"],
                    upper_outer=bands["upper_outer"],
                    lower_outer=bands["lower_outer"],
                    upper_envelope=up_env, lower_envelope=low_env,
                ),
            )
            regime_out = int(cur)
            self._regime = cur
            self._r_prev_close = close
            self._r_prev_low_env = low_env
            self._r_prev_up_env = up_env

        # --- pierce flags ---
        pu = (not _isnan(bands["upper_mid"])
              and high >= bands["upper_mid"] >= low)
        pl = (not _isnan(bands["lower_mid"])
              and high >= bands["lower_mid"] >= low)

        # === state update (AFTER reads) — mirror walk order exactly ===
        for leg in _LEGS:
            trk = self._trk[leg]
            if buy[leg]:
                trk.second_last_buy_value = trk.last_buy_value
                trk.last_buy_value = close
                trk.last_buy_bar_idx = i
            if sell[leg]:
                trk.second_last_sell_value = trk.last_sell_value
                trk.last_sell_value = close
                trk.last_sell_bar_idx = i
            lab = labels[leg]
            if lab != "NA":
                if "VALLEY" in lab:
                    trk.last_buy_label = lab
                else:
                    trk.last_sell_label = lab
            # advance turn tracker (last two EMAs)
            self._ema_p2[leg] = self._ema_p1[leg]
            self._ema_p1[leg] = ema_v[leg]

        self._i += 1

        state = {
            "med_ema": ema_v["med"], "long_ema": ema_v["long"],
            "longest_ema": ema_v["longest"], "atr": atr_v,
            **{f"{k}_band" if False else k: v for k, v in bands.items()},
            "display_regime": regime_out,
        }
        # rename band keys to *_band
        state = {
            "med_ema": ema_v["med"], "long_ema": ema_v["long"],
            "longest_ema": ema_v["longest"], "atr": atr_v,
            "upper_inner_band": bands["upper_inner"],
            "lower_inner_band": bands["lower_inner"],
            "upper_mid_band": bands["upper_mid"],
            "lower_mid_band": bands["lower_mid"],
            "upper_outer_band": bands["upper_outer"],
            "lower_outer_band": bands["lower_outer"],
            "display_regime": regime_out,
        }
        enrich = {
            "pierces_upper_mid": bool(pu), "pierces_lower_mid": bool(pl),
            "dist_to_med_in_hl_units": _n(dist_v["med"]),
            "dist_to_long_in_hl_units": _n(dist_v["long"]),
            "dist_to_longest_in_hl_units": _n(dist_v["longest"]),
            "mean_dist_to_med_in_hl_units": _n(mean_dist_v["med"]),
            "mean_dist_to_long_in_hl_units": _n(mean_dist_v["long"]),
            "mean_dist_to_longest_in_hl_units": _n(mean_dist_v["longest"]),
            "bars_since_med_turn": bars_since["med"],
            "bars_since_long_turn": bars_since["long"],
            "bars_since_longest_turn": bars_since["longest"],
            "pct_change_since_med_turn": pct_since["med"],
            "pct_change_since_long_turn": pct_since["long"],
            "pct_change_since_longest_turn": pct_since["longest"],
            "med_pattern_label": labels["med"],
            "long_pattern_label": labels["long"],
            "longest_pattern_label": labels["longest"],
            "med_turn_buy": bool(buy["med"]), "med_turn_sell": bool(sell["med"]),
            "long_turn_buy": bool(buy["long"]), "long_turn_sell": bool(sell["long"]),
            "longest_turn_buy": bool(buy["longest"]),
            "longest_turn_sell": bool(sell["longest"]),
        }
        return state, enrich


def _n(v):
    return None if _isnan(v) else float(v)


def _is_state_warmup(state: dict) -> bool:
    return (_isnan(state["med_ema"]) or _isnan(state["long_ema"])
            or _isnan(state["longest_ema"]) or _isnan(state["atr"]))


# ==================================================================
# Daily-grain walk — O(weeks + days). Produces state + enrichment in
# ONE pass (single-pass win). Bit-faithful to the pandas daily-grain.
# ==================================================================

def compute_daily_grain_via_engine(
    daily_bars: list[OHLCVBar],
    *,
    session: MarketSession,
    now: datetime,
    params,
    target_days: list[date] | None = None,
    synthetic_atr_for_index: bool = False,
    constant_volume: float | None = None,
) -> tuple[list[dict], list[dict]]:
    """Return (state_rows, enrich_rows), one per emitted trading day.

    Walk: for each week, snapshot the converged state, branch each day
    off it with a prorated forming bar, then advance past the week with
    its real full-volume bar. O(weeks+days) — not O(weeks*days).
    """
    from collections import defaultdict as _dd

    if not daily_bars:
        return [], []

    zone = session.zone
    now_local = now.astimezone(zone)

    completed = daily_to_weekly(
        daily_bars, include_partial_last_week=False, tz=session.tz,
    )
    real_by_monday = {wb.timestamp: wb for wb in completed}

    by_week: dict = _dd(list)
    for b in daily_bars:
        w = _monday_of(b.timestamp, tz=session.tz)
        by_week[w].append((b.timestamp.astimezone(zone).date(), b))
    for w in by_week:
        by_week[w].sort(key=lambda p: p[0])
    weeks_in_order = sorted(by_week.keys())

    tset = set(target_days) if target_days is not None else None

    engine = StatefulIndicatorEngine(
        params, synthetic_atr_for_index=synthetic_atr_for_index,
    )
    state_rows: list[dict] = []
    enrich_rows: list[dict] = []

    for w_monday in weeks_in_order:
        monday_local = w_monday.astimezone(zone).date()
        week_pairs = by_week[w_monday]
        snap = engine.snapshot()

        for (ld, _b) in week_pairs:
            if tset is not None and ld not in tset:
                continue
            week_daily = [b for (l, b) in week_pairs if l <= ld]
            open_ = week_daily[0].open
            hi = max(b.high for b in week_daily)
            lo = min(b.low for b in week_daily)
            cl = week_daily[-1].close
            wtd_vol = sum(b.volume for b in week_daily)
            if constant_volume is not None:
                vol = constant_volume
            else:
                vol = session.imputed_weekly_volume(
                    wtd_vol, monday_local, ld, now_local,
                )
            engine.restore(snap)
            s, e = engine.apply(hi, lo, cl, vol)
            if _is_state_warmup(s):
                continue
            s = dict(s); s["trade_date"] = ld; s["bar_timestamp"] = w_monday
            e = dict(e); e["trade_date"] = ld; e["bar_timestamp"] = w_monday
            state_rows.append(s)
            enrich_rows.append(e)

        # advance past the week with its real full-volume bar
        engine.restore(snap)
        real = real_by_monday.get(w_monday)
        if real is not None:
            adv_vol = (constant_volume if constant_volume is not None
                       else real.volume)
            engine.apply(real.high, real.low, real.close, adv_vol)

    return state_rows, enrich_rows
