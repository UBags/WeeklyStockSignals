"""signal_event table (Phase 6-1).

Sparse per-event rows derived from indicator_state.display_regime
transitions. One row per (series, trade_date, event_kind), only
on bars where a signal actually fires (most bars produce no rows).

Four event kinds, all derived from display_regime time series:
  - BUY       : display_regime went from non-+1 to +1
  - SELL      : display_regime went from non--1 to -1
  - EXIT_BUY  : display_regime went from +1 to non-+1
  - EXIT_SELL : display_regime went from -1 to non--1

These mirror the operator's Pine Script:
  longCondition      = display_background == 1 and prev != 1
  exitLongCondition  = prev == 1 and display_background != 1
  shortCondition     = display_background == -1 and prev != -1
  exitShortCondition = prev == -1 and display_background != -1

Polymorphic series identity matches indicator_state: nullable
(ticker_id, timeframe) OR nullable industry_name, with the same
XOR constraint. Phase 6-1 only writes industry rows; the schema
supports ticker rows for a future Phase 6+.

Note: this layer doesn't decide "act on signal" — it just records
that the signal fired. A future backtest engine (Phase 6-2) or
strategy filter is responsible for deciding which kinds to consume
(e.g. ignoring SELL and EXIT_SELL in a long-only strategy).

Storage scale: signals are sparse. 60 industries × ~20 regime
transitions/year ≈ 1200 rows/year. Tiny.

Legacy parity: migrations/legacy/022_signal_event.sql.

Revision ID: 0015_signal_event
Revises: 0014_indicator_state
Create Date: 2026-05-22
"""
from __future__ import annotations

from alembic import op


revision = "0015_signal_event"
down_revision = "0014_indicator_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS signal_event (
            -- ---- polymorphic series identity ----
            ticker_id        INTEGER          REFERENCES tickers(ticker_id) ON DELETE CASCADE,
            timeframe        VARCHAR(8),
            industry_name    VARCHAR(128)     REFERENCES industry_index(industry_name) ON DELETE CASCADE,

            trade_date       DATE             NOT NULL,

            -- ---- the event ----
            -- One of: BUY, SELL, EXIT_BUY, EXIT_SELL.
            event_kind       VARCHAR(16)      NOT NULL,

            -- ---- regime transition ----
            -- display_regime values before and after this bar.
            regime_before    SMALLINT         NOT NULL,
            regime_after     SMALLINT         NOT NULL,

            -- ---- close at event ----
            -- The close (or index value) on the bar where the event
            -- fired. For industries this is the index value; for
            -- tickers it's the close price.
            close_at_event   DOUBLE PRECISION NOT NULL,

            -- ---- audit ----
            computed_at      TIMESTAMPTZ      NOT NULL DEFAULT NOW(),

            -- ---- constraints ----
            CONSTRAINT signal_event_series_xor_chk
                CHECK (
                    (ticker_id IS NOT NULL AND timeframe IS NOT NULL AND industry_name IS NULL)
                    OR
                    (ticker_id IS NULL AND timeframe IS NULL AND industry_name IS NOT NULL)
                ),
            CONSTRAINT signal_event_kind_chk
                CHECK (event_kind IN ('BUY', 'SELL', 'EXIT_BUY', 'EXIT_SELL')),
            CONSTRAINT signal_event_regime_before_chk
                CHECK (regime_before IN (-1, 0, 1)),
            CONSTRAINT signal_event_regime_after_chk
                CHECK (regime_after IN (-1, 0, 1))
        );
    """)

    # Two partial unique indexes (one per series kind). Same pattern
    # as indicator_state — NULL columns can't be in a single UNIQUE
    # because NULL != NULL.
    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_signal_event_ticker
            ON signal_event (ticker_id, timeframe, trade_date, event_kind)
            WHERE ticker_id IS NOT NULL;
    """)
    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_signal_event_industry
            ON signal_event (industry_name, trade_date, event_kind)
            WHERE industry_name IS NOT NULL;
    """)

    # Date-axis index for "all signals on date D" queries.
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_signal_event_trade_date
            ON signal_event (trade_date DESC);
    """)
    # Event-kind index for "all BUY events in date range" queries.
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_signal_event_kind_date
            ON signal_event (event_kind, trade_date DESC);
    """)

    op.execute("""
        COMMENT ON TABLE signal_event IS
            'Sparse signal events derived from indicator_state.'
            'display_regime transitions. One row per fire, four '
            'event kinds: BUY, SELL, EXIT_BUY, EXIT_SELL.';
    """)

    # Grants — same pattern as 0006-0014.
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trip_etl_user') THEN
                GRANT SELECT, INSERT, UPDATE, DELETE
                    ON signal_event
                    TO trip_etl_user;
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trip_gui_user') THEN
                GRANT SELECT ON signal_event TO trip_gui_user;
            END IF;
        END
        $$;
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS signal_event;")
