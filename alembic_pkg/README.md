# Step 3 — Migration `0018_collapse_regime_columns`

Drops the `core_regime` column from `indicator_state`. The new regime
classifier has a single unified regime per bar; the old core/display
split is obsolete.

`display_regime` stays untouched — same column name, same SMALLINT
encoding, same downstream contract. The chart, the HTTP wire format,
and every reader sees zero change at the column boundary.

## Files

| Bundle path | Repo path |
|---|---|
| `migrations/versions/0018_collapse_regime_columns.py` | wherever your existing Alembic `versions/` directory lives — almost certainly `trip_ema/migrations/alembic_pkg/versions/` based on the README structure I wrote in Step 0 |
| `migrations_legacy/023_collapse_regime_columns.sql` | `trip_ema/migrations/legacy/` (or wherever the existing `021_indicator_state.sql` siblings live) — documentation only, not applied |

If the actual repo paths differ, the only thing that matters is that
`0018_collapse_regime_columns.py` lands in the same directory as
`0017_benchmark_stats.py`. Use that file's location as the truth.

## What the migration does

### `upgrade()`

```sql
ALTER TABLE indicator_state
    DROP CONSTRAINT IF EXISTS indicator_state_core_regime_chk;

ALTER TABLE indicator_state
    DROP COLUMN IF EXISTS core_regime;
```

Drops the CHECK first for clarity in pg_dump diffs; Postgres would
cascade it anyway on `DROP COLUMN`. The `IF EXISTS` guards make
re-runs idempotent.

### `downgrade()`

```sql
ALTER TABLE indicator_state
    ADD COLUMN IF NOT EXISTS core_regime SMALLINT;

UPDATE indicator_state
SET core_regime = display_regime
WHERE core_regime IS NULL;

ALTER TABLE indicator_state
    ALTER COLUMN core_regime SET NOT NULL;

ALTER TABLE indicator_state
    ADD CONSTRAINT indicator_state_core_regime_chk
    CHECK (core_regime IN (-1, 0, 1));
```

**Best-effort, not lossless.** Once the column is dropped, the
original `core_regime` values are gone for good. The downgrade
restores the schema shape and stamps each row with `display_regime`
as the closest available stand-in (same encoding, same value space).
Operators rolling back must not rely on `core_regime` carrying its
original meaning after a downgrade.

## End-to-end verification

Verified against a fresh Postgres 16 cluster in a sandbox:

| Phase | Outcome |
|---|---|
| Pre-upgrade | Three seed rows; both columns; both CHECK constraints present |
| `upgrade()` | `core_regime` column dropped; `core_regime` CHECK dropped; `display_regime` column + values preserved unchanged; row count preserved |
| `downgrade()` | `core_regime` column restored, populated from `display_regime` (values `0, 0, 1` matching `display_regime`); NOT NULL enforced; CHECK restored; illegal value `99` rejected by the restored CHECK |

The downgrade values won't equal the original pre-upgrade values
(seed had `core_regime = 1, 1, 1`; restored has `0, 0, 1` from
`display_regime`). That's the documented data-loss tradeoff.

## DO NOT `alembic upgrade head` YET

This file is ready to commit alongside Step 3 so the diff is
reviewable, but it must not be applied to any running database until
all application code has stopped referencing `core_regime`. Those
sites are:

| File | What it does today | Needs |
|---|---|---|
| `trip_ema/fundamentals/indicator_primitives.py` | Computes `core_regime_series` and emits `"core_regime"` in the per-bar indicator-state dict | Drop the computation + the dict key |
| `trip_ema/fundamentals/runners.py` | Passes `core_regime=bs["core_regime"]` to `IndicatorStateRepository.upsert` (two call sites) | Drop the kwarg |
| `trip_ema/persistence/fundamentals_repo.py` | `find_indicator_states` accepts an optional `core_regime=` filter; `upsert` accepts `core_regime` | Drop the filter + the upsert param |
| `trip_ema/transports/http/routes/fundamentals.py` | Pydantic schema exposes `core_regime: int` on the wire and copies it from the domain object | Drop the field |
| `trip_ema/persistence/tables.py` | SQLAlchemy `Column("core_regime", SmallInteger, …)` | Drop the column |
| `trip_ema/indicators/core_regime.py` | The state-machine module itself | Delete (Step 9 demolition) |
| `trip_ema/indicators/ticker_engine.py` | Calls `step_core_regime` per bar | Delete (Step 9 demolition) |

All of the above land in **Step 9 (demolition)**. The migration runs
**after** Step 9 commits and **before** Step 10 (the regeneration
batch). The recommended sequence:

```
... Steps 4–8 land (new writer, persist, live, backfill,
                    chart endpoint, cache repurpose) ...

Step 9 commit: demolition
  - delete indicators/core_regime.py, display_regime.py,
    change_classifier.py, ticker_engine.py
  - drop core_regime from indicator_primitives.py,
    runners.py, fundamentals_repo.py, routes/fundamentals.py,
    tables.py

# now safe to apply
alembic upgrade head
# (which applies 0018)

Step 10: trip-ema-etl regenerate-signals --exchange NSE …
```

If `0018` is applied before Step 9, the running app crashes on the
next write attempt to `indicator_state` because the SQL `INSERT`
references a dropped column.

## Paste-ready git block

```bash
# from repo root

# Adjust the two cp destinations to match your actual repo layout.
# I'm guessing trip_ema/migrations/alembic_pkg/versions/ based on the
# README structure; replace with wherever 0017_benchmark_stats.py
# lives.

cp step3/migrations/versions/0018_collapse_regime_columns.py \
   trip_ema/migrations/alembic_pkg/versions/0018_collapse_regime_columns.py

cp step3/migrations_legacy/023_collapse_regime_columns.sql \
   trip_ema/migrations/legacy/023_collapse_regime_columns.sql

git add trip_ema/migrations/alembic_pkg/versions/0018_collapse_regime_columns.py \
        trip_ema/migrations/legacy/023_collapse_regime_columns.sql

git commit -m "migration 0018: collapse regime columns on indicator_state

Drops core_regime — the old two-stage core/display split is obsolete
under the new unified regime classifier (trip_ema/regime/compute_regime.py).

display_regime stays untouched. Its name, type, and -1/0/+1 encoding
are unchanged; only the semantic source changes (new unified classifier
instead of the old gated path). No downstream contract change.

upgrade(): drops indicator_state_core_regime_chk CHECK + the column.
downgrade(): restores the column, best-effort populates from
display_regime, enforces NOT NULL, restores the CHECK. Not lossless
— original core_regime values are gone after upgrade.

NOT YET APPLIED. alembic upgrade head must wait until the demolition
commit removes every app-code reference to core_regime (see file
docstring for the full list).

Verified end-to-end on a fresh Postgres 16 cluster: upgrade drops
column + CHECK, preserves display_regime values; downgrade restores
shape, stamps from display_regime, rejects illegal values via the
restored CHECK."

git push origin master
```

Do **not** run `alembic upgrade head` after this commit. The
migration sits in the repo; it gets applied only after Step 9 lands.
