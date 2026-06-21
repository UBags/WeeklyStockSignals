# `trip_ema/etl/` — OHLCV ingest + listings sync

Owns the contract: **the ETL owns truth for bars; the engine accepts
revisions** (ARCHITECTURE invariants 3.1 and 3.6). Wraps yfinance,
reconciles against existing rows, tiers tickers by liquidity, and
syncs the NSE master listing.

The fundamentals stack (industry / shares / financials / market cap /
indicator state / signal events) lives in `trip_ema/fundamentals/`,
not here.

## Files

| File | Purpose |
|---|---|
| `__init__.py` | Marker |
| `runner.py` | The runner. Turns parsed config + DB engine + yfinance source into actual ETL passes (`run_incremental`, `run_backfill`, `recompute_tiers`, …). Doesn't open the engine — accepts one. Composed by the CLI |
| `cli.py` | `trip-ema-etl` entry. Subparser table dispatching to runner functions. Returns 0/1/2 exit codes (success / partial / fatal) |
| `types.py` | Frozen ETL dataclasses: `FetchOutcome`, `RawBar`, `WritePlan`, `Divergence`, `RunKind`, `TickerStatus` |
| `yfinance_source.py` | Wraps `yf.download()` with batching, retry-with-backoff, validation. Returns `{bare_symbol: FetchOutcome}` so callers can match results back to inputs |
| `reconciler.py` | Pure: given new RawBars + existing DB bars → `WritePlan` + `Divergence` list. Honours invariant 3.1 (ETL truth) and 3.6 (engine accepts revisions) |
| `status_repo.py` | `etl_runs` + `etl_ticker_status` lifecycle. Opens a row at CLI start, updates counters as work proceeds, closes on completion. The GUI's ETL History view reads this |
| `tiering.py` | Tier 1/2/3 by trailing-30-trading-day ADV. Pure Python over a SQL result — query lives in `status_repo`; algorithm lives here. CLI: `recompute-tiers` |
| `parked.py` | Parked-failures storage. On `consecutive_failures >= threshold`, mark `parked` in `etl_ticker_status` AND append to `var/etl/parked/<YYYY-MM-DD>.csv`. The CSV is the audit; the column is the queryable state. `unpark` reads the latest CSV |
| `exchanges.py` | Exchange ↔ yfinance-suffix mapping. KNOWN exchanges (`_SUFFIX_MAP`, static) vs CONFIGURED exchanges (from `config.exchanges`). `validate_exchange` / `configured_exchanges` / `UnsupportedExchangeError` |
| `holidays_calendar.py` | NSE trading-day calendar. `holidays` package + per-year YAML override. A trading day is Mon–Fri minus the holiday list |
| `listings_sync.py` | Applies parsed `NseListings` to the `tickers` table: new symbol → INSERT tier=3, active=TRUE; matching ISIN → UPDATE enrichment; missing symbol → mark inactive |
| `nse_downloader.py` | Listings downloader. Handles NSE's anti-bot protection (real UA, primed session cookies). Brittle by nature |
| `nse_listings_parser.py` | Pure CSV → `NseListing` records. Tested against committed fixture CSVs — no network |
| `nse_holidays_override.yaml` | Per-year YAML overrides for the `holidays` package's India calendar (muhurat, mid-week closures, …) |
| `nse_holidays_2026.yaml` | 2026 override file |

## CLI surface

```
trip-ema-etl incremental          # daily incremental ingest
trip-ema-etl backfill --days N    # historical backfill
trip-ema-etl sync-listings        # NSE master → tickers table
trip-ema-etl recompute-tiers      # weekly tier reshuffle
trip-ema-etl unpark               # rehome rows from latest parked CSV
```

## Boundaries

- The runner doesn't open the DB engine — caller injects one.
- The reconciler is pure: bars in, plan out, no I/O.
- The downloader and the parser are deliberately split so the parser
  is unit-testable against fixtures.
- Bar storage is always `1D`. Weekly aggregation happens on-demand
  in the indicator engine — **weekly bars are never stored**.
