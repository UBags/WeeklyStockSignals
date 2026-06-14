# `trip_ema/scheduler/` — Compute scheduler, market orchestrator, distributor

The "what happens at each tick" layer. Owns the per-market tick loop,
the per-tick fetch + ingest + cache-update sequence, and the dispatcher
that turns regime changes into notification jobs.

## Files

| File | Purpose |
|---|---|
| `__init__.py` | Marker |
| `_wall_clock.py` | Thin wrapper around `datetime.now(tz=...)` — test seam for time |
| `compute_scheduler.py` | `ComputeScheduler.run_once()` — per-tick: detect gaps, fetch fresh bars, ingest, write cache, emit `signal_events` on transition, fire `SubscriberDistributor.notify(...)`. The single writer for `SignalCache` and `signal_events` |
| `market_calendar.py` | python-holidays + YAML override — "is this a trading day?" |
| `market_hours.py` | Per-exchange open/close + tz; "is the market open right now?" |
| `market_orchestrator.py` | `MarketOrchestrator` — owns the tick loop for **one** market. Combines `MarketHours` + `MarketCalendar` + `TickPlanner` to fire `ComputeScheduler.run_once()` at configured wall-clock instants |
| `orchestrator_pool.py` | `OrchestratorPool` — manages one `MarketOrchestrator` per configured market (NSE, BSE, NYSE, …). Each runs in its own daemon thread |
| `tick_planner.py` | Builds the tick schedule for a market day — post-open tick, intraday cadence, pre-close tick. Returns a list of "fire at" instants |
| `subscriber_distributor.py` | Background thread that, on `notify(SignalEvent)`, walks the subscriber set, applies per-subscriber filters, and enqueues `NotificationJob`s (to Redis if configured, in-process otherwise) |

## Invariants

- **3.2 single-writer cache** — only `ComputeScheduler` writes to
  `SignalCache` during normal operation (priming also writes, but
  priming and scheduler don't run concurrently)
- **3.10 single-writer signal_events** — only `ComputeScheduler`
  inserts into `signal_events`. The notifier reads
- **3.6 provisional rollback** — `ComputeScheduler` differentiates
  provisional (intra-bucket) from final (closed-bucket) bars and
  rolls back state on the next final bar after a provisional

## Tick lifecycle

```
MarketOrchestrator.thread_loop()
   wait until next tick instant (from TickPlanner)
   ComputeScheduler.run_once()
      for ticker in registry.iter_engines():
         bars = bars_repo.fetch_new_bars(ticker, since=cached_state.bar_timestamp)
         if no new bars:
             continue
         engine.ingest(bars)
         cache.write(ticker, engine.latest_state())
         if regime transitioned:
             signals_repo.insert(SignalEvent(...))
             SubscriberDistributor.notify(event)
```

## Why one process per market

- Each market has different tz, holidays, hours — concurrency simplifies
  per-market state ownership
- Markets don't share state with each other (no cross-market signal
  emits today), so threading them is safe
- `OrchestratorPool` joins all threads on service shutdown

## Tests

Two trees:
- `tests/scheduler/` — full integration (DB-required); covers
  `MarketOrchestrator`, `OrchestratorPool`, `ComputeScheduler`,
  `SubscriberDistributor`, market hours/calendar, tick planner
- `tests/scheduler_pure/` — pure-Python tests that need no DB —
  notably `test_gap_detection.py`

## Phase 14.6 — per-exchange visibility

The `exchange_state` table tracks per-exchange last-action timestamps
(`last_eod_at`, `last_intraday_at`, etc.). The orchestrator updates
this on every tick; the GUI's System Health card reads it. The change
makes "is NSE behind on ETL?" answerable in one query rather than
inferring from `etl_runs`.
