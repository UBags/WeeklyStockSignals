# `WeeklyStockSignals` (`trip_ema`) — Architecture

**Package:** `WeeklyStockSignals_v10` (codename `trip_ema`)
**Companion docs:** `01_FUNCTIONALITY.md` (what the system does) and `03_OPEN_ITEMS.md` (what hasn't been built)

This document explains *how the pieces are arranged and why*. It complements (but does not replace) `docs/ARCHITECTURE.md` in the repo, which carries the 14 invariants and is the canonical contract for code reviewers.

---

## 1. System context

The product is a multi-process, single-host service. The boundary against the outside world is small:

```mermaid
flowchart LR
    subgraph external["External"]
        yf[yfinance API]
        nse[NSE listings page]
        smtp[SMTP server]
        wh[Webhook target]
        slack[Slack incoming-webhook]
        backtester[External backtester]
        operator[Operator<br/>terminal + GUI]
    end

    subgraph host["Single host"]
        subgraph procs["trip_ema processes"]
            etl[trip-ema-etl<br/>CLI / cron]
            srv[trip-ema<br/>service<br/>FastAPI + scheduler]
            ntf[trip-ema-notifier<br/>worker]
            gui[trip-ema-console<br/>PySide6 GUI]
        end
        subgraph storage["Storage"]
            pg[(Postgres)]
            rd[(Redis)]
            fs[(filesystem<br/>generated_signals/)]
        end
    end

    yf --> etl
    nse --> etl
    etl --> pg

    pg <--> srv
    srv --> rd
    rd --> ntf
    ntf --> pg
    ntf --> smtp
    ntf --> wh
    ntf --> slack

    srv --> fs
    fs -. read-only .-> backtester

    operator -- HTTP --> srv
    operator -- GUI --> gui
    gui -- HTTP --> srv
```

**Topology rationale.** Phase 11 invariant 3.14 says: the Engine, ETL, GUI, and Notifier are process-separated. The Engine and ETL never run in the same Python process (their startup imports are mutually heavy and their failure modes shouldn't entangle). The Notifier is its own process because circuit-breaker failure shouldn't kill the compute loop. The GUI is its own process so a crash there can't take down the service.

---

## 2. Layered module map

The codebase is layered. Lower layers may not import from higher layers; cross-cutting concerns belong in `api` or `scheduler`.

```mermaid
flowchart TB
    subgraph L1["Layer 1 — Domain (pure)"]
        D1["domain/<br/>types, enums, dataclasses<br/>(immutable)"]
    end

    subgraph L2["Layer 2 — Pure math"]
        L2A["indicators/<br/>VW-EMA, ATR, bands,<br/>aggregation"]
    end

    subgraph L3["Layer 3 — Engine"]
        L3A["engine/<br/>TickerEngine, regimes,<br/>change_classifier"]
    end

    subgraph L4["Layer 4 — Persistence + Cache"]
        L4A["persistence/<br/>repos, connection, migrator"]
        L4B["cache/<br/>SignalCache (in-memory)"]
    end

    subgraph L5["Layer 5 — Compositional"]
        L5A["etl/<br/>fetchers, runner, CLI,<br/>signal_generator (P15)"]
        L5B["registry/<br/>TickerRegistry"]
        L5C["config/<br/>schema, loader,<br/>exchange_overrides (P15)"]
    end

    subgraph L6["Layer 6 — Schedulers + Notifiers"]
        L6A["scheduler/<br/>ComputeScheduler,<br/>Orchestrator, Distributor"]
        L6B["notifications/<br/>transports, breakers,<br/>batcher, redis queue"]
    end

    subgraph L7["Layer 7 — Boundary"]
        L7A["transports/http/<br/>FastAPI routes, schemas,<br/>dependencies"]
        L7B["api/<br/>lifecycle (prime_all)"]
        L7C["gui/<br/>PySide6 console"]
        L7D["notifier_worker.py<br/>(separate process)"]
        L7E["app.py<br/>(main service entrypoint)"]
    end

    subgraph cross["Cross-cutting"]
        X["observability/<br/>logging, metrics, tracing,<br/>correlation IDs"]
    end

    L2 --> L1
    L3 --> L2
    L3 --> L1
    L4 --> L1
    L5 --> L4
    L5 --> L3
    L5 --> L1
    L6 --> L4
    L6 --> L3
    L6 --> L5
    L7 --> L6
    L7 --> L5
    L7 --> L4
    L7 --> L3

    X -. used by every layer .-> L7
```

**Invariant 3.14 enforcement** is by import discipline (no cyclic dependencies, low-layer modules don't import from high-layer modules). A test (`tests/test_no_dead_island.py`) walks the import graph to catch new violations.

---

## 3. The four "actors" (running processes)

### 3.1 `trip-ema` main service

```mermaid
flowchart TB
    main["app.py main()"]
    main --> step1["1 load config + validate"]
    step1 --> step2["2 create DB engine"]
    step2 --> step3["3 alembic upgrade head"]
    step3 --> step4["4 build TickerRegistry"]
    step4 --> step5["5 build SignalCache"]
    step5 --> step6["6 prime_all_tickers<br/>(walk history, build engine state)"]
    step6 --> step7["7 build OrchestratorPool<br/>(one orchestrator per market)"]
    step7 --> step8["8 wire transports (subscribers, signals)"]
    step8 --> step9["9 wire SubscriberDistributor + Redis"]
    step9 --> step10["10 create FastAPI app + set_state(AppState)"]
    step10 --> step11["11 install SIGINT/SIGTERM handlers"]
    step11 --> step12["12 uvicorn.run(app)"]

    subgraph runtime["Runtime threads"]
        ut[uvicorn HTTP worker]
        scht["ComputeScheduler<br/>(orchestrator-fired)"]
        dt[SubscriberDistributor thread]
        execp["etl_executor ThreadPool<br/>(admin actions + Phase 15)"]
    end
```

**One process.** Do NOT run uvicorn with `--workers N`. Multiple workers would mean multiple TickerRegistries, multiple SignalCaches, and multiple schedulers racing on the DB — they all assume single-writer.

### 3.2 `trip-ema-notifier` worker

A separate process that pops jobs off Redis and dispatches via transport. Lives in `trip_ema/notifier_worker.py`. The reasoning: a wedged SMTP server shouldn't slow down the compute loop, and a breaker-tripped transport shouldn't trip the main service's circuit.

### 3.3 `trip-ema-etl` CLI

Stateless. Each invocation opens the DB, does its work, exits. No background threads. Designed to be cron-driven.

### 3.4 `trip-ema-console` GUI

PySide6 application. Talks to the main service exclusively over HTTP (never directly to Postgres) so a single deploy point owns auth, validation, and write paths. (Phase 12 invariant.)

---

## 4. Data flow — live signal path

```mermaid
sequenceDiagram
    autonumber
    participant Cron as cron
    participant ETL as trip-ema-etl
    participant DB as Postgres
    participant Orch as MarketOrchestrator
    participant Sched as ComputeScheduler
    participant Reg as TickerRegistry
    participant Eng as TickerEngine
    participant Cache as SignalCache
    participant Dist as SubscriberDistributor
    participant Q as Redis
    participant NW as NotifierWorker
    participant Mail as SMTP/Webhook
    participant Sub as Subscriber

    Cron->>ETL: trip-ema-etl incremental-eod --exchange NSE
    ETL->>DB: fetch bars via yfinance, upsert ohlcv_bars
    ETL->>DB: update exchange_state.last_eod_at

    Note over Orch: At configured wall-clock tick<br/>(post-open + cadence + pre-close)
    Orch->>Sched: run_once()
    Sched->>DB: read fresh bars
    Sched->>Reg: get engine(ticker)
    Reg->>Eng: ingest(new_bar)
    Eng-->>Reg: new BarState
    Sched->>Cache: write CachedSignal
    alt regime transitioned
        Sched->>DB: INSERT signal_events
        Sched->>Dist: notify(SignalEvent)
        Dist->>DB: enumerate matching subscribers
        Dist->>Q: enqueue NotificationJob per subscriber
    end

    Q-->>NW: pop job
    NW->>DB: is_event_superseded? (last_sent_at vs event ts)
    alt not superseded AND breaker closed
        NW->>Mail: send
        NW->>DB: notification_log INSERT
    else superseded OR breaker open
        NW->>DB: notification_dlq / drop
    end
    Mail-->>Sub: email / webhook / Slack
```

**Single-writer invariants.** Only `ComputeScheduler` writes to `SignalCache`. Only `ComputeScheduler` writes to `signal_events`. The notifier worker writes to `notification_log` (and DLQ); nothing else does. These are documented as invariants 3.2 and 3.10 in `docs/ARCHITECTURE.md`.

---

## 5. Data flow — Phase 15 backtest path

The backtest pipeline runs **fully outside** the live path. Different writes, different read patterns, no shared mutable state with the scheduler.

```mermaid
sequenceDiagram
    autonumber
    participant Op as Operator
    participant Route as POST /v1/admin/signals/generate
    participant Pg as Postgres
    participant Exec as etl_executor (ThreadPool)
    participant Wrap as _run_signal_generation_safe
    participant Gen as generate_historical_signals
    participant Tcfg as resolve_indicator_params
    participant Bars as BarsRepository
    participant Eng as TickerEngine (fresh)
    participant Repo as SignalBacktestRunsRepository
    participant Fs as generated_signals/{EX}.json

    Op->>Route: POST exchange=NSE&force_full=...&timeframe=1W
    Route->>Pg: BEGIN
    Route->>Pg: pg_advisory_xact_lock(hash(NSE))
    Route->>Pg: SELECT etl_runs WHERE in_flight AND exchange=NSE
    alt already running
        Route-->>Op: 409 Conflict
    else clear
        Route->>Pg: INSERT etl_runs (kind=signal_backtest, exchange=NSE)
        Route->>Pg: COMMIT (advisory lock auto-released)
        Route->>Exec: submit(_run_signal_generation_safe, run_id, ...)
        Route-->>Op: 202 Accepted (run_id, est. output path)
    end

    Exec->>Wrap: invoke (background thread)
    Wrap->>Gen: generate_historical_signals(run_id=...)

    opt force_full
        Gen->>Pg: wipe resume rows for exchange in dedicated tx
    end

    Gen->>Fs: load existing JSON or empty skeleton
    Gen->>Pg: fetch active tickers for exchange

    loop For each ticker × timeframe
        Gen->>Repo: get(exchange, ticker_id, tf)
        Repo-->>Gen: SignalBacktestRun (or None)
        Gen->>Bars: fetch_history(ticker_id, '1D', limit=10_000)
        Bars-->>Gen: bars_1d
        alt timeframe=1W
            Gen->>Gen: daily_to_weekly(bars_1d, tz)
        end
        Gen->>Tcfg: resolve params for (exchange, tf)
        Tcfg-->>Gen: IndicatorParams
        Gen->>Eng: build fresh TickerEngine(ref, tf, params)
        loop for bar in bars_used
            Gen->>Eng: ingest(bar)
            Eng-->>Gen: BarState
            Gen->>Gen: classify_change(prev_display, state.display_bg)
            alt signal != NO_CHANGE AND bar.date >= start_date
                Gen->>Gen: emit SignalEvent
            end
        end
        Gen->>Gen: _merge_signals_into(in_memory, ticker, tf, window, events)
        Gen->>Pg: UPSERT signal_backtest_runs in per-ticker tx
    end

    Gen->>Gen: _refresh_counts(in_memory)
    Gen->>Fs: write tmp file
    Gen->>Fs: fsync + os.replace(tmp -> final)
    Gen-->>Wrap: SignalGenerationResult
    Wrap->>Pg: close etl_runs row with outcome=success
```

**Two key non-obvious design choices** in this flow:

1. **Lifecycle split.** The HTTP route opens the `etl_runs` row in the request thread (so the concurrent-run guard's SELECT sees it) and signals to the runner "don't open your own". The runner does the work; the wrapper closes the row. The asymmetric ownership is the price of having both a CLI path (runner opens *and* closes) and an HTTP path (route opens, runner runs, wrapper closes) without duplicating logic.

2. **Advisory lock around the guard.** Plain read-committed transactions don't serialize `SELECT then INSERT`. Two concurrent POSTs to the same exchange could each pass the guard. `pg_advisory_xact_lock(hash(exchange))` serializes them. The lock is auto-released at commit/rollback so there's no leak risk.

---

## 6. Persistence architecture

### 6.1 Schema topology

```mermaid
erDiagram
    tickers ||--o{ ohlcv_bars : "has bars"
    tickers ||--o{ signal_events : "produces events (live)"
    tickers ||--o{ signal_backtest_runs : "tracked for backtest resume"
    tickers ||--o{ etl_ticker_status : "parked state"
    tickers ||--o{ corporate_actions : "splits/divs"
    subscribers ||--o{ notification_log : "received"
    subscribers ||--o{ notification_dlq : "failed delivery"

    tickers {
        int ticker_id PK
        text exchange
        text symbol
        text instrument
        boolean is_active
        timestamptz delisted_at
        int tier
        timestamptz created_at
        timestamptz updated_at
    }
    ohlcv_bars {
        int ticker_id FK
        text timeframe
        timestamptz bar_timestamp
        numeric open
        numeric high
        numeric low
        numeric close
        numeric volume
        timestamptz updated_at
    }
    signal_events {
        bigserial id PK
        int ticker_id FK
        text signal_type
        timestamptz bar_timestamp
        numeric price_at_signal
        timestamptz computed_at
        uuid correlation_id
    }
    signal_backtest_runs {
        text exchange PK
        int ticker_id FK,PK
        text timeframe PK
        date computed_from_date
        date computed_through_date
        timestamptz computed_at
        bigint last_run_id
    }
    etl_runs {
        bigserial run_id PK
        text kind
        text exchange
        timestamptz started_at
        timestamptz finished_at
        text outcome
        uuid correlation_id
        text notes
    }
    exchange_state {
        text exchange PK
        timestamptz last_eod_at
        timestamptz last_intraday_at
        timestamptz last_listings_refresh_at
        timestamptz updated_at
    }
```

### 6.2 Migration topology

Phase 12 introduced **two parallel schema-management systems**: Alembic is canonical, but the legacy SQL migrator is kept byte-equivalent. A parity test (`tests/persistence/test_legacy_migrator.py`) asserts they produce identical schemas.

```mermaid
flowchart LR
    subgraph alembic["Canonical (Alembic)"]
        a1["0001_initial_schema"]
        a2["0002_db_roles"]
        a3["0003_drop_tickers_source_timeframe"]
        a4["0004_exchange_state"]
        a5["0005_signal_backtest_runs<br/>+ etl_runs.exchange"]
    end
    subgraph legacy["Legacy parallel (SQL)"]
        l1["001_initial_schema.sql"]
        l2["002_signal_state.sql"]
        l3["003_subscribers.sql"]
        l4["004_partitions_seed.sql"]
        l5["005_drop_ticker_signal_state.sql"]
        l6["006_bars_updated_at.sql"]
        l7["007_ticker_tz_source_timeframe.sql"]
        l8["008_drop_notification_log_dedup_index.sql"]
        l9["010_etl_schema.sql"]
        l10["011_exchange_state.sql"]
        l11["012_signal_backtest_runs.sql"]
    end
    a5 -. byte-equivalent .-> l11
    parity["test_legacy_migrator.py<br/>compares table sets"]
    alembic --> parity
    legacy --> parity
```

**Why two systems?** Phase 12 needed Alembic for the role-grant migrations (legacy SQL didn't support down-migration), but the legacy migrator's invariant was baked into the bootstrap script (`scripts/restart_service.sh` style deployment). Both run in parallel and the parity test catches drift.

### 6.3 Per-process least-privilege (Phase 12)

Three Postgres roles:

```mermaid
flowchart LR
    subgraph procs[Processes]
        etl_proc[trip-ema-etl]
        engine_proc[trip-ema service<br/>+ ComputeScheduler]
        gui_proc[GUI / HTTP read paths]
    end
    subgraph roles[Postgres roles]
        etl_role[trip_etl_user<br/>SELECT/INSERT/UPDATE/DELETE on ETL tables]
        engine_role[trip_engine_user<br/>SELECT bars, INSERT signal_events,<br/>SELECT tickers]
        gui_role[trip_gui_user<br/>SELECT everything, no writes]
    end
    etl_proc --> etl_role
    engine_proc --> engine_role
    gui_proc --> gui_role
```

The `signal_backtest_runs` table (Phase 15a) follows this convention: `trip_etl_user` reads+writes (the signal generator runs as the ETL identity), `trip_engine_user` reads (for any future consumer), `trip_gui_user` reads (for the GUI's "last generated" panel).

---

## 7. HTTP transport architecture

### 7.1 Router decomposition

Each router owns a thematic slice of routes. They share a common `dependencies.py` module for auth + DB engine + executor + config injection.

```mermaid
flowchart LR
    subgraph public["Public routers (no auth)"]
        h["health"]
        s["signals"]
        c["compute"]
        p["pulse"]
    end
    subgraph admin["Admin routers (Bearer-token gate)"]
        a["admin (info)"]
        a_etl["admin_etl"]
        a_exst["admin_exchange_state"]
        a_ex["admin_exchanges"]
        a_sig["admin_signals (P15)"]
        a_sub["admin_subscribers"]
        a_notif["admin_notifications"]
        a_orch["admin_orchestrator"]
        a_hlth["admin_health"]
    end
    deps["dependencies.py<br/>get_db_engine, get_config,<br/>get_etl_executor,<br/>require_admin_token"]
    state["AppState (singleton)<br/>db_engine, cache, registry,<br/>etl_executor, config, ..."]
    deps --> state
    public --> deps
    admin --> deps
```

### 7.2 Async-action pattern

Admin endpoints that kick off heavyweight work follow a single pattern:

```mermaid
flowchart TB
    in[POST endpoint]
    in --> v1[Validate query params<br/>raise HTTPException 400 on bad input]
    v1 --> v2[Validate exchange]
    v2 --> v3[Check executor available<br/>raise 503 if None]
    v3 --> v4["Open etl_runs row<br/>(kind=*, exchange=, correlation_id=)"]
    v4 --> v5["executor.submit(worker, args...)"]
    v5 --> v6[return 202 Accepted with run_id]

    v5 -.background.-> w[Worker function]
    w --> w1[run the actual work]
    w1 --> w2[close etl_runs row]
```

**Phase 15b's variation.** The `admin_signals/generate` route also acquires a `pg_advisory_xact_lock(hash(exchange))` inside the same transaction as the SELECT-then-INSERT pair. Other admin actions don't need this because they're either single-row writes or have weaker concurrency requirements; signal generation does because mid-flight uniqueness on `(exchange, in_flight=true)` matters.

### 7.3 Schema / response binding

All responses use Pydantic models in `trip_ema/transports/http/schemas.py`. FastAPI handles the serialization; ISO timestamps + tz-aware datetimes throughout.

---

## 8. GUI architecture

### 8.1 View / widget split

```mermaid
flowchart TB
    mw[MainWindow<br/>tab container]
    mw --> sv[SignalsView]
    mw --> ssv[SubscribersView]
    mw --> esv[EtlStatusView]
    mw --> nv[NotificationsView]
    mw --> shv[SystemHealthView]
    mw --> tv[TickersView]
    mw --> stv[SettingsView]

    subgraph shared["Shared widgets (P15c)"]
        ep[ExchangePicker]
        bd[BackfillDialog]
        sgp[SignalGenerationPanel]
    end

    esv -- uses --> ep
    esv -- opens --> bd
    sv -- contains --> sgp
    sgp -- uses --> ep

    subgraph plumbing["Plumbing"]
        hc[TripEmaHttpClient]
        wk["workers.submit<br/>(QThreadPool)"]
        cs[QSettings persistence]
    end

    sv & ssv & esv & nv & shv & tv & stv --> hc
    sv & ssv & esv & nv & shv & tv & stv --> wk
    ep & stv --> cs
```

**Test architecture for the GUI.** Two test trees:

- `tests/gui/` — full PySide6 tests using `pytest-qt`. Cover view construction, widget behavior, signal/slot wiring.
- `tests/gui_pure/` — pure unit tests that don't need a `QApplication`. Cover the HTTP client and any other GUI-adjacent logic that can be tested without Qt.

The split exists because Qt teardown is fragile (Phase 12 invested heavily in stabilizing it via `conftest.py`'s `managed_widget` and `_drain_after_every_gui_test`). Where possible, logic is moved to `gui_pure/`.

### 8.2 Worker pattern

Every HTTP call from the GUI goes through `gui.workers.submit(callable)` which runs on `QThreadPool` and returns a worker with `finished` / `failed` signals. This keeps the Qt main thread responsive.

```mermaid
sequenceDiagram
    participant View as some GUI view
    participant Workers as workers.submit
    participant Pool as QThreadPool
    participant HTTP as TripEmaHttpClient
    participant API as service HTTP API

    View->>Workers: submit(lambda: http.list_exchanges())
    Workers->>Pool: start(Worker)
    Pool->>HTTP: lambda invocation
    HTTP->>API: GET /v1/admin/exchanges
    API-->>HTTP: 200 payload
    HTTP-->>Pool: result
    Pool->>View: emit finished(result)
    View->>View: update UI on main thread
```

### 8.3 Exchange picker reuse (Phase 15c lesson)

The picker logic was duplicated/inlined in `EtlStatusView` before Phase 15c. When `SignalGenerationPanel` needed the same behavior, the picker was extracted to `widgets/exchange_picker.py`. The pattern: `QComboBox` + status hint + loading-sentinel + persistence via `QSettings` + `selection_changed` signal. EtlStatusView was refactored to use the same widget; the test `tests/gui/test_etl_status_view_exchange_picker.py` (18 tests) guards the refactor.

---

## 9. Cross-cutting concerns

### 9.1 Configuration

`config.yaml` is loaded via `trip_ema/config/loader.py` → validated by Pydantic models in `trip_ema/config/schema.py`. Environment-variable references (`password_env: "PG_PASSWORD"`) are resolved at load time.

```mermaid
flowchart LR
    f[config.yaml] --> ldr[loader.py]
    env[Environment vars] --> ldr
    ldr --> schema[schema.py<br/>Pydantic validation]
    schema --> cfg["Config object<br/>(frozen)"]
    cfg --> usrs["Consumed by:<br/>app.py, CLI, ETL,<br/>scheduler, GUI"]

    subgraph blocks["Key config blocks"]
        db[database]
        http[http]
        ind["indicators<br/>(ema + atr + volume_sma_length)"]
        mh[market_hours]
        etl_blk[etl]
        orc[orchestrator]
        notif[notifications]
        sub_b[subscribers]
        eo["exchange_overrides<br/>(Phase 15a, optional)"]
    end
    schema --> blocks
```

**Phase 15a added `exchange_overrides`** as an optional top-level block. The schema's `_validate_exchange_overrides` runs as a `model_validator` to reject unknown timeframes and unknown exchanges at config-load time, so the service either starts with a known-good config or fails clearly.

### 9.2 Correlation IDs

A correlation ID is a UUID minted at the start of a logical operation. It propagates through every log line, every DB write that has a correlation_id column, and every OpenTelemetry span.

```mermaid
flowchart LR
    subgraph mints["Where IDs are minted"]
        m1["HTTP request<br/>(if no X-Correlation-Id header)"]
        m2["ComputeScheduler.run_once()"]
        m3["SubscriberDistributor._enqueue_jobs"]
        m4["NotifierWorker._handle_job"]
        m5["generate_historical_signals (P15)"]
    end
    subgraph use["Where IDs are read"]
        u1[log records]
        u2[OTel spans]
        u3[DB rows]
    end
    mints --> ctx[correlation_id context-var]
    ctx --> use
```

### 9.3 Observability stack

| Concern | Module | Output |
|---|---|---|
| Logging | `observability/logging_config.py` | Structured JSON to stdout (default) |
| Metrics | `observability/metrics.py`, `metrics_server.py` | Prometheus scrape on `/metrics` |
| Tracing | `observability/tracing.py` | OTLP / Zipkin compatible spans |
| Correlation | `observability/correlation.py` | Context-var propagation |

---

## 10. Test architecture

```mermaid
flowchart TB
    subgraph nodes["No DB / no Qt"]
        engine_t["tests/engine/"]
        ind_t["tests/indicators/"]
        par_t["tests/parity/<br/>(Pine fixtures)"]
        sched_pure["tests/scheduler_pure/"]
        ntf_pure["tests/notifier_pure/"]
        gui_pure["tests/gui_pure/"]
        cfg_t["tests/config/"]
        tr_pure["tests/transports_pure/<br/>(mocked DB)"]
    end

    subgraph dbtests["DB-required (sqlite or Postgres skipped)"]
        per_t["tests/persistence/"]
        etl_t["tests/etl/"]
    end

    subgraph qtests["Qt-required"]
        gui_t["tests/gui/<br/>(pytest-qt)"]
    end

    subgraph http["HTTP integration"]
        tr_t["tests/transports/"]
    end

    subgraph e2e["End-to-end"]
        api_t["tests/api/<br/>(stateless + cached + lifecycle)"]
    end

    invariant["tests/test_no_dead_island.py<br/>(import-graph check)"]
```

**Two pytest-qt tactics.** GUI tests use a `managed_widget` factory + `_drain_after_every_gui_test` autouse fixture (Phase 12 work) to handle deferred-deletion correctly. Some flaky tests (notably `tests/gui/test_signals.py` per the design doc) are `--deselect`-ed by convention; the design's expected count of "1138 + ~100 = 1238 passing" includes the deselect.

---

## 11. Deployment & operations

### 11.1 Single-host, single-process model

```mermaid
flowchart LR
    subgraph host["One host"]
        subgraph services["systemd or shell"]
            s1["trip-ema (service)<br/>port 8000"]
            s2["trip-ema-notifier"]
            s3["trip-ema-console (GUI)<br/>only when operator is using it"]
        end
        subgraph storage["Local"]
            pg[(Postgres)]
            rd[(Redis)]
            fs[("./generated_signals/")]
            logs[("/tmp/trip-ema*.log")]
        end
        subgraph cron["cron"]
            c1["incremental-eod nightly"]
            c2["recompute-tiers weekly"]
            c3["refresh-listings weekly"]
        end
    end
    s1 <--> pg
    s1 <--> rd
    s2 <--> rd
    s2 --> pg
    s1 --> fs
    cron --> s1
    s3 <-- HTTP --> s1
```

**Scripts.**

| Script | Purpose |
|---|---|
| `scripts/restart_service.sh` | Kill + restart `trip-ema`; poll `/healthz` until primed |
| `scripts/gui_start.sh` | Idempotent GUI launcher (skip if already running) |
| `scripts/smoke_test_fast.sh` / `_full.sh` | Health checks for CI / ops |
| `scripts/health_check.sh` | One-shot probe |
| `scripts/audit_dead_island.sh` | Run the import-graph invariant test |

### 11.2 What can't be horizontally scaled today

Invariants 3.2 (SignalCache), 3.6 (engine provisional/final), 3.10 (notification dedup) all assume one process. Running `uvicorn --workers N` would produce N caches that disagree.

The path to horizontal scaling, when needed, is leader-election or ticker-sharding — *not* naive process multiplication. Captured in `docs/ARCHITECTURE.md` §8.

---

## 12. Phase 15 architectural patterns to internalize

These are the patterns that Phase 15 either established or reinforced:

| Pattern | Where introduced / reinforced |
|---|---|
| **Per-ticker commit, not one big transaction** | `signal_generator._process_ticker_timeframe` commits `signal_backtest_runs` after each ticker. A mid-run kill leaves resume rows for committed tickers durable; the next run picks up cleanly. |
| **Asymmetric run_id lifecycle** | The runner accepts `run_id=None` (own the lifecycle) or `run_id=<int>` (caller owns the close). Lets one runner serve both CLI and HTTP. |
| **Advisory lock around concurrent-run guard** | First time this pattern appears; should be the template for any future `SELECT then INSERT` mutex pattern across processes. |
| **Atomic file write via temp + rename** | tmp file + fsync + `os.replace`. Generic enough to be reusable for any "single per-key file" pattern. |
| **In-memory merge with three-case semantics** | Add / replace / delete operations on a date-keyed dict, documented and tested explicitly. Reusable shape for any "recompute a window, preserve outside" use case. |
| **Reusable widget extraction** | The picker was inline in 14.6c, extracted in 15c so it could be reused in two views. Future tabs should consume widgets rather than reimplementing them. |
| **Endpoint audit as living documentation** | `docs/gui_endpoint_audit.md` documents the GUI-coverage status of every route. When a new route ships, the table must be updated in the same commit. |

---

## 13. Summary

The architecture is **layered, single-host, single-process for live computation**, with the backtest pipeline added as a parallel branch that consumes the same engine code but writes to its own storage (`signal_backtest_runs` table + JSON file). All cross-process boundaries are well-defined (Engine ↔ ETL ↔ GUI ↔ Notifier each a separate Python process). The HTTP API is the single integration point operators and the GUI use; nothing else talks directly to Postgres.

Phase 15's contribution to the architecture is small but principled: it introduces a parallel-output pipeline without disturbing the live path, encodes resume semantics into the persistence layer (`signal_backtest_runs`), demonstrates the asymmetric-lifecycle pattern for run rows, and reinforces the widget-extraction discipline for the GUI.

The system is intentionally **un-microserviced** for now. If horizontal scaling becomes a requirement, the next architectural conversation is leader-election or sharding, not splitting the service into N tiny processes.
