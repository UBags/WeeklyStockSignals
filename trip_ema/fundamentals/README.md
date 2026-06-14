# `trip_ema/fundamentals/` — Fundamentals vertical slice (Phases 1-8)

A parallel pipeline that enriches the ticker universe with sector /
industry classification, shares outstanding, quarterly financials, daily
market cap, modified-Laspeyres industry indices, the Triple-EMA indicator
stack applied to those indices, and the four-kind signal-event derivation.

The design rationale and per-phase decisions are in
`../../docs/FUNDAMENTALS_PHASES_1_TO_8.md`.

## How the CLI surface attaches

The fundamentals subcommands are **not** a separate binary. They ship
as part of `trip-ema-etl`. `cli.py::register_subparsers(subparsers, _add_exchange_arg)`
is called from `trip_ema/etl/cli.py::_build_parser()`, which mounts every
fundamentals subcommand onto the same argparse tree and merges the
handlers into the shared dispatch table. From the operator's
perspective there is one CLI; the split here is for code locality.

So `trip-ema-etl refresh-industry --exchange NSE` reaches a handler
defined in this module, dispatched by the ETL module's `main()`.

## Files

| File | Purpose |
|---|---|
| `__init__.py` | Domain dataclasses (`IndustryClassification`, `IndustryOverride`, `FundamentalsRun`, `SharesOutstanding`, `QuarterlyFinancial`, `DailyMarketCap`, `IndustryIndex`, `IndustryIndexConstituent`, `IndustryIndexValue`, `IndicatorState`, `SignalEvent`) + tuple-of-literals enums for source / status / methodology fields |
| `cli.py` | `register_subparsers(...)` — mounts subcommands onto `trip-ema-etl`'s argparse tree. Subcommands: `refresh-industry`, `backfill-etf-classification`, `refresh-shares`, `refresh-financials`, `compute-market-cap`, `build-industry-indices`, `compute-index-values`, `compute-indicator-state`, `compute-signals`, `set-industry-override`, `delete-industry-override`, `fundamentals-status`, `rebalance-industry-index`, `run-eod-pipeline` |
| `eod_pipeline.py` | Orchestrates all 8-10 phases in order; per-step progress table; exit codes 0/1/2 |
| `scheduler.py` | Skip-if-recent guard (80-day window) shared across Phase 1-3 refresh commands |
| `runners.py` | Generic run-row management (open + close `fundamentals_run` rows; resume support) |
| `industry_fetch.py` | Phase 1 — yfinance sector/industry classification fetch + ETF subbranch |
| `shares_fetch.py` | Phase 2 — yfinance `sharesOutstanding` / `impliedSharesOutstanding` fetch |
| `financials_fetch.py` | Phase 3.A — yfinance `quarterly_income_stmt` fetch (revenue + ebitda) |
| `market_cap_fetch.py` | Phase 4A — historical mcap path (yfinance `histMarketCap`) |
| `market_cap_compute.py` | Phase 4A — `close × shares` compute path (COMPUTED / APPROX_TODAYS_SHARES) |
| `index_compute.py` | Phase 4B — modified-Laspeyres index construction + daily-value computation + mid-cycle delisting renormalization |
| `indicator_primitives.py` | Phase 5 — math primitives for indicator-state derivation: bars-since-turn, pct-change-since-turn, pattern-label detection |
| `signal_compute.py` | Phase 6-1 — derives `signal_event` rows from `indicator_state.display_regime` transitions |

## Pipeline ordering

```
1. refresh-industry          (Phase 1 — yfinance, skip-if-recent)
2. (backfill-etf-classification)  (Phase 1 ETF subbranch)
3. refresh-shares            (Phase 2 — yfinance)
4. refresh-financials        (Phase 3.A — yfinance)
5. compute-market-cap        (Phase 4A — local; backfill mode = yfinance)
6. build-industry-indices    (Phase 4B — local)
7. compute-index-values      (Phase 4B — local)
8. compute-indicator-state   (Phase 5 — local)
9. compute-signals           (Phase 6-1 — local)
   --include-tickers:
10. compute-indicator-state (per-ticker)  (Phase 7)
11. compute-signals (per-ticker)          (Phase 7)
```

Each step's output is the next step's input. `run-eod-pipeline` chains
them all in a single command (Phase 4C) with a per-step progress table.

## Skip-if-recent guard (80 days)

Phase 1-3 fetches consult `fundamentals_run` for a recent SUCCESS or
PARTIAL row. If found within 80 days, the step is skipped. `--force`
overrides. The intent: yfinance's per-ticker data is updated quarterly
at most, so daily re-fetches waste API budget.

Steps 4-9 have their own (shorter) guards or no guard at all.

## Polymorphic identity (industries vs tickers)

Tables `indicator_state` and `signal_event` are polymorphic: a row is
EITHER for an industry index (`industry_name` populated) OR for a ticker
(`ticker_id` + `timeframe` populated). DB CHECK constraints enforce
"exactly one of the two non-NULL". Phase 5/6 ships the industry path;
Phase 7 adds the ticker path.

## HTTP surface

The fundamentals layer also exposes routes under `/v1/fundamentals/*`
in `trip_ema/transports/http/routes/fundamentals.py` — read endpoints
for the various tables, plus async POSTs that fire the same handlers
on the service's `etl_executor`. Used by automation and (eventually)
the GUI.

## Tests

`tests/fundamentals/` — 23 files, one per module + integration cases.
Notable:
- `test_eod_pipeline.py` — full multi-step orchestration (Phase 4C)
- `test_index_compute.py` — modified-Laspeyres math + delisting renorm
- `test_indicator_domain.py`, `test_indicator_primitives.py`,
  `test_indicator_state_runners.py` — Phase 5
- `test_signal_compute.py`, `test_signal_event_runners.py` — Phase 6
- `test_per_ticker_runners.py` — Phase 7
