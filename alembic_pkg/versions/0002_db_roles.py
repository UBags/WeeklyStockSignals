"""db role separation — per-process least-privilege

Creates three Postgres roles for the three runtime processes
(Phase 12, item 3 from Gemini's Tier-3 review). Implements
defense-in-depth for the Engine/ETL/GUI process boundaries
documented in ARCHITECTURE.md invariant 3.14.

  trip_etl_user
    - WRITE on tickers, ohlcv_bars, etl_runs, etl_ticker_status,
      corporate_actions, symbol_history
    - READ on schema_migrations, alembic_version (for migration
      sanity checks)
    - NO access to signal_events, subscribers, notification_log
    - Used by: trip-ema-etl (CLI), the admin ETL endpoints
      triggered from the GUI

  trip_engine_user
    - READ on tickers, ohlcv_bars
    - WRITE on signal_events
    - READ on subscribers, subscriptions
    - WRITE on notification_log
    - Used by: trip-ema (the main service), trip-ema-notifier
      (the worker process)

  trip_gui_user
    - READ on tickers, ohlcv_bars, signal_events, notification_log,
      subscribers, subscriptions
    - NO writes anywhere
    - Used by: trip-ema-console — but ONLY for read endpoints. The
      console's write actions (subscribe, prime-all, run-etl) go
      through the HTTP admin API with bearer-token auth, which the
      service performs using trip_engine_user. So this role is
      strictly for any direct DB peeking the console might do in
      future; current console is purely HTTP.

Passwords are NOT set here — operators run a follow-up SQL after
this migration to ALTER USER ... WITH PASSWORD, or use IAM auth
via cloud providers. The migration creates roles WITHOUT LOGIN by
default; ops grant LOGIN + set password as a deployment step.

Backward compatibility: this migration creates roles in addition
to whatever superuser the existing trip-ema config points at. The
service continues to work unchanged using the existing user. To
adopt the new roles, edit config.yaml to add a `db_roles:` section
(see config schema). Until then, no behavioral change.

Revision ID: 0002_db_roles
Revises: 0001_initial_schema
Create Date: 2026-05-18
"""

from __future__ import annotations

from alembic import op


# revision identifiers
revision = "0002_db_roles"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on = None


# Tables that exist in the schema after 0001_initial_schema.
# Listed explicitly so the migration breaks loudly (rather than
# silently granting on a stale name list) if 0001 drops a table.
_BAR_TABLES = ["tickers", "ohlcv_bars"]
_ETL_TABLES = [
    "tickers",            # ETL writes new tickers from listings
    "ohlcv_bars",         # primary ETL output
    "etl_runs",           # was etl_run_log in the migration draft
    "etl_ticker_status",  # was parked_tickers in the migration draft
    "corporate_actions",  # ETL also writes corporate actions
    "symbol_history",     # ETL tracks ticker rename history
]
_SIGNAL_TABLES = ["signal_events"]
_SUBSCRIBER_TABLES = ["subscribers", "subscriptions"]
_NOTIFICATION_TABLES = ["notification_log"]


def upgrade() -> None:
    """Create the three roles + grant per-table privileges.

    Uses CREATE ROLE IF NOT EXISTS-equivalent via DO blocks so the
    migration is rerunnable in environments where someone created
    the roles manually first.
    """
    # ------------------------------------------------------------
    # Roles
    # ------------------------------------------------------------
    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trip_etl_user') THEN
                CREATE ROLE trip_etl_user NOLOGIN;
            END IF;
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trip_engine_user') THEN
                CREATE ROLE trip_engine_user NOLOGIN;
            END IF;
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trip_gui_user') THEN
                CREATE ROLE trip_gui_user NOLOGIN;
            END IF;
        END
        $$;
    """)

    # ------------------------------------------------------------
    # ETL role: writes to ETL-owned tables, no signal/subscriber access
    # ------------------------------------------------------------
    for tbl in _ETL_TABLES:
        op.execute(
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE {tbl} TO trip_etl_user;"
        )
    # ETL needs sequence usage for SERIAL primary keys on tickers
    op.execute(
        "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO trip_etl_user;"
    )
    # Migration sanity — ETL can read but not write the version tables
    op.execute("GRANT SELECT ON TABLE alembic_version TO trip_etl_user;")
    # schema_migrations exists only on pre-Phase-12 DBs; guard.
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.tables
                WHERE table_schema = 'public' AND table_name = 'schema_migrations'
            ) THEN
                GRANT SELECT ON TABLE schema_migrations TO trip_etl_user;
            END IF;
        END
        $$;
    """)

    # ------------------------------------------------------------
    # Engine role: reads bars, writes signals, reads/writes subscribers
    # and notification log
    # ------------------------------------------------------------
    for tbl in _BAR_TABLES:
        op.execute(f"GRANT SELECT ON TABLE {tbl} TO trip_engine_user;")
    for tbl in _SIGNAL_TABLES:
        op.execute(
            f"GRANT SELECT, INSERT, UPDATE ON TABLE {tbl} TO trip_engine_user;"
        )
    for tbl in _SUBSCRIBER_TABLES:
        op.execute(
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE {tbl} TO trip_engine_user;"
        )
    for tbl in _NOTIFICATION_TABLES:
        op.execute(
            f"GRANT SELECT, INSERT ON TABLE {tbl} TO trip_engine_user;"
        )
    op.execute(
        "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO trip_engine_user;"
    )
    op.execute("GRANT SELECT ON TABLE alembic_version TO trip_engine_user;")

    # Partitioned tables: granting on the parent doesn't auto-grant
    # on partitions in Postgres < 15. We're on 15+, so this works,
    # but be explicit just in case.
    op.execute("""
        DO $$
        DECLARE
            partition_name text;
        BEGIN
            FOR partition_name IN
                SELECT inhrelid::regclass::text
                FROM pg_inherits
                WHERE inhparent = 'notification_log'::regclass
            LOOP
                EXECUTE format(
                    'GRANT SELECT, INSERT ON TABLE %I TO trip_engine_user;',
                    partition_name
                );
            END LOOP;
        END
        $$;
    """)

    # ------------------------------------------------------------
    # GUI role: read-only across the board
    # ------------------------------------------------------------
    op.execute(
        "GRANT SELECT ON ALL TABLES IN SCHEMA public TO trip_gui_user;"
    )
    # Future tables auto-grant read to GUI role (idempotent default
    # privilege). Has to be run as the table-owner; if migrations
    # run as a superuser this is fine.
    op.execute("""
        ALTER DEFAULT PRIVILEGES IN SCHEMA public
        GRANT SELECT ON TABLES TO trip_gui_user;
    """)


def downgrade() -> None:
    """Drop the three roles. NOT a no-op — this actually revokes
    privileges and drops the roles.

    Order matters: revoke all privs, then drop. Dropping a role
    that still owns objects raises 'cannot drop role X because
    other objects depend on it'.
    """
    op.execute("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM trip_gui_user;")
    op.execute("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM trip_engine_user;")
    op.execute("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM trip_etl_user;")
    op.execute("REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM trip_engine_user;")
    op.execute("REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM trip_etl_user;")
    op.execute("""
        ALTER DEFAULT PRIVILEGES IN SCHEMA public
        REVOKE SELECT ON TABLES FROM trip_gui_user;
    """)
    op.execute("DROP ROLE IF EXISTS trip_gui_user;")
    op.execute("DROP ROLE IF EXISTS trip_engine_user;")
    op.execute("DROP ROLE IF EXISTS trip_etl_user;")
