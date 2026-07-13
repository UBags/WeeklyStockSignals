"""fundamentals_fetch_status: what we asked the provider, and what it said.

A record of OUR FETCHING, kept beside the data tables rather than inside them.

instrument_shares and quarterly_financials hold facts about the COMPANY. "yfinance
has no sharesOutstanding for this ticker" is a fact about US. Storing the second
inside the first forces a lie:

  * quarterly_financials is keyed (ticker_id, fiscal_quarter) — there is no date
    meaning "none", so a sentinel would invent a quarter that never existed;
  * SharesRepository.get_latest is ORDER BY as_of_date DESC LIMIT 1, so a
    today-dated sentinel would SHADOW a ticker's real historical shares —
    market_cap_compute would skip it (skipped_no_shares) and a healthy large cap
    would silently leave every industry index.

Separating them makes that bug structurally impossible instead of merely guarded.
etl_ticker_status already does exactly this for the OHLCV path.

Only UNAVAILABLE is recorded: the provider was asked and has nothing
(SharesFetchPermanent / FinancialsFetchPermanent / IndustryFetchPermanent —
"there's nothing to retry"). Transient failures are not persisted; they retry
naturally and would only accumulate as noise.

No expiry, by design. The monthly job re-asks and updates last_checked_at, so this
is a record of what we learned, not a cache. A microcap yfinance ignores today may
gain coverage next quarter; a negative cache that never expires is a lie with a
longer shelf life. first_seen_at and attempts say how long a ticker has been dark.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0049_fundamentals_fetch_status"
down_revision = "0048_fix_hist_signals_comment"
branch_labels = None
depends_on = None

_DATASETS = ("shares", "financials", "industry")
_STATUSES = ("UNAVAILABLE",)


def upgrade() -> None:
    op.create_table(
        "fundamentals_fetch_status",
        sa.Column("ticker_id", sa.Integer, nullable=False),
        # Which reference dataset this verdict is about.
        sa.Column("dataset", sa.String(16), nullable=False),
        # UNAVAILABLE only, for now. A permanent, provider-side absence.
        sa.Column("status", sa.String(16), nullable=False),
        # The provider's own words, so a human can tell a genuine absence from a
        # parser bug without re-running the fetch.
        sa.Column("reason", sa.Text, nullable=True),
        # How long this ticker has been dark, and how often we have re-asked.
        sa.Column(
            "first_seen_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "last_checked_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "attempts", sa.Integer, nullable=False, server_default=sa.text("1"),
        ),
        sa.PrimaryKeyConstraint("ticker_id", "dataset",
                                name="fundamentals_fetch_status_pkey"),
        sa.ForeignKeyConstraint(
            ["ticker_id"], ["tickers.ticker_id"], ondelete="CASCADE",
            name="fundamentals_fetch_status_ticker_id_fkey",
        ),
        sa.CheckConstraint(
            "dataset IN ('shares', 'financials', 'industry')",
            name="ffs_dataset_chk",
        ),
        sa.CheckConstraint(
            "status IN ('UNAVAILABLE')",
            name="ffs_status_chk",
        ),
    )
    # The hot read: "which tickers on this exchange are known-dark for shares?"
    # runs on every gap-only sweep and every audit.
    op.create_index(
        "ix_ffs_dataset_status",
        "fundamentals_fetch_status",
        ["dataset", "status"],
    )
    op.execute(
        "COMMENT ON TABLE fundamentals_fetch_status IS "
        "'What we asked the fundamentals provider and what it said. A record of "
        "OUR FETCHING, deliberately kept OUT of instrument_shares / "
        "quarterly_financials, which hold facts about the COMPANY. "
        "UNAVAILABLE = provider was asked and has nothing (a *Permanent fetch "
        "error). Transient failures are not recorded. No expiry: the monthly "
        "re-check updates last_checked_at, so this is a record of what we "
        "learned, not a cache.'"
    )


def downgrade() -> None:
    op.drop_index("ix_ffs_dataset_status", table_name="fundamentals_fetch_status")
    op.drop_table("fundamentals_fetch_status")
