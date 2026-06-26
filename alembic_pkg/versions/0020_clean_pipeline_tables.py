"""Clean pipeline V1 — new tables alongside legacy.

Introduces the four tables that drive the V1 architecture (see
docs/architecture/CLEAN_PIPELINE_V1.md and the broader
CLEAN_PIPELINE_REFACTOR.md plan). The legacy `indicator_state`
(polymorphic) and `signal_events` (unified plural) tables are NOT
touched by this migration — they keep their writers until Step 14
(demolitions) replaces those callers. Migration 0023 (Step 18)
drops the legacy tables once nothing reads or writes them.

What this migration adds:

  - **stock_indicator_state** — per-ticker EMA/ATR/regime trajectory.
    Replaces the ticker rows of the polymorphic `indicator_state`.
    PK: (ticker_id, timeframe, bar_timestamp). The `timeframe` column
    is retained for future multi-timeframe support (Phase 16); V1
    writes only WEEKLY ('1W').

  - **industry_indicator_state** — per-industry EMA/ATR/regime
    trajectory. Replaces the industry rows of `indicator_state`.
    PK: (industry_name, timeframe, bar_timestamp). Synthetic ATR
    semantics for single-value index series (Phase 1B convention).

  - **current_signals** — exactly one row per ticker that experienced
    a regime change in the current cycle's compute step. PK:
    ticker_id. Rotated wholesale into `historical_signals` at the
    start of each cycle's signal step (Step 6 of the plan).

  - **historical_signals** — append-only audit log of every signal
    ever fired. PK: `signal_id BIGSERIAL`. No other uniqueness —
    rotation across cycles intentionally produces duplicate
    (ticker, bar_timestamp, signal_type) rows when the same regime
    change is re-detected on a still-active in-progress bar.

Why `bar_timestamp TIMESTAMPTZ` (not `trade_date DATE`):
  Legacy `indicator_state` used `trade_date DATE`; `signal_events`
  used `bar_timestamp TIMESTAMPTZ`. The new tables align on
  TIMESTAMPTZ because:
    1. Carries time-of-day, which matters for future intraday
       timeframes (15m, 1h, 4h — Phase 16).
    2. Pine's IST-Monday-anchored weekly bar is naturally a
       TIMESTAMPTZ at 00:00 IST (Postgres renders this as 05:30
       in UTC — known and expected).
    3. Matches the signal tables for cross-table coherence.

Why no FK from signals to indicator-state tables:
  Signals are derived from indicator state but logically
  independent. An indicator-state row can exist without ever
  producing a signal; signals can survive after their source
  indicator row is replaced by a recompute. Loose coupling.

Why no FK from `notification_log` to the new signal tables:
  Step 10's `notification_log` denormalization drops the FK to old
  `signal_events` entirely; rows will carry ticker_id +
  bar_timestamp + signal_type directly. That migration (0022) is
  independent from this one.

CHECK constraints:
  - display_regime ∈ {-1, 0, 1}
  - atr >= 0
  - EMAs > 0
  - signal_type ∈ {BUY, SELL, EXIT_FROM_BUY, EXIT_FROM_SELL}
  - regime_before ∈ {-1, 0, 1} and regime_after ∈ {-1, 0, 1}
  - regime_before != regime_after (a signal IS a regime change;
    same-regime rows would be a bug)
  - price_at_signal > 0

Indexes:
  - stock_indicator_state:
      (bar_timestamp DESC) — global recent-bar scans
      (ticker_id, bar_timestamp DESC) — per-ticker latest, history
  - industry_indicator_state:
      (bar_timestamp DESC), (industry_name, bar_timestamp DESC)
  - historical_signals:
      (ticker_id, bar_timestamp DESC, computed_at DESC) —
          per-ticker "last N signals" query
      (bar_timestamp DESC, computed_at DESC) — global "recent
          signals" scans

Grants:
  - trip_etl_user: SELECT, INSERT, UPDATE, DELETE on all four,
    plus USAGE on `historical_signals_signal_id_seq`.
  - trip_gui_user: SELECT on all four.

Legacy parity: `migrations/legacy/024_clean_pipeline_tables.sql`.
(Note: if 0019 already shipped a legacy parity file at slot 024,
bump this one to 025 and adjust the legacy README catalogue.)

Revision ID: 0020_clean_pipeline_tables
Revises: 0019_drop_signal_industry
Create Date: 2026-06-26
"""
from __future__ import annotations

from alembic import op


revision = "0020_clean_pipeline_tables"
down_revision = "0019_drop_signal_industry"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ====================================================
    # stock_indicator_state
    # ====================================================
    op.execute("""
        CREATE TABLE IF NOT EXISTS stock_indicator_state (
            ticker_id          INTEGER          NOT NULL
                                                REFERENCES tickers(ticker_id) ON DELETE CASCADE,
            timeframe          VARCHAR(8)       NOT NULL,
            bar_timestamp      TIMESTAMPTZ      NOT NULL,

            -- The three EMAs.
            med_ema            DOUBLE PRECISION NOT NULL,
            long_ema           DOUBLE PRECISION NOT NULL,
            longest_ema        DOUBLE PRECISION NOT NULL,

            -- ATR.
            atr                DOUBLE PRECISION NOT NULL,

            -- Bands derived from EMAs + ATR.
            upper_inner_band   DOUBLE PRECISION NOT NULL,
            lower_inner_band   DOUBLE PRECISION NOT NULL,
            upper_mid_band     DOUBLE PRECISION NOT NULL,
            lower_mid_band     DOUBLE PRECISION NOT NULL,
            upper_outer_band   DOUBLE PRECISION NOT NULL,
            lower_outer_band   DOUBLE PRECISION NOT NULL,

            -- Regime classification for this bar.
            display_regime     SMALLINT         NOT NULL,

            -- Bookkeeping.
            computed_at        TIMESTAMPTZ      NOT NULL DEFAULT NOW(),

            PRIMARY KEY (ticker_id, timeframe, bar_timestamp),

            CONSTRAINT stock_indicator_state_display_regime_chk
                CHECK (display_regime IN (-1, 0, 1)),
            CONSTRAINT stock_indicator_state_atr_chk
                CHECK (atr >= 0),
            CONSTRAINT stock_indicator_state_emas_positive_chk
                CHECK (med_ema > 0 AND long_ema > 0 AND longest_ema > 0)
        );
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_stock_indicator_state_bar_ts
            ON stock_indicator_state (bar_timestamp DESC);
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_stock_indicator_state_ticker_bar
            ON stock_indicator_state (ticker_id, bar_timestamp DESC);
    """)
    op.execute("""
        COMMENT ON TABLE stock_indicator_state IS
            'V1 per-ticker EMA/ATR/regime trajectory. PK (ticker_id, '
            'timeframe, bar_timestamp). Replaces ticker rows of the '
            'polymorphic indicator_state. Upserted incrementally each '
            'cycle (last N weeks) or fully via Step 17 regenerate CLI.';
    """)

    # ====================================================
    # industry_indicator_state
    # ====================================================
    op.execute("""
        CREATE TABLE IF NOT EXISTS industry_indicator_state (
            industry_name      VARCHAR(128)     NOT NULL
                                                REFERENCES industry_index(industry_name) ON DELETE CASCADE,
            timeframe          VARCHAR(8)       NOT NULL,
            bar_timestamp      TIMESTAMPTZ      NOT NULL,

            med_ema            DOUBLE PRECISION NOT NULL,
            long_ema           DOUBLE PRECISION NOT NULL,
            longest_ema        DOUBLE PRECISION NOT NULL,

            -- Synthetic ATR for single-value index series.
            atr                DOUBLE PRECISION NOT NULL,

            upper_inner_band   DOUBLE PRECISION NOT NULL,
            lower_inner_band   DOUBLE PRECISION NOT NULL,
            upper_mid_band     DOUBLE PRECISION NOT NULL,
            lower_mid_band     DOUBLE PRECISION NOT NULL,
            upper_outer_band   DOUBLE PRECISION NOT NULL,
            lower_outer_band   DOUBLE PRECISION NOT NULL,

            display_regime     SMALLINT         NOT NULL,

            computed_at        TIMESTAMPTZ      NOT NULL DEFAULT NOW(),

            PRIMARY KEY (industry_name, timeframe, bar_timestamp),

            CONSTRAINT industry_indicator_state_display_regime_chk
                CHECK (display_regime IN (-1, 0, 1)),
            CONSTRAINT industry_indicator_state_atr_chk
                CHECK (atr >= 0),
            CONSTRAINT industry_indicator_state_emas_positive_chk
                CHECK (med_ema > 0 AND long_ema > 0 AND longest_ema > 0)
        );
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_industry_indicator_state_bar_ts
            ON industry_indicator_state (bar_timestamp DESC);
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_industry_indicator_state_industry_bar
            ON industry_indicator_state (industry_name, bar_timestamp DESC);
    """)
    op.execute("""
        COMMENT ON TABLE industry_indicator_state IS
            'V1 per-industry EMA/ATR/regime trajectory. PK '
            '(industry_name, timeframe, bar_timestamp). Replaces '
            'industry rows of the polymorphic indicator_state. '
            'Synthetic ATR semantics (Phase 1B).';
    """)

    # ====================================================
    # current_signals — this cycle's regime-change events
    # ====================================================
    op.execute("""
        CREATE TABLE IF NOT EXISTS current_signals (
            ticker_id          INTEGER          PRIMARY KEY
                                                REFERENCES tickers(ticker_id) ON DELETE CASCADE,
            timeframe          VARCHAR(8)       NOT NULL,
            bar_timestamp      TIMESTAMPTZ      NOT NULL,
            signal_type        VARCHAR(16)      NOT NULL,
            regime_before      SMALLINT         NOT NULL,
            regime_after       SMALLINT         NOT NULL,
            price_at_signal    DOUBLE PRECISION NOT NULL,
            computed_at        TIMESTAMPTZ      NOT NULL DEFAULT NOW(),

            CONSTRAINT current_signals_signal_type_chk
                CHECK (signal_type IN ('BUY', 'SELL', 'EXIT_FROM_BUY', 'EXIT_FROM_SELL')),
            CONSTRAINT current_signals_regime_before_chk
                CHECK (regime_before IN (-1, 0, 1)),
            CONSTRAINT current_signals_regime_after_chk
                CHECK (regime_after IN (-1, 0, 1)),
            CONSTRAINT current_signals_regime_changes_chk
                CHECK (regime_before <> regime_after),
            CONSTRAINT current_signals_price_chk
                CHECK (price_at_signal > 0)
        );
    """)
    op.execute("""
        COMMENT ON TABLE current_signals IS
            'V1 signals fired in the CURRENT cycle only. One row per '
            'ticker that just changed regime. PK ticker_id (max one '
            'row per ticker). Rotated wholesale into historical_signals '
            'at start of next cycle. Typical size < 100 rows.';
    """)

    # ====================================================
    # historical_signals — append-only audit log
    # ====================================================
    op.execute("""
        CREATE TABLE IF NOT EXISTS historical_signals (
            signal_id          BIGSERIAL        PRIMARY KEY,
            ticker_id          INTEGER          NOT NULL
                                                REFERENCES tickers(ticker_id) ON DELETE CASCADE,
            timeframe          VARCHAR(8)       NOT NULL,
            bar_timestamp      TIMESTAMPTZ      NOT NULL,
            signal_type        VARCHAR(16)      NOT NULL,
            regime_before      SMALLINT         NOT NULL,
            regime_after       SMALLINT         NOT NULL,
            price_at_signal    DOUBLE PRECISION NOT NULL,
            computed_at        TIMESTAMPTZ      NOT NULL,
            archived_at        TIMESTAMPTZ      NOT NULL DEFAULT NOW(),

            CONSTRAINT historical_signals_signal_type_chk
                CHECK (signal_type IN ('BUY', 'SELL', 'EXIT_FROM_BUY', 'EXIT_FROM_SELL')),
            CONSTRAINT historical_signals_regime_before_chk
                CHECK (regime_before IN (-1, 0, 1)),
            CONSTRAINT historical_signals_regime_after_chk
                CHECK (regime_after IN (-1, 0, 1)),
            CONSTRAINT historical_signals_regime_changes_chk
                CHECK (regime_before <> regime_after),
            CONSTRAINT historical_signals_price_chk
                CHECK (price_at_signal > 0)
        );
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_historical_signals_ticker_bar
            ON historical_signals (ticker_id, bar_timestamp DESC, computed_at DESC);
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_historical_signals_bar_ts
            ON historical_signals (bar_timestamp DESC, computed_at DESC);
    """)
    op.execute("""
        COMMENT ON TABLE historical_signals IS
            'V1 append-only audit log of every signal ever fired. '
            'No uniqueness beyond signal_id BIGSERIAL — rotation '
            'across cycles intentionally produces duplicate rows '
            'while the source bar remains the latest. Read-side '
            'DISTINCT ON (bar_timestamp, signal_type) dedupes for '
            'GUI display.';
    """)

    # ====================================================
    # Grants
    # ====================================================
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trip_etl_user') THEN
                GRANT SELECT, INSERT, UPDATE, DELETE
                    ON stock_indicator_state,
                       industry_indicator_state,
                       current_signals,
                       historical_signals
                    TO trip_etl_user;
                GRANT USAGE, SELECT
                    ON SEQUENCE historical_signals_signal_id_seq
                    TO trip_etl_user;
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trip_gui_user') THEN
                GRANT SELECT
                    ON stock_indicator_state,
                       industry_indicator_state,
                       current_signals,
                       historical_signals
                    TO trip_gui_user;
            END IF;
        END
        $$;
    """)


def downgrade() -> None:
    """Drop the four new tables. Safe in pre-prod (no callers yet).
    CASCADE handles the FKs, indexes, sequences, and constraints."""
    op.execute("DROP TABLE IF EXISTS historical_signals CASCADE;")
    op.execute("DROP TABLE IF EXISTS current_signals CASCADE;")
    op.execute("DROP TABLE IF EXISTS industry_indicator_state CASCADE;")
    op.execute("DROP TABLE IF EXISTS stock_indicator_state CASCADE;")
