"""Create intraday_signal_log: the within-day signal chain.

historical_signals holds ONE net signal per (ticker, timeframe, signal_date)
— the EOD truth, enforced by its UNIQUE constraint. That grain is correct and
stays.

But the weekly chart's regime can turn several times WITHIN a day as the
forming bar moves (close and imputed volume both change on every intraday
tick). Those transitions are real and the operator wants to see them:

    BUY -> EXIT_FROM_BUY -> BUY

They cannot be recorded in historical_signals (the UNIQUE on signal_date
permits only one row per day), and current_signals holds at most one row per
ticker (PK ticker_id) which is overwritten on every tick. So today they are
lost entirely.

This table is the append-only log of that chain. No uniqueness beyond the
BIGSERIAL: several rows per (ticker, signal_date) is the entire point. The
writer suppresses a transition whose signal_type equals the most recent logged
one for that (ticker, signal_date), so consecutive duplicates never appear —
a repeat of the same type is always separated by an intervening signal.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0047_intraday_signal_log"
down_revision = "0046_signal_ensemble_features"
branch_labels = None
depends_on = None

_SIGNAL_TYPES = ("BUY", "SELL", "EXIT_FROM_BUY", "EXIT_FROM_SELL")


def upgrade() -> None:
    op.create_table(
        "intraday_signal_log",
        sa.Column("log_id", sa.BigInteger, primary_key=True,
                  autoincrement=True),
        sa.Column("ticker_id", sa.Integer, nullable=False),
        sa.Column("timeframe", sa.String(8), nullable=False),
        # The trading day the transition belongs to (daily grain, matches
        # historical_signals.signal_date).
        sa.Column("signal_date", sa.Date, nullable=False),
        # IST-Monday week anchor of that day's week (matches the rest of V1).
        sa.Column("bar_timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("signal_type", sa.String(16), nullable=False),
        sa.Column("regime_before", sa.SmallInteger, nullable=False),
        sa.Column("regime_after", sa.SmallInteger, nullable=False),
        sa.Column("price_at_signal", sa.Float, nullable=False),
        # The intraday instant the transition was observed. Ordering key for
        # the chain.
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["ticker_id"], ["tickers.ticker_id"], ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "price_at_signal > 0", name="intraday_signal_log_price_chk",
        ),
        sa.CheckConstraint(
            "regime_before <> regime_after",
            name="intraday_signal_log_regime_changes_chk",
        ),
        sa.CheckConstraint(
            "regime_before IN (-1, 0, 1)",
            name="intraday_signal_log_regime_before_chk",
        ),
        sa.CheckConstraint(
            "regime_after IN (-1, 0, 1)",
            name="intraday_signal_log_regime_after_chk",
        ),
        sa.CheckConstraint(
            "signal_type IN ("
            + ", ".join(f"'{t}'" for t in _SIGNAL_TYPES)
            + ")",
            name="intraday_signal_log_signal_type_chk",
        ),
    )
    # The read path is always "this ticker's chain for this day, in order",
    # and the writer's lookup is "the most recent row for this (ticker, day)".
    # One index serves both.
    op.create_index(
        "ix_intraday_signal_log_ticker_date",
        "intraday_signal_log",
        ["ticker_id", "timeframe", "signal_date", "computed_at"],
    )
    op.create_index(
        "ix_intraday_signal_log_date",
        "intraday_signal_log",
        ["signal_date"],
    )
    op.execute(
        "COMMENT ON TABLE intraday_signal_log IS "
        "'Append-only within-day signal chain. Several rows per "
        "(ticker, signal_date) by design — the weekly-chart regime can turn "
        "more than once a day as the forming bar moves. historical_signals "
        "keeps the ONE net signal per day; this keeps the sequence. Ordered "
        "by computed_at.'"
    )


def downgrade() -> None:
    op.drop_index("ix_intraday_signal_log_date",
                  table_name="intraday_signal_log")
    op.drop_index("ix_intraday_signal_log_ticker_date",
                  table_name="intraday_signal_log")
    op.drop_table("intraday_signal_log")
