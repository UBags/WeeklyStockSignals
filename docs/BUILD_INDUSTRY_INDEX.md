# Building the chain-linked industry index from scratch

End-to-end sequence to build the annual-April, total-mcap, chain-linked industry
indices from 2006. Run in order — each step depends on the previous.

Parameters baked into the design: annual rebalance on **April 1** (holiday →
next trading day), **top 12** constituents (**min 4**, else the industry's
rebalance is skipped), **total market-cap** weighting, market cap = adjusted
close × **current** shares, **chain-linked** continuity (link_factor per
rebalance in `industry_index_period`).

---

## Prerequisites

- Bars populated: `ohlcv_bars` (1d) back to your target start (e.g. 2006-01-02),
  with `adj_close` where available (falls back to `close` per-row).
- Current shares present: `instrument_shares` has a latest row per active EQ
  ticker (the `get_latest` current-shares source).
- Migration `0038_industry_index_period` applied (Commit 1).

---

## Step 0 — Wipe (only for a clean rebuild)

```bash
psql -U trip_etl_user -d trip_ema -f scripts/wipe_industry_index.sql
```

Clears `industry_index` + everything that cascades from it (constituents,
values, periods, `industry_indicator_state`/`enrichment`, `benchmark_stats`).
Those derived tables are rebuilt in Step 4.

---

## Step 1 — Populate market cap (2006 → today)

Adjusted close × current shares, `source=APPROX_TODAYS_SHARES`, per trading day.

```python
from datetime import date
from trip_ema.config import load_config
from trip_ema.persistence.connection import build_engine_from_config
from trip_ema.fundamentals.market_cap_compute import populate_market_cap_range

cfg = load_config(); eng = build_engine_from_config(cfg)
res = populate_market_cap_range(
    eng, "NSE",
    date_from=date(2006, 1, 2), date_to=date.today(),
    use_current_shares_always=True,   # adjusted price × CURRENT shares
    progress_cb=lambda s, m: print(s, m),
)
print(res)   # {'days': N, 'approx': ..., 'skipped_no_close': ..., ...}
```

Large job (≈ tickers × trading days). Idempotent — safe to re-run. Repeat for
other exchanges (BSE/NYSE) if you index them.

---

## Step 2 — Build the rebalance chains (all industries)

Discovers industries with ≥ 4 active EQ tickers, creates an `ANNUAL_APR` index
for each anchored at the first April rebalance, and walks every April date to
today calling `run_rebalance` (each stores its chain-link factor).

```python
from trip_ema.fundamentals.runners import build_all_industry_index_chains

out = build_all_industry_index_chains(
    eng, "NSE", through=date.today(),
    n=12, min_constituents=4,
)
print(len(out["chains"]), "chains;", len(out["failed"]), "failed")
```

---

## Step 3 — Backfill daily index values (all industries)

Walks every trading day per index, writing chain-linked `industry_index_value`
rows (pre-base_date days skipped automatically).

```python
from trip_ema.fundamentals.runners import run_index_value_backfill_all

vb = run_index_value_backfill_all(
    eng, from_date=date(2006, 1, 2), to_date=date.today(), force=True,
)
print(vb["n_ok"], "ok;", vb["n_failed"], "failed")
```

`force=True` overrides the skip-if-recent guard (needed for a full rebuild).

---

## Step 4 — Rebuild the downstream derived tables (cleared by the wipe)

The wipe cascaded through the industry indicator + benchmark tables. Rebuild
them now that the index values exist:

- `industry_indicator_state` / `industry_indicator_enrichment` — via the normal
  industry daily-grain stage (`_stage_industry_state` / `_stage_industry_enrichment`
  in `industry_stages.py`, mode="full"), or a full recompute/backfill run.
- `industry_benchmark_stats` — `run_benchmark_stats_for_all_industries`.

(Exact invocation depends on how you drive those stages operationally — the
backfill pipeline or a direct call. They read `industry_index_value`, so they
must run *after* Step 3.)

---

## Off-schedule rebalance (mid-year delisting / corporate action)

When a constituent is delisted (merger, privatisation), re-pick the basket as of
a chosen date with the no-defaults operator script:

```bash
python scripts/rebalance_industry.py \
    --industry "Specialty Chemicals" \
    --exchange NSE \
    --date 2019-07-15 \
    --n 12 \
    --min-constituents 4 \
    --reason "PIIND delisted (merger) — re-pick basket" \
    --confirm YES
```

It chain-links the new basket to the current index level on that date (same
continuity math as an April rebalance), then re-run Step 3 for that industry
(or `run_index_value_backfill` for the single industry) to refresh values from
the rebalance date forward.

---

## Verify

```sql
-- Rebalance periods per industry (should be ~1 per year from base_date):
SELECT industry_name, COUNT(*) AS periods, MIN(rebalance_date), MAX(rebalance_date)
FROM industry_index_period GROUP BY industry_name ORDER BY 1;

-- Continuity spot-check: values should NOT jump to 1000 at a rebalance.
-- Pick an industry and eyeball values around an April date:
SELECT trade_date, value, effective_rebalance_date
FROM industry_index_value
WHERE industry_name = 'Specialty Chemicals'
  AND trade_date BETWEEN '2019-03-25' AND '2019-04-10'
ORDER BY trade_date;
```
