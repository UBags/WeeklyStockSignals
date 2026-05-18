# Running the tests

Two-tier test suite. The DB-free tier runs in ~12s and gates every
PR; the Postgres-backed tier runs in CI on main and on demand.

## DB-free

```bash
# Non-Qt
pytest tests/indicators tests/engine tests/cache tests/notifications \
       tests/scheduler_pure tests/notifier_pure tests/config \
       tests/transports_pure tests/parity tests/observability \
       tests/etl tests/gui_pure

# Qt — separate invocation to dodge the pytest-qt teardown race
QT_QPA_PLATFORM=offscreen pytest tests/gui
```

The DB-free suite is ~465 tests and uses fakeredis for any test
that touches the queue.

## Postgres-backed

```bash
# Requires Postgres 15+ installed and pytest-postgresql
pip install trip-ema[test]
pytest tests/persistence tests/scheduler tests/registry \
       tests/transports tests/notifier tests/api
```

The `db_engine` fixture in `tests/persistence/conftest.py`
provisions a fresh database per test via pytest-postgresql, runs
`alembic upgrade head` against it, then disposes it on teardown.

Tests under `tests/persistence/test_legacy_migrator.py` use a
separate `legacy_db_engine` fixture that runs the archived SQL
migrator against `migrations/legacy/` — for regression coverage of
the historical files.

## CI

`.github/workflows/tests.yml` runs both tiers:

- **`db-free` job** — every push and PR. Two pytest invocations
  (non-Qt + gui_pure first, then tests/gui alone) to avoid the
  pytest-qt teardown race. The Qt step parses pytest's output for
  pass/fail rather than trusting the exit code — a benign teardown
  segfault doesn't fail the build, but a real test failure does.
- **`postgres-integration` job** — main pushes + manual dispatch
  + same-repo PRs. Provisions a `postgres:16` service container.

See `tests.yml` for the exact invocations.

## The pytest-qt teardown race

PySide6 wraps Qt's C++ object hierarchy. At test teardown, Python's
GC races with pytest-qt's QApplication destruction; if the GC wins,
deleted C++ objects get re-deleted → SIGSEGV. `tests/gui/conftest.py`
mitigates with a `managed_widget` factory and an autouse
event-loop-drain fixture that processes `deleteLater` events
before the next test runs.

Empirical impact: standalone `tests/gui` reliability ~4-5/5
(was 3/5). The race isn't fully eliminated because some teardown
happens inside pytest's atexit handlers, after fixtures can
intervene. The CI two-invocation pattern + output-parsing wrapper
remain belt-and-braces.

Documented honestly rather than papered over.
