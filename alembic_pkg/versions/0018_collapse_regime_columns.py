"""Collapse regime columns on `indicator_state`.

The new regime architecture (see `trip_ema/regime/compute_regime.py`)
has a single regime per bar — there is no longer a separate "core"
underlying state and "display" gated paint. The classification rules
are unified.

What this migration changes on `indicator_state`:
  - Drops the `core_regime SMALLINT NOT NULL` column.
  - Drops the matching `indicator_state_core_regime_chk` CHECK
    constraint (`core_regime IN (-1, 0, 1)`).

What this migration deliberately keeps:
  - The `display_regime SMALLINT NOT NULL` column. Its name, type,
    and -1/0/+1 encoding stay exactly as today. Semantically, its
    contents are now produced by the new unified classifier rather
    than the old two-stage core→display path, but the chart, the
    HTTP wire format (`HistoricalBarPayload.display_bg`), and every
    downstream reader sees no change in shape or meaning of the
    value at the column boundary.
  - The associated `indicator_state_display_regime_chk` CHECK
    constraint. Unchanged.

Why drop `core_regime` rather than rename it:
  - The downstream contract is `display_regime` — that's what the
    chart reads, what `HistoricalBarPayload` exposes, and what
    operator emails are colored from. Renaming would force a
    GUI + API surface change for zero semantic benefit. Drop is
    the cheaper move.

Deploy ordering — IMPORTANT:
  - This migration assumes NO application code still writes to or
    reads from `core_regime`. Run `alembic upgrade head` AFTER:
      1. `indicator_primitives.py` no longer emits `core_regime`
         in the indicator-state row dict.
      2. `fundamentals/runners.py` no longer passes `core_regime`
         to `IndicatorStateRepository.upsert`.
      3. `fundamentals_repo.py` `find_indicator_states` filter
         no longer accepts a `core_regime=` parameter.
      4. `routes/fundamentals.py` Pydantic schema no longer
         exposes `core_regime` on the wire.
      5. `tables.py` SQLAlchemy `Column("core_regime", ...)` is
         removed.
    The demolition commit (Step 9 in the migration plan) makes
    these changes in the same diff that deletes
    `indicators/core_regime.py`, `display_regime.py`,
    `change_classifier.py`, `ticker_engine.py`. Apply this
    migration AFTER that commit lands and BEFORE running the
    signal-regeneration batch.

Downgrade semantics:
  - Re-adds `core_regime SMALLINT` (nullable initially).
  - Best-effort populates from `display_regime` (same encoding,
    same value space). Note this is NOT a true round-trip — the
    original core_regime values are gone for good. The downgrade
    restores the schema shape and stamps each row with the closest
    available stand-in. Operators rolling back should not rely on
    `core_regime` carrying meaningful values after a downgrade.
  - Adds NOT NULL.
  - Adds back the CHECK constraint.

Legacy parity: `migrations/legacy/023_collapse_regime_columns.sql`.

Revision ID: 0018_collapse_regime_columns
Revises: 0017_benchmark_stats
Create Date: 2026-06-20
"""
from __future__ import annotations

from alembic import op


revision = "0018_collapse_regime_columns"
down_revision = "0017_benchmark_stats"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Drop the CHECK constraint first. ALTER TABLE ... DROP COLUMN in
    # Postgres would cascade the constraint away, but being explicit
    # keeps the intent obvious in pg_dump diffs and reads cleanly in
    # operator logs.
    op.execute("""
        ALTER TABLE indicator_state
            DROP CONSTRAINT IF EXISTS indicator_state_core_regime_chk;
    """)

    op.execute("""
        ALTER TABLE indicator_state
            DROP COLUMN IF EXISTS core_regime;
    """)


def downgrade() -> None:
    # Add the column nullable, populate from display_regime, then
    # enforce NOT NULL. Three steps because NOT NULL on a freshly
    # added column with existing rows requires a default — and we
    # don't have a meaningful default, so we backfill row-by-row
    # from display_regime first.

    op.execute("""
        ALTER TABLE indicator_state
            ADD COLUMN IF NOT EXISTS core_regime SMALLINT;
    """)

    # Best-effort backfill. NOT a recovery — the original core_regime
    # values are gone for good. This stamps each row with
    # display_regime as the closest available stand-in (same encoding,
    # same value space). See docstring.
    op.execute("""
        UPDATE indicator_state
        SET core_regime = display_regime
        WHERE core_regime IS NULL;
    """)

    op.execute("""
        ALTER TABLE indicator_state
            ALTER COLUMN core_regime SET NOT NULL;
    """)

    op.execute("""
        ALTER TABLE indicator_state
            ADD CONSTRAINT indicator_state_core_regime_chk
            CHECK (core_regime IN (-1, 0, 1));
    """)
