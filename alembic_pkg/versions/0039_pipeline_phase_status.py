"""Create pipeline_phase_status: crash-durable per-phase completion ledger.

The backfill pipeline is long-running (hours) and must be resumable: if it
crashes / is interrupted (power outage, SIGKILL, yfinance wedge) partway, a
re-run should skip phases already COMPLETE and resume from the first phase that
isn't. This table is that ledger.

One row per (exchange, phase_name). status is RUNNING while a phase executes,
COMPLETE only after the phase's function returns successfully, FAILED if it
raised. A crash mid-phase leaves the row at RUNNING (never COMPLETE), so the
next run re-executes that phase — a half-done phase is never mistaken for done.

Downgrade drops the table (functional, for test round-trips; operational policy
remains roll-forward-only).
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0039_pipeline_phase_status"
down_revision = "0038_industry_index_period"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pipeline_phase_status",
        sa.Column("exchange", sa.String(16), nullable=False),
        sa.Column("phase_name", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        # The backfill_runs.run_id that last touched this phase (audit trail).
        sa.Column("backfill_run_id", sa.BigInteger, nullable=True),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("completed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("rows_affected", sa.BigInteger, nullable=True),
        sa.Column("last_error", sa.Text, nullable=True),
        sa.Column(
            "updated_at", sa.TIMESTAMP(timezone=True),
            nullable=False, server_default=sa.text("NOW()"),
        ),
        sa.PrimaryKeyConstraint(
            "exchange", "phase_name",
            name="pipeline_phase_status_pkey",
        ),
        sa.CheckConstraint(
            "status IN ('RUNNING', 'COMPLETE', 'FAILED')",
            name="pps_status_chk",
        ),
    )


def downgrade() -> None:
    op.drop_table("pipeline_phase_status")
