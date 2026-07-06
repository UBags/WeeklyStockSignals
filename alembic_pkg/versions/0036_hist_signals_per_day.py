"""historical_signals: per-day grain — add signal_date + (ticker,tf,date) unique

Revision ID: 0036_hist_signals_per_day
Revises: 0035_daily_grain_industry
Create Date: 2026-07-06

Per-day signal model: signals are re-derived every processing day and the day
is delete-then-rewritten (idempotent), so historical_signals goes from
append-only (surrogate signal_id PK) to one logical row per
(ticker_id, timeframe, signal_date).

signal_date is added NULLABLE on purpose. The rotation primitive
(rotate_current_to_historical) still runs until the orchestration commit
removes it, and it INSERTs into historical_signals via raw INSERT...SELECT
without a signal_date. Nullable keeps that path — and the whole suite — green
during the transition. A LATER migration tightens signal_date to NOT NULL once
rotation is gone and every writer sets it.

Downgrade policy: operational discipline is ROLL-FORWARD-ONLY — never run a
downgrade against production; correct a bad migration with a new forward one.
downgrade() below is nonetheless a real, exact reverse of upgrade(), because
the migration test suite round-trips every migration (downgrade then upgrade)
against a throwaway database and that contract must hold. "Don't downgrade
prod" lives in the runbook, not as a raise that would also block the tests.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0036_hist_signals_per_day"
down_revision = "0035_daily_grain_industry"
branch_labels = None
depends_on = None

_UNIQUE = "historical_signals_ticker_tf_date_key"
_INDEX = "ix_historical_signals_signal_date"


def upgrade() -> None:
    # Observation day D the signal was recorded for (= stock_indicator_state.date
    # of the producing row), NOT bar_timestamp. Nullable for the transition.
    op.add_column(
        "historical_signals",
        sa.Column("signal_date", sa.Date(), nullable=True),
    )
    # Per-day grain: at most one signal per ticker/timeframe/day. This is the key
    # the daily delete-then-write is scoped on. (NULLs are distinct in Postgres,
    # so transitional rotation rows with NULL signal_date don't collide.)
    op.create_unique_constraint(
        _UNIQUE,
        "historical_signals",
        ["ticker_id", "timeframe", "signal_date"],
    )
    op.create_index(_INDEX, "historical_signals", ["signal_date"])


def downgrade() -> None:
    # Exact reverse of upgrade(), in reverse order. See module docstring:
    # this is for test round-trips / local rebuilds, not production use.
    op.drop_index(_INDEX, table_name="historical_signals")
    op.drop_constraint(_UNIQUE, "historical_signals", type_="unique")
    op.drop_column("historical_signals", "signal_date")
