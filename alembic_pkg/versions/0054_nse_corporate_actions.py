"""nse_corporate_actions: the authoritative NSE corporate-action feed.

One row per ATOMIC action. The nselib feed's free-text `subject` is parsed
(trip_ema.etl.nse_actions_parser) into typed actions; a compound event
(e.g. bonus + face-value split on one ex-date) becomes two rows. Scale
events (split / consolidation / bonus) carry a reduced NEW:OLD share ratio
and a price_factor (= ratio_denominator / ratio_numerator); value-transfer
events (demerger / capital_reduction) are flagged `censor`; dividends carry
their amount in `detail`. The full source row is retained in `raw_payload`
so the table can be re-parsed without re-fetching.

This is the source of truth the OHLCV seam corrector reconciles against:
it reads factor rows (censor = false, price_factor NOT NULL) grouped by
(ticker_id, ex_date). Kept SEPARATE from `corporate_actions` (which holds
yfinance-sourced rows + reconciler candidates) — this one is the raw
authoritative NSE feed, keyed on isin.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0054_nse_corporate_actions"
down_revision = "0053_signal_enrichment_validity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "nse_corporate_actions",
        sa.Column("action_id", sa.BigInteger, primary_key=True),
        # Resolved from isin; nullable — the feed carries names not yet (or no
        # longer) in `tickers`, and the audit row is kept regardless.
        sa.Column("ticker_id", sa.Integer, nullable=True),
        # The feed's natural key. tickers.isin is CHAR(12) with a partial unique
        # index; we keep the isin here even when ticker_id can't be resolved.
        sa.Column("isin", sa.String(12), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column(
            "exchange", sa.String(8), nullable=False,
            server_default=sa.text("'NSE'"),
        ),
        # The ex-date — the price-adjustment boundary the corrector anchors on.
        sa.Column("ex_date", sa.Date, nullable=False),
        # Parser outputs.
        sa.Column("action_type", sa.String(24), nullable=False),
        sa.Column("detail", sa.Text, nullable=True),
        # NEW:OLD shares, reduced. NULL for non-scale actions.
        sa.Column("ratio_numerator", sa.Integer, nullable=True),
        sa.Column("ratio_denominator", sa.Integer, nullable=True),
        # = ratio_denominator / ratio_numerator (post/pre price). NULL for
        # non-scale actions. The exact value the corrector snaps R to.
        sa.Column("price_factor", sa.Float, nullable=True),
        # Real value transfer (demerger / capital reduction) — continuity is
        # WRONG here; the reconcile censors the crossing window instead of
        # splicing.
        sa.Column(
            "censor", sa.Boolean, nullable=False, server_default=sa.text("false"),
        ),
        # Raw feed text + full source row, so the table re-parses without a
        # re-fetch.
        sa.Column("subject", sa.Text, nullable=False),
        sa.Column(
            "source", sa.String(16), nullable=False,
            server_default=sa.text("'nselib'"),
        ),
        sa.Column("raw_payload", postgresql.JSONB, nullable=True),
        sa.Column(
            "detected_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.text("NOW()"),
        ),
        sa.ForeignKeyConstraint(
            ["ticker_id"], ["tickers.ticker_id"], ondelete="SET NULL",
            name="nse_corporate_actions_ticker_id_fkey",
        ),
    )

    # Idempotent monthly upsert key. NULLS NOT DISTINCT (PG15+) so dividend /
    # non-scale rows (NULL ratios) still dedupe on (isin, ex_date, type,
    # md5(subject)); a REVISED subject hashes differently and lands as a new
    # row (revision, not duplicate) — which is the intent.
    op.create_index(
        "uq_nse_corp_actions_dedupe",
        "nse_corporate_actions",
        [
            "isin", "ex_date", "action_type",
            "ratio_numerator", "ratio_denominator",
            sa.text("md5(subject)"),
        ],
        unique=True,
        postgresql_nulls_not_distinct=True,
    )
    # Hot read for the reconcile: a ticker's scale/censor events by date.
    op.create_index(
        "ix_nse_corp_actions_ticker_exdate",
        "nse_corporate_actions",
        ["ticker_id", "ex_date"],
    )
    # Resolve-on-refresh + audit by isin.
    op.create_index(
        "ix_nse_corp_actions_isin",
        "nse_corporate_actions",
        ["isin"],
    )

    op.execute(
        "COMMENT ON TABLE nse_corporate_actions IS "
        "'Authoritative NSE corporate-action feed (nselib), one row per atomic "
        "action. Parsed from the feed subject: scale events carry a reduced "
        "NEW:OLD share ratio + price_factor; demerger/capital_reduction are "
        "censor=true; dividends carry amount in detail. raw_payload keeps the "
        "full source row for re-parse. The OHLCV corrector reconciles against "
        "the factor rows (price_factor NOT NULL, censor=false) by "
        "(ticker_id, ex_date).'"
    )


def downgrade() -> None:
    op.drop_index("ix_nse_corp_actions_isin", table_name="nse_corporate_actions")
    op.drop_index(
        "ix_nse_corp_actions_ticker_exdate", table_name="nse_corporate_actions"
    )
    op.drop_index(
        "uq_nse_corp_actions_dedupe", table_name="nse_corporate_actions"
    )
    op.drop_table("nse_corporate_actions")
