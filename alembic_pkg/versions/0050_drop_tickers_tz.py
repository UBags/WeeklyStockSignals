"""drop tickers.tz — a column that lied.

    SELECT tz, COUNT(*) FROM tickers GROUP BY tz;
     tz  | count
    -----+-------
     UTC |  2685

Every NSE ticker claimed to be in UTC.

The column was never authoritative. It was passed as `fallback_tz` to
market_session_for(ticker.ref.exchange, tz), and for any KNOWN exchange the
_EXCHANGE_SPECS entry wins and the value is ignored. Its only effect was on the
UNKNOWN-exchange path, where it produced a generic 09:00-16:00 session in 'UTC' —
silently plausible, and wrong. 'UTC' was the worst possible value it could hold.

market_session_for now RAISES on an unknown exchange (if you do not know when the
market opens you cannot prorate its volume), so the fallback is gone and with it
the last reader of this column. config.yaml's market_hours is the single source of
truth for what a market is.

A column that lies is worse than no column.

Revision ID: 0050_drop_tickers_tz
Revises: 0049_fundamentals_fetch_status
"""
from alembic import op
import sqlalchemy as sa

revision = "0050_drop_tickers_tz"
down_revision = "0049_fundamentals_fetch_status"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("tickers", "tz")


def downgrade() -> None:
    # Restored with its original default. The value was never meaningful — it said
    # UTC for 2,685 Indian equities — so there is nothing to reconstruct.
    op.add_column(
        "tickers",
        sa.Column(
            "tz", sa.Text(), nullable=False, server_default=sa.text("'UTC'"),
        ),
    )
