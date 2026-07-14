# `tests/` — test suite

## 1. Role in the larger application

`tests/` is the full test suite for `trip-ema`. **2,534 tests
passing / 14 skipped** as of post-Step-15b. Every production
module in `trip_ema/` has corresponding test coverage; cross-module
integration scenarios get their own files.

Three layers of testing:

1. **Pure / unit tests.** No DB, no network, no Qt. Run in
   ~15 seconds. Cover the bulk of production code logic.
2. **Postgres integration tests.** Real DB via the
   `postgres_engine` fixture. Cover repositories, runners,
   end-to-end V1 cycle.
3. **GUI tests via pytest-qt.** Headless via
   `QT_QPA_PLATFORM=offscreen`. Separate pytest invocation to
   avoid teardown race conditions.

```mermaid
flowchart TB
    push[Push / PR] --> fast[Fast suite ~588 tests ~15s]
    fast --> done1{All green?}

    main[Push to main] --> full[Full suite]
    manual[Manual dispatch] --> full
    same[Same-repo PR] --> full
    full --> pg[postgres:16 service container]
    full --> qt[QT_QPA_PLATFORM=offscreen]
    full --> done2{All green?}
```

## 2. Architecture and constraints

| Rule | Why |
|------|-----|
| **Test tree mirrors `trip_ema/` package structure** | Easy to find tests for a module by path correspondence |
| **`_pure` suffix split** | DB-free tests (`scheduler_pure`, `notifier_pure`, `transports_pure`, `gui_pure`) for fast CI feedback; DB-required siblings (`scheduler`, `notifier`, `transports`) for full coverage |
| **One test file per production file** | When you refactor `domain/state.py`, the matching `tests/domain/test_state.py` is where you go |
| **Plus integration files for cross-cutting scenarios** | `tests/etl/test_signal_generator*.py` covers Phase 15 architectural patterns across files |
| **`postgres_engine` fixture clean per-test** | TRUNCATE all tables + reset sequences in `setup` and `teardown`. No fixture state leaks |
| **GUI tests in separate pytest invocation** | pytest-qt has teardown race conditions; isolating Qt avoids cascade failures |
| **No mocks for compute primitives** | Indicator math is tested with real numbers; parity harness compares against TradingView CSV exports |
| **Mocks for external services** | yfinance, Brevo HTTP. Recorded responses in `tests/fixtures/` |
| **Pre-existing failures documented as non-regressions** | 51 fakeredis `evalsha` failures (Lua scripting limitation), 1 intermittent GUI teardown segfault. Build doesn't fail on these |

## 3. File inventory

```
tests/
├── __init__.py
├── README.md
├── conftest.py                       Shared fixtures (postgres_engine, time, ...)
├── test_no_dead_island.py            Import-graph invariant
│
├── api/                              trip_ema/api/ (DB)
├── app/                              trip_ema/app.py wiring (no DB)
├── backfill/                         trip_ema/backfill/ (DB)
├── cache/                            trip_ema/cache/ (no DB)
├── config/                           trip_ema/config/ (no DB)
├── domain/                           trip_ema/domain/ (no DB)
├── engine/                           trip_ema/engine/ (no DB)
├── etl/                              trip_ema/etl/ (mixed)
├── fundamentals/                     trip_ema/fundamentals/ (DB)
│
├── gui/                              trip_ema/gui/ (Qt + offscreen)
│   ├── conftest.py                   managed_widget + drain_pending_deletions
│   └── test_*.py
│
├── gui_pure/                         GUI-adjacent code; no Qt
│
├── indicators/                       trip_ema/indicators/ (no DB)
├── notifications/                    trip_ema/notifications/ (no DB)
├── notifier/                         trip_ema/notifier_worker.py (DB)
├── notifier_pure/                    worker logic without DB
├── observability/                    trip_ema/observability/ (no DB)
├── orchestrator/                     trip_ema/orchestrator/ (DB)
│
├── parity/                           Pine numerical parity harness
│   ├── README.md
│   ├── fixtures/                     TradingView CSV exports
│   └── test_pine_parity.py
│
├── persistence/                      trip_ema/persistence/ (DB)
│   ├── conftest.py                   postgres_engine + clean-schema reset
│   └── test_*.py
│
├── registry/                         trip_ema/registry/ (no DB)
├── regime/                           trip_ema/regime/ (no DB)
├── scheduler/                        trip_ema/scheduler/ (DB; orchestrator)
├── scheduler_pure/                   scheduler logic without DB
├── signals/                          trip_ema/signals/ (DB)
├── snapshots/                        trip_ema/snapshots/ (DB)
├── transports/                       trip_ema/transports/http/ (DB)
└── transports_pure/                  HTTP routes against mocked deps
```

## 4. The `_pure` suffix convention

When a module has both DB-required and pure-Python tests, the
latter live in a sibling directory named `<module>_pure/`. The
split exists because:

1. **DB-free suite runs on every push and PR.** ~588 tests,
   ~15 seconds wall time.
2. **DB-required suite runs only on main + manual + same-repo
   PRs.** Provisions a `postgres:16` service container.

Faster feedback loop without sacrificing integration coverage.

**Examples:**

| Pure tree | DB tree | Splits |
|-----------|---------|--------|
| `scheduler_pure/` | `scheduler/` | Pure: tick-planner math, calendar truth table. DB: MarketOrchestrator full cycle |
| `notifier_pure/` | `notifier/` | Pure: worker class logic. DB: full Redis + DB integration |
| `transports_pure/` | `transports/` | Pure: HTTP routes via mocked dependencies. DB: real DB integration |
| `gui_pure/` | `gui/` | Pure: helpers + HTTP client. Qt: full view wiring |

## 5. Top-level test files

### 5.1 `test_no_dead_island.py`

The import-graph invariant. Walks every `.py` file under
`trip_ema/`, parses imports, builds a directed graph, asserts:

- **No cycles.** A imports B, B imports A is forbidden.
- **No layer violations.** `domain/` doesn't import from
  `transports/`; `persistence/` doesn't import from `orchestrator/`.
- **No dead islands.** No module nothing imports from
  (intentional entry points like `app.py` and `etl/cli.py` are
  whitelisted).

Run via `scripts/audit_dead_island.sh`. Catches accidental
circular imports introduced during refactors.

### 5.2 `conftest.py` (root)

Shared fixtures available to every test:

- `postgres_engine` — see §6.
- `config` — minimal `Config` object for tests that need it.
- `frozen_time(when)` — `freezegun` context manager for
  time-sensitive tests.

## 6. The `postgres_engine` fixture

The single most important integration-test piece. Lives in
`tests/persistence/conftest.py`:

```python
@pytest.fixture(scope="session")
def postgres_engine_session():
    """One engine per pytest session; reused across tests."""
    url = os.environ.get("TRIP_EMA_TEST_DB_URL")
    if not url:
        pytest.skip("TRIP_EMA_TEST_DB_URL not set; skipping DB tests")
    engine = create_engine(url, pool_size=2, pool_recycle=300)
    # Run migrations once per session
    run_alembic_upgrade_head_via_test(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def postgres_engine(postgres_engine_session):
    """Per-test fixture: clean schema via TRUNCATE + sequence reset."""
    engine = postgres_engine_session
    with engine.begin() as conn:
        conn.execute(text("""
            TRUNCATE
                tickers, ohlcv_bars, signal_events_legacy,
                stock_indicator_state, current_signals, ...
            RESTART IDENTITY CASCADE
        """))
    yield engine
```

**Per-test clean schema** via TRUNCATE + RESTART IDENTITY. No
fixture state leaks between tests.

**Session-scoped engine** so connection pool setup cost amortises
across the whole DB suite (typically 200+ tests).

### 6.1 Running DB tests

```bash
export TRIP_EMA_TEST_DB_URL=postgresql://test:test@localhost:5432/trip_ema_test
pytest tests/persistence
```

CI provisions Postgres via `services:` in the workflow:

```yaml
services:
  postgres:
    image: postgres:16
    env:
      POSTGRES_DB: trip_ema_test
      POSTGRES_USER: test
      POSTGRES_PASSWORD: test
```

## 7. The two-invocation Qt pattern

`tests/gui/` uses pytest-qt with the offscreen platform:

```bash
QT_QPA_PLATFORM=offscreen pytest tests/gui -v
```

CI splits Qt vs non-Qt into **two separate pytest invocations**.
Reasons:

1. **pytest-qt teardown race.** Qt's C++ destructor order
   sometimes segfaults at process exit. A successful test run can
   still cause a non-zero process exit. CI parses output rather
   than trusting the exit code.

2. **Slot signal-emission lifecycle.** Mixing Qt and non-Qt tests
   in one process means QApplication state from a previous Qt test
   leaks into a non-Qt test that constructs a widget.

`tests/gui/conftest.py` introduces the deferred-deletion pattern:

```python
@pytest.fixture
def managed_widget():
    """Factory for QWidgets that get deleteLater()'d at test teardown."""
    created = []
    def factory(cls, *args, **kw):
        w = cls(*args, **kw)
        created.append(w)
        return w
    yield factory
    for w in created:
        w.deleteLater()
    QApplication.processEvents()


@pytest.fixture(autouse=True)
def _drain_after_every_gui_test(qtbot):
    """Drain pending deletions before next test runs."""
    yield
    drain_pending_deletions(timeout_ms=500)
```

Combined: standalone clean-exit rate went from ~3/5 to ~4-5/5.

## 8. The parity harness (`tests/parity/`)

Pine numerical parity. The harness loads TradingView CSV exports
for specific tickers + timeframes (committed under
`tests/parity/fixtures/`), pushes the bars through the Python
indicator pipeline, and asserts bar-by-bar equivalence with the
exported indicator values:

| Column | TradingView export | Python result | Comparison |
|--------|---------------------|---------------|------------|
| `med_ema` | TV ribbon EMA #1 | `IndicatorSeries.emas.med` | ≤ 1e-4 absolute |
| `long_ema` | TV ribbon EMA #2 | `IndicatorSeries.emas.long` | ≤ 1e-4 |
| `longest_ema` | TV ribbon EMA #3 | `IndicatorSeries.emas.longest` | ≤ 1e-4 |
| `atr` | TV ATR | `IndicatorSeries.atr` | ≤ 1e-4 |
| `regime` | TV regime state | `compute_regime(...)` | exact match |

Any drift in `trip_ema/indicators/` or `trip_ema/regime/` shows up
here first.

The fixtures are checked into git so the test is reproducible.
Refreshing them is a manual operator workflow when Pine is
upgraded.

## 9. Per-package test inventory

### 9.1 `tests/domain/` (no DB)

- `test_change_signal.py` — enum truth + signal_type derivation
- `test_regime.py` — Regime / CoreRegime alias semantics
- `test_signal_event.py` — frozen invariants, repr
- `test_state.py` — BarState, IndicatorParams, PositionState
- `test_ticker.py` — TickerRef parsing, equality

### 9.2 `tests/regime/` (no DB)

- `test_compute_regime.py` — full 5-rule truth table including
  sticky-on-NaN, regime persistence across bars

### 9.3 `tests/indicators/` (no DB)

- `test_aggregation.py` — `_monday_of`, `bucket_end`,
  `daily_to_weekly` with various tz scenarios
- `test_aggregation_dst.py` — DST edge cases for NYSE
- `test_atr.py` — Wilder warmup vs steady state
- `test_vw_ema.py` — VW-EMA pipeline; Pine-faithful seed behaviour
- `test_bands.py` — boundary equality + NaN propagation
- `test_gap_detection.py` — `detect_weekly_gaps` truth table

### 9.4 `tests/persistence/` (DB)

20+ test files, one per repository:

- `test_bars_repo.py`
- `test_tickers_repo.py`
- `test_current_signals_repo.py`
- `test_historical_signals_repo.py`
- `test_stock_indicator_state_repo.py`
- `test_compute_runs_repo.py`
- `test_backfill_runs_repo.py`  — incl. concurrent-active enforcement
- `test_alembic_bridge.py` — all three state transitions
- `test_alembic_full_chain.py` — upgrade head + downgrade base
- `test_alembic_each_revision_reversible.py`
- `test_tables_match_migrations.py`

### 9.5 `tests/orchestrator/` (DB)

- `test_derive_signals.py` — full truth table
- `test_rotate_signals.py` — atomicity, idempotency
- `test_recompute_market.py` — all 8 stages happy path, halt-on-error
- `test_notify_subscribers.py` — early exits, per-subscriber isolation

### 9.6 `tests/scheduler/` (DB) + `tests/scheduler_pure/` (no DB)

Pure:
- `test_wall_clock.py` — tick math truth table
- `test_market_calendar.py` — decision order
- `test_market_hours.py`
- `test_tick_planner.py`

DB:
- `test_market_orchestrator.py` — run_once happy + error paths
- `test_orchestrator_pool.py`
- `test_compute_scheduler.py` (legacy)

### 9.7 `tests/backfill/` (DB)

- `test_pipeline_happy_path.py`
- `test_pipeline_per_ticker_isolation.py`
- `test_pipeline_marker_lifecycle.py`
- `test_pipeline_idempotency.py`
- `test_pipeline_failed_stage.py`

### 9.8 `tests/notifications/` (no DB)

- `test_compose_email.py`
- `test_send_email.py` — Brevo JSON shape, error result extraction
- `test_enrichment.py`
- `test_benchmark_rerank.py`
- `test_benchmark_stats_compute.py` — against numpy.polyfit
- `test_batcher_html.py`
- `test_circuit_breaker.py`
- `test_redis_queue.py` — fakeredis

### 9.9 `tests/fundamentals/` (DB)

- `test_industry_fetch.py` — yfinance error taxonomy
- `test_market_cap_compute.py`
- `test_index_compute.py`
- `test_indicator_primitives.py`
- `test_v1_runners.py`
- `test_runners.py`
- `test_eod_pipeline.py`
- `test_signal_generator.py` — 4 files, 42 tests for Phase 15a

### 9.10 `tests/etl/` (mixed)

- `test_yfinance_source.py`
- `test_reconciler.py`
- `test_listings_sync.py`
- `test_holidays_calendar.py`
- `test_status_repo.py`
- `test_tiering.py`
- `test_parked.py`
- `test_exchanges.py`
- `test_runner_eod.py`

### 9.11 `tests/transports/` (DB) + `tests/transports_pure/` (no DB)

Pure:
- `test_admin_signals.py` — 28 tests for Phase 15b
- `test_admin_etl.py`
- `test_admin_subscribers.py`

DB:
- `test_app.py` — create_app + lifespan
- `test_health.py`
- `test_signals.py`
- `test_compute.py`
- `test_admin_etl.py` — async-action pattern
- `test_admin_signals.py` — advisory-lock concurrent
- `test_pulse.py` — SSE subscribe + snapshot
- `test_auth.py`

### 9.12 `tests/gui/` (Qt) + `tests/gui_pure/` (no Qt)

Pure:
- `test_http_client.py`
- `test_signals_helpers.py`
- `test_csv_export.py`
- `test_format_mcap.py`

Qt:
- `test_signals.py`
- `test_tickers.py`
- `test_etl_status.py`
- `test_subscribers.py`
- `test_notifications.py`
- `test_chart_widget.py`
- `test_industry_panel.py`
- `test_workers.py`

### 9.13 `tests/observability/` (no DB)

- `test_correlation.py` — contextvar across threading
- `test_logging_config.py`
- `test_metrics.py` — no-op shim path
- `test_metrics_server.py` — three soft-fail paths
- `test_tracing.py`

### 9.14 `tests/cache/` (no DB)

- `test_signal_cache.py`
- `test_current_signals_cache.py`

### 9.15 `tests/parity/` (no DB)

- `test_pine_parity.py` — Pine reference comparison

## 10. Running the suite

### 10.1 Fast (DB-free)

```bash
pytest tests/indicators tests/engine tests/cache tests/notifications \
       tests/scheduler_pure tests/notifier_pure tests/config \
       tests/transports_pure tests/parity tests/observability \
       tests/etl tests/gui_pure tests/domain tests/regime \
       tests/notifier_pure tests/snapshots
```

~588 tests, ~15 seconds on the NUC.

### 10.2 GUI (Qt, separate invocation)

```bash
QT_QPA_PLATFORM=offscreen pytest tests/gui
```

### 10.3 DB integration

```bash
export TRIP_EMA_TEST_DB_URL=postgresql://test:test@localhost:5432/trip_ema_test
pytest tests/persistence tests/scheduler tests/registry \
       tests/transports tests/notifier tests/api tests/fundamentals \
       tests/orchestrator tests/backfill
```

### 10.4 Full

```bash
pytest tests
```

Everything in one invocation. Slower; only for pre-merge gate.

## 11. Known non-regressions

Pre-existing failures we don't gate on:

1. **51 fakeredis `evalsha` failures.** fakeredis doesn't fully
   implement Lua scripting (`EVAL` / `EVALSHA`); the
   `redis_queue.py` atomic-dedup uses these. CI excludes them.
2. **Intermittent GUI teardown segfault** on
   `test_run_eod_targets_picker_selection`. PySide6 destructor
   order race. Tactical workaround:
   `--deselect "tests/gui/test_signals.py::TestRangeButton::test_range_button_click_updates_years"`.

Both documented in project memory. Don't try to "fix" them in
isolation — they're known tooling limitations.

## 12. Common pitfalls

1. **Don't share state between tests.** Each test gets a clean DB
   via `postgres_engine` fixture; don't insert rows in a fixture
   that other tests depend on. Use per-test inserts.
2. **`@pytest.fixture(scope="session")` on anything that mutates
   DB.** Session scope + mutation = state leak. Module or function
   scope.
3. **Mocking yfinance.** Use the patterns in
   `tests/fundamentals/test_industry_fetch.py` — patch
   `yfinance.Ticker` to return a stub object with `.info` /
   `.quarterly_income_stmt` / etc.
4. **GUI tests inside non-GUI files.** Importing PySide6 at the
   top of a non-Qt test file forces an X server or offscreen
   platform for that test's run. Keep Qt imports inside
   `tests/gui/`.
5. **Time-sensitive tests without `freezegun`.** Real clock makes
   tests flaky. Use `frozen_time(when)` from the root conftest.
6. **DB tests committing across the fixture boundary.** The
   `postgres_engine` fixture's TRUNCATE only works against committed
   data. If your test opens its own transaction and DOESN'T commit,
   nothing for the next test to clean up. If it DOES commit, TRUNCATE
   handles it. Don't bypass `transaction()`.
7. **Asserting on `caplog` after a configure_logging.** The
   `disable_existing_loggers=True` default in Python's `fileConfig`
   wipes caplog's handler. Use `disable_existing_loggers=False`
   everywhere (Alembic's env.py is correct on this).

## 13. Test conventions

| Convention | Example |
|-----------|---------|
| `test_<topic>` files | `test_compute_regime.py`, `test_compose_email.py` |
| `Test<Class>` test classes for grouping | `class TestRecomputeMarket: ...` |
| `def test_<scenario>` test methods | `def test_halts_on_stage_1_failure(self): ...` |
| Arrange / Act / Assert sections | Comment-separated within each test |
| Inline fixtures via `@pytest.fixture` | Per-file local fixtures; reuse via conftest if cross-file |
| `pytest.raises(SomeError)` for failure cases | Not `try/except` with manual assert |
| `pytest.mark.parametrize` for truth tables | Per-input-row parametrization rather than for-loops |

## 14. Extension paths

- **A new test file.** Mirror the production path:
  `trip_ema/foo/bar.py` → `tests/foo/test_bar.py`. Use existing
  fixtures.
- **A new fixture.** Local first (per-file conftest), promote to
  package conftest if used across files, root conftest if used
  everywhere.
- **A new integration scenario.** Add to the package's directory
  rather than a top-level `tests/integration/` — keeps the test
  next to the code being tested.
- **A new no-DB sibling.** When a module accretes both DB and
  non-DB logic, split: create `<module>_pure/` for the latter,
  keep the original directory for DB-required.

## 15. Related packages

- `trip_ema/*` — every production package this suite covers.
- `alembic_pkg/` — migrations applied by `postgres_engine` fixture.
- `scripts/audit_dead_island.sh` — wraps `test_no_dead_island.py`.
- CI workflow `.github/workflows/*.yml` (out of tree) — invokes
  the fast + DB + Qt suites.
- `tests/fixtures/` (per-test-dir; not all dirs have one) —
  recorded yfinance / Brevo / NSE CSV responses.
