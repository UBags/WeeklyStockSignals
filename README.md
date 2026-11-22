# WeeklyStockSignals · `trip_ema`

**Weekly-chart trend signals for Indian equities, computed every trading day and delivered by email.**

`trip_ema` is a Python port of a TradingView Pine Script indicator — a volume-weighted **Triple EMA** ribbon with **ATR bands** and a three-state **regime** classifier — wrapped in a production service. It ingests NSE daily bars, recomputes the *weekly* chart of every listed instrument (~2,700 tickers) several times per session, detects regime flips (`BUY`, `SELL`, `EXIT_FROM_BUY`, `EXIT_FROM_SELL`), enriches each signal with industry, lead/lag and benchmark context, and emails a ranked digest to subscribers. A PySide6 desktop console sits on top for operations.

![Indicator example](docs/images/indicator_example.png)

<sub>Rendered by <a href="scripts/render_readme_chart.py"><code>scripts/render_readme_chart.py</code></a> from <b>synthetic</b> prices, using the production functions (<code>compute_indicator_series</code>, <code>_compute_regime_series</code>, <code>DetermineSignal</code>). No market data is involved.</sub>

| | |
|---|---|
| **Language / runtime** | Python 3.12+ |
| **Service** | FastAPI + uvicorn, two background threads, subprocess-per-cycle |
| **Storage** | PostgreSQL (16–18), SQLAlchemy 2.0 Core, 56 Alembic revisions |
| **Data sources** | NSE listing archives, yfinance (`auto_adjust=False`), `nselib` corporate actions |
| **Delivery** | Brevo transactional-email HTTP API |
| **Console** | PySide6 + pyqtgraph |
| **Observability** | Structured logs, Prometheus, OpenTelemetry (all optional) |
| **Size** | ~70k LOC in `trip_ema/`, 245 test modules |
| **Version** | 0.12.0 |

> **Not investment advice.** This is a signal-generation and research platform. Signals are mechanical outputs of a technical indicator; nothing here claims they are profitable.

---

## Contents

1. [How it works, in one picture](#1-how-it-works-in-one-picture)
2. [The indicator](#2-the-indicator)
3. [Runtime architecture](#3-runtime-architecture)
4. [The live cycle](#4-the-live-cycle)
5. [Signal storage model](#5-signal-storage-model)
6. [Data ingestion and OHLCV correction](#6-data-ingestion-and-ohlcv-correction)
7. [Industry indices, lead/lag and enrichment](#7-industry-indices-leadlag-and-enrichment)
8. [Forward-walk labels (research layer)](#8-forward-walk-labels-research-layer)
9. [Notifications](#9-notifications)
10. [Operator console](#10-operator-console)
11. [HTTP API](#11-http-api)
12. [Database](#12-database)
13. [Installation and running](#13-installation-and-running)
14. [Backfill](#14-backfill)
15. [Testing and CI](#15-testing-and-ci)
16. [Repository layout](#16-repository-layout)
17. [Design invariants](#17-design-invariants)
18. [Status and roadmap](#18-status-and-roadmap)

---

## 1. How it works, in one picture

```mermaid
flowchart LR
    subgraph SRC["External sources"]
        NSE["NSE listing archives<br/>(equities + ETFs CSV)"]
        YF["yfinance<br/>daily OHLCV, shares,<br/>financials, industry"]
        NL["nselib<br/>corporate actions"]
    end

    subgraph SVC["trip-ema service (one process)"]
        SCH["RunScheduler<br/>wall-clock ticks"]
        DSP["RunDispatcher<br/>one cycle at a time"]
        API["FastAPI<br/>public + admin API"]
    end

    CYC["trip-ema-etl cycle<br/>(subprocess per tick)"]
    PG[("PostgreSQL")]
    BREVO["Brevo HTTP API"]
    SUBS["Subscribers"]
    GUI["trip-ema-console<br/>(PySide6)"]

    SCH -- "enqueue (exchange, kind)" --> DSP
    DSP -- "spawn" --> CYC
    NSE --> CYC
    YF --> CYC
    NL --> CYC
    CYC -- "bars, state, signals" --> PG
    CYC -- "ranked digest" --> BREVO --> SUBS
    API <--> PG
    GUI -- "admin actions (HTTP)" --> API
    GUI -. "read-only SQL" .-> PG
```

On each scheduled tick the service starts **one subprocess** that fetches fresh bars, recomputes the indicator state for every stock and industry index, derives signals, and (after the close) enriches, labels and emails them. The HTTP service never computes signals itself. It schedules, serves and administers.

---

## 2. The indicator

### 2.1 Construction

Each of the three ribbon lines is a **volume-weighted, Hull-polished, multi-stage EMA**. It is a direct port of the Pine source, including Pine's NaN warm-up semantics (`trip_ema/indicators/vw_ema.py`, `trip_ema/fundamentals/indicator_primitives.py`).

```mermaid
flowchart LR
    V["volume"] --> SMA["sma(volume, 2)"]
    C["close"] --> NUM["ema(close × vol, base)"]
    SMA --> NUM
    SMA --> DEN["ema(vol, base)"]
    NUM --> VW1["vw1 = num / den"]
    DEN --> VW1
    VW1 --> VW2["vw2 = ema(vw1, s1)"]
    VW2 --> HULL["Hull polish<br/>wma(2·wma(vw2, ⌊s2/2⌋) − wma(vw2, s2), ⌊√s2⌋)"]
    HULL --> FIN{"final set?"}
    FIN -- yes --> OUT1["ema(·, final)"]
    FIN -- no --> OUT2["polished value"]
```

| Line | `base` | `s1` | `s2` | `final` |
|---|---:|---:|---:|---:|
| **Med** | 8 | 4 | 4 | — |
| **Long** | 15 | 4 | 4 | 3 |
| **Longest** | 28 | 7 | 5 | 3 |

**ATR** is Wilder's RMA of true range (length 14): NaN for bars 1…13, seeded from the SMA at bar 14, recursive after that. Three band pairs hug the *outer edges* of the ribbon:

```
upper_k = max(med, long, longest) + mult_k × ATR
lower_k = min(med, long, longest) − mult_k × ATR        mult = 0.2 (inner) · 2.0 (mid) · 4.0 (outer)
```

All parameters live in the `ema:` and `atr:` blocks of `config.yaml`.

### 2.2 Regime state machine

`trip_ema/regime/compute_regime.py` holds the single canonical implementation. Rules are evaluated top to bottom, and the first match wins:

```mermaid
stateDiagram-v2
    direction LR
    [*] --> NEUTRAL
    NEUTRAL --> BULLISH: close ≥ upper_inner
    NEUTRAL --> BEARISH: close ≤ lower_inner
    BULLISH --> BULLISH: close ≥ ribbon low<br/>OR (close ≥ lower_inner AND prev close ≥ prev ribbon low)
    BULLISH --> BEARISH: close ≤ lower_inner (sharp reversal)
    BULLISH --> NEUTRAL: continuation fails
    BEARISH --> BEARISH: close ≤ ribbon high<br/>OR (close ≤ upper_inner AND prev close ≤ prev ribbon high)
    BEARISH --> BULLISH: close ≥ upper_inner (sharp reversal)
    BEARISH --> NEUTRAL: continuation fails
```

During warm-up, before all bands are finite, the classifier returns NEUTRAL.

### 2.3 From regime transitions to signals

`DetermineSignal` (`trip_ema/domain/determine_signal.py`) is the **only** place a signal is derived from a regime pair. It is total and never raises.

| Before → After | Signal |
|---|---|
| any → **BULLISH** | `BUY` (wins over `EXIT_FROM_SELL` on a BEARISH→BULLISH reversal) |
| any → **BEARISH** | `SELL` (wins over `EXIT_FROM_BUY` on a BULLISH→BEARISH reversal) |
| BULLISH → NEUTRAL | `EXIT_FROM_BUY` |
| BEARISH → NEUTRAL | `EXIT_FROM_SELL` |
| unchanged | `NO_CHANGE` (never persisted) |

### 2.4 Weekly chart, daily grain

Signals are computed on the **weekly** chart but **evaluated every trading day**. Each row of `stock_indicator_state` is *the weekly chart as it stands at the close of day D*, with the current week's bar still forming. A signal fires on day D when the weekly regime on D differs from D‑1.

```mermaid
flowchart LR
    subgraph W["Week of Mon 2026-07-13 (one weekly bar, still forming)"]
        D1["Mon<br/>weekly bar = Mon"] --> D2["Tue<br/>weekly bar = Mon..Tue"] --> D3["Wed<br/>= Mon..Wed"] --> D4["Thu<br/>= Mon..Thu"] --> D5["Fri<br/>= Mon..Fri (final)"]
    end
    D1 -.-> R1["regime(D) vs regime(D-1)<br/>→ signal on signal_date = D"]
    D3 -.-> R1
```

That is why every signal table carries two date columns: `signal_date` is the trading day the signal fired, and `bar_timestamp` is the IST-Monday week anchor that all days of the week share. **Filter on `signal_date`.**

---

## 3. Runtime architecture

### 3.1 Processes and threads

```mermaid
flowchart TB
    subgraph MAIN["trip-ema  (python -m trip_ema.app)"]
        direction TB
        UV["uvicorn / FastAPI<br/>HTTP :8000"]
        RS["RunScheduler thread<br/>reads market_hours each loop,<br/>sleeps until next tick"]
        RQ[["RunQueue"]]
        RD["RunDispatcher thread<br/>gated on update_run_in_progress<br/>and backfill_runs"]
        EX["ETL ThreadPoolExecutor<br/>max_workers = 2<br/>(GUI-triggered jobs)"]
        CACHE["CurrentSignalsCache<br/>(in-memory read model)"]
        MET["Prometheus /metrics<br/>(optional, own port)"]
        RS --> RQ --> RD
        UV --> CACHE
    end

    RD -- "Popen: trip_ema.etl.cli cycle<br/>--exchange X --kind intraday or eod" --> CYC["cycle subprocess"]
    UV -- "POST /v1/admin/etl/enqueue-run" --> RQ
    UV -- "POST /v1/admin/etl/reset-in-progress<br/>SIGTERM → SIGKILL" --> CYC
    CYC --> PG[("PostgreSQL")]
    UV --> PG
```

* **Single process, single writer.** Do not run uvicorn with `--workers N`. The scheduler, dispatcher and cache assume one owner.
* **One cycle at a time, globally.** The `update_run_in_progress` row (unique per exchange) plus a backfill check means a cycle never overlaps another cycle or a running backfill.
* **Cancellation is clean.** The cycle subprocess installs a SIGTERM handler, so a force-reset still closes its `compute_runs` row as cancelled.

### 3.2 When ticks fire

`trip_ema/scheduler/tick_planner.py` resolves each exchange's `tick_schedule:` into four instants per trading day, skipping weekends and holidays (python-`holidays` plus `configs/nse_holidays_2026.yaml`). The `mid_session` tick sits at the midpoint between the last open-anchored tick and the first close-anchored tick.

```mermaid
gantt
    title NSE trading day (IST): session 09:15 to 15:30
    dateFormat HH:mm
    axisFormat %H:%M
    section Session
    Market open                     :active, s1, 09:15, 15:30
    section Ticks
    post_open  +15m  (intraday)     :milestone, t1, 09:30, 0m
    mid_session midpoint (intraday) :milestone, t2, 12:15, 0m
    pre_close  -30m  (intraday)     :milestone, t3, 15:00, 0m
    post_close +60m  (EOD)          :crit, milestone, t4, 16:30, 0m
```

`post_close` is the only **EOD** tick. The other three are **intraday**. The kind decides what the cycle writes (§5) and whether the EOD-only stages run (§4).

---

## 4. The live cycle

`trip-ema-etl cycle --exchange NSE --kind eod` is the unit of work, whether the dispatcher spawns it or an operator runs it by hand. It is implemented in `trip_ema/etl/cli.py::cmd_cycle` and `trip_ema/orchestrator/recompute_market.py`.

```mermaid
flowchart TB
    START(["tick"]) --> F["1 · Fetch OHLCV<br/>whole universe, tier-1 first"]
    F --> SS["2 · Stock indicator state<br/>(incremental, per ticker)"]
    SS --> SE["3 · Stock indicator enrichment"]
    SE --> SIG["4 · Signals<br/>intraday transition log<br/>+ process_signals_for_day"]
    SIG --> CR["5 · Cache rebuild"]
    CR --> IIV["6 · Industry index values<br/>(recent window)"]
    IIV --> IS["7 · Industry indicator state"]
    IS --> IE["8 · Industry indicator enrichment"]
    IE --> K{"kind == eod?"}
    K -- no --> N
    K -- yes --> PS["9 · Per-signal stages<br/>signal_enrichment → labels → ensemble"]
    PS --> FW["9a · Forward-walk labels<br/>for tickers that EXITed today"]
    FW --> LL["9b · Monthly lead/lag refresh<br/>(first EOD of the month only)"]
    LL --> N["10 · Notify subscribers<br/>(skipped if Brevo not configured)"]
    N --> END(["compute_runs row closed"])

    classDef eod fill:#fff3e0,stroke:#f57c00;
    class PS,FW,LL eod;
```

A stage error halts the remaining stages and is recorded on the `compute_runs` row (`failed_stage`, `error_message`). A failure on one ticker or one industry inside a stage is isolated and does not halt the stage. Each stage publishes its live name to `compute_runs.current_stage`, so the console can show progress even for scheduler-driven runs whose stdout it can't read.

### 4.1 One scheduled tick, end to end

```mermaid
sequenceDiagram
    autonumber
    participant RS as RunScheduler
    participant RQ as RunQueue
    participant RD as RunDispatcher
    participant DB as PostgreSQL
    participant CY as cycle subprocess
    participant YF as yfinance / NSE
    participant BR as Brevo

    RS->>RS: sleep until next tick (NSE post_close 16:30 IST)
    RS->>RQ: enqueue(NSE, eod)
    RD->>RQ: poll
    RD->>DB: update_run_in_progress empty? no active backfill?
    RD->>DB: INSERT update_run_in_progress(NSE, eod)
    RD->>CY: Popen(trip_ema.etl.cli cycle --exchange NSE --kind eod)
    RD->>DB: record pid
    CY->>DB: INSERT compute_runs
    CY->>YF: fetch daily bars (all active tickers)
    YF-->>CY: OHLCV
    CY->>DB: upsert ohlcv_bars
    CY->>DB: state → enrichment → signals → industries (stages 2–8)
    CY->>DB: signal_enrichment / labels / ensemble / forward walk (EOD only)
    CY->>DB: read current_signals + rank + lead/lag
    CY->>BR: POST /v3/smtp/email (one digest per due subscriber)
    CY->>DB: notification_log, compute_runs.finished_at
    CY-->>RD: exit 0
    RD->>DB: DELETE update_run_in_progress
```

---

## 5. Signal storage model

Three tables, three different questions. All are written atomically by `trip_ema/orchestrator/process_day.py`, so derivation and both writes share one transaction.

```mermaid
flowchart LR
    DER["derive_signal_for_ticker_this_cycle<br/>(regime D-1 → regime D)"]
    DER -->|"every intraday tick<br/>append transitions"| ISL[("intraday_signal_log<br/>full within-day chain<br/>e.g. BUY → EXIT_FROM_BUY → BUY")]
    DER -->|"upsert flips,<br/>delete non-flips"| CS[("current_signals<br/>PK ticker_id<br/>'what fired today'")]
    DER -->|"EOD and backfill only:<br/>delete day D + insert flips"| HS[("historical_signals<br/>UNIQUE ticker, timeframe, signal_date<br/>one NET signal per day")]
```

| Mode | `historical_signals` | `current_signals` |
|---|---|---|
| `intraday` | never written (the day isn't final) | upsert flips + delete non-flips |
| `eod` | delete day D, insert flips | upsert flips + delete non-flips |
| `backfill` | delete day D, insert flips | only when D is today |

`current_signals` is refreshed with row-level upsert and delete, **not** `TRUNCATE`, so readers in the console and API never hit an `ACCESS EXCLUSIVE` lock.

---

## 6. Data ingestion and OHLCV correction

### 6.1 Universe and fetch

* **Listings:** `trip-ema-etl refresh-listings --exchange NSE` pulls the NSE equities and ETF archives and syncs `tickers`. Renames are tracked through ISIN continuity (`symbol_history`).
* **Tiers:** a weekly recompute ranks tickers by 30-day average traded value. Tier 1 is the top 200, tier 2 the next 500, tier 3 the rest. Tiers only decide **fetch order**. Every cycle fetches the full universe.
* **Parking:** three consecutive fetch failures park a ticker (`etl_ticker_status`). Unpark it from the console or with `trip-ema-etl unpark`.

### 6.2 Seam correction against authoritative corporate actions

yfinance is fetched with `auto_adjust=False`, which gives split-adjusted but dividend-unadjusted bars. Splits and bonuses can still leave **seams** in the series, i.e. unadjusted legs spliced to adjusted ones. Before any indicator is computed, `trip_ema/backfill/ohlcv_correction_stage.py` reconciles every affected ticker against `nselib`'s NSE corporate-action feed (`nse_corporate_actions`):

```mermaid
flowchart TB
    A["daily bars around each ex_date"] --> B["boundary-excluding medians<br/>before vs after"]
    B --> C["ratio r = after / before"]
    C --> D{"classify r against<br/>{1, f, 1/f, f², 1/f²}<br/>(f = nselib price_factor)"}
    D -- "≈ 1" --> OK["ADJUSTED_OK<br/>leave untouched"]
    D -- "≈ 1/f" --> SEAM["SEAM_PRESENT<br/>rescale pre-boundary leg by exact f"]
    D -- "≈ f² / 1/f²" --> BOT["BOTCHED<br/>(double or inverted adjustment)<br/>snap to exact factor"]
    D -- "no match" --> CEN["censor window"]
    DM["demerger /<br/>capital reduction"] --> CEN
    SEAM --> W["write corrected bars<br/>back to ohlcv_bars"]
    BOT --> W
```

`data/nse_corporate_actions.csv` is committed as an offline fallback snapshot. Any fallback path must report the snapshot's export date, so that bars are never silently reconciled against stale data. Design notes: [`docs/01_ohlcv_correction_design.md`](docs/01_ohlcv_correction_design.md), [`docs/nselib_source.md`](docs/nselib_source.md).

---

## 7. Industry indices, lead/lag and enrichment

```mermaid
flowchart LR
    II["instrument_industry<br/>(yfinance / overrides)"] --> IDX["industry_index<br/>Modified Laspeyres,<br/>semi-annual Apr/Oct rebalance,<br/>top-N by market cap"]
    MC["daily_market_cap<br/>(close × shares)"] --> IDX
    IDX --> IIV["industry_index_value<br/>daily, base 1000,<br/>chain-linked periods"]
    IIV --> IST["industry_indicator_state<br/>same Triple-EMA/ATR/regime"]
    IST --> BR["industry_regime_breadth"]
    IST --> CORR["industry_regime_corr<br/>pairwise corr + best lag"]
    CORR --> ENS["influential_industry_ensemble<br/>leaders per industry"]
    IIV --> SSL["sector_stock_leadlag<br/>stock vs own industry:<br/>LEADER / LAGGARD /<br/>COUPLED / DECOUPLED"]
    ENS --> SEF["signal_ensemble_features<br/>do the leaders confirm?"]
```

Every signal gets a feature row in `signal_enrichment`:

* signed, polarity-adjusted distance to each EMA in ATR(10/15/20/30/40) units, plus 75 band-membership flags
* `seq_in_week` and `seq_in_consec_weeks` (causal repeat counters)
* `data_is_valid` and `bars_available`, so long-period ATR families aren't trusted before there is enough history

On top of that it gets the ensemble confirmation features and benchmark statistics (`stock_benchmark_stats`: β, α, R², relative strength over short/medium/long windows). Specs: [`docs/enrichment_features_spec.md`](docs/enrichment_features_spec.md), [`docs/leadlag-analytics-design.md`](docs/leadlag-analytics-design.md).

---

## 8. Forward-walk labels (research layer)

When a position **closes** (an `EXIT_FROM_*` signal), its window becomes final and the EOD cycle labels it in `signal_labels`. Each label is a 1:1 child of the signal's enrichment row.

```mermaid
timeline
    title Forward-walk window for one BUY
    signal_date (D) : BUY fires on the weekly regime flip
    D + 1 : Entry = open of first bar after D
    Window : Daily walk tracks gain rungs 1…100%, loss rungs 5…20%, MAE / MFE and their bar index, time underwater
    Exit day N : EXIT_FROM_BUY fires, Exit = close of day N, realized return, capture ratio, Sharpe in window
    Censoring : Demerger or capital reduction inside the window, window_censored = true (bookkeeping row, no truncation)
```

The window yields 21 gain rungs and 6 loss rungs (including 7½% and 12½%). Each rung gets a hit boolean and a `bars_to_*` integer, with −1 as the "never hit" sentinel. There are also excess returns versus industry and market, ATR-normalised excursions, and in-window slope and volatility. The forward walk fans out across tickers on the shared threaded `fan_out` primitive (`trip_ema/fundamentals/parallel.py`). Each ticker's failure is isolated. Spec: [`docs/02_signal_forward_walk_labels_spec.md`](docs/02_signal_forward_walk_labels_spec.md).

Backfill a range with `trip-ema-etl forward-walk --exchange NSE [--tickers …]`.

---

## 9. Notifications

```mermaid
flowchart LR
    CS[("current_signals")] --> J["join tickers +<br/>instrument_industry"]
    J --> RK["rank_score =<br/>0.5·stock RS 26w<br/>+ 0.3·industry RS 26w<br/>+ 0.2·stock R² 26w"]
    RK --> G["group by industry<br/>(Unknown last)"]
    G --> LL["append lead/lag context<br/>+ intraday chain"]
    LL --> R["render HTML + plain text"]
    R --> DUE{"subscriber due?<br/>notify_cadence_minutes"}
    DUE -- yes --> B["Brevo POST /v3/smtp/email"]
    B --> NL[("notification_log<br/>monthly partitions")]
```

* Subject line: `[trip_ema] <cycle_id> — N signal(s) (b BUY, s SELL, e EXIT)`
* Subscribers set a cadence in multiples of 15 minutes, from 15 to 1,440. A "Test notification" button sends a one-off email through the configured transport.
* The Brevo key lives in `secrets/brevo.yaml` (gitignored; template in `secrets/brevo.yaml.example`). Without it, the notify stage is skipped and the cycle still succeeds.
* A Redis-backed queue, DLQ endpoints and circuit-breaking transports still exist in `trip_ema/notifications/`, but the V1 cycle sends directly and does not need them.

---

## 10. Operator console

`trip-ema-console` is a desktop client.

* **Administrative writes** go through the admin HTTP API: subscribers, listings and tier refreshes, unparking, and force-resetting a stuck run.
* **Manual cycles and backfills** are launched as local `trip-ema-etl` subprocesses (QProcess). Their stdout drives the live stage list.
* **Table views** read Postgres directly and read-only, optionally as `trip_gui_user`.

Without `TRIP_EMA_ADMIN_TOKEN` the console starts in read-only mode, with write buttons disabled.

| Tab | What it does |
|---|---|
| ⚙ **ETL Status** | Run history, live stage progress (for local runs and scheduler-driven runs alike), parked tickers, tier counts; start a cycle or backfill, reset a stuck run |
| 📊 **Tickers** | Searchable universe with per-ticker signal history; CSV export; jump to chart |
| 📈 **Signals** | pyqtgraph chart: candles, three EMAs, ATR bands, regime fill, signal markers, volume pane, crosshair OHLC; 1Y/3Y/5Y/10Y/All ranges; industry-index charts |
| 👥 **Subscribers** | CRUD over subscribers, filters and cadence; test send |
| 🔔 **Notifications** | Recent `notification_log` entries, colour-coded by signal |
| ❤ **System Health** | Status cards polled every 10 s: universe, ETL freshness, scheduler, cache, queue, recent events |
| ⚙ **Settings** | GUI preferences (`QSettings`, never `config.yaml`); read-only config viewer; timestamped config backup |

The console also has a backtest dialog, a market-trend dialog, industry panels and a signal-generation panel.

```mermaid
sequenceDiagram
    participant Op as Operator
    participant GUI as trip-ema-console
    participant CY as local cycle (QProcess)
    participant DB as PostgreSQL
    participant API as trip-ema API

    rect rgb(232, 245, 233)
    Note over Op,DB: Manual run, launched from the console
    Op->>GUI: Start cycle (ETL Status)
    GUI->>CY: python -m trip_ema.etl.cli cycle --exchange NSE
    loop each stage
        CY-->>GUI: stdout "[stage] running / done / failed"
        GUI-->>Op: stage list updates
    end
    CY->>DB: bars, state, signals, compute_runs
    end

    rect rgb(227, 242, 253)
    Note over Op,API: Scheduled run, spawned by the service
    loop mutex tick
        GUI->>DB: in-flight compute_runs, current_stage
        GUI-->>Op: mirror the remote stage
    end
    Op->>GUI: Reset stuck run
    GUI->>API: POST /v1/admin/etl/reset-in-progress (Bearer)
    API-->>GUI: SIGTERM, then SIGKILL, row released
    end
```

---

## 11. HTTP API

Default `http://0.0.0.0:8000`. Admin routes require `Authorization: Bearer $TRIP_EMA_ADMIN_TOKEN`. They return **503** if the token env var is unset, **401** if the header is missing, and **403** if the token is wrong.

| Group | Endpoints |
|---|---|
| **Health** | `GET /healthz` · `GET /readyz` |
| **Signals** | `GET /v1/signals/{exchange}/{symbol}/history` · `GET /v1/signals/industry/{industry_name}/history` · `POST /v1/compute` |
| **ETL / runs** | `POST /v1/admin/etl/{enqueue-run, run-intraday, run-eod, backfill, backfill-shares, refresh-listings, recompute-tiers, unpark-all, reset-in-progress}` · `GET /v1/admin/etl/{status, parked, queue-snapshot}` |
| **Exchanges** | `GET /v1/admin/exchanges` · `GET /v1/admin/exchange-state[/{exchange}]` |
| **Signals admin** | `POST /v1/admin/signals/generate` · `GET /v1/admin/signals/generated` · `POST /v1/admin/compute-now` · `GET /v1/admin/stale-tickers` |
| **V1 pipeline (manual recompute)** | `POST /v1/admin/v1pipeline/{stock-indicator-state, stock-indicator-enrichment, industry-index-value, industry-indicator-state, industry-indicator-enrichment}` |
| **Subscribers** | `GET/POST /v1/admin/subscribers` · `GET/PATCH/DELETE /v1/admin/subscribers/{id}` · `PUT /v1/admin/subscribers/{id}/filters` |
| **Notifications** | `GET /v1/admin/notifications/recent` · `GET /v1/admin/notifications/dlq` · `POST …/dlq/{job_id}/replay` · `DELETE …/dlq/{job_id}` |
| **Health snapshot** | `GET /v1/admin/health-snapshot` |
| **Fundamentals** | industry (get, refresh, override), shares, quarterly financials, market cap, industry indices (list, constituents, values, build, compute): all under `/v1/fundamentals/…` |

FastAPI's interactive docs are at `/docs` while the service is running.

---

## 12. Database

PostgreSQL, managed exclusively by **Alembic** (`alembic_pkg/versions/0001…0056`). Migrations run at service start, and a failed migration is fatal. Core relationships:

```mermaid
erDiagram
    tickers ||--o{ ohlcv_bars : "daily bars"
    tickers ||--o{ stock_indicator_state : "weekly chart @ day D"
    stock_indicator_state ||--|| stock_indicator_enrichment : "1:1 (ticker, tf, date)"
    tickers ||--o| current_signals : "fired this cycle"
    tickers ||--o{ historical_signals : "net signal per day"
    tickers ||--o{ intraday_signal_log : "within-day chain"
    tickers ||--o{ signal_enrichment : "features per signal"
    signal_enrichment ||--o| signal_labels : "forward-walk outcome"
    signal_enrichment ||--o{ signal_ensemble_features : "per variant"
    tickers ||--o| instrument_industry : "classification"
    tickers ||--o{ nse_corporate_actions : "splits, bonuses, demergers"
    tickers ||--o{ daily_market_cap : ""
    industry_index ||--o{ industry_index_constituent : "per rebalance"
    industry_index ||--o{ industry_index_value : "daily value"
    industry_index ||--o{ industry_indicator_state : "weekly chart @ day D"
    tickers ||--o{ sector_stock_leadlag : "vs own industry"
    subscribers ||--o{ subscriptions : "filters"
    subscribers ||--o{ notification_log : "sends (partitioned)"

    tickers {
        int ticker_id PK
        varchar exchange
        varchar symbol
        char isin
        smallint tier
        bool is_active
    }
    historical_signals {
        bigint signal_id PK
        int ticker_id FK
        date signal_date "UNIQUE with ticker, tf"
        timestamptz bar_timestamp "week anchor"
        varchar signal_type
        smallint regime_before
        smallint regime_after
        float price_at_signal
    }
    stock_indicator_state {
        int ticker_id PK
        varchar timeframe PK
        date date PK
        float med_ema
        float long_ema
        float longest_ema
        float atr
        smallint display_regime
    }
```

Run bookkeeping tables: `compute_runs` (PK `compute_run_id`), `etl_runs`, `backfill_runs` (PK `run_id`), `update_run_in_progress`, `pipeline_phase_status`, `exchange_state`, `fundamentals_run`.

**Optional least-privilege roles** (migration `0002_db_roles`):

```mermaid
flowchart LR
    ETL["trip_etl_user<br/>ETL + cycle"] -- "read/write" --> T[("all tables")]
    ENG["trip_engine_user<br/>service"] -- "read/write<br/>(no TRUNCATE)" --> T
    GUIU["trip_gui_user<br/>console"] -- "SELECT only" --> T
```

Enable them with a `database.roles:` block. See [`docs/operator/db-roles.md`](docs/operator/db-roles.md).

---

## 13. Installation and running

### 13.1 Prerequisites

* Python ≥ 3.12, PostgreSQL ≥ 16 (developed on 18)
* A Brevo account with a verified sender (only needed to send email)
* Linux recommended. The console also needs the usual Qt runtime libraries.

### 13.2 Install

```bash
git clone https://github.com/UBags/WeeklyStockSignals.git
cd WeeklyStockSignals
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[all,test]"          # service + ETL + GUI + migrations + observability + tests
```

| Extra | Pulls in |
|---|---|
| *(core)* | pydantic, SQLAlchemy, psycopg2, FastAPI, uvicorn, PyYAML, redis |
| `etl` | yfinance, pandas, requests, holidays |
| `gui` | PySide6, pyqtgraph |
| `migrations` | alembic |
| `observability` / `tracing` / `tracing-otlp` | prometheus_client / OpenTelemetry |
| `gui-build` | PyInstaller (standalone console binary) |

### 13.3 Configure

```bash
createdb trip_ema                                   # owner: trip_ema_app (see config.yaml → database)
export TRIP_EMA_DB_PASSWORD='…'                     # never stored in config.yaml
export TRIP_EMA_ADMIN_TOKEN="$(openssl rand -hex 32)"
cp secrets/brevo.yaml.example secrets/brevo.yaml && chmod 600 secrets/brevo.yaml   # optional
```

`config.yaml` is the single runtime config: database, the `ema` and `atr` parameters, `market_hours` with per-exchange `tick_schedule`, ETL concurrency and tier sizes, HTTP, and observability. [`docs/operator/configuration.md`](docs/operator/configuration.md) has the reference.

### 13.4 Run

```bash
# 1. Service: applies migrations, primes the cache, starts scheduler + dispatcher + API
trip-ema --config config.yaml
#    or, detached with log at /tmp/trip-ema.log and a /healthz wait:
./scripts/restart_service.sh

# 2. First-time data load
trip-ema-etl refresh-listings --exchange NSE
trip-ema-etl backfill        --exchange NSE --date_from 2016-01-01

# 3. Ad-hoc cycle (what the scheduler does on every tick)
trip-ema-etl cycle --exchange NSE --kind eod

# 4. Console
trip-ema-console --config config.yaml --base-url http://localhost:8000
```

`scripts/` holds the operational toolbox: `health_check.sh`, `db_status.sh`, `smoke_test_fast.sh`, `track_backfill_progress.sh`, `preflight_coldstart.sh` and others. See [`scripts/operator_commands.md`](scripts/operator_commands.md).

---

## 14. Backfill

`trip-ema-etl backfill` rebuilds the whole derived model for a date range. A partial unique index makes it exclusive: only one active `backfill_runs` row can exist, and the live cycle defers while a backfill runs.

```mermaid
flowchart TB
    S0["acquire backfill lock<br/>(backfill_runs)"] --> S1["1 · fetch bars<br/>(unless --no_fetch_bars)"]
    S1 --> S1b["1b · OHLCV seam correction"]
    S1b --> S2["2–3 · stock state + enrichment<br/>(full mode, per ticker)"]
    S2 --> S4["4–6 · industry index values,<br/>state, enrichment (all industries)"]
    S4 --> S7["7 · delete historical_signals<br/>≥ Monday of date_from"]
    S7 --> S8["8 · day-walk: derive and insert<br/>historical_signals per day"]
    S8 --> S9["9 · rebuild current_signals<br/>(final day only)"]
    S9 --> S10["10 · release lock (finally)"]
```

Scope it with `--stocks SYM1,SYM2`, force a full recompute with `--rebuild`, and clear a stale lock with `--release-lock`.

---

## 15. Testing and CI

```bash
# DB-free suites: fast, no Postgres
pytest tests/indicators tests/domain tests/regime tests/cache tests/config \
       tests/notifications tests/scheduler_pure tests/transports_pure tests/parity tests/etl

# Qt suites: separate invocation (pytest-qt teardown race)
QT_QPA_PLATFORM=offscreen pytest tests/gui

# Postgres-backed suites (pytest-postgresql; pg_ctl path in pyproject.toml)
pytest tests/persistence tests/orchestrator tests/fundamentals tests/backfill tests/api
```

* **Pine parity harness.** `tests/parity/` auto-discovers CSV fixtures and asserts bar-by-bar agreement at an absolute tolerance of `1e-9`. Drop a TradingView data-window export into `tests/parity/fixtures/` to anchor against real Pine output.
* **Architecture tests.** `tests/test_no_dead_island.py` and `tests/architecture/` guard against unreachable modules and layer violations.
* **GitHub Actions.** `.github/workflows/` has `tests.yml` (DB-free, Postgres integration, ruff), `release.yml` (wheel + PyInstaller console build), `docs.yml` (MkDocs) and `install-smoke.yml`.

---

## 16. Repository layout

```
trip_ema/
├── app.py                 service entry point (trip-ema)
├── indicators/            VW triple-EMA, ATR, bands, weekly aggregation (pure)
├── regime/                canonical regime classifier
├── domain/                frozen dataclasses, DetermineSignal, market sessions, OHLCV corrector
├── fundamentals/          state/enrichment populators, industry indices, lead/lag,
│                          signal enrichment, ensemble, forward-walk labels, benchmark stats
├── orchestrator/          recompute_market, process_day, intraday chain, notify_subscribers
├── backfill/              10-stage backfill pipeline, bulk day-walk, OHLCV correction stage
├── etl/                   trip-ema-etl CLI, NSE listings, yfinance source, nselib actions,
│                          tiering, parking, reconciliation
├── scheduler/             tick planner, trading calendar, RunScheduler, RunQueue, RunDispatcher
├── persistence/           SQLAlchemy Core repositories, Alembic bridge, connections
├── cache/                 CurrentSignalsCache (in-memory read model)
├── notifications/         email composer, Brevo transport, lead/lag rendering, (legacy) queue/DLQ
├── transports/http/       FastAPI app, routes, schemas, admin-token dependency
├── gui/                   PySide6 console: main window, views, chart widget, backtest engine
├── observability/         logging, correlation IDs, Prometheus metrics, OpenTelemetry
└── config/                Pydantic config schema + loader, Brevo secrets
alembic_pkg/versions/      0001 … 0056 schema migrations
tests/                     ~245 test modules (pure, Qt, Postgres-backed, parity, architecture)
scripts/                   operator shell tools, probes, README chart renderer
diagnostics/               standalone research and data-quality scripts (strategy backtests, exit optimisation)
docs/                      design specs, operator guides, per-package READMEs (MkDocs site)
data/                      nse_corporate_actions.csv offline snapshot
configs/                   exchange holiday overrides
```

---

## 17. Design invariants

These are the rules the code is written around. Break one only deliberately, and document why.

1. **Wrong data → wrong decisions → lost money.** Correctness beats speed. Censor or refuse rather than guess.
2. **The ETL is the source of truth for bar availability.** The pipeline never interpolates or invents bars.
3. **Pine numerical fidelity is non-negotiable.** EMA α = 2/(n+1), Wilder RMA seeding, WMA weights 1…n, and NaN warm-up all match Pine. If the parity harness doesn't catch a "simplification", the harness is under-specified.
4. **One rule, one place.** Regime: `compute_regime`. Signal: `DetermineSignal`. Day attribution: the market's own trading date (`market_tz(exchange)`), never India's by default.
5. **Daily grain, weekly chart.** Filter by `signal_date` or `date`. `bar_timestamp` is a shared week anchor and must never be used as a day bound.
6. **All timestamps are timezone-aware UTC internally.** Local zones are applied only at the boundary.
7. **Domain types are immutable** (`frozen=True, slots=True`). Mutation means replacement.
8. **Single process, single writer.** No `--workers N`. One cycle at a time. Backfill and live cycles are mutually exclusive.
9. **Process boundaries.** The service schedules and serves, and `trip-ema-etl` subprocesses compute, whether spawned by the dispatcher or by the console. The console's administrative writes go through the admin API, and its preferences live in `QSettings`, never in `config.yaml`.
10. **Observability is additive.** Removing metrics or tracing must never change behaviour.

[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) and [`docs/architecture/invariants.md`](docs/architecture/invariants.md) carry the original long-form rationale. Parts of them predate the V1 pipeline described here (they mention `ComputeScheduler` and `SignalCache`, which have since been removed).

---

## 18. Status and roadmap

**Live:** NSE end-to-end: scheduled intraday and EOD cycles, OHLCV seam correction, industry indices, lead/lag, per-signal enrichment, forward-walk labels, email digests, operator console.

**Configured but not yet ingesting:** BSE and NYSE. Both exchanges appear in `market_hours` and the tick planner handles them, but `refresh-listings` raises `UnsupportedExchangeError` for non-NSE exchanges, and several industry tables lack an exchange column.

**Planned work:**

* Lead/lag analytics phases A–E: `industry_correlation_pairs`, `industry_regime_events`, `ticker_industry_transmission_stats`, then email and GUI surfacing
* A common notifier primitive with per-subscriber filters (signal bitmask, exchanges, industries, stocks, EOD-only, entitlement window)
* An optional NIFTY trend-regime filter
* Multi-exchange schema work (exchange columns on industry tables and subscribers)

---

<sub>Proprietary. © Uddipan Bagchi / CosTheta Technologies, Bengaluru.</sub>
