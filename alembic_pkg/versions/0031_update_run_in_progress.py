"""Add update_run_in_progress table.

Phase 1a — batch-run dispatcher foundation. Presence of a row in
this table indicates that a batch cycle (intraday or eod) is
currently executing for the given exchange. The row is INSERTED by
the dispatcher just before spawning the cycle subprocess and
DELETED after the subprocess exits (success, failure, or forced
termination).

Design intent (per operator spec):
  - Row presence == in-progress. No `finished_at` column. Prevents
    the class of bug where a crashed run leaves finished_at NULL
    forever and everything downstream thinks a run is still in
    progress (see the Q8 discussion in the design phase).
  - Startup cleanup: the service DELETEs all rows at boot, so a
    crash-then-restart is guaranteed to converge to empty state.
  - Force-reset endpoint: operator can DELETE rows manually via
    POST /v1/admin/etl/reset-in-progress if the dispatcher itself
    is wedged.
  - Unique index on `exchange` future-proofs the schema for the
    per-exchange parallelism planned for a later phase. Today's
    dispatcher only runs one row at a time globally (Q5).

Revision ID: 0031_update_run_in_progress
Revises:     0030_drop_legacy_signal_tables
Created:     Phase 1a
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0031_update_run_in_progress"
down_revision = "0030_drop_legacy_signal_tables"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "update_run_in_progress",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("exchange", sa.Text, nullable=False),
        sa.Column(
            "kind", sa.Text, nullable=False,
        ),
        sa.Column(
            "started_at", sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "pid", sa.Integer, nullable=True,
            comment="OS PID of the spawned cycle subprocess. NULL "
                    "briefly between INSERT and Popen return.",
        ),
        sa.CheckConstraint(
            "kind IN ('intraday', 'eod')",
            name="ck_uripc_kind",
        ),
    )
    op.create_index(
        "ux_uripc_exchange",
        "update_run_in_progress",
        ["exchange"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ux_uripc_exchange", table_name="update_run_in_progress")
    op.drop_table("update_run_in_progress")
