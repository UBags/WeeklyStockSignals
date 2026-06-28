"""compute_runs + market_overview_daily

Step 13a: data layer for the ETL Status GUI.

Two new tables, no FK chains, no destructive ops:

  - compute_runs: one row per scheduled tick OR backfill invocation.
    Surfaces the activity-log table in the upcoming ETL GUI. Backfills
    write here in addition to backfill_runs (which has destructive-
    lock semantics not relevant to the activity log).

  - market_overview_daily: aggregate counts per (snapshot_date,
    exchange). The 11 metrics feed the GUI's Overall Market Overview
    footer; the [See Trend] dialog reads 90 days of history.

Revision ID: 0029_compute_runs_mkt_overview
Revises:     0028_backfill_runs
Created:     Step 13a (V1 cutover — ETL GUI data layer)
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0029_compute_runs_mkt_overview"
down_revision = "0028_backfill_runs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---- compute_runs ---------------------------------------------------
    op.create_table(
        "compute_runs",
        sa.Column(
            "compute_run_id", sa.Integer,
            primary_key=True, autoincrement=True,
        ),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("exchange", sa.String(16), nullable=False),
        sa.Column(
            "started_at", sa.TIMESTAMP(timezone=True),
            nullable=False, server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "finished_at", sa.TIMESTAMP(timezone=True),
            nullable=True,
        ),
        sa.Column("tickers_attempted", sa.Integer, nullable=True),
        sa.Column("tickers_fetched", sa.Integer, nullable=True),
        sa.Column("failed_stage", sa.String(64), nullable=True),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column("notes", sa.Text, nullable=True),
        sa.CheckConstraint(
            "kind IN ('intraday', 'eod', 'backfill')",
            name="ck_compute_runs_kind",
        ),
    )
    # Read pattern: ORDER BY started_at DESC LIMIT 15 → covering
    # index on started_at speeds up the activity-log query.
    op.create_index(
        "ix_compute_runs_started_at_desc",
        "compute_runs", [sa.text("started_at DESC")],
    )
    # Find in-flight runs (finished_at IS NULL) for crash recovery.
    op.create_index(
        "ix_compute_runs_in_flight",
        "compute_runs", ["exchange", "kind"],
        postgresql_where=sa.text("finished_at IS NULL"),
    )

    # ---- market_overview_daily -----------------------------------------
    op.create_table(
        "market_overview_daily",
        sa.Column("snapshot_date", sa.Date, nullable=False),
        sa.Column("exchange", sa.String(16), nullable=False),
        # Signal counts (from current_signals at this date, scoped to
        # the in-progress weekly bar of snapshot_date)
        sa.Column(
            "n_buys", sa.Integer,
            nullable=False, server_default=sa.text("0"),
        ),
        sa.Column(
            "n_sells", sa.Integer,
            nullable=False, server_default=sa.text("0"),
        ),
        sa.Column(
            "n_exit_buys", sa.Integer,
            nullable=False, server_default=sa.text("0"),
        ),
        sa.Column(
            "n_exit_sells", sa.Integer,
            nullable=False, server_default=sa.text("0"),
        ),
        # Regime counts (from stock_indicator_state.display_regime)
        sa.Column(
            "n_green_regime", sa.Integer,
            nullable=False, server_default=sa.text("0"),
        ),
        sa.Column(
            "n_red_regime", sa.Integer,
            nullable=False, server_default=sa.text("0"),
        ),
        sa.Column(
            "n_neutral_regime", sa.Integer,
            nullable=False, server_default=sa.text("0"),
        ),
        # EMA-envelope position (daily close vs max/min of med/long/
        # longest EMA for the in-progress weekly bar)
        sa.Column(
            "n_above_highest_ema", sa.Integer,
            nullable=False, server_default=sa.text("0"),
        ),
        sa.Column(
            "n_below_lowest_ema", sa.Integer,
            nullable=False, server_default=sa.text("0"),
        ),
        sa.Column(
            "n_between_emas", sa.Integer,
            nullable=False, server_default=sa.text("0"),
        ),
        sa.Column(
            "computed_at", sa.TIMESTAMP(timezone=True),
            nullable=False, server_default=sa.text("NOW()"),
        ),
        sa.PrimaryKeyConstraint(
            "snapshot_date", "exchange",
            name="pk_market_overview_daily",
        ),
    )
    # Trend dialog reads: WHERE exchange = ? AND snapshot_date >= ?
    # ORDER BY snapshot_date. The composite PK already serves this
    # in the snapshot_date direction, but an explicit per-exchange
    # ordering helps the planner when only one exchange is queried.
    op.create_index(
        "ix_market_overview_daily_exch_date",
        "market_overview_daily", ["exchange", "snapshot_date"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_market_overview_daily_exch_date",
        table_name="market_overview_daily",
    )
    op.drop_table("market_overview_daily")
    op.drop_index(
        "ix_compute_runs_in_flight", table_name="compute_runs",
    )
    op.drop_index(
        "ix_compute_runs_started_at_desc", table_name="compute_runs",
    )
    op.drop_table("compute_runs")
