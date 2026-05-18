# trip_ema — Architecture & Invariants

This document is the design contract for `trip_ema`. It exists so that
six months from now, when someone — possibly you — is about to change
something in the codebase, they can read this first and understand
*why* the system is shaped the way it is, and which constraints they
must not silently break.

If you find yourself violating an invariant listed here, that's a
signal to either (a) stop and rethink, or (b) update this document
along with the code. The invariants aren't decoration; each one was
purchased with a bug.

---

## 1. What the system does, in one paragraph

`trip_ema` is a Python port of a Pine Script indicator (Weekly Triple
EMA + ATR bands + regime classification) for tickers traded on NSE/BSE
(Indian markets) and adjacent exchanges. It ingests OHLCV bars from a
Postgres table written by an external ETL pipeline, computes a
weekly-timeframe signal per ticker, persists state changes to
`signal_events`, caches the latest snapshot in memory for fast API
reads, and delivers notifications to subscribers via email/webhook/
Slack/etc. through a Redis-queued worker.

It is faithfully numerical with the Pine source — to the point where
NaN warmup semantics are matched.

## 2. Actors

```
┌──────────┐    writes OHLCV   ┌────────────┐
│   ETL    │ ────────────────▶ │  Postgres  │
└──────────┘                   └─────┬──────┘
                                     │ reads bars
                                     ▼
                  ┌──────────────────────────────────────┐
                  │  ComputeScheduler  (single thread)   │
                  │  ┌────────────────────────────────┐  │
                  │  │ TickerRegistry → TickerEngine  │  │
                  │  │   (per-ticker indicator state) │  │
                  │  └────────────────────────────────┘  │
                  │           │            │             │
                  │  writes   ▼            ▼  writes     │
                  │   ┌────────────┐  ┌────────────────┐ │
                  │   │ SignalCache│  │ signal_events  │ │
                  │   │ (memory)   │  │   (Postgres)   │ │
                  │   └────────────┘  └────────────────┘ │
                  └──────────────────────────────────────┘
                                     │
                  reads cache ◀──────┘           reads events
                       │                              │
                       ▼                              ▼
                  ┌──────────┐                ┌──────────────────────┐
                  │ HTTP API │                │ SubscriberDistributor│
                  │  (FastAPI)│               │   (background thread)│
                  └──────────┘                └──────────┬───────────┘
                                                         │ enqueues
                                                         ▼
                                                  ┌──────────────┐
                                                  │ Redis Queue  │
                                                  └──────┬───────┘
                                                         │ pops
                                                         ▼
                                                  ┌──────────────┐
                                                  │ NotifierWorker│
                                                  │  (1+ threads)│
                                                  └──────────────┘
                                                         │
                                                         ▼
                                       email / webhook / Slack / ...
```

## 3. Core invariants

These are absolute. Violating one means breaking some user-visible
guarantee.

### 3.1 The ETL is the source of truth for bar availability.

The application *consumes* what's in Postgres; it does not infer,
generate, or interpolate missing bars. If the ETL hasn't written week
T+1's source bars, the application refuses to advance the engine for
that ticker until the data arrives. A "data gap" is an ops event, not
a math problem the app can solve.

Why this matters: a previous design tried to be clever about partial
weeks. It caused silent state corruption when the gap eventually filled
and bars arrived in non-chronological order. Now the rule is: empty
weekly bucket = stale ticker = no compute. See `_check_for_data_gap`
in `compute_scheduler.py`.

### 3.2 Single writer to `SignalCache`.

Only `ComputeScheduler` writes to the cache. Other components (HTTP
handlers, distributor, notifier worker) read but never mutate. The
cache's internal `threading.Lock` exists for defense, not because the
write pattern is contended.

If a future change introduces a second writer, the lock guarantees
become more important and the read paths may need to copy rather than
return live references. Don't add a second writer without thinking
about this.

### 3.3 Pine Script numerical fidelity is non-negotiable.

The indicator math must match Pine's `ta.ema`, `ta.sma`, `ta.atr`,
`ta.rma` to floating-point parity. Specifically:

- `ta.sma(volume, N)` returns NaN until N samples are available. The
  Python VW-EMA mirrors this with an early return.
- `ta.atr(N) = ta.rma(tr, N)`. RMA returns NaN for bars 1..(N-1),
  seeds from `sma(tr[0:N])` at bar N, then applies Wilder's recurrence.
- All EMAs use `α = 2 / (length + 1)`.
- WMA uses linear weights `[1, 2, ..., n]` divided by `n(n+1)/2`.

If a refactor proposes "simplifying" any of these, the corresponding
test in `tests/indicators/` should fail. If it doesn't, the test is
under-specified, not the code.

### 3.4 Domain types are immutable.

`OHLCVBar`, `BarState`, `SignalEvent`, `CachedSignal`, `TickerRef`,
`ChangeSignal`, `CoreRegime`, `IndicatorParams` are all
`@dataclass(frozen=True, slots=True)` or `Enum`. Mutation is replacement:
build a new value, atomically swap.

Why: makes reasoning about who-can-see-what trivial, makes the
single-writer-cache invariant enforceable, makes snapshots cheap
(copy a reference, not deep-copy a graph).

### 3.5 All timestamps are UTC timezone-aware.

`bar_timestamp`, `computed_at`, `last_sent_at`, every datetime in the
domain — all UTC, all `tzinfo`-aware. Naive datetimes are rejected
defensively (e.g., `_monday_of` raises ValueError).

Why: a previous bug had us silently treating naive datetimes as UTC.
That hid IST/ET week-anchor bugs for weeks. Never again.

Local-zone interpretation happens at the boundary functions
(`_monday_of`, `bucket_end`) using `ZoneInfo`. Internal state never
holds a naive datetime.

### 3.6 Provisional ↔ final bar semantics.

The engine accepts two ingest paths:

- `ingest(bar)` — final, committed. Snapshot any held provisional state
  before applying.
- `ingest_provisional(bar)` — re-applicable. Take a snapshot if not
  already held; on subsequent calls with the same timestamp, roll back
  to the snapshot and re-apply with the updated bar values.

When a provisional call arrives with a timestamp *newer* than the
held snapshot's bar, the snapshot is committed automatically before
the new provisional is taken (A1 erased-bar fix).

The `_states` deque is snapshot by *contents* (tuple), not length.
Length-based rollback is broken at the deque's `maxlen` — the deque
silently drops the oldest entry on append, and the rollback can't
recover it (Phase 9 A1 fix).

### 3.7 DST-correct week boundaries.

`bucket_end` does calendar arithmetic on local dates, then converts
back to UTC. Adding `timedelta(days=7)` to a UTC-aware Monday gives
the wrong instant during DST transition weeks in zones like
America/New_York or Europe/London (167h or 169h local weeks). For
IST, UTC, and other non-DST zones, the two approaches agree.

### 3.8 No subscriber-visible STALE_DATA notifications.

When a ticker is stale, signal events for it are *not* generated and
nothing flows to subscribers. The staleness is an ops signal only,
exposed via `/v1/admin/stale-tickers` and `stale_tickers_count` in
`/readyz`. Subscribers see only regime-change events.

Why: mixing operational signals into the subscriber notification
stream confuses users ("why am I getting an EXIT followed by a STALE
followed by a BUY?") and dilutes the meaning of a trading signal.
The two concerns live on separate planes by design.

### 3.9 `stale_since` is the FIRST observation, never overwritten.

When `_mark_ticker_stale` runs on a cache entry that's already stale,
it preserves the existing `stale_since` rather than updating to `now`.
This means dashboards can show "stale for X hours" instead of
"stale as of latest poll." If you ever find yourself writing
`stale_since=now` unconditionally, stop.

### 3.10 Notification dedup is enforced at the application layer.

Three layers of defense:

1. `find_pending_for_subscriber` filters `DISTINCT ON (ticker_id,
   timeframe)` so each subscriber sees only the latest event per
   ticker/timeframe.
2. The Redis dedup hash prevents in-flight double-enqueue of the same
   `(subscriber_id, event_id)` pair.
3. `is_event_superseded` runs at the worker's last moment before
   dispatch, dropping stale jobs that newer events have rendered
   irrelevant.

The database `notification_log` table has NO unique constraint
enforcing dedup (the previous one was broken because `last_sent_at`
made every row unique). Migration 008 dropped it. Don't try to put
the constraint back without solving the partitioned-table-with-
non-unique-partition-key problem first.

### 3.11 The cache is bounded by ticker count, not by time.

`SignalCache` is a dict keyed by `(TickerRef, Timeframe)`. Each entry
has a bounded `state_history` tuple. Memory grows with ticker count,
not with time. At 5000 tickers × 200 BarStates × ~150 bytes ≈ 150MB.

If you ever consider adding LRU eviction here, ask first: what
problem are you solving? The cache size is a design parameter, not
an emergent leak.

### 3.12 Observability is additive — never load-bearing.

The observability stack (`trip_ema/observability/`) consists of:

- **Correlation IDs** (`contextvars.ContextVar`) — generated at the
  scheduler when a logical operation begins; propagated through Redis
  payloads to the worker. Stripping them must never change behavior.
- **Prometheus metrics** — counters/gauges/histograms on a separate
  port (default disabled). A failure to start the metrics server
  logs loudly but does NOT prevent the main service from booting.
  If `prometheus_client` isn't installed at all, the metric symbols
  degrade to no-op stubs and the rest of the codebase keeps importing
  cleanly.
- **OpenTelemetry tracing** — off by default. Every span site uses
  `get_tracer().start_as_current_span(...)`; the no-op tracer is the
  default when OTel isn't configured. Span attribute setters are
  guarded so they never raise even outside an active span. If the
  `opentelemetry` packages aren't installed, `get_tracer()` returns
  a local in-module no-op tracer rather than `ImportError`.

The contract: removing or breaking any observability code path
must not cause user-facing behavior to change. If a test fails
because of a metrics import failure, the fix is to make the import
defensive, not to make the metric required.

**Latency-when-disabled:** when tracing is disabled (the default),
the only runtime cost is a `get_tracer()` call returning a cached
no-op tracer plus a no-op context-manager entry/exit per span site.
This is sub-microsecond; it does not change user-visible latency
characteristics. The same holds for metrics when `prometheus_client`
is missing — the no-op `inc`/`set`/`observe` methods are bare-body
function calls.

### 3.13 Circuit breakers wrap transports; outcome shape unchanged.

`CircuitBreakingTransport` is an adapter around `NotificationTransport`:
same `send(address, batch) -> bool` interface. When OPEN, it returns
`False` (the worker's standard failure path: nack and retry on the
next distributor cycle). The worker doesn't know whether `False` came
from a real transport failure or a fast-fail. That's intentional —
the breaker provides backpressure without complicating the worker.

Per-transport instance: SMTP problems must not trip the webhook
breaker. The breaker name (= the transport key) appears in metric
labels so dashboards can show per-transport health independently.

### 3.14 Runtime process boundaries: Engine, ETL, GUI.

trip_ema ships three distinct runtime entities. They share Postgres
and (optionally) Redis; they do NOT share Python state, threads, or
configuration on disk.

**The Engine** (`trip-ema`) is the long-running service. It hosts the
`TickerRegistry`, the `ComputeScheduler`, the `SignalCache`, and the
HTTP API. It is the single writer to `SignalCache` (invariant 3.2)
and the only process that may compute signal events.

**The ETL** (`trip-ema-etl`) is a separate CLI invoked manually or
via cron. It is the single writer to `ohlcv_bars`, `tickers`, and the
two ETL bookkeeping tables (`etl_runs`, `etl_ticker_status`). It does
not share memory or threads with the Engine. Its existence as a
separate process is what makes invariant 3.1 ("ETL is the source of
truth for bar availability") cleanly enforceable — the Engine has no
way to write bars even if some future code path tried.

**The GUI** (`trip-ema-console`) is a desktop PySide6 client. It
interacts with the Engine via the HTTP Admin API for all writes and
reads from Postgres directly (read-only) for table views. It MUST
treat the Engine's `config.yaml` as read-only. GUI preferences
(refresh cadences, default chart range) live in `QSettings` —
NEVER in `config.yaml`. The GUI's only filesystem write to the
config file is the explicit Backup action (`shutil.copy2` to a
user-chosen path), and that COPIES, never modifies in place.

**One documented exception — ETL triggered from the GUI:**
The admin endpoint `POST /v1/admin/etl/run/{kind}` runs the ETL
inside the Engine process via a bounded `ThreadPoolExecutor`
(`max_workers=2` by default). This is an operational convenience —
operators can click "backfill" in the GUI without shell access. It
is NOT a violation of the boundary: the runner still writes to
Postgres through the same code path as the standalone CLI, the
work is off the HTTP request thread, and the executor is capped.
If yfinance ever proves unreliable enough to wedge the main service,
the right fix is to switch the endpoint to `subprocess.Popen` rather
than removing the GUI feature.

## 4. Layer responsibilities

| Layer | Reads from | Writes to | Owns |
|-------|------------|-----------|------|
| `domain` | nothing | nothing | the type vocabulary |
| `indicators` | nothing | nothing | pure math (EMA, ATR, WMA, aggregation) |
| `engine` | indicators | nothing external | per-ticker state machine |
| `persistence` | Postgres | Postgres | SQL boundary |
| `cache` | nothing | nothing | in-memory signal snapshots |
| `registry` | persistence | engine | ticker→engine map; priming |
| `scheduler` | persistence, registry, engine | cache, persistence, partition extender | the compute loop |
| `notifications` | nothing external | transport endpoints | message format, transports, circuit breakers |
| `notifier_worker` | Redis, persistence | persistence, transports | dispatch + last-mile staleness |
| `transports.http` | cache, registry, persistence | response serialization | REST API |
| `api` | registry, persistence, cache | registry, cache | lifecycle (prime_all) |
| `observability` | nothing | log streams, metrics endpoint, tracer | logging, metrics, correlation IDs, OTel |

A change that crosses these boundaries the wrong way (e.g., the
cache reading from Postgres, or `indicators` importing from
`persistence`) is a smell. Cross-cutting concerns belong in `api`
or `scheduler`.

## 5. What's NOT enforced

Things that look like invariants but aren't:

- **Cache freshness vs. database `signal_events`**. The cache lags
  the database by up to one compute cycle. If you need the absolute
  latest committed event, query `signal_events` directly, not the
  cache.
- **Subscriber-side ordering**. Out-of-order delivery within a
  transport (email being slower than webhook for the same event) is
  not guarded. The system protects against stale events being
  delivered *after* fresher ones via `is_event_superseded`, but
  doesn't synchronize delivery times across transports for the same
  event.
- **Idempotency of transport-level retries**. If `transport.send()`
  succeeds at the network level but the response is lost before we
  see it, we will re-send on the next cycle. The dedup hash gives
  us at-least-once within the dedup window (default 1 hour). Beyond
  that window, duplicates are possible. We chose at-least-once over
  at-most-once because missing a signal is worse than seeing it
  twice.

## 6. When to break an invariant

Document the exception explicitly in this file before merging the
change. If it's not in this document, the next reviewer is entitled
to revert it.

## 7. Quick map: where to look for what

| Question | File(s) |
|----------|---------|
| How does an EMA actually compute? | `trip_ema/indicators/vw_ema.py` |
| Where does Pine's NaN warmup live? | `trip_ema/indicators/vw_ema.py::update`, `trip_ema/indicators/atr.py::update` |
| How does the engine handle a re-applied provisional? | `trip_ema/engine/ticker_engine.py::ingest_provisional`, `_apply_bar`, `_take_snapshot`, `_rollback_to_snapshot` |
| Where is gap detection? | `trip_ema/scheduler/compute_scheduler.py::_check_for_data_gap`, `trip_ema/indicators/aggregation.py::detect_weekly_gaps`, `trip_ema/persistence/bars_repo.py::weekly_buckets_with_bars` |
| How do timestamps cross zones? | `trip_ema/indicators/aggregation.py::_monday_of`, `bucket_end` |
| Where is the change-classifier (signal generation rule)? | `trip_ema/engine/change_classifier.py` |
| Where does a notification's lifecycle live? | `trip_ema/scheduler/subscriber_distributor.py` (enqueue), `trip_ema/notifier_worker.py` (dispatch) |
| Where are correlation IDs minted and propagated? | `trip_ema/observability/correlation.py`, `trip_ema/scheduler/compute_scheduler.py::run_once`, `subscriber_distributor.py::_enqueue_jobs`, `notifier_worker.py::_handle_job` |
| Where are Prometheus metrics defined? | `trip_ema/observability/metrics.py` |
| Where are OTel spans created? | `compute_scheduler.py`, `subscriber_distributor.py`, `notifier_worker.py` (search for `start_as_current_span`) |
| How do circuit breakers wrap a transport? | `trip_ema/notifications/circuit_breaker.py`; wiring in `trip_ema/app.py::_wrap_transports_with_breakers` and `notifier_worker.py` |
| What's the DB schema? | `migrations/*.sql` in order |
| Where does the ETL fetch bars from? | `trip_ema/etl/yfinance_source.py`; CLI in `trip_ema/etl/cli.py`; orchestration in `trip_ema/etl/runner.py` |
| How are ETL failures parked? | `trip_ema/etl/parked.py` (3-strike counter); state in `etl_ticker_status` table |
| Where is tier-based scheduling logic? | `trip_ema/etl/tiering.py` (NIFTY-200 / next-500 / rest bucketing) |
| Where does the operator console live? | `trip_ema/gui/`; entry point `trip_ema/gui/console.py`; shell `trip_ema/gui/main_window.py`; individual tabs in `trip_ema/gui/views/` |
| How does the chart replay the engine server-side? | `trip_ema/transports/http/routes/signals.py::get_signal_history` — primes a fresh `TickerEngine` over the requested window |
| How are admin write actions gated? | `trip_ema/transports/http/dependencies.py::require_admin_token` (bearer token via `http.admin_token_env`) |
| Where do GUI prefs persist? | `QSettings` group `trip_ema/gui`; load/save helpers in `trip_ema/gui/views/settings.py` (`load_gui_settings`, `save_gui_settings`) |

## 8. Deployment & Operational Notes

This section is for operators, not for code reviewers. It documents
the runtime relationships and defaults that aren't directly visible
in the source.

### 8.1 Single-writer, single-process by design.

The current deployment model is **one process** running the FastAPI
app + the `ComputeScheduler` thread + (optionally) the
`SubscriberDistributor` thread. Do NOT run uvicorn with `--workers N`
— it would produce N copies of every ticker engine, N schedulers
racing on the DB, and N caches that don't agree. The single-writer
invariants (3.2 SignalCache, 3.6 engine provisional/final, 3.10
notification dedup) all assume one process.

If you eventually need horizontal scale, see invariant 3.2 + Grok's
deferred "horizontal scaling" item (April 2026 review): you'd need
leader election or ticker sharding, not naive process multiplication.

### 8.2 Graceful-shutdown timing.

The graceful-shutdown sequence is:

  1. uvicorn receives SIGTERM (or SIGINT)
  2. uvicorn waits for in-flight HTTP requests, up to
     `--timeout-graceful-shutdown` (default 30s)
  3. uvicorn invokes the ASGI lifespan shutdown
  4. Our shutdown callback runs: distributor.stop() →
     compute_scheduler.stop() → notifier_proc.terminate() →
     join(timeout=`http.notifier_subprocess_shutdown_timeout_seconds`)
     → metrics_server.stop() → db_engine.dispose()
  5. uvicorn exits

The whole sequence must fit inside `--timeout-graceful-shutdown` or
uvicorn force-kills the process partway through. **Recommended
relationship:** set `http.notifier_subprocess_shutdown_timeout_seconds`
to roughly 50% of uvicorn's value (so steps 2-4 leave headroom for
step 4's final db.dispose). At defaults (15s notifier, 30s uvicorn),
that's already correct. If you bump the notifier timeout, bump
uvicorn's too.

### 8.3 Prometheus scrape configuration.

The `/metrics` endpoint runs on its OWN port (separate from the
main API), configured via `observability.metrics_port`. Typical
production setup:

  - Main API on port 8000, internet-facing through a load balancer.
  - Metrics on port 9090, on a private network only the Prometheus
    server can reach. Firewall accordingly.
  - Scrape interval: 15s is fine. The histograms and gauges all
    have sane reset behavior.

Key metrics for alerting:

  - `trip_ema_scheduler_stale_tickers > N`: ETL is behind for N
    tickers. N >= 5% of registered tickers is page-worthy.
  - `rate(trip_ema_scheduler_errors_total[5m]) > 0`: scheduler is
    hitting per-ticker errors. Investigate logs.
  - `trip_ema_transport_circuit_state{state="open"} == 1`: at
    least one transport's breaker is OPEN. Compose with the
    transport label to know which one.
  - `rate(trip_ema_notifier_jobs_processed_total{outcome="failed"}[5m])
     / rate(trip_ema_notifier_jobs_processed_total[5m]) > 0.1`:
    failure rate exceeds 10%.

The `trip_ema_info{version="..."}` gauge is always 1; join other
metrics against it in PromQL to annotate dashboards with the deployed
version.

### 8.4 What `stale_tickers_count` actually means.

A ticker is "stale" when the `ComputeScheduler` has detected that an
expected closed weekly bucket has no source bars in the database
(see invariant 3.1 and `_check_for_data_gap`). It means the ETL is
behind for that ticker — NOT that the application is broken.

When the gauge rises:

  - **One or two tickers stale** → routine ETL backfill in progress;
    the system will self-heal once data arrives.
  - **Many tickers stale at once** → the ETL pipeline is likely down
    or behind. Page the ETL team, not the application team.
  - **Slow accumulation over hours** → check the ETL's per-ticker
    backfill rate. Some tickers may have permanent issues (delisted,
    renamed) — use the `/v1/admin/stale-tickers` endpoint to see
    which.

The `/readyz` health check reports `stale_tickers_count`
informationally; it does NOT return 503 (Phase 9 design decision —
ETL gaps shouldn't cascade into application-level failure cascades).

### 8.5 Pine parity harness workflow.

The harness (`tests/parity/`) auto-discovers `*.csv` fixtures in
`tests/parity/fixtures/`. To add a real TradingView fixture:

  1. Open the Weekly_Triple_EMA Pine script on TradingView for the
     ticker + timeframe you want to anchor.
  2. Export ~100 rows of OHLCV + indicator values via the data
     window. Required columns:
     `timestamp,open,high,low,close,volume,expected_med_ema,expected_long_ema,expected_longest_ema,expected_atr`
  3. Save as `tradingview_<symbol>_<timeframe>.csv` in
     `tests/parity/fixtures/`.
  4. Run `pytest tests/parity`. Any drift surfaces at `1e-9`
     absolute tolerance with the bar timestamp and column.

The `--regenerate` CLI on the harness regenerates the synthetic
self-pin fixture from the current Python implementation. Use it
when you deliberately change indicator math AND have updated the
real TV fixtures to match — the order matters: TV fixtures are the
source of truth; the self-pin is a fast regression net.

### 8.6 DB role separation (Phase 12).

Invariant 3.14 codifies the Engine/ETL/GUI process boundaries in
Python. Phase 12 added defense in depth at the database level via
three Postgres roles:

  - `trip_etl_user` — WRITE on `tickers`, `ohlcv_bars`,
    `etl_run_log`, `parked_tickers`; READ-only on
    `alembic_version`; NO access to signal_events or subscribers
  - `trip_engine_user` — READ on bar tables; WRITE on signals,
    subscribers, notification_log; sequence usage for serial PKs
  - `trip_gui_user` — READ-only across all public-schema tables;
    no writes anywhere

The roles are created by Alembic migration `0002_db_roles.py`. The
migration creates roles with `NOLOGIN`; ops grant `LOGIN` and set
passwords as a deployment step (typically per-environment).

Adoption is opt-in. Without a `database.roles:` section in
config.yaml, every process uses the top-level
`database.user`/`database.password_env` — preserving back-compat.
Once configured per role, each process picks its appropriate
credentials via
`create_engine_for_role(config, "etl"|"engine"|"gui")`.

**Migration ordering matters:** `alembic upgrade head` must run as
a user with full schema privileges (the existing top-level user).
The per-role users don't have privileges to alter schema. So
whichever process runs migrations at startup keeps using the
top-level credentials; the per-role connections are for steady-state
operation.

Example config.yaml fragment (in addition to the standard fields):

```yaml
database:
  host: db.example.com
  database: trip_ema
  user: trip_ema_app          # used for migrations + as fallback
  password_env: TRIP_EMA_DB_PASSWORD
  roles:
    etl:
      user: trip_etl_user
      password_env: TRIP_EMA_DB_ETL_PASSWORD
    engine:
      user: trip_engine_user
      password_env: TRIP_EMA_DB_ENGINE_PASSWORD
    gui:
      user: trip_gui_user
      password_env: TRIP_EMA_DB_GUI_PASSWORD
```

What this guards against: a bug in the GUI's DB code attempting an
INSERT into `ohlcv_bars` becomes a Postgres permission error
instead of silent data corruption. Without role separation, the
boundary lives only in the Python code; with it, the database
enforces the contract too.
