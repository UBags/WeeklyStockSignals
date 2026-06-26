"""Migration 0021 — indicator enrichment tables.

Adds the two enrichment tables that hold the 23 enrichment fields
(band-pierce flags, distance metrics, bars-since-turn,
pct-change-since-turn, pattern labels, per-EMA turn flags) for
ticker series and industry series.

Architectural intent: these fields are downstream of the core
indicator state and may change, be dropped, or be replaced over
time. Keeping them in a separate table from the core indicator
state lets us evolve them without touching the stable core
schema.

Tables added:
  - stock_indicator_enrichment    — keyed by (ticker_id, timeframe, bar_timestamp)
  - industry_indicator_enrichment — keyed by (industry_name, timeframe, bar_timestamp)

Foreign keys:
  Each enrichment row is keyed by the SAME composite PK as its
  matching state row, with a composite FK back to the state table
  using ON DELETE CASCADE. An enrichment row cannot exist without
  its matching state row, and dropping a state row drops the
  enrichment row with it. Writes must be sequenced: state first,
  enrichment second.

Indexes:
  The PK (composite, also the FK columns) is sufficient for the
  expected access pattern of "give me enrichment for this state
  row" (JOIN on PK = PK). No additional indexes are created. If
  date-axis or per-entity history queries on enrichment become
  hot, indexes can be added in a future migration.

Pattern label constraint:
  No CHECK on the `*_pattern_label` columns — the legacy
  convention is to constrain the label set in application code
  (PATTERN_LABELS) rather than in the DB. The label set could
  grow; CHECK constraints would require a migration on each
  growth.

Grants:
  - trip_etl_user: full CRUD (writes during compute, reads during
    diagnostics).
  - trip_gui_user: SELECT only.

Legacy parity: migrations/legacy/025_indicator_enrichment_tables.sql.

Revision ID: 0021_indicator_enrichment_tables
Revises:    0020_clean_pipeline_tables
Create Date: 2026-06-26
"""
from __future__ import annotations

from alembic import op


revision = "0021_indicator_enrichment"
down_revision = "0020_clean_pipeline_tables"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # =================================================================
    # stock_indicator_enrichment — 1:1 with stock_indicator_state
    # =================================================================
    op.execute("""
        CREATE TABLE IF NOT EXISTS stock_indicator_enrichment (
            ticker_id          INTEGER          NOT NULL,
            timeframe          VARCHAR(8)       NOT NULL,
            bar_timestamp      TIMESTAMPTZ      NOT NULL,

            -- Band-pierce flags: did close cross through the mid-bands
            -- this bar (high >= mid >= low intra-bar).
            pierces_upper_mid  BOOLEAN          NOT NULL DEFAULT FALSE,
            pierces_lower_mid  BOOLEAN          NOT NULL DEFAULT FALSE,

            -- Distance of close from each EMA, in high-low units.
            -- Nullable until warmup completes for the EMA.
            dist_to_med_in_hl_units            DOUBLE PRECISION,
            dist_to_long_in_hl_units           DOUBLE PRECISION,
            dist_to_longest_in_hl_units        DOUBLE PRECISION,

            -- Rolling mean of those distances.
            mean_dist_to_med_in_hl_units       DOUBLE PRECISION,
            mean_dist_to_long_in_hl_units      DOUBLE PRECISION,
            mean_dist_to_longest_in_hl_units   DOUBLE PRECISION,

            -- Bars elapsed since each EMA last changed direction.
            -- NULL until that EMA has had at least one turn.
            bars_since_med_turn                INTEGER,
            bars_since_long_turn               INTEGER,
            bars_since_longest_turn            INTEGER,

            -- Pct change in close since each EMA last turned.
            pct_change_since_med_turn          DOUBLE PRECISION,
            pct_change_since_long_turn         DOUBLE PRECISION,
            pct_change_since_longest_turn      DOUBLE PRECISION,

            -- Categorical pattern label per EMA. Allowed values are
            -- constrained by application code (PATTERN_LABELS), not
            -- by a DB CHECK — the label set could grow.
            med_pattern_label      VARCHAR(32)  NOT NULL DEFAULT 'NA',
            long_pattern_label     VARCHAR(32)  NOT NULL DEFAULT 'NA',
            longest_pattern_label  VARCHAR(32)  NOT NULL DEFAULT 'NA',

            -- Did this specific EMA flip its direction this bar.
            med_turn_buy           BOOLEAN      NOT NULL DEFAULT FALSE,
            med_turn_sell          BOOLEAN      NOT NULL DEFAULT FALSE,
            long_turn_buy          BOOLEAN      NOT NULL DEFAULT FALSE,
            long_turn_sell         BOOLEAN      NOT NULL DEFAULT FALSE,
            longest_turn_buy       BOOLEAN      NOT NULL DEFAULT FALSE,
            longest_turn_sell      BOOLEAN      NOT NULL DEFAULT FALSE,

            -- Bookkeeping.
            computed_at        TIMESTAMPTZ      NOT NULL DEFAULT NOW(),

            PRIMARY KEY (ticker_id, timeframe, bar_timestamp),

            CONSTRAINT stock_indicator_enrichment_state_fk
                FOREIGN KEY (ticker_id, timeframe, bar_timestamp)
                REFERENCES stock_indicator_state
                    (ticker_id, timeframe, bar_timestamp)
                ON DELETE CASCADE
        );
    """)

    op.execute("""
        COMMENT ON TABLE stock_indicator_enrichment IS
            'V1 per-ticker enrichment fields (band-pierce flags, '
            'distance metrics, bars-since-turn, pct-change-since-turn, '
            'pattern labels, per-EMA turn flags). 1:1 with '
            'stock_indicator_state via composite FK with ON DELETE '
            'CASCADE. Separated from core so the enrichment set can '
            'evolve without touching the stable core schema.';
    """)

    # =================================================================
    # industry_indicator_enrichment — 1:1 with industry_indicator_state
    # =================================================================
    op.execute("""
        CREATE TABLE IF NOT EXISTS industry_indicator_enrichment (
            industry_name      VARCHAR(128)     NOT NULL,
            timeframe          VARCHAR(8)       NOT NULL,
            bar_timestamp      TIMESTAMPTZ      NOT NULL,

            -- Mirror of stock_indicator_enrichment — both series types
            -- carry the full enrichment for uniform consumer code.
            pierces_upper_mid  BOOLEAN          NOT NULL DEFAULT FALSE,
            pierces_lower_mid  BOOLEAN          NOT NULL DEFAULT FALSE,

            dist_to_med_in_hl_units            DOUBLE PRECISION,
            dist_to_long_in_hl_units           DOUBLE PRECISION,
            dist_to_longest_in_hl_units        DOUBLE PRECISION,

            mean_dist_to_med_in_hl_units       DOUBLE PRECISION,
            mean_dist_to_long_in_hl_units      DOUBLE PRECISION,
            mean_dist_to_longest_in_hl_units   DOUBLE PRECISION,

            bars_since_med_turn                INTEGER,
            bars_since_long_turn               INTEGER,
            bars_since_longest_turn            INTEGER,

            pct_change_since_med_turn          DOUBLE PRECISION,
            pct_change_since_long_turn         DOUBLE PRECISION,
            pct_change_since_longest_turn      DOUBLE PRECISION,

            med_pattern_label      VARCHAR(32)  NOT NULL DEFAULT 'NA',
            long_pattern_label     VARCHAR(32)  NOT NULL DEFAULT 'NA',
            longest_pattern_label  VARCHAR(32)  NOT NULL DEFAULT 'NA',

            med_turn_buy           BOOLEAN      NOT NULL DEFAULT FALSE,
            med_turn_sell          BOOLEAN      NOT NULL DEFAULT FALSE,
            long_turn_buy          BOOLEAN      NOT NULL DEFAULT FALSE,
            long_turn_sell         BOOLEAN      NOT NULL DEFAULT FALSE,
            longest_turn_buy       BOOLEAN      NOT NULL DEFAULT FALSE,
            longest_turn_sell      BOOLEAN      NOT NULL DEFAULT FALSE,

            computed_at        TIMESTAMPTZ      NOT NULL DEFAULT NOW(),

            PRIMARY KEY (industry_name, timeframe, bar_timestamp),

            CONSTRAINT industry_indicator_enrichment_state_fk
                FOREIGN KEY (industry_name, timeframe, bar_timestamp)
                REFERENCES industry_indicator_state
                    (industry_name, timeframe, bar_timestamp)
                ON DELETE CASCADE
        );
    """)

    op.execute("""
        COMMENT ON TABLE industry_indicator_enrichment IS
            'V1 per-industry enrichment fields. Mirror of '
            'stock_indicator_enrichment. 1:1 with '
            'industry_indicator_state via composite FK with '
            'ON DELETE CASCADE.';
    """)

    # =================================================================
    # Grants
    # =================================================================
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trip_etl_user') THEN
                GRANT SELECT, INSERT, UPDATE, DELETE
                    ON stock_indicator_enrichment, industry_indicator_enrichment
                    TO trip_etl_user;
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trip_gui_user') THEN
                GRANT SELECT
                    ON stock_indicator_enrichment, industry_indicator_enrichment
                    TO trip_gui_user;
            END IF;
        END
        $$;
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS industry_indicator_enrichment;")
    op.execute("DROP TABLE IF EXISTS stock_indicator_enrichment;")
