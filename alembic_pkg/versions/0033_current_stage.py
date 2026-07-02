"""Add compute_runs.current_stage — live per-stage progress marker.

Phase 3f — auto-run stage visibility. Manual (GUI-spawned) cycle runs
drive the ETL Status stage panel from QProcess stdout. Scheduler-driven
(auto) runs Popen the identical cycle subprocess from inside the service
process, so their stdout is unreachable by the GUI. This column lets
cmd_cycle publish the currently-running stage to the DB, where the GUI's
existing mutex-tick poll can read it.

`current_stage` holds the INTERNAL stage token (e.g. 'fetch_bars', 'iiv',
'notify') — the same token that appears inside the [...] of cmd_cycle's
stdout markers; the GUI maps it to a display label via
STAGE_NAME_TO_LABEL. NULL means no stage is currently active (run not
yet started, or already finished — finish() nulls it).

Additive + nullable -> zero-downtime; pre-3f code ignores the column.

Revision ID: 0033_current_stage
Revises:     0032_drop_indicator_state
Created:     Phase 3f
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0033_current_stage"
down_revision = "0032_drop_indicator_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "compute_runs",
        sa.Column("current_stage", sa.String(64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("compute_runs", "current_stage")
