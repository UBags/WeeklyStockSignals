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
FROM signal_events s
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
FROM signal_events s
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
FROM signal_events s
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
FROM signal_events s
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
