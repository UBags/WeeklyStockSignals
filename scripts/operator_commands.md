# trip-ema operator commands

Day-to-day operational recipes. Every command in this file is meant to be
copy-pasted into a terminal. Sections are independent — read them in any order.

For debugging recipes (orchestrator ticks, pulse streams, log digging), see
[`debug_recipes.md`](debug_recipes.md).

---

## 1. Service lifecycle

### Start (or restart) the service

```bash
./scripts/restart_service.sh
```

Restarts cleanly: kills whatever's bound to port 8000, waits for the socket to
release, starts a new `trip-ema` process under `nohup`, polls `/healthz` until
priming completes, and prints `cache_size` so you can confirm it loaded data.

Pass a custom config as the first arg if needed:

```bash
./scripts/restart_service.sh /path/to/other_config.yaml
```

### Stop the service without restarting

```bash
ss -tlnp 2>/dev/null | grep :8000              # find PID
kill <pid>
```

### Tail logs while it's running

```bash
tail -f /tmp/trip-ema.log
```

### Verify the service is healthy

```bash
curl -s http://localhost:8000/healthz | python3 -m json.tool
```

Expect `status: ok`, `cache_size > 0`. If `cache_size = 0`, the service started
but hasn't primed — wait, or check `/tmp/trip-ema.log` for the priming step.

---

## 2. GUI

### Start the operator console (idempotent)

```bash
./scripts/gui_start.sh
```

Idempotent: re-running while the GUI is open is a no-op. Needs a real display
(`$DISPLAY` or `$WAYLAND_DISPLAY` set). Logs to `/tmp/trip-ema-console.log`.

### Stop the GUI

Close the window normally. If it's wedged:

```bash
pkill -f trip-ema-console
```

---

## 3. Initial load for a new exchange

When adding a new exchange (e.g. BSE for the first time), do these in order:

```bash
# 1. Confirm the exchange is configured in config.yaml's market_hours block.
grep -A 2 "exchange: BSE" config.yaml

# 2. Pull the listings CSV and sync the tickers table.
trip-ema-etl refresh-listings --exchange BSE

# 3. Confirm tickers landed.
psql -h localhost -U trip_ema_app -d trip_ema -c "
SELECT exchange, COUNT(*)
FROM tickers
WHERE exchange = 'BSE' AND is_active
GROUP BY exchange;"

# 4. Backfill historical bars. --start is required; backfill runs from --start
#    to today. There is no --end flag. 15-year backfill = 2010-01-01 or so.
trip-ema-etl backfill --exchange BSE --start 2010-01-01

# 5. Verify bars landed.
psql -h localhost -U trip_ema_app -d trip_ema -c "
SELECT t.exchange, b.timeframe, COUNT(*) AS bars,
       MIN(b.bar_timestamp)::date AS first_bar,
       MAX(b.bar_timestamp)::date AS last_bar
FROM ohlcv_bars b JOIN tickers t USING (ticker_id)
WHERE t.exchange = 'BSE'
GROUP BY t.exchange, b.timeframe;"

# 6. Compute tiers from 30-day ADV (otherwise everything sits in tier-3).
trip-ema-etl recompute-tiers --exchange BSE

# 7. Optionally restart the service so it picks up the new tickers
#    in its priming pass.
./scripts/restart_service.sh
```

For very large backfills (15+ years × 2000+ tickers), this can take hours.
The ETL runs against `yfinance` which has its own throttling — there's nothing
to tune client-side; just let it run.

**Tip:** if interrupted, re-running `backfill --start <date>` is safe. The
upsert primary key on `ohlcv_bars` makes the operation idempotent.

---

## 4. Daily updates per exchange

Run after market close each day, per exchange:

```bash
trip-ema-etl incremental-eod --exchange NSE
trip-ema-etl incremental-eod --exchange BSE
trip-ema-etl incremental-eod --exchange NYSE
```

`incremental-eod` fetches the last 7 days of bars by default (set via
`--lookback-days N` if you want more overlap, e.g. after a long downtime).
The window protects against late or revised bars without re-fetching the
whole history.

### Via HTTP (preferred for automation / cron from another host)

```bash
# Single exchange, async (returns 202 Accepted immediately):
curl -X POST \
  -H "Authorization: Bearer $TRIP_EMA_ADMIN_TOKEN" \
  "http://localhost:8000/v1/admin/etl/run-eod?exchange=NSE"

# Then check status:
curl -s -H "Authorization: Bearer $TRIP_EMA_ADMIN_TOKEN" \
  "http://localhost:8000/v1/admin/etl/status?exchange=NSE" \
  | python3 -m json.tool
```

The exchange is a query parameter, not a JSON body. The route returns 202 and
the actual ETL runs on the service's `etl_executor` thread.

### Via the GUI

ETL Status tab → pick the exchange in the dropdown at the top-left → click
**Run EoD**. Confirms when the action is accepted; the runs table refreshes
every few seconds with the new in-flight row.

### Backtest signal generation (Phase 15b)

After daily ETL completes, regenerate the per-exchange backtest JSON file
that external consumers read. This replays every active ticker through a
fresh engine and writes `generated_signals/{EXCHANGE}.json`.

```bash
# Fire the run (async; returns 202 with run_id):
curl -X POST \
  -H "Authorization: Bearer $TRIP_EMA_ADMIN_TOKEN" \
  "http://localhost:8000/v1/admin/signals/generate?exchange=NSE"

# With explicit timeframes (default is 1W only):
curl -X POST \
  -H "Authorization: Bearer $TRIP_EMA_ADMIN_TOKEN" \
  "http://localhost:8000/v1/admin/signals/generate?exchange=NSE&timeframe=1D&timeframe=1W"

# Force full recompute (wipes per-(ticker, timeframe) resume rows
# and recomputes from each ticker's earliest bar). Use after
# changing engine params or exchange_overrides:
curl -X POST \
  -H "Authorization: Bearer $TRIP_EMA_ADMIN_TOKEN" \
  "http://localhost:8000/v1/admin/signals/generate?exchange=NSE&force_full=true"
```

A second POST while one is in flight for the same exchange returns 409
with the in-flight `run_id`. Wait for it to finish (the runner writes
`outcome='success'`/`'failed'` to its etl_runs row when done) or kill the
worker.

The CLI equivalent is `trip-ema-etl generate-signals --exchange NSE` (see
§5 above) — use it for one-off operator work; use the HTTP endpoint for
automation or when the GUI button (Phase 15c) becomes available.

---

## 5. Ad-hoc updates per exchange

### Refresh intraday only (during market hours, tier-1 only)

```bash
trip-ema-etl incremental-intraday --exchange NSE
```

On non-trading days this prints `skipped (non-trading day)` and exits 0.

### Re-fetch a specific window (e.g. after a vendor data correction)

`backfill` always runs from `--start` to today. To re-fetch just the last
week, use a recent start date:

```bash
trip-ema-etl backfill --exchange NSE --start 2026-05-13
```

The upserts in `ohlcv_bars` mean re-fetching overlapping data is safe —
existing rows are overwritten with the latest values from upstream.

### Re-fetch only tier-1 tickers (faster, smaller scope)

```bash
trip-ema-etl backfill --exchange NSE --start 2026-05-13 --tier 1
```

`--tier` takes `1`, `2`, `3`, or `all` (default).

### Backfill from the GUI (Phase 15d)

The ETL Status tab now has a **Backfill…** button next to the other
action buttons. Clicking it opens a modal that asks for a start date
(defaults to today minus 30 days) and an optional tier (off by
default → all tiers). It posts to `/v1/admin/etl/backfill` through
the same machinery as the CLI/HTTP path — the only difference is
the entry point.

Use this when you want a quick window-refresh without a terminal;
use the CLI/HTTP for scripted backfills or when you need
exchange-agnostic operations.

### Refresh listings mid-month (corporate actions, new IPOs)

```bash
trip-ema-etl refresh-listings --exchange NSE
```

The runner writes deltas to `symbol_history` and adds pending entries to
`corporate_actions` for renames. New listings become active immediately;
they get bars on the next `incremental-eod`.

### Approve a pending corporate action

```bash
trip-ema-etl approve-action 42 --notes "Verified rename via Bloomberg"
```

Where `42` is the `action_id` from the corporate_actions table. Pending
actions block their associated ticker from updating bars.

### Unpark all tickers (clear retry-block)

After fixing whatever caused tickers to fail (network issue, vendor data
glitch), unpark them so they retry on the next EoD:

```bash
trip-ema-etl unpark --exchange NSE
```

The CLI prints how many tickers it unparked.

### Generate historical signals for backtesting (Phase 15a)

Replay every active ticker's bars through a fresh engine and write a
per-exchange JSON file (`generated_signals/{EXCHANGE}.json`) that
external backtesters can consume. First run for an exchange is the
slow one (~30 min for a full NSE universe); subsequent runs are
incremental via a per-(ticker, timeframe) resume table.

```bash
trip-ema-etl generate-signals --exchange NSE                # default: 1W only
trip-ema-etl generate-signals --exchange NSE --timeframe 1D --timeframe 1W
trip-ema-etl generate-signals --exchange NSE --force-full   # wipe resume rows
```

`--force-full` recomputes from each ticker's earliest bar; use after
changing engine params (`ema:`/`atr:` or `exchange_overrides:`).
The HTTP equivalent (`POST /v1/admin/signals/generate`) is added in
Phase 15b.

---

## 6. Inventory queries

### Total scrips (tickers) per exchange

```bash
psql -h localhost -U trip_ema_app -d trip_ema -c "
SELECT
    exchange,
    COUNT(*) FILTER (WHERE is_active)        AS active,
    COUNT(*) FILTER (WHERE NOT is_active)    AS inactive,
    COUNT(*)                                  AS total
FROM tickers
GROUP BY exchange
ORDER BY exchange;"
```

### Total bars per exchange and timeframe

```bash
psql -h localhost -U trip_ema_app -d trip_ema -c "
SELECT
    t.exchange,
    b.timeframe,
    COUNT(*)                          AS bars,
    MIN(b.bar_timestamp)::date        AS first_bar,
    MAX(b.bar_timestamp)::date        AS last_bar
FROM ohlcv_bars b
JOIN tickers t USING (ticker_id)
GROUP BY t.exchange, b.timeframe
ORDER BY t.exchange, b.timeframe;"
```

### Tier counts per exchange (tier-1 / tier-2 / tier-3)

```bash
psql -h localhost -U trip_ema_app -d trip_ema -c "
SELECT
    t.exchange,
    ts.tier,
    COUNT(*) AS tickers
FROM tickers t
LEFT JOIN etl_ticker_status ts USING (ticker_id)
WHERE t.is_active
GROUP BY t.exchange, ts.tier
ORDER BY t.exchange, ts.tier NULLS LAST;"
```

`NULL` tier means the ticker has no `etl_ticker_status` row yet — usually
because `recompute-tiers` hasn't been run since it was added.

### Parked ticker counts per exchange

```bash
psql -h localhost -U trip_ema_app -d trip_ema -c "
SELECT
    t.exchange,
    COUNT(*) AS parked,
    MAX(ts.parked_at) AS most_recent
FROM etl_ticker_status ts
JOIN tickers t USING (ticker_id)
WHERE ts.is_parked
GROUP BY t.exchange
ORDER BY t.exchange;"
```

---

## 7. Alerts (signal events) per exchange

Alerts in trip-ema are rows in the `signal_events` table — one per
`(ticker, timeframe, bar)` where the engine emitted BUY / SELL /
EXIT_FROM_BUY / EXIT_FROM_SELL. `computed_at` is when the engine fired
the signal; `bar_timestamp` is the bar it's attached to.

### Last 60 minutes — per exchange

```bash
psql -h localhost -U trip_ema_app -d trip_ema -c "
SELECT
    t.exchange,
    COUNT(*)                                                       AS alerts,
    COUNT(*) FILTER (WHERE s.signal_type = 'BUY')                  AS buys,
    COUNT(*) FILTER (WHERE s.signal_type = 'SELL')                 AS sells,
    COUNT(*) FILTER (WHERE s.signal_type LIKE 'EXIT_FROM%')        AS exits
FROM historical_signals s
JOIN tickers t USING (ticker_id)
WHERE s.computed_at >= NOW() - INTERVAL '60 minutes'
GROUP BY t.exchange
ORDER BY t.exchange;"
```

### Today (since local midnight) — per exchange

```bash
psql -h localhost -U trip_ema_app -d trip_ema -c "
SELECT
    t.exchange,
    COUNT(*)                                                       AS alerts,
    COUNT(*) FILTER (WHERE s.signal_type = 'BUY')                  AS buys,
    COUNT(*) FILTER (WHERE s.signal_type = 'SELL')                 AS sells,
    COUNT(*) FILTER (WHERE s.signal_type LIKE 'EXIT_FROM%')        AS exits
FROM historical_signals s
JOIN tickers t USING (ticker_id)
WHERE s.computed_at >= DATE_TRUNC('day', NOW())
GROUP BY t.exchange
ORDER BY t.exchange;"
```

### Since Monday — per exchange

`DATE_TRUNC('week', ...)` in Postgres returns the start of the ISO week,
which is Monday 00:00:00 in the database's local time zone. To use the
operator's timezone explicitly:

```bash
psql -h localhost -U trip_ema_app -d trip_ema -c "
SELECT
    t.exchange,
    COUNT(*)                                                       AS alerts,
    COUNT(*) FILTER (WHERE s.signal_type = 'BUY')                  AS buys,
    COUNT(*) FILTER (WHERE s.signal_type = 'SELL')                 AS sells,
    COUNT(*) FILTER (WHERE s.signal_type LIKE 'EXIT_FROM%')        AS exits
FROM historical_signals s
JOIN tickers t USING (ticker_id)
WHERE s.computed_at >= DATE_TRUNC('week', NOW())
GROUP BY t.exchange
ORDER BY t.exchange;"
```

### Most recent alerts per exchange (with ticker + price)

```bash
psql -h localhost -U trip_ema_app -d trip_ema -c "
SELECT
    t.exchange,
    t.symbol,
    s.signal_type,
    s.price_at_signal,
    to_char(s.computed_at, 'YYYY-MM-DD HH24:MI:SS') AS fired_at,
    to_char(s.bar_timestamp, 'YYYY-MM-DD HH24:MI:SS') AS bar
FROM historical_signals s
JOIN tickers t USING (ticker_id)
WHERE s.computed_at >= NOW() - INTERVAL '60 minutes'
ORDER BY s.computed_at DESC
LIMIT 50;"
```

---

## 8. ETL status snapshots

### CLI (read-only, exchange-agnostic)

```bash
trip-ema-etl status --limit 10
trip-ema-etl status --limit 20 -v        # also lists first 20 parked tickers
```

### HTTP (with optional `?exchange=` to scope tier counts)

```bash
# All exchanges, default limit
curl -s -H "Authorization: Bearer $TRIP_EMA_ADMIN_TOKEN" \
  http://localhost:8000/v1/admin/etl/status | python3 -m json.tool

# Tier counts scoped to BSE; recent_runs + parked_count stay global
curl -s -H "Authorization: Bearer $TRIP_EMA_ADMIN_TOKEN" \
  "http://localhost:8000/v1/admin/etl/status?exchange=BSE&limit=5" \
  | python3 -m json.tool

# Per-exchange ETL state (last EoD, listings sync, etc.)
curl -s -H "Authorization: Bearer $TRIP_EMA_ADMIN_TOKEN" \
  http://localhost:8000/v1/admin/exchange-state/NSE \
  | python3 -m json.tool

# All exchange-states at once
curl -s -H "Authorization: Bearer $TRIP_EMA_ADMIN_TOKEN" \
  http://localhost:8000/v1/admin/exchange-state \
  | python3 -m json.tool
```

### One-shot operator dashboard

```bash
./scripts/health_check.sh
```

Prints sections 1–6 of the most common checks in this file (service health,
ticker counts, bar counts, exchange_state, alert counts, recent runs).
Read-only. Safe to run any time.

### Generated backtest file (Phase 15b)

Inspect the per-exchange JSON file the signal generator writes
(metadata only — the `by_date` payload is not returned, only the
top-level counts and file mtime/size):

```bash
curl -s -H "Authorization: Bearer $TRIP_EMA_ADMIN_TOKEN" \
  "http://localhost:8000/v1/admin/signals/generated?exchange=NSE" \
  | python3 -m json.tool
```

Sample output:

```json
{
  "exchange": "NSE",
  "exists": true,
  "filename": "NSE.json",
  "size_bytes": 14_680_064,
  "mtime": "2026-05-21T01:42:17.123456+00:00",
  "generated_by_run_id": 29,
  "schema_version": "1.0",
  "counts": {
    "tickers": 2283,
    "signals_total": 105684,
    "signals_by_type": {
      "BUY": 32987, "SELL": 35396,
      "EXIT_FROM_BUY": 17857, "EXIT_FROM_SELL": 19444
    },
    "first_date": "2005-01-03",
    "last_date": "2026-05-15"
  }
}
```

A 200 with `exists: false` means the exchange is configured but no
`generate-signals` run has finished yet for it. That's a normal
post-deploy state, not an error.

---

## 9. The admin token

Many admin endpoints require `Authorization: Bearer $TRIP_EMA_ADMIN_TOKEN`.
Set it in your shell:

```bash
export TRIP_EMA_ADMIN_TOKEN="<value from config.yaml or env>"
```

Or look up which env var the running config reads:

```bash
grep "admin_token_env" config.yaml
```

In the GUI, the same token is read from the same env var. If you launch the
GUI from a terminal where `TRIP_EMA_ADMIN_TOKEN` is unset, the action buttons
on the ETL Status tab will stay disabled.

---

## 10. Smoke tests after a code change

```bash
./scripts/smoke_test_fast.sh         # ~5s, hits /healthz + one ticker history
./scripts/smoke_test_full.sh         # ~30s, broader coverage
```

Neither restarts the service. Run them after `./scripts/restart_service.sh`
to confirm the new install is serving valid data.

---

## 11. Operator commands for GUI-deferred endpoints (Phase 15d)

The full GUI-coverage audit lives in
[`docs/gui_endpoint_audit.md`](../docs/gui_endpoint_audit.md). The
recipes below cover the endpoints that don't have a GUI surface today
and probably won't get one until a workflow forces the issue. Each is
a deliberate gap, not an oversight.

### Orchestrator status (per-market)

The System Health tab doesn't render an orchestrator card yet (the
health snapshot doesn't include one). Until that lands, use the
endpoint directly:

```bash
curl -s -H "Authorization: Bearer $TRIP_EMA_ADMIN_TOKEN" \
     http://localhost:8000/v1/admin/orchestrator/status | jq
```

Shows per-market last-run time, last outcome, paused state, and a
pool-level `any_unhealthy` boolean.

### Pause / resume / run-now a single market

No GUI buttons today. CLI workflow:

```bash
TOKEN="$TRIP_EMA_ADMIN_TOKEN"

# Pause one market (stops the orchestrator from ticking it)
curl -s -X POST -H "Authorization: Bearer $TOKEN" \
     http://localhost:8000/v1/admin/orchestrator/NSE/pause

# Resume
curl -s -X POST -H "Authorization: Bearer $TOKEN" \
     http://localhost:8000/v1/admin/orchestrator/NSE/resume

# Force a single tick now (one-shot, doesn't change scheduling)
curl -s -X POST -H "Authorization: Bearer $TOKEN" \
     http://localhost:8000/v1/admin/orchestrator/NSE/run-now
```

### Reprime a single ticker

When one ticker's signals look wrong (vendor-data correction, engine
parameter change), reprime its engine state without touching others:

```bash
curl -s -X POST -H "Authorization: Bearer $TRIP_EMA_ADMIN_TOKEN" \
     "http://localhost:8000/v1/admin/reprime?exchange=NSE&symbol=RELIANCE"
```

Returns the new `priming_completed_at` timestamp on success.

---

## 12. Fundamentals (Phases 1–4)

The fundamentals layer enriches the ticker universe with sector / industry
classification, shares outstanding, quarterly financials, daily market cap,
and modified-Laspeyres industry indices. Each phase is independent — you
can refresh one without re-running the others — but they have a natural
ordering for an initial bring-up.

Read the design doc at `docs/FUNDAMENTALS_PHASES_1_TO_8.md` for the
why-behind-the-what.

### 12.1 Initial bring-up sequence (one-shot, in order)

For a fresh database or a new exchange. Each step takes a while (yfinance
calls are serialized at ~2s/ticker for politeness):

```bash
# Phase 1: industry classification (~80 min for 2300 NSE tickers)
trip-ema-etl --config config.yaml -v refresh-industry --exchange NSE

# Phase 1 ETF subbranch (~10 min for 320 ETFs — quick because the
# ETF path makes one call per ticker but mostly hits the cache)
trip-ema-etl --config config.yaml -v backfill-etf-classification --exchange NSE

# Phase 2: shares outstanding (~80 min)
trip-ema-etl --config config.yaml -v refresh-shares --exchange NSE

# Phase 3.A: quarterly financials (~90 min — slightly slower because
# each yfinance call returns 4 quarters of data)
trip-ema-etl --config config.yaml -v refresh-financials --exchange NSE

# Phase 4A: daily market cap. Single-day mode populates today's mcap
# using local data only (~5 seconds, no yfinance). Backfill mode does
# a 1-year historical pull via yfinance (~60-90 min).
trip-ema-etl --config config.yaml compute-market-cap --exchange NSE --date $(date +%Y-%m-%d)
trip-ema-etl --config config.yaml -v compute-market-cap --exchange NSE --backfill-from 2025-05-22

# Phase 4B: industry index construction (~5 seconds — no yfinance)
trip-ema-etl --config config.yaml build-industry-indices --exchange NSE

# Phase 4B: backfill daily index values across all built indices
# (~2-5 min — no yfinance, all local computation)
trip-ema-etl --config config.yaml compute-index-values --all-active
```

Each phase respects the 80-day skip-if-recent guard — re-running the same
command won't redo work that's already been done. Use `--force` to override.

### 12.2 Daily maintenance

After EoD bars are ingested, run the daily mcap compute, then refresh the
index values. Both are fast (no yfinance):

```bash
trip-ema-etl --config config.yaml compute-market-cap --exchange NSE --date $(date +%Y-%m-%d)
trip-ema-etl --config config.yaml compute-index-values --all-active --force
```

`--force` is needed on the value refresh because the skip-if-recent guard
otherwise skips runs less than 80 days old. We want daily refreshes.

If you'd rather do it via HTTP (e.g. from a cron job on another host):

```bash
TOKEN="$TRIP_EMA_ADMIN_TOKEN"

# Single-day mcap compute is CLI-only (cheap and local). Backfill / historical
# work goes through HTTP:
curl -s -X POST -H "Authorization: Bearer $TOKEN" \
     "http://localhost:8000/v1/fundamentals/compute-market-cap?exchange=NSE&backfill_from=$(date -d 'yesterday' +%Y-%m-%d)"

# Refresh values for all indices
curl -s -X POST -H "Authorization: Bearer $TOKEN" \
     "http://localhost:8000/v1/fundamentals/compute-index-values?all_active=true&force=true"
```

The HTTP endpoints are fire-and-forget (202 Accepted). They return
immediately; the work happens in the background. Poll
`/v1/fundamentals/runs` to track progress.

### 12.3 Inspection — what's there

```bash
# Tabular summary: per-phase coverage + recent runs + index list
trip-ema-etl --config config.yaml fundamentals-status

# Same, filtered to one exchange
trip-ema-etl --config config.yaml fundamentals-status --exchange NSE

# Same, filtered to one phase
trip-ema-etl --config config.yaml fundamentals-status --phase industry
trip-ema-etl --config config.yaml fundamentals-status --phase market_cap
trip-ema-etl --config config.yaml fundamentals-status --phase index_value
```

Via HTTP:

```bash
TOKEN="$TRIP_EMA_ADMIN_TOKEN"

# List all active industry indices
curl -s -H "Authorization: Bearer $TOKEN" \
     "http://localhost:8000/v1/fundamentals/industry-indices" | jq

# One index's definition
curl -s -H "Authorization: Bearer $TOKEN" \
     "http://localhost:8000/v1/fundamentals/industry-indices/Information%20Technology%20Services" | jq

# Current constituents of an index
curl -s -H "Authorization: Bearer $TOKEN" \
     "http://localhost:8000/v1/fundamentals/industry-indices/Information%20Technology%20Services/constituents" | jq

# Constituents as of a historical date
curl -s -H "Authorization: Bearer $TOKEN" \
     "http://localhost:8000/v1/fundamentals/industry-indices/Information%20Technology%20Services/constituents?as_of=2026-04-01" | jq

# Daily value time series (newest first; default returns everything)
curl -s -H "Authorization: Bearer $TOKEN" \
     "http://localhost:8000/v1/fundamentals/industry-indices/Information%20Technology%20Services/values?from_date=2026-01-01&limit=30" | jq
```

URL-escape industry names that contain spaces (`%20`), `&`, or other special
characters. The names yfinance uses are sometimes verbose — e.g.
"Information Technology Services" rather than the shorter "IT - Services".

### 12.4 Direct SQL queries (read-only)

Most operator inspection is faster via SQL than HTTP. Set up `~/.pgpass`
once so you don't have to type the password:

```bash
cat > ~/.pgpass << 'PGEOF'
localhost:5432:trip_ema:trip_ema_app:devpassword
localhost:5432:trip_ema:trip_etl_user:devpassword
localhost:5432:trip_ema:trip_gui_user:devpassword
localhost:5432:*:postgres:devpassword
PGEOF
chmod 0600 ~/.pgpass
```

Then any libpq tool reads it automatically. Examples:

```bash
# First 10 days of an industry's index value series
psql -h localhost -U trip_ema_app -d trip_ema -c "
SELECT trade_date, value, constituents_used, renormalized
FROM industry_index_value
WHERE industry_name = 'Information Technology Services'
ORDER BY trade_date
LIMIT 10;
"

# Latest 5 days of the same
psql -h localhost -U trip_ema_app -d trip_ema -c "
SELECT trade_date, value, constituents_used
FROM industry_index_value
WHERE industry_name = 'Information Technology Services'
ORDER BY trade_date DESC
LIMIT 5;
"

# Coverage summary: rows per index
psql -h localhost -U trip_ema_app -d trip_ema -c "
SELECT industry_name, COUNT(*) AS rows,
       MIN(trade_date) AS earliest, MAX(trade_date) AS latest
FROM industry_index_value
GROUP BY industry_name
ORDER BY industry_name;
"

# Industries that did NOT get indexed (and why we'd want to know)
psql -h localhost -U trip_ema_app -d trip_ema -c "
WITH classified AS (
    SELECT ii.industry, COUNT(*) AS tickers
    FROM instrument_industry ii
    JOIN tickers t ON t.ticker_id = ii.ticker_id
    WHERE t.is_active AND t.instrument_type = 'EQ' AND t.exchange = 'NSE'
    GROUP BY ii.industry
)
SELECT c.industry, c.tickers
FROM classified c
LEFT JOIN industry_index ix ON ix.industry_name = c.industry
WHERE ix.industry_name IS NULL
ORDER BY c.tickers DESC, c.industry;
"

# Latest daily market cap top-20 NSE EQ tickers
psql -h localhost -U trip_ema_app -d trip_ema -c "
SELECT t.symbol, mc.market_cap / 1e12 AS mcap_lakh_crore
FROM daily_market_cap mc
JOIN tickers t ON t.ticker_id = mc.ticker_id
WHERE mc.trade_date = (SELECT MAX(trade_date) FROM daily_market_cap)
  AND t.exchange = 'NSE'
ORDER BY mc.market_cap DESC
LIMIT 20;
"
```

### 12.5 Recovery — overrides and re-runs

```bash
# Operator override: reclassify a ticker into a different industry.
# This beats the yfinance auto-classification for both Phase 1 and
# the Phase 4B index selector.
trip-ema-etl --config config.yaml set-industry-override \
    --exchange NSE --symbol RELIANCE \
    --sector Energy --industry "Oil & Gas Integrated"

# Remove an override (revert to yfinance's classification)
trip-ema-etl --config config.yaml delete-industry-override \
    --exchange NSE --symbol RELIANCE

# Re-run a Phase 1-3 refresh that crashed mid-flight
trip-ema-etl --config config.yaml refresh-industry --exchange NSE --resume 42
trip-ema-etl --config config.yaml refresh-shares --exchange NSE --resume 43

# Force a Phase 1-4A re-run within the 80-day window
trip-ema-etl --config config.yaml refresh-industry --exchange NSE --force

# Operator-driven rebalance for one industry on a specific date
# (e.g. recovering from a partial rebalance or after override changes)
trip-ema-etl --config config.yaml rebalance-industry-index \
    --industry "Information Technology Services" --date 2026-04-01
```

### 12.6 Exit codes

The fundamentals CLI commands return:

- **0**: clean success
- **1**: PARTIAL — some work succeeded, some failed (typical when yfinance
  returns errors for a handful of tickers)
- **2**: fatal — couldn't run at all (bad arguments, exchange not configured,
  index not found)

In particular, `compute-market-cap --date today` returns 1 if no rows were
written. The usual cause is that EoD bars haven't been ingested yet for
the date — try yesterday's date or wait for the daily bars ingestion.

### 12.7 Indicator state (Phase 5)

After computing index values, you can compute the Triple-EMA indicator stack
for each industry index: 3 EMAs (medium/long/longest), ATR, 6 ATR bands, 2
regime states (`core_regime` and `display_regime`), bars-since-turn metrics,
and "higher valleys / lower tops" pattern labels. Output lands in the
`indicator_state` table, one wide row per (industry, trade_date).

Per-ticker indicator state isn't wired in yet (Phase 6+); only per-industry
works in this phase.

```bash
# Compute for one industry (replaces the temp.py scratch script)
trip-ema-etl --config config.yaml compute-indicator-state \
    --industry "Information Technology Services"

# All active industries with value rows
trip-ema-etl --config config.yaml compute-indicator-state --all-industries

# Force re-compute (skip-if-recent guard bypass)
trip-ema-etl --config config.yaml compute-indicator-state --all-industries --force
```

The compute is fast (no yfinance, no network — pure Python over local DB
reads). All 60 industries take ~30 seconds on a workstation.

Why no `--from-date` / `--to-date`: the regime state machine is sequential
through time. Computing the last 30 days alone would seed the EMAs and
regime from those 30 bars rather than the full history, producing different
(wrong) values. The runner always computes whole-history per industry.

Via HTTP:

```bash
TOKEN="$TRIP_EMA_ADMIN_TOKEN"

# Read indicator history for an industry
curl -s -H "Authorization: Bearer $TOKEN" \
     "http://localhost:8000/v1/fundamentals/indicator-state/industries/Information%20Technology%20Services?from_date=2026-04-01&limit=10" | jq

# Trigger fire-and-forget compute
curl -s -X POST -H "Authorization: Bearer $TOKEN" \
     "http://localhost:8000/v1/fundamentals/compute-indicator-state?all_industries=true"
```

Inspecting via SQL:

```bash
# Latest regime per industry.
# industry_indicator_state is DAILY grain: DISTINCT ON + ORDER BY date DESC
# takes the genuinely latest row. (`core_regime` was collapsed away in
# migration 0018 — there is only display_regime now.)
psql -h localhost -U trip_gui_user -d trip_ema -c "
SELECT DISTINCT ON (industry_name)
       industry_name,
       date,
       display_regime,
       round(med_ema::numeric, 2)  AS med_ema,
       round(long_ema::numeric, 2) AS long_ema,
       round(atr::numeric, 2)      AS atr
FROM industry_indicator_state
WHERE timeframe = '1W'
ORDER BY industry_name, date DESC;
"

# Industries currently in bullish display regime (display=+1)
psql -h localhost -U trip_gui_user -d trip_ema -c "
WITH latest AS (
    SELECT DISTINCT ON (industry_name)
           industry_name, date, display_regime, med_ema
    FROM industry_indicator_state
    WHERE timeframe = '1W'
    ORDER BY industry_name, date DESC
)
SELECT industry_name, date,
       round(med_ema::numeric, 2) AS med_ema
FROM latest
WHERE display_regime = 1
ORDER BY industry_name;
"

# Recent industry regime FLIPS (last 30 days).
# `med_turn_buy` no longer exists — the turn/pattern columns went with the
# 0018 regime collapse. A flip is now a change in display_regime between
# consecutive daily rows, which is exactly what LAG() finds.
psql -h localhost -U trip_gui_user -d trip_ema -c "
WITH d AS (
    SELECT industry_name, date, display_regime,
           LAG(display_regime) OVER (
               PARTITION BY industry_name ORDER BY date
           ) AS prev_regime
    FROM industry_indicator_state
    WHERE timeframe = '1W'
)
SELECT industry_name, date, prev_regime, display_regime
FROM d
WHERE prev_regime IS DISTINCT FROM display_regime
  AND date >= CURRENT_DATE - 30
ORDER BY date DESC, industry_name;
"
```

For the IT industry on a recent date, you should see something like:

```
 industry_name                    | trade_date | core_regime | display_regime | med_ema | long_ema | atr
----------------------------------+------------+-------------+----------------+---------+----------+-------
 Information Technology Services  | 2026-05-22 |          -1 |             -1 |  839.60 |   848.29 | 12.95
```

`fundamentals-status` now includes an "Indicator state coverage" section
after the "Industry indices" section, showing per-industry row counts.

### 12.8 Signal events (Phase 6-1)

After indicator state is computed, you can derive signal events:
four sparse event kinds keyed off `display_regime` transitions.

| Event kind | When it fires |
|---|---|
| `BUY` | `display_regime` transitions from non-+1 to +1 |
| `SELL` | `display_regime` transitions from non--1 to -1 |
| `EXIT_BUY` | `display_regime` transitions from +1 to non-+1 |
| `EXIT_SELL` | `display_regime` transitions from -1 to non--1 |

These map 1:1 to your Pine Script's `longCondition`,
`exitLongCondition`, `shortCondition`, `exitShortCondition`. Whether
to act on shorts is a downstream concern; this layer records all
four kinds unconditionally.

```bash
# After compute-indicator-state has populated indicator_state:
trip-ema-etl --config config.yaml compute-signals --all-industries

# Single industry
trip-ema-etl --config config.yaml compute-signals --industry "Information Technology Services"

# Force re-compute (bypass skip-if-recent 80-day guard)
trip-ema-etl --config config.yaml compute-signals --all-industries --force
```

Behavior:
  - Signals are derived purely from `indicator_state`; no network,
    no yfinance, very fast (~5 seconds for all industries).
  - **Delete-then-insert per industry**: when re-computing, all
    prior `signal_event` rows for the industry are deleted before
    the new ones are written. This guarantees the table reflects
    the current compute exactly — no lingering stale rows from
    an older `indicator_state`.

Via HTTP:

```bash
TOKEN="$TRIP_EMA_ADMIN_TOKEN"

# All BUY signals for IT in the last 90 days
curl -s -H "Authorization: Bearer $TOKEN" \
     "http://localhost:8000/v1/fundamentals/signal-events/industries/Information%20Technology%20Services?kind=BUY&from_date=2026-02-22" | jq

# Cross-industry scanner: all BUY signals in the last 7 days
curl -s -H "Authorization: Bearer $TOKEN" \
     "http://localhost:8000/v1/fundamentals/signal-events/recent?kind=BUY&days=7" | jq

# Trigger compute
curl -s -X POST -H "Authorization: Bearer $TOKEN" \
     "http://localhost:8000/v1/fundamentals/compute-signals?all_industries=true"
```

SQL inspection:

```bash
# Latest signal per industry
psql -h localhost -U trip_ema_app -d trip_ema -c "
SELECT industry_name, trade_date, event_kind,
       regime_before, regime_after,
       round(close_at_event::numeric, 2) AS close_at_event
FROM signal_event s1
WHERE industry_name IS NOT NULL
  AND trade_date = (
      SELECT MAX(trade_date) FROM signal_event
      WHERE industry_name = s1.industry_name
  )
ORDER BY trade_date DESC, industry_name;
"

# All BUY signals in the last 30 days, with the industry value at that bar
psql -h localhost -U trip_ema_app -d trip_ema -c "
SELECT industry_name, trade_date,
       round(close_at_event::numeric, 2) AS close
FROM signal_event
WHERE event_kind = 'BUY'
  AND industry_name IS NOT NULL
  AND trade_date >= CURRENT_DATE - INTERVAL '30 days'
ORDER BY trade_date DESC, industry_name;
"

# Per-kind counts per industry
psql -h localhost -U trip_ema_app -d trip_ema -c "
SELECT industry_name, event_kind, COUNT(*) AS n
FROM signal_event
WHERE industry_name IS NOT NULL
GROUP BY industry_name, event_kind
ORDER BY industry_name, event_kind;
"
```

`fundamentals-status` now includes a "Signal events coverage"
section showing per-industry event count and latest event date.

Pipeline recap:

```
1. trip-ema-etl refresh-industry --exchange NSE        # Phase 1
2. trip-ema-etl refresh-shares --exchange NSE          # Phase 2
3. trip-ema-etl refresh-financials --exchange NSE      # Phase 3
4. trip-ema-etl compute-market-cap                     # Phase 4A
5. trip-ema-etl build-industry-indices                 # Phase 4B
6. trip-ema-etl compute-index-values --all-active      # Phase 4B
7. trip-ema-etl compute-indicator-state --all-industries  # Phase 5
8. trip-ema-etl compute-signals --all-industries       # Phase 6-1  ← new
```

Each step's output is the next step's input. A full EoD refresh
re-runs all 8 in order.

### 12.9 Per-ticker indicators and signals (Phase 7)

Phase 5 and Phase 6 ship per-industry indicator state and signal
events. Phase 7 adds per-ticker variants for individual stocks.

```bash
# Compute indicator state for one ticker (weekly bars)
trip-ema-etl --config config.yaml compute-indicator-state \
    --ticker RELIANCE --exchange NSE --timeframe 1W

# Bulk per-ticker compute for all active NSE tickers, weekly
trip-ema-etl --config config.yaml compute-indicator-state \
    --all-tickers --timeframe 1W --exchange NSE

# Compute signals (depends on indicator_state being populated first)
trip-ema-etl --config config.yaml compute-signals \
    --ticker RELIANCE --exchange NSE --timeframe 1W

trip-ema-etl --config config.yaml compute-signals \
    --all-tickers --timeframe 1W --exchange NSE
```

Tickers with fewer than 30 bars on the requested timeframe are
skipped (counted as "skipped" not "failed" — operationally these
are usually newly-listed names without enough history to seed
EMAs).

Querying per-ticker results via SQL:

```bash
# Latest indicator state for RELIANCE.
# stock_indicator_state is DAILY grain (0034): each row is the WEEKLY chart
# recomputed with that day's forming bar. `date` is the trading day;
# `bar_timestamp` is the IST-Monday week anchor shared by the whole week —
# order by `date`, never by the anchor. (`core_regime` collapsed away in 0018.)
psql -h localhost -U trip_gui_user -d trip_ema -c "
SELECT i.date, i.bar_timestamp AS week_anchor,
       i.display_regime,
       round(i.med_ema::numeric, 2)  AS med_ema,
       round(i.long_ema::numeric, 2) AS long_ema
FROM stock_indicator_state i
JOIN tickers t ON i.ticker_id = t.ticker_id
WHERE t.symbol = 'RELIANCE' AND t.exchange = 'NSE'
  AND i.timeframe = '1W'
ORDER BY i.date DESC
LIMIT 5;
"

# All BUY signals in the last 30 days across NSE tickers (1W)
psql -h localhost -U trip_ema_app -d trip_ema -c "
SELECT t.symbol, s.trade_date,
       round(s.close_at_event::numeric, 2) AS close
FROM signal_event s
JOIN tickers t ON s.ticker_id = t.ticker_id
WHERE s.event_kind = 'BUY'
  AND s.ticker_id IS NOT NULL
  AND s.timeframe = '1W'
  AND t.exchange = 'NSE'
  AND s.trade_date >= CURRENT_DATE - INTERVAL '30 days'
ORDER BY s.trade_date DESC;
"
```

Via HTTP:

```bash
TOKEN="$TRIP_EMA_ADMIN_TOKEN"

# Read indicator history for a ticker
curl -s -H "Authorization: Bearer $TOKEN" \
     "http://localhost:8000/v1/fundamentals/indicator-state/tickers/42?timeframe=1W&limit=20" | jq

# Trigger fire-and-forget per-ticker compute
curl -s -X POST -H "Authorization: Bearer $TOKEN" \
     "http://localhost:8000/v1/fundamentals/compute-indicator-state?all_tickers=true&timeframe=1W&exchange=NSE"

curl -s -X POST -H "Authorization: Bearer $TOKEN" \
     "http://localhost:8000/v1/fundamentals/compute-signals?all_tickers=true&timeframe=1W&exchange=NSE"
```

### 12.10 EoD pipeline (Phase 4C)

Run all 8 fundamentals phases (10 with `--include-tickers`) as
one command:

```bash
# Daily EoD — per-industry only (default; ~5-10 min when shares/
# financials skip-if-recent, ~90 min if they actually fetch from
# yfinance)
trip-ema-etl --config config.yaml run-eod-pipeline --exchange NSE

# Heavier run that also processes individual tickers
trip-ema-etl --config config.yaml run-eod-pipeline \
    --exchange NSE --include-tickers --timeframe 1W

# Recovery: force re-compute of every phase (use sparingly —
# bypasses all skip-if-recent guards and may take hours)
trip-ema-etl --config config.yaml run-eod-pipeline \
    --exchange NSE --force
```

Output is a per-step progress table:

```
Running EoD pipeline on NSE (include_tickers=False, timeframe=1W, force=False)

  [SUCCESS ] refresh-industry                                3.2s  s=0 f=0 sk=1
             Skipping (recent: age=12 days < 80)
  [SUCCESS ] refresh-shares                                  4.1s  s=0 f=0 sk=1
  [SUCCESS ] refresh-financials                              5.0s  s=0 f=0 sk=1
  [SUCCESS ] compute-market-cap                             18.3s  s=2150 f=0 sk=87
             compute-market-cap on NSE for today: 2150 COMPUTED, 0 APPROX,...
  [SUCCESS ] build-industry-indices                         42.1s  s=60 f=0 sk=0
  [SUCCESS ] compute-index-values                           65.7s  s=57 f=0 sk=0
             compute-index-values: 57 SUCCESS, 0 PARTIAL, 0 FAILED (of 60 indices)
  [SUCCESS ] compute-indicator-state (industries)           23.4s  s=57 f=0 sk=0
  [SUCCESS ] compute-signals (industries)                    8.9s  s=57 f=0 sk=0

Pipeline SUCCESS in 170.7s (8 steps)
```

Exit codes:
  - **0** — overall SUCCESS.
  - **1** — overall PARTIAL (some steps PARTIAL, or some bulk
    units within a step failed but the step itself completed).
  - **2** — overall FAILED (a fatal step error aborted the
    pipeline; remaining steps marked NOT_RUN).

Via HTTP (fire-and-forget):

```bash
TOKEN="$TRIP_EMA_ADMIN_TOKEN"

curl -s -X POST -H "Authorization: Bearer $TOKEN" \
     "http://localhost:8000/v1/fundamentals/run-eod-pipeline?exchange=NSE"

# With per-ticker compute:
curl -s -X POST -H "Authorization: Bearer $TOKEN" \
     "http://localhost:8000/v1/fundamentals/run-eod-pipeline?exchange=NSE&include_tickers=true&timeframe=1W"
```

The HTTP variant returns 202 immediately and the pipeline runs in
the executor thread; check progress via `fundamentals-status` or
direct DB queries on `fundamentals_run`.

### 12.11 Pipeline ordering (updated)

```
EoD pipeline (default):
  1. refresh-industry          (yfinance, ~60 min if not cached)
  2. refresh-shares            (yfinance, ~60 min)
  3. refresh-financials        (yfinance, ~60 min)
  4. compute-market-cap        (local, ~20s)
  5. build-industry-indices    (local, ~40s)
  6. compute-index-values      (local, ~60s)
  7. compute-indicator-state   (local, ~25s for 60 industries)
  8. compute-signals           (local, ~10s for 60 industries)

EoD pipeline (--include-tickers):
  9. compute-indicator-state (tickers)  (local, ~5-10 min for ~3500)
 10. compute-signals (tickers)          (local, ~2-3 min)
```

Steps 1-3 are skip-if-recent for 80 days, so daily re-runs only
hit yfinance once per ~3 months. Steps 4-10 do real work whenever
their inputs (bars / market cap / index values / indicator state)
have advanced.

### 12.12 Running the pipeline on non-trading days

The pipeline is safe to run any day — on weekends and holidays
each step decides for itself whether there's work:

  - **refresh-industry/shares/financials**: skip-if-recent (80
    days) guard. After your first full run, subsequent daily runs
    skip these in seconds because the last completed run is recent.
    Both SUCCESS and PARTIAL status count as "recent" — ETF
    failures in shares/financials don't cause re-runs.
  - **compute-market-cap**: detects "no bars for today" and emits
    SKIPPED status. Doesn't abort.
  - **build-industry-indices**: in steady state (all qualifying
    industries already indexed), only sub-threshold industries
    appear in `failed`. The step reports PARTIAL but does not
    abort downstream phases — the existing index fleet is intact.
  - **compute-index-values/indicator-state/signals**: each has its
    own skip-if-recent guard (80 days), so re-runs on the same
    data are cheap.

Real-world Saturday run (2026-05-23, market closed):

```
  [SUCCESS ] refresh-industry                               0.0s  s=320 f=0 sk=0
  [SUCCESS ] refresh-shares                                 0.1s  s=0 f=0 sk=1     <- skip-if-recent
  [SUCCESS ] refresh-financials                             0.1s  s=0 f=0 sk=1     <- skip-if-recent
  [SKIPPED ] compute-market-cap                             0.6s  s=0 f=0 sk=2347  <- no-trading-day
             compute-market-cap on NSE: no bars for today...market was closed
  [PARTIAL ] build-industry-indices                         0.2s  s=0 f=3 sk=0     <- 3 sub-threshold industries
             ...steady state — existing indices unchanged...
  [SUCCESS ] compute-index-values                          12.4s  s=57 f=0 sk=0    <- nothing new from today
  [SUCCESS ] compute-indicator-state (industries)           ...
  [SUCCESS ] compute-signals (industries)                   ...

Pipeline PARTIAL in 15.3s
```

A Saturday run takes 15 seconds, not 3 hours.
