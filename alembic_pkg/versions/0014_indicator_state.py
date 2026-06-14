"""indicator_state table (Phase 5-1).

Per-(series, trade_date) wide row capturing the full Triple-EMA
indicator state from `Weekly - Triple EMA with Background`
(operator's Pine Script). One row per bar per series.

A "series" is either:
  - A ticker's OHLCV bars at a given timeframe (e.g. NSE:RELIANCE
    weekly). Populated via ohlcv_bars JOIN.
  - An industry index's daily value series (Phase 4B output).
    Populated via industry_index_value JOIN.

The schema uses polymorphic columns: nullable `ticker_id` +
nullable `industry_name`, with a CHECK constraint that exactly
one is non-NULL. Downstream JOINs become explicit:
  - WHERE ticker_id IS NOT NULL JOIN tickers
  - WHERE industry_name IS NOT NULL JOIN industry_index

The decision (see docs/FUNDAMENTALS_PHASES_1_TO_8.md §23) was
"polymorphic columns" over "discriminator string", because:
  - Phase 6+ strategy engine wants natural JOINs by source kind.
  - CHECK-constrained nullable columns are well-understood
    Postgres idiom; no application-level interpretation needed.

What this migration adds:
  - indicator_state — the wide indicator row.

Why one wide row, not per-EMA decomposed:
  - Indicators are always computed together; no scenario writes
    EMAs but not ATR or vice versa.
  - The hot read path is "give me full state for series X on
    date D" — one row, one query.
  - ~25 columns is well within Postgres's comfort zone.

Numerical-precision choices:
  - All EMA / ATR / band values: DOUBLE PRECISION. Same precision
    as Pine Script (which uses Pine's `float` ≈ IEEE 754 double).
  - Counters / distances: INTEGER where bounded, DOUBLE PRECISION
    where unbounded.
  - Regime states: SMALLINT (-1, 0, +1).
  - Pattern labels: VARCHAR(32). Allowed values constrained by
    application code, not DB CHECK (the label set could grow).

Storage scale: 2241 NSE EQ tickers × 250 weekly bars/year ≈
560k rows/yr if we run weekly tickers; plus 60 industry indices
× 250 daily bars/yr ≈ 15k rows/yr for indices. Per-day daily
ticker indicators would be ~140M/yr per universe — that's
larger but still Postgres-tractable.

Legacy parity: migrations/legacy/021_indicator_state.sql.

Revision ID: 0014_indicator_state
Revises: 0013_industry_index_value
Create Date: 2026-05-22
"""
from __future__ import annotations

from alembic import op


revision = "0014_indicator_state"
down_revision = "0013_industry_index_value"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS indicator_state (
            -- ---- polymorphic series identity ----
            ticker_id        INTEGER          REFERENCES tickers(ticker_id) ON DELETE CASCADE,
            timeframe        VARCHAR(8),
            industry_name    VARCHAR(128)     REFERENCES industry_index(industry_name) ON DELETE CASCADE,
            trade_date       DATE             NOT NULL,

            -- ---- the three EMAs ----
            -- All three are computed with volume-weighted construction
            -- and Hull-WMA smoothing (see Phase 5-2 design doc).
            med_ema          DOUBLE PRECISION NOT NULL,
            long_ema         DOUBLE PRECISION NOT NULL,
            longest_ema      DOUBLE PRECISION NOT NULL,

            -- ---- ATR + bands ----
            -- atr is the raw ATR(N) value. For industries (single-point
            -- series), this is the synthetic ATR = |Δclose| × √2 per
            -- the operator's specification.
            atr              DOUBLE PRECISION NOT NULL,
            -- Bands: upper = max(3 EMAs) + multiplier × ATR.
            upper_inner_band DOUBLE PRECISION NOT NULL,
            lower_inner_band DOUBLE PRECISION NOT NULL,
            upper_mid_band   DOUBLE PRECISION NOT NULL,
            lower_mid_band   DOUBLE PRECISION NOT NULL,
            upper_outer_band DOUBLE PRECISION NOT NULL,
            lower_outer_band DOUBLE PRECISION NOT NULL,

            -- ---- regime states ----
            -- -1 / 0 / +1. SMALLINT is wide enough; CHECK enforces.
            core_regime      SMALLINT         NOT NULL,
            display_regime   SMALLINT         NOT NULL,

            -- ---- pierce markers (intra-bar wicks crossing mid bands) ----
            pierces_upper_mid BOOLEAN         NOT NULL DEFAULT FALSE,
            pierces_lower_mid BOOLEAN         NOT NULL DEFAULT FALSE,

            -- ---- per-EMA enrichment (the "info table" content) ----
            -- Distance from each EMA, expressed as multiples of the
            -- 26-period SMA of (high - low). Used for the operator
            -- table's "Price to X-Term EMA: N.NN × HL Range Avg" rows.
            -- NULL when the underlying HL-range-avg is not yet defined
            -- (early bars).
            dist_to_med_in_hl_units      DOUBLE PRECISION,
            dist_to_long_in_hl_units     DOUBLE PRECISION,
            dist_to_longest_in_hl_units  DOUBLE PRECISION,
            -- 52-period SMA of the above, also from the operator table.
            mean_dist_to_med_in_hl_units      DOUBLE PRECISION,
            mean_dist_to_long_in_hl_units     DOUBLE PRECISION,
            mean_dist_to_longest_in_hl_units  DOUBLE PRECISION,

            -- ---- bars-since-last-turn per EMA ----
            -- NULL until each EMA has had at least one turn.
            bars_since_med_turn      INTEGER,
            bars_since_long_turn     INTEGER,
            bars_since_longest_turn  INTEGER,
            -- % change since the last turn, signed.
            pct_change_since_med_turn      DOUBLE PRECISION,
            pct_change_since_long_turn     DOUBLE PRECISION,
            pct_change_since_longest_turn  DOUBLE PRECISION,

            -- ---- valley / top pattern labels ----
            -- One of: 'NA', '1 SUCCESSIVE HIGHER VALLEY',
            -- '2 SUCCESSIVE HIGHER VALLEYS', '1 SUCCESSIVE LOWER TOP',
            -- '2 SUCCESSIVE LOWER TOPS'. Per EMA.
            med_pattern_label      VARCHAR(32) NOT NULL DEFAULT 'NA',
            long_pattern_label     VARCHAR(32) NOT NULL DEFAULT 'NA',
            longest_pattern_label  VARCHAR(32) NOT NULL DEFAULT 'NA',

            -- ---- raw turn signals at this bar ----
            -- Did each EMA print a turn at this exact bar? Used by
            -- downstream signal aggregation and by run-bookkeeping.
            med_turn_buy        BOOLEAN NOT NULL DEFAULT FALSE,
            med_turn_sell       BOOLEAN NOT NULL DEFAULT FALSE,
            long_turn_buy       BOOLEAN NOT NULL DEFAULT FALSE,
            long_turn_sell      BOOLEAN NOT NULL DEFAULT FALSE,
            longest_turn_buy    BOOLEAN NOT NULL DEFAULT FALSE,
            longest_turn_sell   BOOLEAN NOT NULL DEFAULT FALSE,

            -- ---- audit ----
            computed_at      TIMESTAMPTZ      NOT NULL DEFAULT NOW(),

            -- ---- constraints ----
            CONSTRAINT indicator_state_series_xor_chk
                CHECK (
                    (ticker_id IS NOT NULL AND timeframe IS NOT NULL AND industry_name IS NULL)
                    OR
                    (ticker_id IS NULL AND timeframe IS NULL AND industry_name IS NOT NULL)
                ),
            CONSTRAINT indicator_state_core_regime_chk
                CHECK (core_regime IN (-1, 0, 1)),
            CONSTRAINT indicator_state_display_regime_chk
                CHECK (display_regime IN (-1, 0, 1)),
            CONSTRAINT indicator_state_atr_chk
                CHECK (atr >= 0),
            CONSTRAINT indicator_state_emas_positive_chk
                CHECK (med_ema > 0 AND long_ema > 0 AND longest_ema > 0)
        );
    """)

    # Unique constraint enforces "one row per (series, trade_date)".
    # Two unique indexes — one for ticker series, one for industry —
    # because Postgres can't put a single UNIQUE on (ticker_id,
    # timeframe, industry_name, trade_date) where some columns are
    # NULL (NULL != NULL, so the UNIQUE wouldn't actually fire).
    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_indicator_state_ticker
            ON indicator_state (ticker_id, timeframe, trade_date)
            WHERE ticker_id IS NOT NULL;
    """)
    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_indicator_state_industry
            ON indicator_state (industry_name, trade_date)
            WHERE industry_name IS NOT NULL;
    """)

    # Date-axis index: "show me all indicator states on date D" is
    # used by cross-series analytics (e.g. "which industries entered
    # regime=+1 today"). Without this, that query would scan the
    # whole table.
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_indicator_state_trade_date
            ON indicator_state (trade_date DESC);
    """)

    op.execute("""
        COMMENT ON TABLE indicator_state IS
            'Per-bar Triple-EMA indicator state. One row per '
            '(series, trade_date). Series is either a ticker '
            '(ticker_id + timeframe) or an industry index '
            '(industry_name) — CHECK enforces exactly one source.';
    """)
    op.execute("""
        COMMENT ON COLUMN indicator_state.core_regime IS
            'Underlying regime: -1 (bearish), 0 (neutral), +1 '
            '(bullish). State machine driven by price vs inner '
            'ATR bands + EMA filters.';
    """)
    op.execute("""
        COMMENT ON COLUMN indicator_state.display_regime IS
            'Display regime: -1 / 0 / +1. Gated by core_regime + '
            'candle direction + outer-band proximity (anti-chase '
            'filter). This is the regime downstream strategy '
            'consumers actually read.';
    """)

    # Grants — same pattern as 0006-0013.
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trip_etl_user') THEN
                GRANT SELECT, INSERT, UPDATE, DELETE
                    ON indicator_state
                    TO trip_etl_user;
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trip_gui_user') THEN
                GRANT SELECT ON indicator_state TO trip_gui_user;
            END IF;
        END
        $$;
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS indicator_state;")
