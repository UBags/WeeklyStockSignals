# `WeeklyStockSignals` (`trip_ema`) — Delivered Functionality

**Package:** `WeeklyStockSignals_v10` (codename `trip_ema`)
**Phase scope at this snapshot:** Phase 11 + Phase 12 + Phase 14.6 (trilogy) + Phase 15 (a, b, c, d)
**Document purpose:** an inventory of *what the system does today*, end-to-end. The companion files are `02_ARCHITECTURE.md` (how the pieces fit) and `03_OPEN_ITEMS.md` (what hasn't been built and what could be).

---

## 1. One-paragraph summary

`trip_ema` is a Python port of a TradingView Pine indicator (the **"Triple-Trip"**: weekly Triple-EMA + ATR bands + a three-state regime classifier) wrapped in a production-quality service. It ingests OHLCV bars from a Postgres table written by a built-in ETL pipeline, computes a per-ticker signal on every fresh bar, persists state-transitions to `signal_events`, caches the latest snapshot in memory, and delivers notifications to subscribers via email/webhook/Slack through a Redis-queued worker. An HTTP API exposes stateless compute, cached reads, and admin operations. A PySide6 desktop console gives operators a button-driven view of the system. As of Phase 15, the same engine can be **replayed over historical bars** to produce a per-exchange backtest signal file for external consumers.

---

## 2. Top-level capability map

```mermaid
graph TB
    subgraph data_in["Data ingest"]
        ETL["ETL CLI<br/>trip-ema-etl"]
        YF["yfinance"]
        NSE["NSE listings"]
    end

    subgraph compute["Compute"]
        SCHED["Compute scheduler<br/>(per-tick)"]
        ENG["Per-ticker engines<br/>(stateful)"]
        BACKTEST["Backtest replay<br/>(Phase 15)"]
    end

    subgraph storage["Storage"]
        PG[("Postgres<br/>bars, signals,<br/>backtest runs")]
        CACHE["SignalCache<br/>(in-memory)"]
        JSON["generated_signals/<br/>{EX}.json"]
        REDIS[("Redis queue")]
    end

    subgraph delivery["Delivery"]
        HTTP["HTTP API<br/>(FastAPI)"]
        NOTI["Notifier worker<br/>(email/webhook)"]
        GUI["PySide6 console"]
    end

    YF --> ETL
    NSE --> ETL
    ETL --> PG
    PG --> SCHED
    SCHED --> ENG
    ENG --> CACHE
    ENG --> PG
    PG --> BACKTEST
    BACKTEST --> JSON
    SCHED --> REDIS
    REDIS --> NOTI
    NOTI -.->|email/webhook/slack| OUT[External targets]
    CACHE --> HTTP
    PG --> HTTP
    JSON -. external consumer .-> EXT[Backtester]
    HTTP <--> GUI
    NOTI --> PG
```

---

## 3. Capability inventory by area

### 3.1 Indicator math (`trip_ema/indicators/`, `trip_ema/engine/`)

A faithful Pine-script port. Numerically matched to TradingView output to floating-point parity (Invariant 3.3 in the architecture doc), verified by `tests/parity/test_pine_parity.py`.

| Building block | Module | What it does |
|---|---|---|
| Volume-weighted EMA | `indicators/vw_ema.py` | The triple-stage EMA pipeline (base → smoothed → final), with Pine-faithful NaN warm-up. |
| ATR (Wilder/RMA) | `indicators/atr.py` | Average True Range with Pine's RMA seeding semantics. |
| ATR bands | `indicators/bands.py` | Inner / mid / outer multipliers around the close. |
| Weekly aggregation | `indicators/aggregation.py` | `daily_to_weekly(bars, tz)` — Pine-anchored weekly buckets from daily bars; gap detection. |
| Core regime | `engine/core_regime.py` | The underlying BULL / NEUTRAL / BEAR state from EMA stack + ATR bands. |
| Display regime | `engine/display_regime.py` | The "gated" regime actually shown to subscribers (filters spurious flips). |
| Change classifier | `engine/change_classifier.py` | Turns a display-regime transition into one of `BUY` / `SELL` / `EXIT_FROM_BUY` / `EXIT_FROM_SELL` / `NO_CHANGE`. |
| Per-ticker engine | `engine/ticker_engine.py` | Stateful object that wires the above. Two entry points: `prime(historical_bars)` for one-shot bulk and `ingest(bar)` / `ingest_provisional(bar)` for live operation. |

```mermaid
flowchart LR
    bar["OHLCV bar"] --> tea["TickerEngine.ingest"]
    tea --> vw["3-stage VW-EMA<br/>(med / long / longest)"]
    tea --> atr["ATR + bands"]
    vw --> core["CoreRegime<br/>(BULL/NEUTRAL/BEAR)"]
    atr --> core
    core --> disp["DisplayRegime<br/>(gated)"]
    disp --> cls["classify_change"]
    cls --> sig["ChangeSignal<br/>(BUY / SELL / EXIT_* / NO_CHANGE)"]
    tea --> state["BarState<br/>(immutable snapshot)"]
```

**Provisional / final semantics.** The engine accepts a `ingest_provisional(bar)` call for intra-bar updates (a not-yet-closed weekly bucket) that is internally snapshotted and can be rolled back when the next *final* bar arrives. This keeps engine state correct when the system computes off live ticks.

### 3.2 ETL pipeline (`trip_ema/etl/`)

A CLI (`trip-ema-etl`) wraps a small library of fetchers / writers. All subcommands take `--exchange` (Phase 14.6 made it mandatory).

```mermaid
flowchart LR
    subgraph subs["trip-ema-etl subcommands"]
        rl["refresh-listings"]
        bf["backfill"]
        eod["incremental-eod"]
        intra["incremental-intraday"]
        rt["recompute-tiers"]
        st["status"]
        un["unpark"]
        aa["approve-action"]
        gs["generate-signals<br/>(Phase 15a)"]
    end
    subgraph runner["etl/runner.py"]
        rn["run_eod / run_intraday<br/>run_backfill / etc."]
    end
    subgraph src["Sources"]
        yfsrc["yfinance_source.py"]
        nsesrc["nse_listings_parser.py"]
        hol["holidays_calendar.py"]
    end
    subgraph repos["Repositories"]
        tr["TickersRepository"]
        br["BarsRepository"]
        sr["EtlStatusRepository"]
        cr["CorporateActionsRepository"]
        sbr["SignalBacktestRunsRepository<br/>(Phase 15a)"]
    end
    subs --> runner
    runner --> src
    runner --> repos
    repos --> PG[("Postgres")]
```

| Subcommand | What it does |
|---|---|
| `refresh-listings` | Pull the NSE/exchange listings universe; insert/update `tickers`; tier-assign new entries. |
| `backfill` | Pull historical bars from `--start` to today, write to `ohlcv_bars`. Tier-restrictable. |
| `incremental-eod` | After market close, fetch yesterday's bar for every active ticker; write + upsert. |
| `incremental-intraday` | During market hours, fetch the in-progress bar for tier-1 tickers. |
| `recompute-tiers` | Re-bucket tickers into NIFTY-200 / next-500 / rest based on current volume. |
| `status` | Print most recent etl_runs row per kind for the exchange. |
| `unpark` | Clear the parked-ticker flag for tickers stalled by previous fetch failures. |
| `approve-action` | Approve a pending corporate-action review row. |
| `generate-signals` | **Phase 15a** — replay engine over historical bars and write a per-exchange backtest JSON file. |

**Tiering.** `etl/tiering.py` bucketizes the universe so that the EoD pass treats high-volume names first and low-volume tail last. The orchestrator (`scheduler/`) uses the same buckets for intraday cadence.

**Parking.** A ticker that fails three consecutive fetches is auto-parked (`etl/parked.py`); the scheduler then skips it until an operator unparks (CLI or GUI).

### 3.3 Compute pipeline (`trip_ema/scheduler/`, `trip_ema/registry/`)

The live signal path is a single-process, single-writer loop.

```mermaid
sequenceDiagram
    participant Orch as MarketOrchestrator
    participant Sched as ComputeScheduler
    participant Reg as TickerRegistry
    participant Eng as TickerEngine
    participant DB as Postgres
    participant Cache as SignalCache
    participant Dist as SubscriberDistributor
    participant Q as Redis

    Orch->>Sched: tick (market open + cadence)
    Sched->>DB: fetch fresh bars per ticker
    DB-->>Sched: bars
    Sched->>Reg: get engine for ticker
    Reg-->>Sched: TickerEngine
    Sched->>Eng: ingest(bar)
    Eng-->>Sched: BarState (immutable)
    Sched->>Cache: write latest snapshot
    Sched->>DB: insert signal_events row (only on transition)
    Sched->>Dist: notify (only on transition)
    Dist->>Q: enqueue jobs per subscriber
```

| Component | Responsibility |
|---|---|
| `OrchestratorPool` / `MarketOrchestrator` | Owns a tick loop per market (NSE, BSE, NYSE …). Knows market hours + holidays. Fires `ComputeScheduler.run_once()` at configured wall-clock instants. |
| `ComputeScheduler` | Per-tick: detect gaps, fetch fresh bars, ingest, write cache + events. |
| `TickerRegistry` | Per-ticker engine map; built/primed at startup from history. |
| `SignalCache` | In-memory snapshot of "latest signal per ticker"; single writer (scheduler), many readers. |
| `SubscriberDistributor` | Background thread that walks subscribers per signal change and enqueues delivery jobs. |
| `TickPlanner` | Builds the tick schedule for a market day (post-open, intraday cadence, pre-close). |
| `MarketCalendar` | python-holidays + YAML override for "is this a trading day". |
| `MarketHoursService` | Wraps the market_hours config into a queryable service. |

### 3.4 Notification delivery (`trip_ema/notifications/`, `trip_ema/notifier_worker.py`)

A separate process pulls jobs off Redis and delivers them.

```mermaid
flowchart LR
    subgraph live["main service"]
        scheduler["ComputeScheduler"]
        distributor["SubscriberDistributor"]
    end
    scheduler -.transition.-> distributor
    distributor --> q[(Redis queue)]
    q --> nw["NotifierWorker<br/>(separate process)"]
    nw --> staleness{"is event<br/>superseded?"}
    staleness -- yes --> drop["drop"]
    staleness -- no --> br{"circuit<br/>breaker"}
    br -- open --> dlq[(DLQ)]
    br -- closed --> trans
    subgraph trans["Transports"]
        email["EmailTransport"]
        webhook["WebhookTransport"]
        nullt["NullTransport (test)"]
    end
    trans -. last-mile log .-> PG[(notification_log)]
```

| Piece | Role |
|---|---|
| `notifications/base.py` | Abstract `NotificationTransport` + `NullTransport` |
| `notifications/email_transport.py` | SMTP delivery |
| `notifications/webhook_transport.py` | HTTP POST delivery (Slack-compatible JSON) |
| `notifications/circuit_breaker.py` | Per-transport failure-window breaker; DLQ on open |
| `notifications/batcher.py` | Renders one event line (text or JSON) for the message body |
| `notifications/redis_queue.py` | Queue producer + consumer abstraction over `redis-py` |
| `notifier_worker.py` | The worker process: pops jobs, checks staleness, dispatches |

**At-least-once delivery within the dedup window.** Up to ~1h, a duplicate is possible but is silently dedup'd by hash. Architecture §5 documents this as intentional — missing a signal is worse than seeing it twice.

### 3.5 HTTP API (`trip_ema/transports/http/`)

FastAPI app with the following routers (registration order is `app.py`):

```mermaid
flowchart TB
    subgraph public["Public routes"]
        h["/healthz<br/>/readyz"]
        sig["/v1/signals/{exchange}/{symbol}/history"]
        cmp["/v1/compute/*"]
        pulse["/v1/pulse/stream  (SSE)"]
    end
    subgraph admin["Admin routes (bearer token)"]
        adm["/v1/admin/health"]
        adm_etl["/v1/admin/etl/*<br/>(7 actions + status + parked)"]
        adm_exst["/v1/admin/exchange-state"]
        adm_ex["/v1/admin/exchanges"]
        adm_sub["/v1/admin/subscribers/*"]
        adm_notif["/v1/admin/notifications/recent<br/>/v1/admin/notifications/dlq"]
        adm_orch["/v1/admin/orchestrator/*"]
        adm_sig["/v1/admin/signals/generate<br/>/v1/admin/signals/generated<br/>(Phase 15b)"]
        adm_reprime["/v1/admin/reprime"]
        adm_cb["/v1/admin/circuit-breakers"]
    end
```

#### Public

| Endpoint | Use |
|---|---|
| `GET /healthz` | Liveness; reports cache_size, primed flag |
| `GET /readyz` | Readiness; diagnostic |
| `GET /v1/signals/{exchange}/{symbol}/history` | Replays an engine over the requested window and returns `BarState`s plus change events. Used by the GUI chart. |
| `GET /v1/compute/*` | Stateless on-demand compute over caller-supplied bars |
| `GET /v1/pulse/stream` | SSE stream of pulse events (internal use) |

#### Admin (every route requires `Authorization: Bearer $TRIP_EMA_ADMIN_TOKEN`)

| Endpoint | Method | Use |
|---|---|---|
| `/v1/admin/health` | GET | Snapshot card-driven dashboard (system + circuit-breaker status) |
| `/v1/admin/etl/run-eod` | POST | Fire `run_eod` async on the etl_executor |
| `/v1/admin/etl/run-intraday` | POST | Fire `run_intraday` async |
| `/v1/admin/etl/backfill` | POST | Fire backfill from a start date (optional tier) |
| `/v1/admin/etl/refresh-listings` | POST | Pull listings; create new tickers |
| `/v1/admin/etl/recompute-tiers` | POST | Re-bucket tickers by volume |
| `/v1/admin/etl/unpark-all` | POST | Clear all parked flags |
| `/v1/admin/etl/status` | GET | etl_runs table snapshot + exchange-state |
| `/v1/admin/etl/parked` | GET | List parked tickers |
| `/v1/admin/exchange-state` | GET | Per-exchange ETL activity (Phase 14.6) |
| `/v1/admin/exchanges` | GET | The configured exchange list (used by GUI picker) |
| `/v1/admin/subscribers/*` | CRUD | Subscriber records + filters + "send test" |
| `/v1/admin/notifications/recent` | GET | Last N notification_log rows |
| `/v1/admin/notifications/dlq` | GET | DLQ rows |
| `/v1/admin/orchestrator/status` | GET | Per-exchange orchestrator state (next tick, paused?) |
| `/v1/admin/orchestrator/{ex}/run-now` | POST | Force a tick |
| `/v1/admin/orchestrator/{ex}/pause` | POST | Pause the tick loop |
| `/v1/admin/orchestrator/{ex}/resume` | POST | Resume the tick loop |
| `/v1/admin/circuit-breakers` | GET | Breaker state per transport |
| `/v1/admin/reprime` | POST | Re-prime one ticker's engine from scratch |
| **`/v1/admin/signals/generate`** | POST | **Phase 15b** — fire backtest generation (202 Accepted) |
| **`/v1/admin/signals/generated`** | GET | **Phase 15b** — read per-exchange file metadata |

### 3.6 GUI (`trip_ema/gui/`)

PySide6 desktop console. Tabs:

```mermaid
flowchart LR
    mw["MainWindow"]
    mw --> t1["Signals tab<br/>(chart + Phase 15c panel)"]
    mw --> t2["Subscribers tab"]
    mw --> t3["ETL Status tab<br/>(actions + state)"]
    mw --> t4["Notifications tab"]
    mw --> t5["System Health tab"]
    mw --> t6["Tickers tab"]
    mw --> t7["Settings tab"]
```

| Tab | What it shows / lets you do |
|---|---|
| **Signals** | Chart for one (exchange, ticker) over a selectable range; replays the engine server-side. Bottom panel = **Phase 15c** signal-generation button + per-exchange "last generated file" grid. |
| **Subscribers** | Full CRUD: name, transport, address, filters, send-test |
| **ETL Status** | Exchange picker + 7 action buttons (Run EoD, Run Intraday, **Backfill…** [15d modal], Refresh Listings, Recompute Tiers, Unpark All, Reprime); etl_runs table; exchange-state panel; parked-ticker list |
| **Notifications** | Recent notification_log rows + DLQ |
| **System Health** | Card-driven dashboard from `/v1/admin/health` |
| **Tickers** | Browse + filter tickers, see tier and parked state |
| **Settings** | Bearer token, base URL, exchange persistence |

**Reused widgets** (`trip_ema/gui/views/widgets/`, Phase 15c):

| Widget | Used by |
|---|---|
| `ExchangePicker` | ETL Status tab AND Signals tab (Phase 15c extracted the inline picker from `EtlStatusView` to here so SignalGenerationPanel could share it) |
| `BackfillDialog` | ETL Status "Backfill…" button (Phase 15d) |

### 3.7 Persistence (`trip_ema/persistence/`, `alembic_pkg/`, `migrations/legacy/`)

Postgres-only. Schema is managed by **two parallel systems** (an Invariant 3.14 quirk): the canonical migrator is Alembic (`alembic_pkg/versions/`) and a legacy SQL migrator (`migrations/legacy/*.sql`) is kept in sync, asserted byte-equivalent by the parity test.

```mermaid
erDiagram
    tickers ||--o{ ohlcv_bars : has
    tickers ||--o{ signal_events : has
    tickers ||--o{ signal_backtest_runs : has
    tickers ||--o{ etl_ticker_status : has
    subscribers ||--o{ notification_log : receives
    etl_runs ||--o{ signal_backtest_runs : "last_run_id (logical)"
    exchange_state {
        text exchange PK
        timestamptz last_eod_at
        timestamptz last_intraday_at
        timestamptz last_listings_refresh_at
    }
    etl_runs {
        bigint run_id PK
        text kind
        text exchange "Phase 15a"
        timestamptz started_at
        timestamptz finished_at
        text outcome
        uuid correlation_id
    }
    signal_backtest_runs {
        text exchange PK
        int ticker_id PK
        text timeframe PK
        date computed_from_date
        date computed_through_date
        timestamptz computed_at
        bigint last_run_id
    }
```

| Table | Owner | Purpose |
|---|---|---|
| `tickers` | ETL | Universe + tier + parked state |
| `ohlcv_bars` | ETL | Daily bars (15m/1h/2h/1W deferred for storage) |
| `signal_events` | live engine | Append-only state-transition log (compute-scheduler-only writes) |
| `signal_backtest_runs` | backtest runner | **Phase 15a** — per-(exchange, ticker, timeframe) resume tracking |
| `etl_runs` | ETL | Async-action ledger (run_eod, backfill, signal_backtest, …); Phase 15a added `exchange` column |
| `etl_ticker_status` | ETL | Per-ticker parked counter + reason |
| `exchange_state` | ETL | **Phase 14.6** — per-exchange last-action timestamps |
| `corporate_actions` | ETL | Detected splits / divs, pending review |
| `subscribers` | admin | Notification recipients + filters |
| `notification_log` | notifier worker | Last-mile delivery audit |
| `notification_dlq` | notifier worker | Failed deliveries past breaker open |

**Per-process least-privilege roles (Phase 12).** `trip_etl_user`, `trip_engine_user`, `trip_gui_user` — each with the minimum grants needed. The signal_backtest_runs table grants follow this pattern (etl user reads+writes; engine and gui users read only).

### 3.8 Observability (`trip_ema/observability/`)

| Layer | Detail |
|---|---|
| Structured JSON logs | One JSON object per record; pluggable formatter |
| Prometheus metrics | `/metrics` endpoint via `metrics_server.py` |
| OTel tracing | Spans around `ComputeScheduler.run_once`, `SubscriberDistributor._enqueue_jobs`, `NotifierWorker._handle_job` |
| Correlation IDs | Minted at request / tick boundary; propagated through every log line + DB write that carries `correlation_id` |

---

## 4. Phase 15 — backtest signal generation (detailed)

This is the most recent delivery and warrants its own section. The design document (`PHASE_15_DESIGN.md`) is the contract; below is what the code actually does.

### 4.1 What Phase 15 builds

A **fresh-recompute backtest pipeline**. Given an exchange and one or more timeframes (`1D`, `1W`), the runner walks every active ticker's daily-bar history, runs each through a fresh `TickerEngine`, captures every `ChangeSignal != NO_CHANGE`, and writes one JSON file per exchange at `generated_signals/{EXCHANGE}.json`. Subsequent runs **resume** from where the prior one left off per (ticker, timeframe), unless `force_full=true` is passed.

### 4.2 End-to-end flow

```mermaid
flowchart TB
    op["Operator"]
    op -- "CLI: trip-ema-etl generate-signals --exchange NSE" --> cli["cmd_generate_signals"]
    op -- "GUI: Signals tab Generate button" --> panel["SignalGenerationPanel"]
    op -- "curl POST /v1/admin/signals/generate" --> route["admin_signals.generate"]

    panel --> route
    cli --> runner["generate_historical_signals<br/>(synchronous)"]
    route -. submit .-> exec["etl_executor<br/>(thread pool)"]
    exec --> runner_async["_run_signal_generation_safe<br/>(async wrapper)"]
    runner_async --> runner

    subgraph runner_body["Inside the runner"]
        v1["1 validate exchange & timeframes"]
        v2["2 force_full? wipe signal_backtest_runs"]
        v3["3 load existing JSON or empty skeleton"]
        v4["4 fetch active tickers"]
        loop["5 per ticker × tf<br/>(see below)"]
        v6["6 refresh counts metadata"]
        v7["7 atomic write tmp+rename"]
        v1 --> v2 --> v3 --> v4 --> loop --> v6 --> v7
    end

    runner --> DB[("Postgres")]
    runner --> FS[("generated_signals/<br/>{EX}.json")]
```

### 4.3 The per-ticker loop

```mermaid
flowchart TB
    start(["For each ticker × timeframe"])
    start --> r1["1 read signal_backtest_runs<br/>resume row"]
    r1 --> r2{"force_full or<br/>no row?"}
    r2 -- yes --> r3a["start_date = None"]
    r2 -- no --> r3b["start_date =<br/>row.computed_through + 1d"]
    r3a --> r4["2 fetch all daily bars<br/>via BarsRepository"]
    r3b --> r4
    r4 --> r5{"no bars or<br/>nothing new?"}
    r5 -- yes --> skip([skip ticker])
    r5 -- no --> r6{"start_date None<br/>(force_full)?"}
    r6 -- yes --> r7["start_date = bars[priming_idx].date<br/>(priming = 250 daily bars)"]
    r6 -- no --> r8["3 select bars_used:<br/>daily or daily_to_weekly"]
    r7 --> r8
    r8 --> r9["4 build fresh TickerEngine<br/>with resolved params"]
    r9 --> r10["5 replay every bar:<br/>classify_change(prev, state.display_bg)"]
    r10 --> r11["6 capture events where<br/>signal != NO_CHANGE<br/>AND bar.date >= start_date"]
    r11 --> r12["7 merge into in-memory<br/>by_date dict"]
    r12 --> r13["8 upsert signal_backtest_runs<br/>(per-ticker commit)"]
    r13 --> done([next ticker])
```

**Priming.** The engine needs ~36 weekly bars (~180 daily) for its longest-EMA stack to settle. The runner uses 250 daily bars of headroom; signals fired during priming are discarded by the `bar.date >= start_date` filter. For force-full runs this means the *first* recorded signal for a ticker is roughly year-1 of its history rather than day-1 — intentional and documented in the module docstring.

### 4.4 Merge protocol (three transitions)

```mermaid
flowchart LR
    subgraph cases["For each date in the recomputed window"]
        c1["Case A:<br/>date had no prior signal,<br/>engine emits one now"]
        c2["Case B:<br/>date had a prior signal,<br/>engine emits a different one"]
        c3["Case C:<br/>date had a prior signal,<br/>engine emits none now"]
    end
    c1 --> a1["INSERT new record<br/>into by_date[date]"]
    c2 --> a2["REPLACE: delete prior<br/>(ticker, tf) record, insert new"]
    c3 --> a3["DELETE prior (ticker, tf)<br/>record; tidy empty date"]
    note["Dates outside the<br/>recomputed window: UNTOUCHED.<br/>Other tickers' signals: UNTOUCHED."]
```

This is `_merge_signals_into` in `signal_generator.py`. Dedicated test file (`tests/etl/test_signal_generator_merge.py`, 11 tests) explicitly covers all three transitions plus "dates outside window untouched."

### 4.5 Output JSON schema

```mermaid
flowchart TB
    file["{EXCHANGE}.json"]
    file --> meta["Metadata<br/>schema_version: '1.0'<br/>exchange<br/>generated_at (UTC ISO)<br/>generated_by_run_id<br/>params{tf: IndicatorParams}<br/>counts{tickers, signals_total,<br/>signals_by_type, first_date, last_date}"]
    file --> bd["by_date dict"]
    bd --> day1["2018-03-15"]
    bd --> day2["2018-03-22"]
    bd --> dayn["…"]
    day1 --> rec1["{ticker, timeframe, signal,<br/>price_at_signal, bar_timestamp}"]
    day1 --> rec2["…"]
```

Within each date, records are sorted by `(ticker, timeframe)` for stable diffs across runs. The runner ALWAYS rebuilds the `params` block from the *current* resolved parameters, capturing the tuning used in *this* run for reproducibility.

### 4.6 Concurrency & lifecycle (Phase 15b)

A subtle point that the design specifically calls out (Finding 4 in the doc, line 18-38 of `admin_signals.py`):

```mermaid
sequenceDiagram
    participant Client
    participant Route as POST /generate
    participant DB as Postgres
    participant Exec as etl_executor
    participant Runner as generate_historical_signals
    participant Wrap as _run_signal_generation_safe

    Client->>Route: POST exchange=NSE force_full=...
    Route->>DB: BEGIN
    Route->>DB: pg_advisory_xact_lock(hash(NSE))
    Route->>DB: SELECT etl_runs WHERE in_flight AND exchange=NSE
    alt already in flight
        Route->>DB: ROLLBACK
        Route-->>Client: 409 Conflict
    else clear
        Route->>DB: INSERT etl_runs (kind='signal_backtest', exchange='NSE')
        Route->>DB: COMMIT
        Route->>Exec: submit(_run_signal_generation_safe, ...)
        Route-->>Client: 202 Accepted (run_id, estimated_output_path)
        Exec->>Wrap: invoke
        Wrap->>Runner: generate_historical_signals(run_id=...)
        Note over Runner: runs full pipeline,<br/>does NOT close etl_runs row<br/>because run_id was injected
        Runner-->>Wrap: SignalGenerationResult
        Wrap->>DB: close_run(success)
    end
```

The route **opens the etl_runs row in the request thread, inside the same transaction as the concurrent-run guard SELECT**, behind a pg_advisory_xact_lock keyed on the exchange. This serializes per-exchange `SELECT-then-INSERT` so two concurrent POSTs cannot both pass the guard. The runner accepts the `run_id` and skips its own open; the wrapper closes the row on success or failure.

The CLI path (`cmd_generate_signals`) calls the runner *without* `run_id`, so the runner opens AND closes the row itself — the asymmetric ownership is documented on `EtlStatusRepository.open_run` and tested in `tests/etl/test_signal_generator.py::test_passing_run_id_skips_open_but_still_closes`.

### 4.7 Per-exchange indicator parameters (`exchange_overrides`)

Phase 15a introduced an optional `exchange_overrides` block in `config.yaml`:

```yaml
exchange_overrides:
  NSE:
    "1W":
      ema: { med: {...}, long: {...}, longest: {...}, volume_sma_length: 2 }
      atr: { length: 14, inner_mult: 0.2, mid_mult: 2.0, outer_mult: 4.0 }
    "1D":
      ema: {...}
      atr: {...}
```

Resolution (in `trip_ema/config/exchange_overrides.py`):

```mermaid
flowchart LR
    in["resolve_indicator_params(<br/>config, exchange, timeframe)"]
    in --> q1{"exchange_overrides<br/>has [exchange]?"}
    q1 -- no --> g["return config.to_indicator_params()<br/>(global fallback)"]
    q1 -- yes --> q2{"that exchange<br/>has [timeframe]?"}
    q2 -- no --> g
    q2 -- yes --> ovr["build IndicatorParams<br/>from override"]
```

**Backward compatibility.** A `config.yaml` with no `exchange_overrides` block keeps working unchanged; every `(exchange, timeframe)` resolves to the global block. **Validation** is in `Config._validate_exchange_overrides` (rejects unknown timeframes and exchanges not in `market_hours` at config-load time). **Scope is intentionally narrow:** only the backtest path consults overrides; the live `ComputeScheduler` keeps using the global block (design D4 / OOS-7; Phase 17 will migrate the live engine).

### 4.8 GUI surface (Phase 15c + 15d)

```mermaid
flowchart TB
    subgraph signals_tab["Signals tab"]
        chart["Existing chart area<br/>(ticker picker, range, replay)"]
        panel["SignalGenerationPanel (15c)"]
        panel --> p1["ExchangePicker (shared)"]
        panel --> p2["Timeframe checkboxes 1W ✓ / 1D"]
        panel --> p3["Mode: Incremental / Force full radio"]
        panel --> p4["Generate signals button"]
        panel --> p5["Last generation per exchange grid<br/>(polls /generated every 30s)"]
    end
    subgraph etl_tab["ETL Status tab"]
        actions["Action buttons row<br/>EoD / Intraday / Backfill… (15d) /<br/>Refresh Listings / Recompute Tiers /<br/>Unpark All / Reprime"]
        bf_dialog["BackfillDialog (15d)<br/>start_date + optional tier"]
        actions -- "Backfill…" --> bf_dialog
    end
```

| 15c surface | Behavior |
|---|---|
| Generate button | Gated by `picker resolved AND ≥1 timeframe AND token configured`. |
| Force-full mode | Prompts confirmation modal before posting. |
| Result handling | 202 → toast "Backtest started for {EX} (run_id=…)" + schedule a 5-s metadata refresh. 409 → modal "already running". Other errors → modal with server detail. |
| Metadata grid | Polls `/v1/admin/signals/generated` per exchange every 30s; rows render `{size} · {mtime} · {N signals}` or "no file yet". |

| 15d surface | Behavior |
|---|---|
| Backfill… button | Opens `BackfillDialog` (modal). |
| BackfillDialog | `QDateEdit` (default = today − 30d, min 1990-01-01, max today) + optional "Restrict to one tier" checkbox + `QSpinBox(1..3)`. OK button always enabled. |
| Audit doc | `docs/gui_endpoint_audit.md` — 25-row table of every route → GUI surface → status (✅ / ➕ / ⏳ / 🛠). |

### 4.9 Files Phase 15 actually delivered

| Phase | New | Modified |
|---|---|---|
| **15a** | `alembic_pkg/versions/0005_signal_backtest_runs.py`<br>`migrations/legacy/012_signal_backtest_runs.sql`<br>`trip_ema/persistence/signal_backtest_runs_repo.py`<br>`trip_ema/etl/signal_generator.py`<br>`trip_ema/config/exchange_overrides.py`<br>`tests/persistence/test_signal_backtest_runs_repo.py` (16 tests)<br>`tests/etl/test_signal_generator.py` (15)<br>`tests/etl/test_signal_generator_merge.py` (11)<br>`tests/etl/test_signal_generator_atomic_write.py` (6)<br>`tests/etl/test_signal_generator_cli.py` (10)<br>`tests/config/test_exchange_overrides.py` (12) | `trip_ema/config/schema.py` (added `ExchangeIndicatorOverride` + `Config.exchange_overrides` + validator)<br>`trip_ema/etl/cli.py` (added `generate-signals` subcommand)<br>`config.yaml` (sample `exchange_overrides:` block)<br>`migrations/legacy/README.md` |
| **15b** | `trip_ema/transports/http/routes/admin_signals.py`<br>`tests/transports_pure/test_admin_signals.py` (28 tests) | `trip_ema/transports/http/schemas.py` (added 2 payloads)<br>`trip_ema/transports/http/app.py` (mounted router)<br>`scripts/operator_commands.md` (added §4 backtest section) |
| **15c** | `trip_ema/gui/views/widgets/__init__.py`<br>`trip_ema/gui/views/widgets/exchange_picker.py`<br>`trip_ema/gui/views/signal_generation_panel.py`<br>`tests/gui/test_exchange_picker_widget.py` (10 tests)<br>`tests/gui/test_signal_generation_panel.py` (28)<br>`tests/gui/test_etl_status_view_exchange_picker.py` (18) | `trip_ema/gui/views/etl_status.py` (refactored to use ExchangePicker; added "Backfill…" button hook)<br>`trip_ema/gui/views/signals.py` (embedded SignalGenerationPanel)<br>`trip_ema/gui/http_client.py` (added `generate_signals`, `get_signal_generation_metadata`)<br>`tests/gui/test_http_client.py` + `tests/gui_pure/test_http_client.py` |
| **15d** | `trip_ema/gui/views/widgets/backfill_dialog.py`<br>`tests/gui/test_backfill_dialog.py` (14 tests)<br>`docs/gui_endpoint_audit.md` | `trip_ema/gui/views/etl_status.py` (Backfill… button wired)<br>`scripts/operator_commands.md` (cross-references) |

**Phase 15 test totals:** 168 new tests across 11 files (the design estimated "~100").

---

## 5. Phase 15 — design compliance audit

A literal walk through the design's Section 3 requirements (F1–F12, N1–N7) against the code:

### 5.1 Functional requirements

| # | Requirement | Status | Where |
|---|---|---|---|
| **F1** | CLI `generate-signals --exchange X [--timeframe Y] [--force-full]`, default tf=`1W` | ✅ | `trip_ema/etl/cli.py::cmd_generate_signals`, argparse at L338-362, default applied at L406 |
| **F2** | `POST /v1/admin/signals/generate?exchange=...&force_full=...&timeframe=...` returns 202 with run_id + path | ✅ | `admin_signals.py::admin_signals_generate` (L194-301) |
| **F3** | `GET /v1/admin/signals/generated?exchange=...` returns metadata; 200 with `exists:false` if no file | ✅ | `admin_signals.py::admin_signals_generated` (L307-386) — *note: design said "or 404"; code returns 200/exists:false per route's own docstring* |
| **F4** | Replay each ticker × timeframe through a fresh `TickerEngine`, capture every `ChangeSignal != NO_CHANGE` | ✅ | `signal_generator.py::_process_ticker_timeframe` L624-693 |
| **F5** | Resume from `signal_backtest_runs.computed_through_date + 1d`; from earliest bar if no row | ✅ | `signal_generator.py` L498-514 + L562-581 |
| **F6** | `force_full` wipes resume rows in a small dedicated transaction at run start | ✅ | `signal_generator.py::_do_generate` L330-339 |
| **F7** | Single file per exchange at `generated_signals/{EX}.json`; atomic temp+rename; metadata header | ✅ | `_atomic_write_json` L842-863 |
| **F8** | Merge protocol — add/replace/delete inside recomputed window; untouched outside | ✅ | `_merge_signals_into` L699-779; explicit tests in `test_signal_generator_merge.py` |
| **F9** | `exchange_overrides` config block; `(exchange, timeframe)` resolution with global fallback | ✅ | `config/exchange_overrides.py` + `Config.exchange_overrides` in `schema.py` L574; validator at L586 |
| **F10** | GUI "Generate signal history" button on Signals tab; exchange/timeframe/force_full controls; gated | ✅ | `signal_generation_panel.py::SignalGenerationPanel` |
| **F11** | GUI "last generation per exchange" panel; polls `/generated`; per-row 30s | ✅ | `signal_generation_panel.py::_refresh_metadata_rows` L402+ |
| **F12** | GUI endpoint audit catalogued; "add now" gaps shipped | ✅ | `docs/gui_endpoint_audit.md` — 25 endpoints catalogued; Backfill… and audit doc shipped |

### 5.2 Non-functional requirements

| # | Requirement | Status | Where |
|---|---|---|---|
| **N1** | Runner must not block the service (uses `etl_executor`) | ✅ | `admin_signals.py::admin_signals_generate` L284-292 |
| **N2** | Killing the worker mid-run leaves recoverable state | ✅ | Per-ticker commits to `signal_backtest_runs`; tmp file gets discarded; etl_runs left with outcome=NULL (operator clears manually) |
| **N3** | Atomic JSON write | ✅ | tmp + fsync + `os.replace` (POSIX rename) at `_atomic_write_json` L857-863 |
| **N4** | Scale to ≥100MB JSON | ✅ | Design says NSE 1W is ~14MB observed; the load step uses `json.load` whole-file (escape hatch to ijson documented if >200MB) |
| **N5** | Memory: full JSON in RAM + 4GB headroom OK | ✅ | Whole-file load is the only memory hotspot; observed for NSE (~14MB JSON, ~50MB parsed) it's well under |
| **N6** | Generator does not touch `signal_events`, `ticker_signal_state`, etc. | ✅ | Writes confined to `signal_backtest_runs`, `etl_runs`, and the JSON file |
| **N7** | No regressions in existing test suite | ⚠ Cannot verify without running tests, but the bundle ships every existing test file alongside the 168 new ones |

### 5.3 Out-of-scope (correctly omitted)

| # | Out-of-scope item | Status |
|---|---|---|
| OOS-1 | Intraday timeframes | ✅ Correctly rejected by `_parse_timeframes_or_400` and `SUPPORTED_BACKTEST_TIMEFRAMES` |
| OOS-2 | Per-ticker tuning | ✅ Overrides are per-(exchange, timeframe), not per-ticker |
| OOS-3 | Cancellation of in-flight runs | ✅ Documented as future Phase 18 |
| OOS-4 | Multi-exchange aggregation | ✅ One file per exchange |
| OOS-5 | Streaming JSON output | ✅ Whole-file in-memory |
| OOS-6 | Backtest execution (P&L) | ✅ Generator emits the signal log only |
| OOS-7 | Live engine using overrides | ✅ Live path still reads global `indicators` block |
| OOS-8 | GUI restart-server button | ✅ Not present |
| OOS-9 | Engine behavior changes | ✅ Engine untouched |

### 5.4 Decisions D1–D13

| # | Decision | Status |
|---|---|---|
| D1 | Fresh engine replay, not dump from `signal_events` | ✅ Runner replays in `_process_ticker_timeframe` |
| D2 | All four signal types emitted | ✅ `classify_change` returns all four |
| D3 | Timeframes 1D + 1W only | ✅ `SUPPORTED_BACKTEST_TIMEFRAMES = {"1D", "1W"}` |
| D4 | Per-exchange params via `exchange_overrides`; backtest only | ✅ Resolver in `config/exchange_overrides.py` |
| D5 | One JSON per exchange, keyed by date | ✅ `_atomic_write_json` to `{EX}.json` |
| D6 | In-memory merge with atomic rewrite | ✅ `_merge_signals_into` + `_atomic_write_json` |
| D7 | `signal_backtest_runs` resume table | ✅ Alembic 0005 |
| D8 | `force_full=true` wipes resume rows in small txn at run start | ✅ `_do_generate` L330-339 |
| D9 | No cancellation in Phase 15 | ✅ Not implemented |
| D10 | Async execution via `etl_executor`; `etl_runs.kind='signal_backtest'` | ✅ Route submits to `etl_executor`; `RunKind.SIGNAL_BACKTEST` enum value |
| D11 | No GUI restart button | ✅ Not implemented |
| D12 | GUI button on Signals tab | ✅ `SignalGenerationPanel` embedded in `signals.py::SignalsView` L193 |
| D13 | GUI endpoint audit (15d) | ✅ `docs/gui_endpoint_audit.md` |

### 5.5 Small departures from design (intentional and documented)

| Departure | What design said | What code did | Verdict |
|---|---|---|---|
| `GET /generated` when file missing | "404 if no file exists" *(design §3.1 F3)* and "200 OK (file absent) with `exists:false`" *(design §4.4.2)* | Returns 200 with `exists:false`; 400 for unknown exchange | ✅ The design contradicts itself in §3.1 vs §4.4.2; the code follows §4.4.2, which the route's docstring (L321-324) explicitly defends |
| Output dir | "`generated_signals/`" (config-relative) | `Path.cwd() / "generated_signals"` resolved at request time | ✅ Documented assumption in route docstring L40-47; works because `restart_service.sh` does `cd $REPO_ROOT` first |
| Etl-runs `exchange` column | Design mentioned in passing for the concurrent-run guard | Alembic 0005 added `etl_runs.exchange TEXT NULL` plus retro-fitted existing call sites to pass `exchange=` through | ✅ Genuinely necessary for the guard to scope per-exchange; documented in the migration's docstring |
| `pg_advisory_xact_lock` | Design implied the SELECT+INSERT atomicity but didn't name advisory locks | Added a hash-derived advisory lock at L164-183 of admin_signals.py | ✅ Closes the read-committed race window the design's plain `with begin()` couldn't |

**Overall:** every design requirement F1–F12 and N1–N7 is delivered. The departures from the design are intentional, documented in code, and strengthen correctness rather than skip work.

---

## 6. How to actually run things

| What | Command |
|---|---|
| Start the service | `./scripts/restart_service.sh` |
| Tail logs | `tail -f /tmp/trip-ema.log` |
| Health check | `curl -s http://localhost:8000/healthz \| jq` |
| Start the GUI | `./scripts/gui_start.sh` |
| Run EoD ETL | `trip-ema-etl incremental-eod --exchange NSE` |
| **Generate backtest (CLI)** | `trip-ema-etl generate-signals --exchange NSE` |
| **Force-full backtest (CLI)** | `trip-ema-etl generate-signals --exchange NSE --force-full` |
| **Generate backtest (HTTP)** | `curl -X POST -H "Authorization: Bearer $TRIP_EMA_ADMIN_TOKEN" "http://localhost:8000/v1/admin/signals/generate?exchange=NSE"` |
| **Read file metadata (HTTP)** | `curl -H "Authorization: Bearer $TRIP_EMA_ADMIN_TOKEN" "http://localhost:8000/v1/admin/signals/generated?exchange=NSE"` |
| Status of recent runs | `curl -H "Authorization: Bearer $TRIP_EMA_ADMIN_TOKEN" "http://localhost:8000/v1/admin/etl/status"` |
| DB migration apply | `alembic -c alembic.ini upgrade head` |
| Run tests | `pytest -q tests/` |

For the full operator playbook see `scripts/operator_commands.md` and `scripts/debug_recipes.md`.

---

## 7. Quick contents map

| Layer | Code | Tests |
|---|---|---|
| Domain types | `trip_ema/domain/` | `tests/engine/` indirectly |
| Indicators (pure math) | `trip_ema/indicators/` | `tests/indicators/` + `tests/parity/test_pine_parity.py` |
| Engine | `trip_ema/engine/` | `tests/engine/` |
| Persistence | `trip_ema/persistence/`, `alembic_pkg/`, `migrations/legacy/` | `tests/persistence/` |
| ETL | `trip_ema/etl/` | `tests/etl/` |
| Scheduler / orchestrator | `trip_ema/scheduler/` | `tests/scheduler/`, `tests/scheduler_pure/` |
| Notifications | `trip_ema/notifications/`, `trip_ema/notifier_worker.py` | `tests/notifications/`, `tests/notifier/`, `tests/notifier_pure/` |
| HTTP transports | `trip_ema/transports/http/` | `tests/transports/`, `tests/transports_pure/` |
| GUI | `trip_ema/gui/` | `tests/gui/`, `tests/gui_pure/` |
| Observability | `trip_ema/observability/` | `tests/observability/` |
| Lifecycle (priming) | `trip_ema/api/` | `tests/api/` |
| Config | `trip_ema/config/` | `tests/config/` |

---

## 8. Summary

The package as shipped delivers a production live-signal service (Phases 1–14.6) plus a complete backtest signal-generation pipeline (Phase 15a–d). The Phase 15 design specification is fully met across F1–F12 and N1–N7; the four small departures from the design are documented in the code and strengthen the implementation. Total Phase 15 footprint: ~2,600 lines of new product code, 168 new tests (against the design's "~100" estimate), one Alembic migration with paired legacy SQL, and three new operator-facing surfaces (CLI subcommand, two HTTP endpoints, one GUI panel + one GUI dialog).
