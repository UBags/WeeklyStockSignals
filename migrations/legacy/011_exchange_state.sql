-- Migration 011 — exchange_state table.
--
-- Phase 14.6b: per-exchange ETL snapshot table. One row per exchange,
-- written by the runner inside the same transaction that closes the
-- corresponding etl_runs row. Read by the GUI pulse view and the
-- /v1/admin/exchange-state endpoints.
--
-- IMPORTANT — Phase 12 transition note:
--
-- This file is the legacy-migrator parallel of Alembic revision
-- 0004_exchange_state. The legacy migrator is NOT run against
-- production databases anymore (Phase 12 moved fresh-DB setup to
-- Alembic and stamps pre-existing prod DBs at the 0001 squash
-- point). This file exists for ONE reason: the parity test in
-- tests/persistence/test_legacy_migrator.py runs both migration
-- paths against fresh databases and asserts the table set is
-- identical. Without this file, the legacy fresh-DB path would
-- not have exchange_state and the parity test would fail.
--
-- DO NOT remove this file under the assumption that "legacy is
-- dead." It's load-bearing for the parity test, which is the
-- guard that catches schema drift between the two paths.
--
-- The schema, constraints, grants, and NSE seed below MUST stay
-- bit-equivalent to alembic_pkg/versions/0004_exchange_state.py.
-- If you change one, change the other in the same commit.

BEGIN;

-- ---------- exchange_state table ---------- --

CREATE TABLE IF NOT EXISTS exchange_state (
    exchange                  TEXT        PRIMARY KEY,
    initial_load_started_at   TIMESTAMPTZ,
    initial_load_completed_at TIMESTAMPTZ,
    last_eod_run_at           TIMESTAMPTZ,
    last_eod_outcome          TEXT,
    last_eod_data_date        DATE,
    last_eod_run_id           BIGINT,
    last_intraday_run_at      TIMESTAMPTZ,
    last_listings_sync_at     TIMESTAMPTZ,
    last_listings_run_id      BIGINT,
    last_tier_recompute_at    TIMESTAMPTZ,
    updated_at                TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT exchange_state_outcome_chk
        CHECK (
            last_eod_outcome IS NULL
            OR last_eod_outcome IN ('success', 'partial', 'failed')
        ),
    CONSTRAINT exchange_state_initial_load_order_chk
        CHECK (
            initial_load_completed_at IS NULL
            OR initial_load_started_at IS NULL
            OR initial_load_completed_at >= initial_load_started_at
        )
);

COMMENT ON TABLE exchange_state IS
    'Per-exchange ETL state snapshot. One row per configured exchange. '
    'Written by the runner inside the same transaction that closes etl_runs. '
    'Read by the GUI pulse view and the /v1/admin/exchange-state endpoints.';

-- ---------- Grants (Phase 12 roles) ---------- --
-- Mirror the per-role pattern from migration 002_db_roles (legacy)
-- and Alembic 0002_db_roles. Guard each grant so the migration is
-- safe to run even when the roles don't exist yet (older deployments,
-- test fixtures that skip role creation).

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trip_etl_user') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE
            ON TABLE exchange_state TO trip_etl_user;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trip_engine_user') THEN
        GRANT SELECT ON TABLE exchange_state TO trip_engine_user;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trip_gui_user') THEN
        GRANT SELECT ON TABLE exchange_state TO trip_gui_user;
    END IF;
END
$$;

-- ---------- Seed NSE row ---------- --
--
-- Values captured from the live NSE production DB at Phase 14.6b
-- design time. ON CONFLICT DO NOTHING so a re-run is a no-op (e.g.
-- if someone pre-populated the row manually).
--
-- Bit-equivalent to the seed block in 0004_exchange_state.py.
INSERT INTO exchange_state (
    exchange,
    initial_load_started_at,
    initial_load_completed_at,
    last_eod_run_at,
    last_eod_outcome,
    last_eod_data_date,
    last_eod_run_id,
    last_listings_sync_at,
    last_listings_run_id,
    updated_at
) VALUES (
    'NSE',
    '2026-05-18 22:52:37+05:30',
    '2026-05-19 02:09:23+05:30',
    '2026-05-20 01:01:21+05:30',
    'success',
    '2026-05-19',
    NULL,
    '2026-05-18 18:44:25+05:30',
    NULL,
    NOW()
)
ON CONFLICT (exchange) DO NOTHING;

-- Record this migration as applied (legacy migrator convention).
INSERT INTO schema_migrations (version) VALUES ('011') ON CONFLICT DO NOTHING;

COMMIT;
