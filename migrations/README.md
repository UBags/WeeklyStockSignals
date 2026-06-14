# `migrations/` — Database migrations (legacy + parity)

Phase 12 made Alembic (`../alembic_pkg/`) the **canonical** migration
system. The files here are either:

1. SQL bodies inlined from a pre-Phase-12 design (in `legacy/`), or
2. Parity parallels of post-Phase-12 Alembic revisions (also in `legacy/`)

Production `trip-ema` startup applies migrations exclusively through
Alembic. The files here are **not** applied to fresh production databases.

## Contents

| Path | Purpose |
|---|---|
| `001_initial_schema.sql` | Legacy initial schema (pre-Phase-12) |
| `002_signal_state.sql` | Legacy |
| `003_subscribers.sql` | Legacy |
| `004_partitions_seed.sql` | Legacy — partition seed for `ohlcv_bars` |
| `005_drop_ticker_signal_state.sql` | Legacy cleanup |
| `006_bars_updated_at.sql` | Legacy |
| `007_ticker_tz_source_timeframe.sql` | Legacy |
| `008_drop_notification_log_dedup_index.sql` | Legacy |
| `010_etl_schema.sql` | Legacy (note: no `009`; intentional gap) |
| `legacy/` | Full archive of legacy migrations + post-Phase-12 parity parallels |

## The legacy archive (`legacy/`)

See `legacy/README.md` for the canonical explanation. Short version:

- Files `001-010` are the **historical** schema produced by the
  pre-Phase-12 migrator
- Files `011+` are **parity parallels** of Alembic revisions — used
  ONLY by the legacy-parity test fixture
  (`tests/persistence/test_legacy_migrator.py`)
- Adding an Alembic revision that touches the table set obligates a
  matching parity SQL file here

## Why is the same content seemingly duplicated?

The top-level files `001_*.sql` through `010_etl_schema.sql` were the
operative scripts during Phases 1-11. `legacy/` contains a full archived
copy plus the post-Phase-12 parity parallels. The duplication is
historical accident; the **legacy archive is the canonical reference**.

## Should I touch these?

- Don't edit existing files — create a new Alembic revision instead.
- If adding a new Alembic revision that creates / alters tables, add a
  matching `legacy/NNN_*.sql` parity file in the same commit and
  re-run `tests/persistence/test_legacy_migrator.py` to confirm.
