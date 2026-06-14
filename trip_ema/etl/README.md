# `trip_ema/etl/` — ETL pipeline (yfinance + NSE listings + backtest)

The `trip-ema-etl` CLI's implementation. Pulls OHLCV bars from yfinance,
syncs the NSE listings universe, manages ticker tiers and parking, and
(Phase 15) generates per-exchange backtest signal files.

## Files

| File | Purpose |
|---|---|
| `__init__.py` | Marker |
| `cli.py` | `argparse` entry point for `trip-ema-etl` — dispatches subcommands. Also merges in fundamentals subcommands at build time (see "CLI composition" below) |
| `runner.py` | The high-level orchestrator — `run_eod`, `run_intraday`, `run_backfill`, ... — wired to fetchers + repos |
| `yfinance_source.py` | Wraps yfinance for OHLCV history fetches with retry, throttling, batch handling |
| `nse_downloader.py` | Downloads the NSE listings CSV |
| `nse_listings_parser.py` | Parses the listings CSV into ticker rows + deltas |
| `listings_sync.py` | Reconciles parsed listings against the `tickers` table; records corporate actions for review |
| `exchanges.py` | Per-exchange config helpers — what symbol suffix to add for yfinance (`.NS`, `.BO`), holidays country code, etc. |
| `holidays_calendar.py` | python-holidays + YAML override (`configs/nse_holidays_2026.yaml`) — "is this a trading day?" |
| `tiering.py` | Bucketizes the universe into tier-1 / tier-2 / tier-3 by 30-day ADV; controls EoD and intraday cadence |
| `parked.py` | Per-ticker fail-streak counter — auto-park after 3 consecutive failures; unpark via CLI / GUI |
| `reconciler.py` | Compares fetched bars against existing rows to detect vendor revisions |
| `status_repo.py` | Repo for `etl_runs` + `etl_ticker_status` (run history, per-ticker outcomes) |
| `corporate_actions_repo.py` | Repo for `corporate_actions` — pending splits/divs/renames awaiting operator approval |
| `symbol_history_repo.py` | Repo for `symbol_history` — historical symbol→name mappings for renames |
| `types.py` | ETL-internal dataclasses (`FetchResult`, `TierAssignment`, etc.) |
| `signal_generator.py` | **(Phase 15a)** Replays the engine over history to build the per-exchange backtest JSON. ~900 LOC; the heart of Phase 15 |
| `nse_holidays_override.yaml` | (Reference copy of the holidays override file — actual one lives in `configs/`) |

## CLI subcommands defined here

All take `--exchange` (mandatory since Phase 14.6):

| Subcommand | Purpose |
|---|---|
| `refresh-listings` | Pull the listings CSV; insert/update `tickers`; tier-assign new entries |
| `backfill --start ...` | Pull historical bars from `--start` to today; upsert to `ohlcv_bars`. Optional `--tier 1/2/3` |
| `incremental-eod` | After market close, fetch yesterday's bar for every active ticker; default lookback 7 days |
| `incremental-intraday` | During market hours, fetch the in-progress bar for tier-1 tickers |
| `recompute-tiers` | Re-bucket tickers into NIFTY-200 / next-500 / rest based on current volume |
| `status` | Print most recent `etl_runs` row per kind for the exchange |
| `unpark` | Clear the parked flag for tickers stalled by previous failures |
| `approve-action` | Approve a pending row in `corporate_actions` |
| **`generate-signals`** | **Phase 15a** — replay engine over history; write `generated_signals/{EX}.json` |

## CLI composition — fundamentals merged in

`trip-ema-etl` is a single binary, but its subcommand set comes from
two modules. `cli.py::_build_parser()` calls
`trip_ema.fundamentals.cli.register_subparsers(sub, _add_exchange_arg)`
which mounts every fundamentals subcommand (`refresh-industry`,
`refresh-shares`, `refresh-financials`, `compute-market-cap`,
`build-industry-indices`, `compute-index-values`,
`compute-indicator-state`, `compute-signals`, `fundamentals-status`,
`run-eod-pipeline`, `set-industry-override`,
`delete-industry-override`, `rebalance-industry-index`,
`backfill-etf-classification`) onto the same parser. Their handlers
are merged into the dispatch table at runtime. From the operator's
perspective there is one CLI; from a code-locality perspective the
two modules own their own handlers.

See `../fundamentals/README.md` for those subcommands' semantics.

## Phase 15 backtest pipeline

`signal_generator.generate_historical_signals(...)` is the entry. Per ticker × timeframe:

1. Read `signal_backtest_runs` row (resume cursor)
2. Force-full? Wipe resume rows in a small dedicated transaction
3. Fetch all daily bars from `BarsRepository`
4. Aggregate to weekly via `daily_to_weekly` if `tf=1W`
5. Resolve `IndicatorParams` (consults `exchange_overrides` if set)
6. Build a **fresh** `TickerEngine`; replay every bar
7. Capture every `ChangeSignal != NO_CHANGE` AND `bar.date >= start_date`
8. Merge into in-memory `by_date` dict (3-case protocol: add / replace / delete)
9. Upsert `signal_backtest_runs` in a per-ticker transaction
10. Refresh counts metadata
11. Atomic write tmp + rename

The merge protocol is tested explicitly in
`tests/etl/test_signal_generator_merge.py`.

## Tests

- `tests/etl/test_exchanges.py`, `test_holidays_calendar.py`, `test_nse_listings_parser.py`, `test_parked.py`, `test_reconciler.py`, `test_runner_aggregation.py`, `test_runner_exchange_kwarg.py`, `test_tiering.py`, `test_yfinance_source.py`
- Phase 15 (~50 tests total): `test_signal_generator.py` (15), `test_signal_generator_merge.py` (11), `test_signal_generator_atomic_write.py` (6), `test_signal_generator_cli.py` (10)
- Fixtures: `tests/etl/fixtures/equity_l_sample.csv`, `etf_sample.csv`
