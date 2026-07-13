"""Allow 'UNAVAILABLE' as a source on instrument_shares and quarterly_financials.

A record-of-attempt: we asked the upstream provider, and it has nothing. This is
not the same as never having asked, and the two must be distinguishable.

~107 NSE EQ tickers (microcaps) have no sharesOutstanding on yfinance at all;
fetch_shares raises SharesFetchPermanent for them — "there's nothing to retry".
Without a way to record that, the gap-only sweep re-asks the same dead question
every run, and the fidelity audit scores permanent upstream absence as a data
gap, which means it can never report READY.

instrument_shares.shares_outstanding is already NULLable and insert_snapshot
already documents the NULL as a "record-of-attempt" — the design anticipated
this. What was missing was a source value meaning "we asked; there is nothing",
and a CHECK constraint that permits it.

Rows written with source='UNAVAILABLE' carry NULL in the value column.
market_cap_compute already guards on `shares_outstanding is not None` and counts
those as skipped_no_shares, so such rows cannot leak into a computed market cap.

Constraint-only. No data change, no column change.
"""
from __future__ import annotations

from alembic import op

revision = "0049_unavailable_source"
down_revision = "0048_fix_hist_signals_comment"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE instrument_shares "
        "DROP CONSTRAINT IF EXISTS instrument_shares_source_chk"
    )
    op.execute(
        "ALTER TABLE instrument_shares ADD CONSTRAINT "
        "instrument_shares_source_chk CHECK (shares_source IN ("
        "'YFINANCE', 'YFINANCE_IMPLIED', 'NSE_XBRL', "
        "'MANUAL_OVERRIDE', 'UNAVAILABLE'))"
    )
    op.execute(
        "ALTER TABLE quarterly_financials "
        "DROP CONSTRAINT IF EXISTS quarterly_financials_source_chk"
    )
    op.execute(
        "ALTER TABLE quarterly_financials ADD CONSTRAINT "
        "quarterly_financials_source_chk CHECK (source IN ("
        "'YFINANCE', 'YFINANCE_COMPUTED', 'NSE_XBRL', "
        "'MANUAL_OVERRIDE', 'UNAVAILABLE'))"
    )


def downgrade() -> None:
    # Any UNAVAILABLE rows must go first or the narrowed CHECK will not validate.
    # They are records of absence, so deleting them loses nothing but the memory
    # of having asked.
    op.execute(
        "DELETE FROM instrument_shares WHERE shares_source = 'UNAVAILABLE'"
    )
    op.execute(
        "DELETE FROM quarterly_financials WHERE source = 'UNAVAILABLE'"
    )
    op.execute(
        "ALTER TABLE instrument_shares "
        "DROP CONSTRAINT IF EXISTS instrument_shares_source_chk"
    )
    op.execute(
        "ALTER TABLE instrument_shares ADD CONSTRAINT "
        "instrument_shares_source_chk CHECK (shares_source IN ("
        "'YFINANCE', 'YFINANCE_IMPLIED', 'NSE_XBRL', 'MANUAL_OVERRIDE'))"
    )
    op.execute(
        "ALTER TABLE quarterly_financials "
        "DROP CONSTRAINT IF EXISTS quarterly_financials_source_chk"
    )
    op.execute(
        "ALTER TABLE quarterly_financials ADD CONSTRAINT "
        "quarterly_financials_source_chk CHECK (source IN ("
        "'YFINANCE', 'YFINANCE_COMPUTED', 'NSE_XBRL', 'MANUAL_OVERRIDE'))"
    )
