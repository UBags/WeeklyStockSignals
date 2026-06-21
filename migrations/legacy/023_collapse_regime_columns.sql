---
-- Migration 023 — collapse regime columns on indicator_state.
--
-- Bit-equivalent parallel of alembic_pkg/versions/0018_collapse_regime_columns.py.
-- See that file's docstring for the design.
--
-- This file is documentation-only and is not applied by any runner
-- (Alembic is the exclusive migration path post-Phase 12). Kept for
-- bit-parity with the Alembic revision so legacy SQL-only inspections
-- show the same schema evolution sequence as the Alembic history.

BEGIN;

ALTER TABLE indicator_state
    DROP CONSTRAINT IF EXISTS indicator_state_core_regime_chk;

ALTER TABLE indicator_state
    DROP COLUMN IF EXISTS core_regime;

COMMIT;
