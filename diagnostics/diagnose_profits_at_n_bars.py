#!/usr/bin/env python3
"""
diagnose_profits_at_n_bars.py -- forward-walk replication with a configurable
entry bar and independent MFE / MAE / ever_in_profit scopes.

Read-only. Writes a CSV. Touches no database tables.

PARAMETERS
----------
  --n N
      Enter at the OPEN of bar N after signal_date. N=1 is production.
      Bar 0 is the signal day; bar 1 is the first bar strictly after it.

  --ever_in_profit {excl_entry_bar,incl_entry_bar}   default excl_entry_bar
  --mfe            {excl_entry_bar,incl_entry_bar}   default excl_entry_bar
  --mae            {excl_entry_bar,incl_entry_bar}   default excl_entry_bar

      incl_entry_bar -- excursion over bars [N .. exit]. The entry bar's own
          high is >= its open (= the fill), so mfe_pct >= 0 ALWAYS and
          ever_in_profit becomes near-vacuous (89.8% observed). Production.
      excl_entry_bar -- over bars [N+1 .. exit]. Ignores the entry bar's own
          intraday range, so ever_in_profit means "a LATER bar traded through
          my entry price" (63.8% observed).

      "Entry bar" always means bar N, for any N. The three flags are
      independent: ever_in_profit derives from the MFE of ITS OWN scope, not
      from --mfe. mae_before_mfe compares mae_bar under --mae against mfe_bar
      under --mfe; mixing scopes mixes that comparison too.

REGIME-CHANGE GUARD (N > 1 only)
--------------------------------
Entering at bar N means observing the closes of bars 1 .. N-1 first. If ANY
signal -- BUY, SELL, EXIT_FROM_BUY, EXIT_FROM_SELL -- fires on one of those
dates, the regime changed before the fill and the trade must not be taken.
Dropped and counted as `regime_change_before_entry`. At N=2 this is exactly
the one-bar-trade population.

--allow-regime-change disables it (measurement only; not a tradable rule).

BAR INDICES
-----------
Absolute within the window: 0 = entry bar, so excl_entry_bar indices start at 1.
Rungs (bars_to_gain_* / bars_to_loss_*) always use the full window from the
entry bar, unchanged from production, regardless of the scope flags.

VERIFY FIRST
------------
    python diagnose_profits_at_n_bars.py --n 1 \
        --ever_in_profit incl_entry_bar --mfe incl_entry_bar \
        --mae incl_entry_bar --all-instruments --validate
"""

from __future__ import annotations

import argparse
import bisect
import os
import statistics
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
from sqlalchemy import create_engine, text

DEFAULT_URL = "postgresql+psycopg2://trip_gui_user@/trip_ema"

LONG, SHORT = "LONG", "SHORT"
NEXT_SIGNAL, DATA_END = "NEXT_SIGNAL", "DATA_END"
_WALK_SIDES = {"BUY": LONG, "SELL": SHORT}
_TOL = 1e-9

EXCL, INCL = "excl_entry_bar", "incl_entry_bar"
_TAG = {EXCL: "excl", INCL: "incl"}

GAIN_RUNGS: list[tuple[str, float]] = [
    (str(n), n / 100.0)
    for n in (1, 3, 5, 7, 9, 11, 13, 15, 17, 19, 21, 23, 25, 27, 29, 31, 33,
              35, 50, 75, 100)
]
LOSS_RUNGS: list[tuple[str, float]] = [
    ("5", 0.05), ("7_and_half", 0.075), ("10", 0.10),
    ("12_and_half", 0.125), ("15", 0.15), ("20", 0.20),
]


@dataclass(frozen=True, slots=True)
class Excursion:
    """MFE/MAE over one scope. Bar indices absolute within the window."""
    mfe_pct: float | None
    mfe_bar: int
    mae_pct: float | None
    mae_bar: int


def _excursion(entry_open: float, h: np.ndarray, l: np.ndarray,
               is_long: bool, offset: int) -> Excursion:
    if h.size == 0:
        return Excursion(None, -1, None, -1)
    if is_long:
        favorable = (h - entry_open) / entry_open
        adverse = (entry_open - l) / entry_open
    else:
        favorable = (entry_open - l) / entry_open
        adverse = (h - entry_open) / entry_open
    return Excursion(
        mfe_pct=float(favorable.max()), mfe_bar=int(favorable.argmax()) + offset,
        mae_pct=float(adverse.max()), mae_bar=int(adverse.argmax()) + offset,
    )


def _ols_slope(ys) -> float | None:
    m = len(ys)
    if m < 2:
        return None
    mean_x = (m - 1) / 2.0
    mean_y = sum(ys) / m
    num = sum((i - mean_x) * (y - mean_y) for i, y in enumerate(ys))
    den = sum((i - mean_x) ** 2 for i in range(m))
    return None if den == 0 else num / den


def walk(entry_open: float, h, l, c, side: str, atr20,
         scope_ever: str, scope_mfe: str, scope_mae: str) -> dict:
    """Port of trip_ema.domain.forward_walk.walk_signal with per-metric scopes."""
    if side not in (LONG, SHORT):
        raise ValueError(f"side must be LONG or SHORT, got {side!r}")
    if entry_open is None or entry_open <= 0:
        raise ValueError(f"entry_open must be > 0, got {entry_open!r}")
    if len(c) == 0:
        raise ValueError("walk requires at least the entry bar")
    if (c <= 0).any():
        raise ValueError("walk requires positive closes")

    is_long = side == LONG
    m = len(c)
    exit_close = float(c[-1])
    realized_ret = (
        exit_close / entry_open - 1.0 if is_long
        else (entry_open - exit_close) / entry_open
    )

    # --- rungs: full window from the entry bar, production semantics ------
    def _first_touch(frac: float, gain: bool) -> int:
        if is_long == gain:
            level = entry_open * (1.0 + frac)
            idx = np.flatnonzero(h >= level - level * _TOL)
        else:
            level = entry_open * (1.0 - frac)
            idx = np.flatnonzero(l <= level + level * _TOL)
        return int(idx[0]) if idx.size else -1

    bars_to_gain = {lab: _first_touch(f, True) for lab, f in GAIN_RUNGS}
    bars_to_loss = {lab: _first_touch(f, False) for lab, f in LOSS_RUNGS}

    # --- excursions: both scopes always computed --------------------------
    ex = {
        INCL: _excursion(entry_open, h, l, is_long, 0),
        EXCL: _excursion(entry_open, h[1:], l[1:], is_long, 1),
    }

    mfe_pct, mfe_bar = ex[scope_mfe].mfe_pct, ex[scope_mfe].mfe_bar
    mae_pct, mae_bar = ex[scope_mae].mae_pct, ex[scope_mae].mae_bar

    ever_mfe = ex[scope_ever].mfe_pct
    ever_in_profit = ever_mfe is not None and ever_mfe > 0.0

    mae_before_mfe = mae_bar >= 0 and mfe_bar >= 0 and mae_bar < mfe_bar
    same_bar_extremes = mae_bar >= 0 and mae_bar == mfe_bar

    if atr20 not in (None, 0) and not (isinstance(atr20, float) and np.isnan(atr20)):
        mfe_atr20 = None if mfe_pct is None else mfe_pct * entry_open / atr20
        mae_atr20 = None if mae_pct is None else mae_pct * entry_open / atr20
    else:
        mfe_atr20 = mae_atr20 = None

    time_underwater_bars = int(
        (c < entry_open).sum() if is_long else (c > entry_open).sum()
    )

    closes = [float(v) for v in c]
    slope = _ols_slope(closes)
    slope_per_bar = None if slope is None else slope / entry_open

    rets = [closes[i] / closes[i - 1] - 1.0 for i in range(1, m)]
    if len(rets) >= 2:
        realized_vol = statistics.stdev(rets)
        sharpe_in_window = (
            statistics.fmean(rets) / realized_vol if realized_vol > 0 else None
        )
    else:
        realized_vol = sharpe_in_window = None

    capture_ratio = realized_ret / mfe_pct if mfe_pct and mfe_pct > 0 else None

    out = {
        "entry_open": entry_open, "exit_close": exit_close,
        "realized_ret": realized_ret,
        "mfe_pct": mfe_pct, "mfe_bar": mfe_bar,
        "mae_pct": mae_pct, "mae_bar": mae_bar,
        "ever_in_profit": ever_in_profit,
        "mae_before_mfe": mae_before_mfe,
        "same_bar_extremes": same_bar_extremes,
        "mfe_atr20": mfe_atr20, "mae_atr20": mae_atr20,
        "time_underwater_bars": time_underwater_bars,
        "slope_per_bar": slope_per_bar, "realized_vol": realized_vol,
        "sharpe_in_window": sharpe_in_window, "capture_ratio": capture_ratio,
    }
    for scope, e in ex.items():
        t = _TAG[scope]
        out[f"mfe_pct_{t}"] = e.mfe_pct
        out[f"mfe_bar_{t}"] = e.mfe_bar
        out[f"mae_pct_{t}"] = e.mae_pct
        out[f"mae_bar_{t}"] = e.mae_bar
        out[f"ever_in_profit_{t}"] = e.mfe_pct is not None and e.mfe_pct > 0.0
    for lab, v in bars_to_gain.items():
        out[f"bars_to_gain_{lab}_pct"] = v
        out[f"hit_gain_{lab}_pct"] = v != -1
    for lab, v in bars_to_loss.items():
        out[f"bars_to_loss_{lab}_pct"] = v
        out[f"hit_loss_{lab}_pct"] = v != -1
    return out


SQL_SIGNALS = text("""
SELECT e.signal_enrichment_id, e.ticker_id, e.timeframe, e.signal_date,
       e.signal_type, e.atr20
  FROM signal_enrichment e
  JOIN tickers k ON k.ticker_id = e.ticker_id
 WHERE (:eq_only = false OR k.instrument_type = 'EQ')
 ORDER BY e.ticker_id, e.timeframe, e.signal_date, e.signal_enrichment_id
""")

SQL_BARS = text("""
SELECT o.ticker_id, (o.bar_timestamp AT TIME ZONE :zone)::date AS d,
       o.open, o.high, o.low, o.close
  FROM ohlcv_bars o
  JOIN tickers k ON k.ticker_id = o.ticker_id
 WHERE o.timeframe = '1D'
   AND (:eq_only = false OR k.instrument_type = 'EQ')
 ORDER BY o.ticker_id, o.bar_timestamp
""")

SQL_CENSOR = text("""
SELECT ticker_id, ex_date FROM nse_corporate_actions
 WHERE censor = true AND ticker_id IS NOT NULL
""")

SQL_LABELS = text("""
SELECT signal_enrichment_id, side, entry_date, exit_date, bars_in_window,
       realized_ret, mfe_pct, mae_pct, mfe_bar, mae_bar,
       ever_in_profit, mae_before_mfe, window_censored
  FROM signal_labels WHERE forward_walk_done
""")


def run_walks(sig_df, bars_df, censor, *, n: int, check_regime: bool,
              scope_ever: str, scope_mfe: str, scope_mae: str) -> pd.DataFrame:
    packs: dict[int, tuple] = {}
    for tid, grp in bars_df.groupby("ticker_id", sort=False):
        packs[tid] = (
            grp["d"].tolist(),
            grp["open"].to_numpy(dtype=float), grp["high"].to_numpy(dtype=float),
            grp["low"].to_numpy(dtype=float), grp["close"].to_numpy(dtype=float),
        )

    rows: list[dict] = []
    skipped = {"no_bars": 0, "not_buy_sell": 0, "past_data_end": 0,
               "entry_beyond_data": 0, "regime_change_before_entry": 0,
               "window_ends_before_entry": 0, "bad_prices": 0}

    for (tid, _tf), sigs in sig_df.groupby(["ticker_id", "timeframe"], sort=False):
        pack = packs.get(tid)
        if pack is None:
            skipped["no_bars"] += len(sigs)
            continue
        bar_dates, o, h, l, c = pack
        nbars = len(bar_dates)
        last_bar_date = bar_dates[-1]
        cen = censor.get(tid, [])

        recs = sigs.to_dict("records")
        total = len(recs)
        for i, sig in enumerate(recs):
            side = _WALK_SIDES.get(sig["signal_type"])
            if side is None:
                skipped["not_buy_sell"] += 1
                continue

            entry_idx = bisect.bisect_right(bar_dates, sig["signal_date"])
            if entry_idx >= nbars:
                skipped["past_data_end"] += 1
                continue

            start_idx = entry_idx + (n - 1)
            if start_idx >= nbars:
                skipped["entry_beyond_data"] += 1
                continue

            # --- regime-change guard -------------------------------------
            # Bars entry_idx .. start_idx-1 close BEFORE the fill at bar N's
            # open. A signal of ANY type on one of those dates means the
            # regime flipped before we committed.
            if n > 1 and check_regime and i + 1 < total:
                if recs[i + 1]["signal_date"] <= bar_dates[start_idx - 1]:
                    skipped["regime_change_before_entry"] += 1
                    continue
            # --------------------------------------------------------------

            if i + 1 < total:
                n_date = recs[i + 1]["signal_date"]
                exit_reason, right_censored = NEXT_SIGNAL, False
            else:
                n_date = last_bar_date
                exit_reason, right_censored = DATA_END, True

            exit_idx = bisect.bisect_right(bar_dates, n_date) - 1
            if exit_idx < start_idx:
                skipped["window_ends_before_entry"] += 1
                continue

            entry_date, exit_date = bar_dates[start_idx], bar_dates[exit_idx]
            is_cen = any(entry_date <= d <= exit_date for d in cen)

            base = {
                "signal_enrichment_id": sig["signal_enrichment_id"],
                "ticker_id": tid, "signal_date": sig["signal_date"], "side": side,
                "entry_date": entry_date, "exit_date": exit_date,
                "exit_reason": exit_reason,
                "bars_in_window": exit_idx - start_idx + 1,
                "window_censored": bool(is_cen or right_censored),
            }
            if is_cen:
                rows.append(base)
                continue

            sl = slice(start_idx, exit_idx + 1)
            try:
                base.update(walk(float(o[start_idx]), h[sl], l[sl], c[sl],
                                 side, sig["atr20"],
                                 scope_ever, scope_mfe, scope_mae))
            except ValueError:
                skipped["bad_prices"] += 1
                continue
            rows.append(base)

    print("  skipped:", file=sys.stderr)
    for k, v in skipped.items():
        if v:
            print(f"    {k:<30} {v:,}", file=sys.stderr)
    return pd.DataFrame(rows)


def validate(df: pd.DataFrame, engine) -> int:
    with engine.connect() as conn:
        db = pd.read_sql(SQL_LABELS, conn)
    m = df.merge(db, on="signal_enrichment_id", how="outer",
                 suffixes=("_new", "_db"), indicator=True)
    print("\n=== VALIDATION vs signal_labels ===")
    print(f"  in both      : {(m['_merge'] == 'both').sum():,}")
    print(f"  computed only: {(m['_merge'] == 'left_only').sum():,}")
    print(f"  db only      : {(m['_merge'] == 'right_only').sum():,}")

    b = m[m["_merge"] == "both"]
    bad = 0
    for col in ("realized_ret", "mfe_pct", "mae_pct"):
        x, y = b[f"{col}_new"], b[f"{col}_db"]
        both_null = x.isna() & y.isna()
        d = (~both_null) & ~np.isclose(x, y, rtol=1e-9, atol=1e-9, equal_nan=True)
        print(f"  {col:<22} mismatched: {int(d.sum()):,}")
        bad += int(d.sum())
    # Integer-valued columns MUST be compared numerically, never as strings.
    # An outer merge with any unmatched row on either side promotes that side
    # to float64, so str(145) != str(145.0) and every row flags. That bug
    # reported bars_in_window as 193,548 of 193,548 mismatched.
    for col in ("bars_in_window", "mfe_bar", "mae_bar"):
        x = pd.to_numeric(b[f"{col}_new"], errors="coerce")
        y = pd.to_numeric(b[f"{col}_db"], errors="coerce")
        both_null = x.isna() & y.isna()
        d = (~both_null) & ~np.isclose(x, y, rtol=0.0, atol=0.5, equal_nan=True)
        print(f"  {col:<22} mismatched: {int(d.sum()):,}")
        bad += int(d.sum())
    for col in ("ever_in_profit", "mae_before_mfe", "side", "entry_date",
                "exit_date", "window_censored"):
        d = b[f"{col}_new"].astype(str) != b[f"{col}_db"].astype(str)
        print(f"  {col:<22} mismatched: {int(d.sum()):,}")
        bad += int(d.sum())

    ed = b["exit_date_new"].astype(str) != b["exit_date_db"].astype(str)
    if int(ed.sum()):
        sub = b[ed]
        longer = int((sub["exit_date_new"].astype(str)
                      > sub["exit_date_db"].astype(str)).sum())
        print(f"\n  of {int(ed.sum()):,} exit_date mismatches, {longer:,} run LONGER")
        print("  -> exit_date drives bars_in_window and every excursion;")
        print("     stale signal_labels vs re-derived signals is the usual cause.")

    extra = int((m["_merge"] != "both").sum())
    print(f"\n  TOTAL discrepancies: {bad + extra:,}")
    if bad + extra == 0:
        print("  CLEAN -- geometry reproduces production exactly.")
    return bad + extra


def analyse(df: pd.DataFrame, cfg: str) -> None:
    d = df[(~df["window_censored"].astype(bool)) & df["realized_ret"].notna()].copy()

    # A 1-bar window has NO bars after the entry bar, so under excl_entry_bar
    # the excursion is UNDEFINED (mfe_pct is None), not zero. Pooling those
    # with "post-entry bars existed and none exceeded entry" is wrong: it put
    # ~24.3k one-bar trades into the 0% bucket and made a cell that should be
    # 0.0% win report 7.4%, since the entry bar itself can close up.
    d["mfe_defined"] = d["mfe_pct"].notna()

    print(f"\n{cfg}")
    print(f"(analysis set: {len(d):,} rows; censored + DATA_END excluded)")
    n_undef = int((~d["mfe_defined"]).sum())
    if n_undef:
        print(f"(mfe undefined -- 1-bar windows, no post-entry bar: {n_undef:,} "
              f"= {100 * n_undef / len(d):.1f}%; reported separately below)")

    print("\n=== (a) ever_in_profit by side ===")
    hdr = (f"{'side':<7}{'n':>9}{'undef%':>9}{'ever_PRIMARY%':>15}"
           f"{'ever_excl%':>12}{'ever_incl%':>12}{'end_profit%':>13}"
           f"{'giveback_pp':>13}")
    print(hdr)
    print("-" * len(hdr))
    for side, g in d.groupby("side"):
        und = 100 * (~g["mfe_defined"]).mean()
        pri = 100 * g["ever_in_profit"].mean()
        ex = 100 * g["ever_in_profit_excl"].mean()
        inc = 100 * g["ever_in_profit_incl"].mean()
        end = 100 * (g["realized_ret"] > 0).mean()
        print(f"{side:<7}{len(g):>9,}{und:>9.1f}{pri:>15.1f}{ex:>12.1f}"
              f"{inc:>12.1f}{end:>13.1f}{pri - end:>13.1f}")

    print("\n=== (b) ever_in_profit x mae_before_mfe ===")
    print("    tie_pct = share of cell where mae_bar == mfe_bar")
    print("    defined = false -> 1-bar window, excursion undefined")
    hdr = (f"{'side':<7}{'defined':>9}{'ever':>7}{'mae_b4':>8}{'n':>9}{'pct':>8}"
           f"{'tie_pct':>9}{'win%':>8}{'mean%':>9}{'med%':>9}")
    print(hdr)
    print("-" * len(hdr))
    for side, g in d.groupby("side"):
        for (de, ep, mb), cell in g.groupby(
                ["mfe_defined", "ever_in_profit", "mae_before_mfe"]):
            print(f"{side:<7}{str(de):>9}{str(ep):>7}{str(mb):>8}{len(cell):>9,}"
                  f"{100 * len(cell) / len(g):>8.1f}"
                  f"{100 * cell['same_bar_extremes'].mean():>9.1f}"
                  f"{100 * (cell['realized_ret'] > 0).mean():>8.1f}"
                  f"{100 * cell['realized_ret'].mean():>9.2f}"
                  f"{100 * cell['realized_ret'].median():>9.2f}")

    print("\n=== (c) mfe_pct frequency distribution ===")
    mp = d["mfe_pct"] * 100
    d["bk"] = np.where(
        mp.isna(), -1,
        np.where(mp <= 0, 0, np.where(mp > 30, 16, np.ceil(mp / 2.0))),
    ).astype(int)

    def lbl(b: int) -> str:
        if b == -1:
            return "undef"
        return "0%" if b == 0 else "30%+" if b == 16 else f"{(b - 1) * 2:>2}-{b * 2}%"

    hdr = (f"{'side':<7}{'mfe_bucket':>12}{'n':>9}{'pct':>8}{'cum_pct':>10}"
           f"{'win%':>8}{'mean_ret%':>12}{'contrib_pp':>12}")
    print(hdr)
    print("-" * len(hdr))
    for side, g in d.groupby("side"):
        tot, cum = len(g), 0
        for b, cell in g.groupby("bk"):
            cum += len(cell)
            share = len(cell) / tot
            mean_r = 100 * cell["realized_ret"].mean()
            print(f"{side:<7}{lbl(b):>12}{len(cell):>9,}{100 * share:>8.2f}"
                  f"{100 * cum / tot:>10.2f}"
                  f"{100 * (cell['realized_ret'] > 0).mean():>8.1f}"
                  f"{mean_r:>12.2f}{share * mean_r:>12.2f}")
        tm = 100 * g["realized_ret"].mean()
        print(f"{side:<7}{'TOTAL':>12}{tot:>9,}{100.0:>8.2f}{'':>10}"
              f"{100 * (g['realized_ret'] > 0).mean():>8.1f}{tm:>12.2f}{tm:>12.2f}")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-n", "--n", type=int, default=2, dest="n",
                    help="enter at open of bar n after signal_date (1 = production)")
    ap.add_argument("--ever_in_profit", choices=[EXCL, INCL], default=EXCL)
    ap.add_argument("--mfe", choices=[EXCL, INCL], default=EXCL)
    ap.add_argument("--mae", choices=[EXCL, INCL], default=EXCL)
    ap.add_argument("--allow-regime-change", action="store_true",
                    help="disable the n>1 regime guard (measurement only)")
    ap.add_argument("--validate", action="store_true")
    ap.add_argument("--zone", default="Asia/Kolkata")
    ap.add_argument("--all-instruments", action="store_true",
                    help="include ETFs etc; default is instrument_type='EQ' only")
    ap.add_argument("--out", default=None)
    ap.add_argument("--url", default=os.environ.get("TRIP_DB_URL", DEFAULT_URL))
    args = ap.parse_args()

    if args.n < 1:
        print("--n must be >= 1", file=sys.stderr)
        return 2

    eq_only = not args.all_instruments
    check_regime = not args.allow_regime_change
    guard = "n/a (n=1)" if args.n == 1 else ("on" if check_regime else "OFF")
    cfg = (f"n={args.n}  ever_in_profit={args.ever_in_profit}  mfe={args.mfe}  "
           f"mae={args.mae}  regime_guard={guard}  eq_only={eq_only}")
    print(cfg)

    out = Path(args.out or (
        f"diagnostics/profits_at_bar{args.n}"
        f"_ever-{_TAG[args.ever_in_profit]}"
        f"_mfe-{_TAG[args.mfe]}"
        f"_mae-{_TAG[args.mae]}.csv"))
    out.parent.mkdir(parents=True, exist_ok=True)

    engine = create_engine(args.url)
    with engine.connect() as conn:
        print("loading signals...", file=sys.stderr)
        sig_df = pd.read_sql(SQL_SIGNALS, conn, params={"eq_only": eq_only})
        print(f"  {len(sig_df):,} signals", file=sys.stderr)
        print("loading bars...", file=sys.stderr)
        bars_df = pd.read_sql(SQL_BARS, conn,
                              params={"zone": args.zone, "eq_only": eq_only})
        print(f"  {len(bars_df):,} bars", file=sys.stderr)
        cen_df = pd.read_sql(SQL_CENSOR, conn)

    censor: dict[int, list[date]] = {
        int(t): g["ex_date"].tolist() for t, g in cen_df.groupby("ticker_id")
    }

    print("walking...", file=sys.stderr)
    df = run_walks(sig_df, bars_df, censor, n=args.n, check_regime=check_regime,
                   scope_ever=args.ever_in_profit, scope_mfe=args.mfe,
                   scope_mae=args.mae)
    if df.empty:
        print("no walks produced", file=sys.stderr)
        return 1
    print(f"  {len(df):,} walk rows")

    df.to_csv(out, index=False)
    print(f"written: {out}")

    rc = 0
    if args.validate:
        if args.n != 1 or {args.ever_in_profit, args.mfe, args.mae} != {INCL}:
            print("\nNOTE: production parity requires --n 1 with all three scopes"
                  " = incl_entry_bar, plus --all-instruments for set parity")
        rc = 1 if validate(df, engine) else 0

    analyse(df, cfg)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
