#!/usr/bin/env python3
"""Reconcile yfinance .splits against the stored ohlcv_bars for EVERY active NSE
ticker, to size the real corporate_actions backlog.

Logic per recorded split (yfinance split value v; 2:1 -> v=2.0, 1:20 -> v=0.05):
  observed = stored_close_after / stored_close_before across the effective date.
    |observed - 1|        <= 15%  -> ADJUSTED_OK   (auto_adjust already handled it; LEAVE ALONE)
    |observed - 1/v|      <= 15%  -> SEAM_PRESENT   (auto_adjust MISSED it -> insert & apply)
    close_before/after missing     -> NO_BARS       (predates stored history)
    else                           -> BOTCHED        (auto_adjust wrong, e.g. SUVEN -> re-source)

Only the non-ADJUSTED_OK rows are printed as CSV — that's the backlog. A verdict
tally goes to stderr. Read-only (trip_gui_user). One yfinance call + one DB query
per ticker (bars loaded once, all splits reconciled in-memory).

Run:  python reconcile_splits.py > split_backlog.csv    # progress on stderr
"""
from __future__ import annotations

import sys
import time

import pandas as pd
import yfinance as yf
from sqlalchemy import create_engine, text

# libpq local socket + peer auth (same as `psql -U trip_gui_user -d trip_ema`).
# Adjust if trip_gui_user needs host/password on your box.
DSN = "postgresql+psycopg2://trip_gui_user@/trip_ema"
SUFFIX = ".NS"
SLEEP = 0.3
ADJ_TOL = 0.15

engine = create_engine(DSN)


def load_tickers() -> list[tuple[int, str]]:
    q = text(
        "SELECT ticker_id, symbol FROM tickers "
        "WHERE exchange = 'NSE' AND is_active "
        "AND instrument_type IN ('EQ', 'ETF') ORDER BY symbol"
    )
    with engine.connect() as c:
        return [(r.ticker_id, r.symbol) for r in c.execute(q).all()]


def load_closes(ticker_id: int) -> pd.Series | None:
    q = text(
        "SELECT (bar_timestamp AT TIME ZONE 'Asia/Kolkata')::date AS d, close "
        "FROM ohlcv_bars WHERE ticker_id = :t AND timeframe = '1D' "
        "ORDER BY bar_timestamp"
    )
    with engine.connect() as c:
        rows = c.execute(q, {"t": ticker_id}).all()
    if not rows:
        return None
    s = pd.Series({r.d: float(r.close) for r in rows})
    s.index = pd.to_datetime(s.index)
    return s.sort_index()


def reconcile(closes: pd.Series, eff, v: float):
    eff = pd.Timestamp(eff)
    before = closes[closes.index < eff]
    after = closes[closes.index >= eff]
    if before.empty or after.empty:
        return None, None, "NO_BARS"
    cb, ca = before.iloc[-1], after.iloc[0]
    if not cb:
        return cb, ca, "NO_BARS"
    obs = ca / cb
    exp_unadj = (1.0 / v) if v else None
    if abs(obs - 1.0) <= ADJ_TOL:
        return cb, ca, "ADJUSTED_OK"
    if exp_unadj and abs(obs - exp_unadj) <= ADJ_TOL * exp_unadj:
        return cb, ca, "SEAM_PRESENT"
    return cb, ca, "BOTCHED"


def to_date(ts):
    t = pd.Timestamp(ts)
    return (t.tz_localize(None) if t.tzinfo else t).date()


def main() -> None:
    tickers = load_tickers()
    print(f"# {len(tickers)} active NSE tickers", file=sys.stderr)
    tally: dict[str, int] = {}
    backlog: list[tuple] = []

    for i, (tid, sym) in enumerate(tickers, 1):
        try:
            splits = yf.Ticker(sym + SUFFIX).splits
        except Exception as e:  # noqa: BLE001
            print(f"# {sym}: splits fetch failed: {type(e).__name__}", file=sys.stderr)
            time.sleep(SLEEP)
            continue

        if splits is not None and len(splits):
            closes = load_closes(tid)
            if closes is not None:
                for ts, v in splits.items():
                    if not v:
                        continue
                    cb, ca, verdict = reconcile(closes, to_date(ts), float(v))
                    tally[verdict] = tally.get(verdict, 0) + 1
                    if verdict != "ADJUSTED_OK":
                        backlog.append((sym, to_date(ts), float(v), cb, ca, verdict))

        time.sleep(SLEEP)
        if i % 100 == 0:
            print(f"# {i}/{len(tickers)}  tally={tally}", file=sys.stderr)

    print("symbol,effective_date,split_value,c_before,c_after,verdict")
    for sym, eff, v, cb, ca, verdict in sorted(backlog, key=lambda r: (r[5], r[0], r[1])):
        cbs = "" if cb is None else f"{cb:.4f}"
        cas = "" if ca is None else f"{ca:.4f}"
        print(f"{sym},{eff},{v:g},{cbs},{cas},{verdict}")

    print(f"# final tally: {tally}", file=sys.stderr)
    print(f"# backlog (non-ADJUSTED_OK): {len(backlog)}", file=sys.stderr)


if __name__ == "__main__":
    main()