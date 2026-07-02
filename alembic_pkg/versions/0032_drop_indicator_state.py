"""Drop legacy indicator_state table.

Phase 3d — legacy demolition. The `indicator_state` table's writers
were removed in earlier V1-cutover steps. The last remaining readers
were `_load_latest_indicator_states` in `trip_ema/api/lifecycle.py`
(called by `prime_cache_from_db` at boot and by
`admin_reprime_ticker`). Both readers were demolished alongside
this revision (see the corresponding Phase 3d patcher).

Pre-conditions:
  - No trip_ema/ module references `indicator_state` as a SQL
    identifier in a live code path.
  - No other table has an inbound FK referencing indicator_state
    (verified against DDL — indicator_state only has OUTgoing FKs
    to tickers and industry_index).

Post-conditions:
  - indicator_state table + its indexes + its check constraints +
    its two outbound FKs are gone.
  - tickers and industry_index untouched; only the pointing FKs
    are dropped by CASCADE.
  - SignalCache's `latest_indicator_state` field is also gone
    (via the code patcher); the cache remains a shell that only
    tracks `stale_since` per (ticker_id, timeframe). Both admin
    endpoints (`/prime-all`, `/reprime-ticker`, `/stale-tickers`)
    keep returning 200 for wire compat but now report degenerate
    results.

Reversibility: schema-only. downgrade() recreates an empty
indicator_state table with the same columns + constraints. Data is
NOT restored — recover from a database backup if needed. Same
pattern as 0030_drop_legacy_signal_tables.

Revision ID: 0032_drop_indicator_state
Revises:     0031_update_run_in_progress
Created:     Phase 3d
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0032_drop_indicator_state"
down_revision = "0031_update_run_in_progress"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # DROP TABLE CASCADE handles:
    #   - idx_indicator_state_trade_date
    #   - uq_indicator_state_industry
    #   - uq_indicator_state_ticker
    #   - indicator_state_atr_chk / display_regime_chk /
    #     emas_positive_chk / series_xor_chk
    #   - indicator_state_industry_name_fkey (points to industry_index)
    #   - indicator_state_ticker_id_fkey (points to tickers)
    # Nothing points AT indicator_state, so CASCADE only affects
    # objects owned by indicator_state itself.
    op.execute("DROP TABLE IF EXISTS public.indicator_state CASCADE")


def downgrade() -> None:
    """Recreate the schema (empty). Full column set + constraints,
    matching the pre-3d DDL exactly. Data recovery requires a DB
    backup."""
    op.create_table(
        "indicator_state",
        sa.Column("ticker_id", sa.Integer, nullable=True),
        sa.Column("timeframe", sa.String(8), nullable=True),
        sa.Column("industry_name", sa.String(128), nullable=True),
        sa.Column("trade_date", sa.Date, nullable=False),
        sa.Column("med_ema", sa.Float, nullable=False),
        sa.Column("long_ema", sa.Float, nullable=False),
        sa.Column("longest_ema", sa.Float, nullable=False),
        sa.Column("atr", sa.Float, nullable=False),
        sa.Column("upper_inner_band", sa.Float, nullable=False),
        sa.Column("lower_inner_band", sa.Float, nullable=False),
        sa.Column("upper_mid_band", sa.Float, nullable=False),
        sa.Column("lower_mid_band", sa.Float, nullable=False),
        sa.Column("upper_outer_band", sa.Float, nullable=False),
        sa.Column("lower_outer_band", sa.Float, nullable=False),
        sa.Column("display_regime", sa.SmallInteger, nullable=False),
        sa.Column(
            "pierces_upper_mid", sa.Boolean, nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "pierces_lower_mid", sa.Boolean, nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("dist_to_med_in_hl_units", sa.Float, nullable=True),
        sa.Column("dist_to_long_in_hl_units", sa.Float, nullable=True),
        sa.Column("dist_to_longest_in_hl_units", sa.Float, nullable=True),
        sa.Column("mean_dist_to_med_in_hl_units", sa.Float, nullable=True),
        sa.Column("mean_dist_to_long_in_hl_units", sa.Float, nullable=True),
        sa.Column("mean_dist_to_longest_in_hl_units", sa.Float, nullable=True),
        sa.Column("bars_since_med_turn", sa.Integer, nullable=True),
        sa.Column("bars_since_long_turn", sa.Integer, nullable=True),
        sa.Column("bars_since_longest_turn", sa.Integer, nullable=True),
        sa.Column("pct_change_since_med_turn", sa.Float, nullable=True),
        sa.Column("pct_change_since_long_turn", sa.Float, nullable=True),
        sa.Column("pct_change_since_longest_turn", sa.Float, nullable=True),
        sa.Column(
            "med_pattern_label", sa.String(32), nullable=False,
            server_default=sa.text("'NA'"),
        ),
        sa.Column(
            "long_pattern_label", sa.String(32), nullable=False,
            server_default=sa.text("'NA'"),
        ),
        sa.Column(
            "longest_pattern_label", sa.String(32), nullable=False,
            server_default=sa.text("'NA'"),
        ),
        sa.Column(
            "med_turn_buy", sa.Boolean, nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "med_turn_sell", sa.Boolean, nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "long_turn_buy", sa.Boolean, nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "long_turn_sell", sa.Boolean, nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "longest_turn_buy", sa.Boolean, nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "longest_turn_sell", sa.Boolean, nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "computed_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "atr >= 0", name="indicator_state_atr_chk",
        ),
        sa.CheckConstraint(
            "display_regime IN (-1, 0, 1)",
            name="indicator_state_display_regime_chk",
        ),
        sa.CheckConstraint(
            "med_ema > 0 AND long_ema > 0 AND longest_ema > 0",
            name="indicator_state_emas_positive_chk",
        ),
        sa.CheckConstraint(
            "(ticker_id IS NOT NULL AND timeframe IS NOT NULL "
            "AND industry_name IS NULL) OR "
            "(ticker_id IS NULL AND timeframe IS NULL "
            "AND industry_name IS NOT NULL)",
            name="indicator_state_series_xor_chk",
        ),
        sa.ForeignKeyConstraint(
            ["ticker_id"], ["tickers.ticker_id"],
            ondelete="CASCADE",
            name="indicator_state_ticker_id_fkey",
        ),
        sa.ForeignKeyConstraint(
            ["industry_name"], ["industry_index.industry_name"],
            ondelete="CASCADE",
            name="indicator_state_industry_name_fkey",
        ),
    )
    op.create_index(
        "idx_indicator_state_trade_date", "indicator_state",
        [sa.text("trade_date DESC")],
    )
    op.create_index(
        "uq_indicator_state_ticker", "indicator_state",
        ["ticker_id", "timeframe", "trade_date"],
        unique=True,
        postgresql_where=sa.text("ticker_id IS NOT NULL"),
    )
    op.create_index(
        "uq_indicator_state_industry", "indicator_state",
        ["industry_name", "trade_date"],
        unique=True,
        postgresql_where=sa.text("industry_name IS NOT NULL"),
    )
    op.execute(
        "GRANT SELECT ON TABLE public.indicator_state TO trip_gui_user"
    )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.indicator_state TO trip_etl_user"
    )
