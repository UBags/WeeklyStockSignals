# Migrations

trip_ema uses Alembic exclusively from Phase 12 onwards. The legacy
SQL migrator (`migrations/legacy/*.sql`) is archived for historical
reference only.

## Authoring a new migration

```bash
pip install trip-ema[migrations]
alembic revision -m "describe what this migration does"
```

This creates `alembic_pkg/versions/<rev>_<slug>.py`. Edit the
`upgrade()` and `downgrade()` functions, then:

```bash
TRIP_EMA_CONFIG=config.yaml alembic upgrade head
```

## When to use raw SQL vs the `op` API

- **`op.create_table()`, `op.add_column()`** — preferred for simple
  schema changes. Reads naturally.
- **`op.execute("...")`** — required for Postgres-specific features
  (`NULLS NOT DISTINCT`, partitioning, conditional `DO $$` blocks,
  role management). The squashed `0001_initial_schema` uses
  `op.execute` because it concatenates raw SQL from the legacy
  migrators; new migrations should prefer the `op` API where
  feasible.

## Coexistence with legacy migrator

A database that ran the legacy migrator through version 010 has a
populated `schema_migrations` table but no `alembic_version`. On
first startup after Phase 12, `trip_ema.persistence.alembic_bridge`
stamps `0001_initial_schema` **without** re-running its
`upgrade()` function. Subsequent `alembic upgrade head` then
applies anything past the squash (currently `0002_db_roles`).

Fresh databases go straight through Alembic — no legacy migrator
involvement.

## Testing

`tests/persistence/conftest.py` runs Alembic against the
pytest-postgresql instance for each test. `tests/persistence/
test_legacy_migrator.py` regression-tests the archived SQL files
(it uses a separate `legacy_db_engine` fixture) and includes a
parity test that compares the legacy schema against the squashed
Alembic schema — catches drift between the two paths.
