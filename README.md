# trip_ema

Python port of a TradingView Pine Script indicator (Weekly Triple
EMA + ATR bands + regime classification) with a full backend service
around it: ETL-driven ingest from Postgres, in-memory signal cache,
HTTP API, subscriber notifications via email / webhook / Slack, a
desktop **operator console** (PySide6), and Prometheus + OpenTelemetry
observability.

Built for NSE/BSE-focused weekly signals with reliable alerting.

---

## What it does

Every compute cycle:

1. Reads OHLCV bars from Postgres (the ETL writes; this app never
   does).
2. For each registered ticker, advances the indicator engine with
   any new bars.
3. Detects regime changes (BULLISH / NEUTRAL / BEARISH) using the
   Triple-EMA + ATR bands.
4. Writes a `signal_event` row when a change occurs.
5. Caches the latest state in memory for fast API reads.
6. The `SubscriberDistributor` enqueues notifications for any
   subscribers due for that signal (cadence-based).
7. The `NotifierWorker` pops jobs from Redis and dispatches them
   via the configured transports — with circuit-breaker protection
   around each one.

The indicator math is **faithful to Pine Script** down to the NaN
warmup semantics. The harness in `tests/parity/` exists so that
remains true under refactors.

---

## Quickstart

### Install

```bash
# Core only — engine, API, notifier
pip install trip-ema

# With Prometheus metrics
pip install trip-ema[observability]

# With OpenTelemetry tracing (console exporter)
pip install trip-ema[tracing]

# With the ETL CLI (yfinance + NSE listings ingest)
pip install trip-ema[etl]

# With the desktop operator console (PySide6 + pyqtgraph)
pip install trip-ema[gui]

# With the Alembic-managed migration tooling (Phase 11+; needed
# only if you're authoring new migrations or want auto-stamping
# at startup)
pip install trip-ema[migrations]

# Production: everything
pip install trip-ema[all]

# For running the test suite
pip install trip-ema[test]
```

### Run

```bash
# Apply migrations and start the main service
trip-ema --config config.yaml

# Run the notifier worker (separate process, for queue-mode)
trip-ema-notifier --config config.yaml

# Ingest NSE/BSE data (separate CLI; populates the ohlcv_bars table)
trip-ema-etl refresh-listings --config config.yaml      # ticker universe
trip-ema-etl backfill --config config.yaml --years 10   # bulk history
trip-ema-etl incremental-eod --config config.yaml       # nightly delta

# Open the desktop operator console (read-only without admin token)
TRIP_EMA_ADMIN_TOKEN=... trip-ema-console --config config.yaml
```

The notifier worker is a separate process when you configure a
Redis queue in `config.yaml`. In dev / small deployments without
Redis configured, transports are dispatched in-process by the
`SubscriberDistributor` thread — see "Deployment model" below.

The ETL CLI is a separate package extra; it doesn't run on the same
schedule as the main service and is invoked manually or via cron.
The operator console (`trip-ema-console`) is a desktop GUI for
day-to-day operations — see the "Operator console" section below.

### Minimal config

```yaml
database:
  driver: postgresql
  host: localhost
  port: 5432
  database: market_data
  user: app
  password_env: TRIP_EMA_DB_PWD     # never put the password in the file

ema:
  med:     { base: 8,  s1: 4, s2: 4, final: null }
  long:    { base: 15, s1: 4, s2: 4, final: 3 }
  longest: { base: 28, s1: 7, s2: 5, final: 3 }

atr:
  length: 14
  inner_mult: 0.2
  mid_mult: 2.0
  outer_mult: 4.0

market_hours:
  - exchange: NSE
    open: "09:15"
    close: "15:30"
    tz: Asia/Kolkata
    weekdays: [1, 2, 3, 4, 5]
```

Production additions:

```yaml
observability:
  metrics_port: 9090          # Prometheus /metrics on a separate port
  log_json: true              # JSON log lines for Loki / ELK
  tracing:
    enabled: true             # OpenTelemetry
    exporter: otlp
    otlp_endpoint: "http://otel-collector:4317"

http:
  notifier_subprocess_shutdown_timeout_seconds: 15

transports:
  circuit_breaker:
    enabled: true             # Default; failure_threshold=5, reset=30s
```

See `docs/ARCHITECTURE.md` §8 for the full operational reference.

---

## Deployment model

**Single-writer, single-process.** Do NOT run uvicorn with
`--workers N` — it would produce N copies of every engine, N
schedulers racing on the DB, and N caches that don't agree. The
correctness invariants are written for one process holding the
state.

If you eventually need horizontal scale, that's a real
architectural change (leader election or ticker sharding), not
naive process multiplication. See ARCHITECTURE.md invariant 3.2.

---

## API surface

The HTTP API lives on `http.port` (default 8000); metrics live on
a separate port (`observability.metrics_port`).

| Endpoint | Purpose |
|----------|---------|
| `GET /healthz` | Liveness — always 200 if the process is up |
| `GET /readyz` | Readiness — includes `stale_tickers_count` informationally |
| `GET /v1/signals/{exchange}/{symbol}` | Latest signal for a ticker |
| `GET /v1/signals/{exchange}/{symbol}/history` | Bar-by-bar indicator history for the chart view |
| `POST /v1/compute` | Trigger an out-of-band compute pass |
| `POST /v1/admin/prime-all` | Re-prime all tickers from DB (admin) |
| `POST /v1/admin/compute-now` | Force `ComputeScheduler.run_once` (admin) |
| `GET /v1/admin/stale-tickers` | List tickers flagged stale due to ETL gaps (admin) |
| `POST /v1/admin/reprime-ticker/{exchange}/{symbol}` | Reprime one ticker, refreshing tz/source_timeframe from DB (admin) |
| `GET /v1/admin/circuit-breakers` | Current transport-breaker state per transport (admin) |
| `GET /v1/admin/etl/status` | ETL run history + tier counts (admin) |
| `GET /v1/admin/etl/parked` | Currently parked tickers + failure reasons (admin) |
| `POST /v1/admin/etl/run/{kind}` | Trigger an ETL run (refresh-listings / backfill / eod) (admin) |
| `GET /v1/admin/subscribers` | List all subscribers + their filter sets (admin) |
| `POST /v1/admin/subscribers` | Create a subscriber (admin) |
| `PATCH /v1/admin/subscribers/{id}` | Update subscriber fields (admin) |
| `DELETE /v1/admin/subscribers/{id}` | Delete subscriber (cascades to filters + log) (admin) |
| `PUT /v1/admin/subscribers/{id}/filters` | Replace subscriber's filter set (admin) |
| `POST /v1/admin/subscribers/{id}/test-notification` | One-shot test send via the configured transport (admin) |
| `GET /v1/admin/notifications/recent` | Recent notification_log entries, joined for display (admin) |
| `GET /v1/admin/notifications/dlq` | List notifier dead-letter queue entries (admin, Phase 12) |
| `POST /v1/admin/notifications/dlq/{job_id}/replay` | Move a DLQ entry back to the main queue (admin, Phase 12) |
| `DELETE /v1/admin/notifications/dlq/{job_id}` | Permanently delete a DLQ entry (admin, Phase 12) |
| `GET /v1/admin/health-snapshot` | 8-card system-health snapshot for the GUI dashboard (admin) |

Admin endpoints require a bearer token. Set
`TRIP_EMA_ADMIN_TOKEN=...` in the environment (or override the env
var name via `http.admin_token_env`); endpoints return 503 if the
env var isn't set, 401 without a header, 403 with a wrong token.

---

## Operator console

`trip-ema-console` is a PySide6 desktop application for day-to-day
operations. It connects to the running `trip-ema` service via HTTP
and to Postgres directly (read-only DB queries for table views).
The console mirrors the API's read endpoints in tabbed views and
funnels write operations through the admin endpoints above.

Seven tabs in the sidebar:

| Tab | Purpose |
|-----|---------|
| ETL Status | Recent runs, parked tickers, tier counts. Trigger ETL runs from the GUI (admin). |
| Tickers | Searchable table of all tickers with signal history per row. Export to CSV. |
| Signals | Chart view (pyqtgraph) — candles + EMA lines + ATR bands + regime fill + BUY/SELL markers + volume sub-panel. Crosshair OHLC readout. Ticker search-as-you-type + time-range buttons (1Y/3Y/5Y/10Y/All) + layer toggles. |
| Subscribers | Full CRUD over the `subscribers` table — transport (email/webhook/slack), address, cadence, filter editor (per-ticker / per-signal). Test-notification button. Export to CSV. |
| Notifications | Recent `notification_log` entries with signal color-coding, subscriber + hours filters. Export to CSV. |
| System Health | 8 status cards polled every 10s — ticker universe, ETL freshness, compute scheduler, signal cache, circuit breakers, notification queue, subscribers, recent events. Color-coded by status. |
| Settings | GUI preferences (refresh cadences, default chart range) persisted to `QSettings`. Read-only viewer for the loaded `config.yaml`. Backup-config action writes a timestamped copy. |

Plus Help → About in the menu bar (version, runtime info).

The console runs in **read-only mode** when `TRIP_EMA_ADMIN_TOKEN`
is not set in the environment — all read panels work, all write
buttons are disabled with explanatory tooltips. This is the
recommended posture for analyst seats; ops/admin seats set the
token.

Cross-tab nav: selecting a ticker in the Tickers tab and clicking
"View chart" opens the Signals tab pre-loaded with that ticker.

---

## Where to look for what

The detailed map is in `docs/ARCHITECTURE.md` §7. A few high-traffic
pointers:

| Question | File(s) |
|----------|---------|
| How does an EMA actually compute? | `trip_ema/indicators/vw_ema.py` |
| Where does Pine's NaN warmup live? | `trip_ema/indicators/vw_ema.py::update`, `trip_ema/indicators/atr.py::update` |
| How does the engine handle a re-applied provisional? | `trip_ema/engine/ticker_engine.py::ingest_provisional` |
| Where is gap detection? | `trip_ema/scheduler/compute_scheduler.py::_check_for_data_gap`, `trip_ema/persistence/bars_repo.py::weekly_buckets_with_bars` |
| How do timestamps cross zones? | `trip_ema/indicators/aggregation.py::_monday_of`, `bucket_end` |
| Where does a notification's lifecycle live? | `trip_ema/scheduler/subscriber_distributor.py` (enqueue), `trip_ema/notifier_worker.py` (dispatch) |
| Where are correlation IDs minted and propagated? | `trip_ema/observability/correlation.py` |
| Where are Prometheus metrics defined? | `trip_ema/observability/metrics.py` |
| How do circuit breakers wrap a transport? | `trip_ema/notifications/circuit_breaker.py` |
| Where does the ETL fetch bars from? | `trip_ema/etl/yfinance_fetcher.py`, `trip_ema/etl/cli.py` (the `trip-ema-etl` entry point) |
| Where does the operator console live? | `trip_ema/gui/` — `console.py` is the entry point, `main_window.py` is the sidebar+stack shell, individual tabs are in `gui/views/` |
| How does the chart replay the engine server-side? | `trip_ema/transports/http/routes/signals.py::get_signal_history` — primes a fresh `TickerEngine` over the requested window and returns the bar-by-bar state |
| How are admin write actions gated? | `trip_ema/transports/http/dependencies.py::require_admin_token` — bearer token whose env-var name is in `http.admin_token_env` |
| Where does the notifier DLQ live? | `trip_ema/notifications/redis_queue.py` (the DLQ data structure + Redis HASH attempts counter), `trip_ema/notifier_worker.py::_handle_job` (the threshold check), `trip_ema/transports/http/routes/admin_notifications.py` (the three admin endpoints) |
| Where are the DB roles defined? | `alembic_pkg/versions/0002_db_roles.py` (the CREATE ROLE + GRANT migration), `trip_ema/config/schema.py::DbRolesConfig` (the config), `trip_ema/persistence/connection.py::create_engine_for_role` (the role-aware engine builder) |

---

## Running the tests

```bash
# DB-free tests — fast, no Postgres needed
pytest tests/indicators tests/engine tests/cache tests/notifications \
       tests/scheduler_pure tests/notifier_pure tests/config \
       tests/transports_pure tests/parity tests/observability \
       tests/etl tests/gui_pure

# Qt-using tests — separate invocation to avoid a pytest-qt
# teardown race (see "Continuous integration" below)
QT_QPA_PLATFORM=offscreen pytest tests/gui

# Postgres-backed integration tests — needs pytest-postgresql
pytest tests/persistence tests/scheduler tests/registry \
       tests/transports tests/notifier tests/api
```

The DB-free suite is ~588 tests (465 non-Qt + 123 Qt) and runs in
~15s wall time. The Postgres-backed slices are heavier and meant for
CI / pre-release.

The GUI tests use `pytest-qt` with the offscreen Qt platform; they
run headless in CI. `tests/gui_pure/` is for tests that import GUI
modules but don't need a Qt event loop (e.g. the HTTP client tests).

`tests/gui/conftest.py` (Phase 12) implements the deferred-deletion
pattern from Gemini's pytest-qt PDF: a `managed_widget` factory
fixture plus an autouse `_drain_after_every_gui_test` that pumps
Qt's event loop with `processEvents()` + `sendPostedEvents()`
between tests. Standalone `tests/gui` reliability improved from
~3/5 to ~4-5/5 clean exits as a result. The race isn't fully
eliminated (some teardown happens inside pytest's atexit handlers,
after fixtures can intervene), so the CI workflow's two-invocation
pattern + output-parsing wrapper remain belt-and-braces. The
conftest docstring documents this honestly.

### Continuous integration

GitHub Actions workflows live under `.github/workflows/`:

- **`tests.yml`** — three jobs:
  - `db-free`: runs the full ~588-test suite on every push and PR,
    split into two pytest invocations to avoid the Qt teardown race.
    The Qt-using step checks pass/fail via output parsing rather
    than trusting the process exit code, so a benign teardown
    segfault doesn't fail the build. Real test failures still do.
  - `postgres-integration`: provisions a `postgres:16` service
    container and runs the Postgres-backed slices. Gated to main
    pushes + manual dispatch + same-repo PRs.
  - `lint`: `ruff check`, advisory only.
- **`release.yml`** — tag-triggered. Builds sdist + wheel via
  hatchling, verifies the wheel's `__version__` matches the tag,
  publishes to PyPI via trusted publishing. Phase 12 added a
  `build-gui-installer` job that uses PyInstaller (`gui_installer.spec`
  at repo root) to build a standalone Linux x86_64 operator-console
  binary; the binary is packaged as
  `trip-ema-console-vX.Y.Z-linux-x86_64.tar.gz` and uploaded as a
  release artifact. Windows + macOS builds are deferred.
- **`docs.yml`** — Phase 12. Builds the MkDocs Material site (see
  `mkdocs.yml` at repo root) and publishes to GitHub Pages on push
  to main. Paths-filtered so it only runs when docs change.

### Schema migrations

Phase 12 made Alembic the exclusive migration system. Migrations
live in `alembic_pkg/versions/`; state is tracked in
`alembic_version`. The squashed initial revision
(`0001_initial_schema`) reproduces the schema that the historical
SQL migrators (001-010) produced. The legacy SQL files are
archived in `migrations/legacy/` for human reference only — they
are no longer applied to fresh databases.

For pre-Phase-12 databases that ran the legacy migrator through
version 010, `trip_ema.persistence.alembic_bridge.stamp_baseline_if_needed()`
detects the populated `schema_migrations` table on first startup
and stamps `0001_initial_schema` **without** re-running DDL.
Subsequent `alembic upgrade head` then applies any post-squash
revisions (currently `0002_db_roles`). No manual operator action
required.

To author a new migration:

```bash
pip install trip-ema[migrations]
alembic revision -m "describe what this migration does"
# edit alembic_pkg/versions/<rev>_<slug>.py — use the op API
alembic upgrade head
```

Failure to apply migrations at startup is now **fatal** (was a
logged warning during the Phase 11 transitional period). Operators
who want to run a service without the `[migrations]` extra need
to apply migrations out-of-band; the service will refuse to start
if `alembic upgrade head` can't run.

The full migration story (including the parity test that asserts
the legacy SQL and the squashed Alembic revision produce the same
schema) is in `PHASE12_CHANGELOG.md` and the comments in
`alembic.ini`. The legacy archive in `migrations/legacy/` has its
own README.

### Notifier dead-letter queue (DLQ)

Phase 12. When a notification job fails to deliver `max_attempts`
times (default 5), the worker moves it to a Redis-backed DLQ
instead of cycling it back into the queue. The attempt counter
lives in a persistent Redis HASH keyed by job_id — it survives
the distributor recreating the payload on each cycle.

Admin endpoints:
- `GET /v1/admin/notifications/dlq?limit=` — list entries
- `POST /v1/admin/notifications/dlq/{job_id}/replay` — requeue
- `DELETE /v1/admin/notifications/dlq/{job_id}` — permanent delete

All require the admin bearer token. Tuning: `notifier.max_attempts`
in config.yaml; metric label `outcome="dlq"` on
`trip_ema_notifier_jobs_processed_total`. Full operator workflow
in `docs/operator/dlq.md`.

### DB role separation (optional)

Phase 12. Alembic migration `0002_db_roles.py` creates three
Postgres roles (`trip_etl_user`, `trip_engine_user`,
`trip_gui_user`) with per-table GRANTs that enforce invariant 3.14
at the database level rather than only in Python code.

Adoption is opt-in via a `database.roles:` block in config.yaml.
Without it, all processes use the top-level user/password —
preserving existing deployments. The roles are created with
`NOLOGIN` and no password; operators grant `LOGIN` and set
passwords as a deployment step. Migrations themselves continue to
run as the top-level user (which has full schema privileges); the
per-role users come into play for steady-state operation. See
`docs/ARCHITECTURE.md` §8.6 and `docs/operator/db-roles.md` for
the workflow.

### The Pine parity harness

`tests/parity/` auto-discovers `*.csv` fixtures and asserts the
Python output matches expected indicator values bar-by-bar with
`1e-9` absolute tolerance. The committed fixture is a self-pinned
synthetic dataset that catches Python-side drift.

To upgrade to true Pine-parity: export
`(timestamp, OHLCV, med_ema, long_ema, longest_ema, atr)` from
TradingView's data window for a representative ticker/timeframe,
save as `tradingview_<symbol>_<tf>.csv` in `tests/parity/fixtures/`,
re-run. Any drift surfaces immediately.

Full workflow in ARCHITECTURE.md §8.5.

---

## Design contract

`docs/ARCHITECTURE.md` is the rule-book. The 14 invariants encode
hard-won lessons; if a change appears to violate one, either rethink
or update the doc along with the code. Two of the most important:

- **Invariant 3.1 — The ETL is the source of truth for bar
  availability.** The application *consumes* what's in Postgres; it
  does not infer, generate, or interpolate missing bars.
- **Invariant 3.3 — Pine Script numerical fidelity is
  non-negotiable.** If a refactor proposes "simplifying" the
  indicator math, the parity harness should fail. If it doesn't,
  the test is under-specified, not the code.

---

## License

Proprietary. See LICENSE if present in the source tree.

---

## Status

Production-ready for its intended use case (NSE/BSE-focused weekly
signals). Maturity has been hardened across multiple review rounds
(Gemini + Grok) documented in the long-form conversation thread; the
review outcomes are encoded as invariants in ARCHITECTURE.md.

Phase history:
- **Phase 1-8** — Pine port, engine, cache, scheduler, distributor
- **Phase 9** — HTTP + notifier transports, configuration schema
- **Phase 10** — Observability hardening (Prometheus, OpenTelemetry,
  circuit breakers), single-writer invariant document
- **Phase 11** — ETL backend (NSE/BSE listings + bars), desktop
  operator console (PySide6), expanded admin API. Delivered in four
  slices; see `PHASE11_CHANGELOG.md`
- **Post-Phase-11 (Tier 2)** — version bump to 0.11.0, GitHub
  Actions CI workflows, Alembic migration tooling alongside the
  legacy SQL migrator, end-to-end integration test, invariant 3.14
  in ARCHITECTURE.md codifying Engine/ETL/GUI process boundaries.
  See the "Post-Phase-11 follow-ups (Tier 2)" section of
  `PHASE11_CHANGELOG.md`.
- **Phase 12 (Tier-3 review)** — six items shipped in response to
  Gemini's "Deep Dive: Technical Debt and Hardening" review:
  pytest-qt teardown race fix (deferred-deletion pattern in the
  GUI conftest), Alembic exclusivity (legacy SQL archived, schema
  squashed to a single revision), DB role separation (three
  least-privilege Postgres roles, opt-in), DLQ for the notifier
  worker, PyInstaller GUI installer (Linux x86_64), MkDocs Material
  documentation site. Full detail in `PHASE12_CHANGELOG.md`.

Current version: **0.12.0** (also visible via
`python -c "from trip_ema import __version__; print(__version__)"`).
