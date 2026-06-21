# `trip_ema/fundamentals/` — Phase 1–6 fundamentals stack

Everything between raw OHLCV and emitted signal events: yfinance
metadata fetches, daily market cap, industry indices, indicator
state, and the signal events written to `signal_events`.

The runner per phase is the unit of work; the EoD pipeline wires the
8 phases together. Per-ticker work runs **before** per-industry work
(non-negotiable — the industries reference the per-ticker state).

## Files

| File | Purpose |
|---|---|
| `__init__.py` | Marker |
| `cli.py` | CLI command handlers. Imported and registered by `trip_ema/etl/cli.py` (which stays a thin dispatcher). Convention: `(args, config) -> exit_code` (0/1/2) |
| `runners.py` | One runner per phase (`run_industry_refresh`, `run_shares_refresh`, `run_financials_refresh`, `run_market_cap_compute`, `run_index_build`, `run_index_values`, `run_indicator_state`, `run_signal_compute`). Each owns transaction control, `fundamentals_run` row lifecycle, progress checkpointing, resumability |
| `runner.py` *(if present)* | Shared runner helpers — context, dry-run guard, dispatch table |
| `eod_pipeline.py` | Phase 4C orchestrator. `run_eod_pipeline(...)` wires the 8 phases as a single pipeline. Per-ticker **before** per-industry. `--skip-tickers` is the opt-out; `--include-tickers` is deprecated (no-op) |
| `scheduler.py` | Quarterly fundamentals refresh — a background thread that wakes once per day, checks the calendar, fires `run_industry_refresh()` when the filing window opens and the last SUCCESS row is stale |
| `industry_fetch.py` | `yfinance Ticker.info → IndustryInfo` for one symbol. Typed exceptions for rate-limit / transient / permanent so the runner can apply differentiated retry policy |
| `shares_fetch.py` | `yfinance Ticker.info → SharesInfo` for one symbol. Current scalar shares outstanding |
| `financials_fetch.py` | `yfinance Ticker.quarterly_income_stmt → list[QuarterlyFinancial]` for one symbol |
| `backfill_shares_fetch.py` | `Ticker.get_shares_full(start=…)` — historical time series of shares-outstanding by filing date. Distinct from `shares_fetch` (current scalar) |
| `market_cap_fetch.py` | yfinance historical `daily_market_cap` rows (`source=FETCHED`) for dates BEFORE our local `instrument_shares` coverage |
| `market_cap_compute.py` | Daily market cap (`source=COMPUTED`) by joining `ohlcv_bars.close × instrument_shares.shares_outstanding` for dates within local coverage |
| `index_compute.py` | Phase 4B-2. Index constituent picker (top-N by mcap per industry) + modified-Laspeyres value computer. Two pure functions; the runner wires them into rebalance + value-backfill loops |
| `indicator_primitives.py` | Pine-Script-equivalent primitives (Phase 5-2). Pure pandas Series in / Series out. Implements the same math as the operator's Pine indicator |
| `signal_compute.py` | Phase 6-1. Pure: a time-ordered list of `IndicatorState` rows → emitted `SignalEvent` rows. Four kinds derived from display-regime transitions: BUY, SELL, EXIT_FROM_BUY, EXIT_FROM_SELL |
| `signal_generator.py` | Phase 15a. Historical replay across active tickers on an exchange. Reads `ohlcv_bars`, replays through a fresh `TickerEngine` per (ticker, timeframe), captures every non-`NO_CHANGE` event, merges to one JSON per exchange |

## EoD pipeline order

```
1. refresh-industry         (per-ticker, sector/industry from yfinance)
2. refresh-shares           (per-ticker, current shares outstanding)
3. refresh-financials       (per-ticker, quarterly income)
4. compute-market-cap       (per-ticker, daily_market_cap rows)
5. build-industry-indices   (per-industry, constituent picks)
6. compute-index-values     (per-industry, index values)
7. compute-indicator-state  (per-ticker + per-industry indicator rows)
8. compute-signals          (per-ticker + per-industry signal events)
```

Per-ticker (1–4, 7t, 8t) runs **before** per-industry (5, 6, 7i, 8i).

## Signal-event idempotency

`signal_events` (plural, unified in migration 0016) is the single
canonical table. Writes go through `INSERT … ON CONFLICT DO NOTHING`
guarded by partial unique indexes — re-runs and intra-week
re-evaluations cannot duplicate.

## Signal kind vocabulary

Long forms everywhere — `BUY`, `SELL`, `EXIT_FROM_BUY`, `EXIT_FROM_SELL`.
No short forms. Direct regime flips emit only the new-side OPEN signal;
no EXIT is emitted (precedence suppression).

## Weekly aggregation

`date_trunc('week', bar_timestamp AT TIME ZONE 'Asia/Kolkata')` —
Pine convention, Monday-IST anchored. Trade dates are stored as IST
Mondays. The partial in-progress week is always emitted; intra-week
re-evaluations may emit multiple rows for the same weekly bar
across scheduled ticks (legitimate, not duplicates).
