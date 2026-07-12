"""Correct the stale historical_signals table comment.

The existing comment describes a design that no longer exists:

    'V1 append-only audit log of every signal ever fired. No uniqueness beyond
     signal_id BIGSERIAL — rotation across cycles intentionally produces
     duplicate rows while the source bar remains the latest. Read-side
     DISTINCT ON (bar_timestamp, signal_type) dedupes for GUI display.'

Every clause is now false:

  * "No uniqueness beyond signal_id" — migration 0036 added signal_date and a
    UNIQUE (ticker_id, timeframe, signal_date); 0037 made signal_date NOT NULL.
  * "rotation ... intentionally produces duplicate rows" — rotation was removed.
  * "Read-side DISTINCT ON (bar_timestamp, signal_type)" — no query does this.
    The GUI uses DISTINCT ON (ticker_id) ORDER BY computed_at DESC, i.e. the
    ticker's LATEST signal on the current weekly bar, which is correct.

A comment that lies is worse than no comment: this one would lead the next
reader to believe duplicate rows per day are expected, which is exactly the
assumption that hid the per-day signal bug.

Comment-only. No schema change, no data change.
"""
from __future__ import annotations

from alembic import op

revision = "0048_fix_hist_signals_comment"
down_revision = "0047_intraday_signal_log"
branch_labels = None
depends_on = None

_NEW = (
    "V1 signal log at DAILY grain: at most one NET signal per "
    "(ticker_id, timeframe, signal_date), enforced by the UNIQUE constraint. "
    "A signal is emitted when the weekly-chart regime on day D differs from "
    "day D-1 (see stock_indicator_state, which is daily-grain: each row is the "
    "weekly chart recomputed with that day''s forming bar). bar_timestamp is "
    "the IST-Monday WEEK ANCHOR and is shared by every day of the week — never "
    "filter or group by it when a DAY is meant; use signal_date. Within-day "
    "transition sequences (the regime can turn more than once a day as the "
    "forming bar moves) live in intraday_signal_log, not here."
)

_OLD = (
    "V1 append-only audit log of every signal ever fired. No uniqueness "
    "beyond signal_id BIGSERIAL — rotation across cycles intentionally "
    "produces duplicate rows while the source bar remains the latest. "
    "Read-side DISTINCT ON (bar_timestamp, signal_type) dedupes for GUI "
    "display."
)


def upgrade() -> None:
    op.execute(
        f"COMMENT ON TABLE historical_signals IS '{_NEW}'"
    )
    op.execute(
        "COMMENT ON COLUMN historical_signals.signal_date IS "
        "'The trading day the signal fired. DAILY grain. Carries the UNIQUE "
        "constraint with (ticker_id, timeframe). Filter on THIS, not "
        "bar_timestamp.'"
    )
    op.execute(
        "COMMENT ON COLUMN historical_signals.bar_timestamp IS "
        "'IST-Monday week anchor of the week containing signal_date. NOT "
        "unique per signal — several days of one week share it. Using it as a "
        "day bound is a bug (it silently widens to the whole week).'"
    )


def downgrade() -> None:
    op.execute(
        f"COMMENT ON TABLE historical_signals IS '{_OLD}'"
    )
    op.execute(
        "COMMENT ON COLUMN historical_signals.signal_date IS NULL"
    )
    op.execute(
        "COMMENT ON COLUMN historical_signals.bar_timestamp IS NULL"
    )
