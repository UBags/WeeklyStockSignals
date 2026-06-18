# `tests/` — Test suite

The full test suite for `trip-ema`. Organized to mirror `trip_ema/`
package structure, with a few extra trees for specific testing
strategies (`_pure` suffix, `parity/`).

## Layout

| Path | Tests for | Needs DB? | Needs Qt? |
|---|---|---|---|
| `api/` | `trip_ema/api/` lifecycle, cached, stateless | yes | no |
| `app/` | `trip_ema/app.py` orchestrator-build wiring | no | no |
| `cache/` | `trip_ema/cache/` | no | no |
| `config/` | `trip_ema/config/` | no | no |
| `engine/` | `trip_ema/engine/` | no | no |
| `etl/` | `trip_ema/etl/` (incl. Phase 15 signal generator) | partial | no |
| `fundamentals/` | `trip_ema/fundamentals/` (Phases 1-8) | yes | no |
| `gui/` | `trip_ema/gui/` views, widgets | no | yes |
| `gui_pure/` | GUI-adjacent code that doesn't need a QApplication | no | no |
| `indicators/` | `trip_ema/indicators/` math | no | no |
| `notifications/` | `trip_ema/notifications/` transports + breakers + queue + enrichment | no | no |
| `notifier/` | `trip_ema/notifier_worker.py` (full integration) | yes | no |
| `notifier_pure/` | `notifier_worker` logic without DB | no | no |
| `observability/` | `trip_ema/observability/` | no | no |
| `parity/` | Pine numerical parity harness (see its own README.md) | no | no |
| `persistence/` | `trip_ema/persistence/` repos + migrator + alembic bridge | yes | no |
| `registry/` | `trip_ema/registry/` | no | no |
| `scheduler/` | `trip_ema/scheduler/` orchestrator + compute scheduler | yes | no |
| `scheduler_pure/` | scheduler logic that needs no DB | no | no |
| `transports/` | `trip_ema/transports/http/` full integration | yes | no |
| `transports_pure/` | HTTP routes against mocked DB | no | no |

## Top-level files

- `__init__.py` — marker
- `test_no_dead_island.py` — walks the import graph; asserts no
  circular or layer-violating imports (invariant 3.14 — process /
  module boundaries hold)

## The `_pure` suffix convention

When a module has both DB-required tests (slow, real Postgres) and
pure-Python tests against mocks (fast, no DB), the latter live in a
sibling directory named `<module>_pure/`. This split exists because:

1. The DB-free suite runs on every push and PR (≤ 15s wall time)
2. The DB-required suite runs only on main + manual dispatch + same-
   repo PRs (provisions a `postgres:16` service container)

Faster feedback loop without sacrificing integration coverage.

## Running the suite

```bash
# DB-free — fast, no Postgres needed (~588 tests, ~15s)
pytest tests/indicators tests/engine tests/cache tests/notifications \
       tests/scheduler_pure tests/notifier_pure tests/config \
       tests/transports_pure tests/parity tests/observability \
       tests/etl tests/gui_pure

# Qt-using — separate invocation to avoid pytest-qt teardown race
QT_QPA_PLATFORM=offscreen pytest tests/gui

# Postgres-backed integration tests
pytest tests/persistence tests/scheduler tests/registry \
       tests/transports tests/notifier tests/api tests/fundamentals
```

## The two-invocation pattern for Qt

`tests/gui/` uses pytest-qt with the offscreen platform. The Phase 12
`conftest.py` introduced the deferred-deletion pattern (`managed_widget`
factory + autouse `_drain_after_every_gui_test`) which improved standalone
clean-exit rate from ~3/5 to ~4-5/5. CI still splits Qt vs non-Qt into
two pytest invocations and parses output rather than trusting the exit
code, so a benign teardown segfault doesn't fail the build.

## Tests for new code

Each new module in `trip_ema/` should land with a matching test file.
The convention (followed by every existing module) is one test file per
production file, plus integration tests for cross-module scenarios.

For Phase 15, see `tests/etl/test_signal_generator*.py` (4 files, 42
tests) — the architecture's "per-ticker commit, in-memory merge, atomic
file write, asymmetric run_id lifecycle" patterns all have dedicated
coverage.
