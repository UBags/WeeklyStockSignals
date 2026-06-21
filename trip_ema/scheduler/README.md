# `trip_ema/scheduler/` — periodic loops + market orchestration

The wall-clock-driven side of the service: the periodic compute loop,
the 15-minute notification distributor, the per-market orchestrator
that fires the fetch → compute → distribute pipeline, and the
multi-market calendar / hours services they consume.

(The quarterly fundamentals scheduler lives in
`trip_ema/fundamentals/scheduler.py` — different cadence, different
job.)

## Files

| File | Purpose |
|---|---|
| `__init__.py` | Re-exports `ComputeScheduler`, `SubscriberDistributor`, `MarketOrchestrator`, `OrchestratorPool`, the tick planner, the calendar, the hours service |
| `compute_scheduler.py` | `ComputeScheduler` — periodic compute loop. On a wall-clock cadence, iterates every (ticker, tf) in the warm registry. Fetches new bars from the DB since the engine's last bar, distinguishes provisional (still-open bucket) from final, ingests accordingly, classifies any signal change against the previous compute's display state (read from the cache), persists a `SignalEvent` if changed, replaces the cache entry. **The only writer to `SignalCache`** |
| `market_orchestrator.py` | `MarketOrchestrator` — one per exchange. Wall-clock-anchored loop (via `tick_planner.next_tick_after`) that fires at the per-market schedule (post-open + hourly + pre-close, trading days only). Each tick runs **fetch → compute → distribute** and publishes a summary to the `PulsePublisher` |
| `orchestrator_pool.py` | `OrchestratorPool` — owns N `MarketOrchestrator`s + their lifecycle. Built at startup from `config.market_hours`. `start_all()` / `stop_all()` driven by `app.py`'s startup/shutdown hooks |
| `subscriber_distributor.py` | `SubscriberDistributor` — the 15-minute notification loop. Per due subscriber: query pending events (subject to ticker/signal filters + per-subscriber 3-notifications cap), compose a batch, dispatch via the subscriber's transport, append a `notification_log` row. Honours `MarketHoursService.any_open()` by default (configurable) |
| `tick_planner.py` | `next_tick_after(now, market, cal) -> utc_instant`. Two scheduling modes selected by whether `market.tick_schedule` is populated: explicit schedule (post-open / pre-close / hourly between) or fixed-cadence fallback |
| `market_calendar.py` | `TradingCalendar` per market — generalises the old NSE-only calendar to arbitrary exchanges. Backing sources in priority order: YAML override → `holidays` package by country → weekday-only fallback |
| `market_hours.py` | `MarketHoursService` — "is exchange X open at time T?". Stateless, thread-safe, configured from `Config.market_hours`. Open/close times in exchange-local TZ + ISO weekday list. Callers pass UTC; conversions happen internally |
| `_wall_clock.py` | Tick-math helpers shared by all scheduler loops (`ComputeScheduler`, `SubscriberDistributor`, `MarketOrchestrator`). Avoids three subtly-different copies of `_seconds_until_next_tick` drifting apart |

## Threading model

- `ComputeScheduler` runs on its own thread.
- `SubscriberDistributor` runs on its own thread.
- Each `MarketOrchestrator` runs on its own thread.
- All sleep in **1-second chunks** so shutdown signals are picked up
  promptly.
- Shutdown is graceful: the pool's `stop_all()` sets stop events on
  every loop and joins.

## Writer contract

`ComputeScheduler` is the **only writer to `SignalCache`**. Readers
(`api.cached`, `SubscriberDistributor`) acquire no lock. This invariant
is what keeps the HTTP hot path lock-free.

## Stale-event suppression

The distributor's `find_pending_for_subscriber` query suppresses an
older event for the same `(ticker, timeframe)` once a newer event
exists, even if the older event has remaining notification budget.
The subscriber sees the latest signal — not a backlog of obsolete
ones.

## Why a pool, not one orchestrator

Different markets have different calendars, different hours, and
different rate-limit budgets. One pool member per market lets each
move at its own pace; a single shared loop would have to take the
slowest pace.
