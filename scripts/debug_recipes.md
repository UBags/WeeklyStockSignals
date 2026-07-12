# trip-ema debug recipes

Less-common workflows for diagnosing problems. The everyday operator
recipes live in [`operator_commands.md`](operator_commands.md); use this
file when something's broken or you're chasing a regression.

---

## 1. Force the orchestrator to fire a tick now

The per-market orchestrator runs on a cadence configured in `config.yaml`
(hourly by default). After a manual EoD or a config change you sometimes
want to fire a tick without waiting:

```bash
# After pulling new bars manually:
trip-ema-etl incremental-eod --exchange NSE

# Force the NSE orchestrator to tick now (don't wait for the hourly):
curl -X POST -H "Authorization: Bearer $TRIP_EMA_ADMIN_TOKEN" \
  http://localhost:8000/v1/admin/orchestrator/NSE/run-now

# Watch the pulse stream for the result:
curl -N http://localhost:8000/v1/pulse/stream
```

The `pulse/stream` endpoint is Server-Sent Events. Keep the `curl -N` open
in one terminal while you work in another; you'll see tick events as they
fire.

---

## 2. Check orchestrator status (per-exchange)

```bash
curl -s -H "Authorization: Bearer $TRIP_EMA_ADMIN_TOKEN" \
  http://localhost:8000/v1/admin/orchestrator/status \
  | python3 -m json.tool
```

Shows whether each orchestrator is `running`/`paused`/`failed` and when it
last ticked. If `paused` after a restart, the orchestrator's enable flag
in config may be off:

```bash
grep -A 3 "orchestrator" config.yaml
```

---

## 3. Pause / resume an orchestrator at runtime

```bash
# Pause NSE — the EoD/intraday cadence still runs, but the orchestrator
# stops producing pulse events.
curl -X POST -H "Authorization: Bearer $TRIP_EMA_ADMIN_TOKEN" \
  http://localhost:8000/v1/admin/orchestrator/NSE/pause

# Resume:
curl -X POST -H "Authorization: Bearer $TRIP_EMA_ADMIN_TOKEN" \
  http://localhost:8000/v1/admin/orchestrator/NSE/resume
```

Useful when investigating bad data — pause the orchestrator, fix the
data, then resume.

---

## 4. Tail the service log selectively

```bash
# Errors only
tail -f /tmp/trip-ema.log | grep -E '(ERROR|FATAL|Traceback)'

# Just the ETL runner
tail -f /tmp/trip-ema.log | grep 'trip_ema.etl.runner'

# Just the orchestrator
tail -f /tmp/trip-ema.log | grep 'trip_ema.scheduler.orchestrator'

# Specific correlation ID (after grepping one out of a run summary)
tail -f /tmp/trip-ema.log | grep 'correlation_id=corr-abc123'
```

---

## 5. Inspect a specific ETL run by ID

The runs table in the GUI / `trip-ema-etl status` shows summary rows.
To get full context for one run:

```bash
psql -h localhost -U trip_ema_app -d trip_ema -c "
SELECT *
FROM etl_runs
WHERE run_id = 42;"

# What did this run do at the per-ticker level?
psql -h localhost -U trip_ema_app -d trip_ema -c "
SELECT
    t.exchange, t.symbol, ts.outcome, ts.bars_written, ts.error_message
FROM etl_ticker_status ts
JOIN tickers t USING (ticker_id)
WHERE ts.last_run_id = 42
ORDER BY ts.outcome, t.symbol;"
```

(`etl_ticker_status` is updated by every run; `last_run_id` tracks the
most recent one. For full per-run history you'd need a journal table —
trip-ema doesn't currently keep one. The service log + `etl_runs` notes
field are the audit trail.)

---

## 6. Find parked tickers and why they're parked

```bash
psql -h localhost -U trip_ema_app -d trip_ema -c "
SELECT
    t.exchange, t.symbol,
    ts.parked_at, ts.failure_streak, ts.last_error
FROM etl_ticker_status ts
JOIN tickers t USING (ticker_id)
WHERE ts.is_parked
ORDER BY ts.parked_at DESC
LIMIT 50;"
```

Common reasons:
- `delisted` — vendor returned no data for N consecutive runs
- `yfinance returned no data for symbol` — symbol may be wrong in
  the listings CSV; check `symbol_history`
- timeouts during a network blip — usually recover on next EoD;
  use `unpark --exchange NSE` to retry now

---

## 7. Investigate a single ticker's signal history

```bash
# Most recent signals for one ticker
psql -h localhost -U trip_ema_app -d trip_ema -c "
SELECT
    s.signal_type, s.price_at_signal,
    s.bar_timestamp, s.computed_at
FROM historical_signals s
JOIN tickers t USING (ticker_id)
WHERE t.exchange = 'NSE' AND t.symbol = 'RELIANCE'
ORDER BY s.computed_at DESC
LIMIT 20;"

# Current signal state (what the engine thinks the position is)
psql -h localhost -U trip_ema_app -d trip_ema -c "
SELECT *
FROM ticker_signal_state ts
JOIN tickers t USING (ticker_id)
WHERE t.exchange = 'NSE' AND t.symbol = 'RELIANCE';"
```

The chart view in the GUI shows the same thing with the indicator
overlay drawn in. For deep audits, the SQL is faster.

---

## 8. Get the raw bars used for a recent signal

```bash
psql -h localhost -U trip_ema_app -d trip_ema -c "
SELECT
    b.bar_timestamp,
    b.open, b.high, b.low, b.close, b.volume
FROM ohlcv_bars b
JOIN tickers t USING (ticker_id)
WHERE t.exchange = 'NSE' AND t.symbol = 'RELIANCE'
  AND b.timeframe = '1W'
  AND b.bar_timestamp >= NOW() - INTERVAL '90 days'
ORDER BY b.bar_timestamp DESC;"
```

Pine-parity reproduction lives in the `tests/parity/` directory if you
need to verify the indicator's behavior against TradingView.

---

## 9. Diff the schema between Alembic and legacy

If `tests/persistence/test_legacy_migrator.py::test_alembic_and_legacy_produce_same_tables`
ever fails, the two migration paths have drifted. To see which tables differ:

```bash
# Current Alembic schema in production
psql -h localhost -U trip_ema_app -d trip_ema -c "
SELECT table_name FROM information_schema.tables
WHERE table_schema = 'public'
ORDER BY table_name;"

# Legacy SQL files
ls migrations/legacy/*.sql
```

Then compare. The post-Phase-12 expectation: every Alembic revision that
creates or modifies a table has a matching `NNN_*.sql` parity file in
`migrations/legacy/`. See `migrations/legacy/README.md` for the obligation.

---

## 10. Reset the GUI's stored preferences

The GUI uses QSettings (`~/.config/Anthropic/trip-ema.conf` on Linux). To
wipe everything:

```bash
rm ~/.config/Anthropic/trip-ema.conf       # path may vary by distro
```

Or to reset just one key (e.g. the picker's last-selected exchange after a
config change removed that exchange):

```bash
python3 -c "
from PySide6.QtCore import QSettings
s = QSettings('Anthropic', 'trip-ema')
s.remove('etl/last_selected_exchange')
s.sync()
print('cleared')
"
```

---

## 11. Manual Alembic operations

```bash
# What revision is the DB at?
alembic current

# What's the head of the source tree?
alembic heads

# Apply pending migrations
alembic upgrade head

# Downgrade by one (risky — destructive)
alembic downgrade -1

# Generate a new revision (autogenerate, then hand-edit)
alembic revision --autogenerate -m "describe the change"
```

The repo also has `alembic_pkg/sql/*.sql` and `migrations/legacy/*.sql` —
those are the SQL bodies the Alembic revisions execute and the parity
parallels respectively. Don't edit them by hand once committed; create a
new revision instead.

---

## 12. SSE pulse stream — what to expect

`/v1/pulse/stream` emits one event per orchestrator tick. Each event is a
small JSON document:

```bash
curl -N http://localhost:8000/v1/pulse/stream
```

Output looks like:

```
data: {"exchange":"NSE","tick_at":"...","signals_emitted":0,"...":"..."}

data: {"exchange":"NSE","tick_at":"...","signals_emitted":3,"...":"..."}
```

The blank line between events is part of the SSE protocol. To process the
stream programmatically, use `httpx` or `requests` with `stream=True`.

A quiet stream (no events for >2× the orchestrator cadence) usually means
the orchestrator is paused, the cadence is misconfigured, or the
orchestrator failed silently — check `/v1/admin/orchestrator/status`.

---

## 13. When the service won't start

Most common failure modes:

1. **Port already bound.** `./scripts/restart_service.sh` handles this,
   but if you started the service some other way, kill what's on port 8000.

2. **DB password not set.** The service exits early with a DB connection
   error. Check the log: `grep -i 'password' /tmp/trip-ema.log`.

3. **Migrations not applied.** New install or after a code update with
   pending revisions. Run `alembic upgrade head`.

4. **Priming taking too long.** With 6M+ bars and 2700+ tickers, cold
   priming can take 60–90 seconds. The default
   `restart_service.sh` waits 120s; bump `PRIME_WAIT_SECONDS` if your
   machine is slower.

5. **No exchanges configured.** `market_hours:` is empty or missing in
   `config.yaml`. The service starts but does nothing useful.

For anything else, read `/tmp/trip-ema.log` from the top — startup errors
are loud and have stack traces.
