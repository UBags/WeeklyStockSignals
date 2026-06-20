"""Phase 6-3: benchmark stats tables for alpha/beta/RS rankings.

Two new tables capture per-(stock × benchmark × window) and
per-(industry × NIFTY × window) regression statistics, recomputed
weekly after the EoD pipeline. Downstream consumer: email
notification reordering, which uses a composite

    rank_score = 0.5 × RS_26w + 0.3 × industry_RS_26w + 0.2 × R²

to surface BUY events with the most promising momentum first.

Tables:
  stock_benchmark_stats    — 6 rows / ticker / as-of week
    (3 windows {SHORT, MEDIUM, LONG} × 2 benchmarks {INDUSTRY, NIFTY})
  industry_benchmark_stats — 3 rows / industry / as-of week
    (3 windows × 1 benchmark {NIFTY})

Window labels map to a fixed observation count: SHORT=26w, MEDIUM=52w,
LONG=156w. Weekly regression on weekly arithmetic returns. Below 8
observations the regression stats are NULL but n_weeks is still
recorded so callers can filter.

Storage scale:
  - stock_benchmark_stats: 2,667 tickers × 6 × 52 wk/yr ≈ 832k rows/yr
  - industry_benchmark_stats: 60 × 3 × 52 wk/yr ≈ 9k rows/yr

Both are well below the threshold where partitioning matters.

The (as_of_date) component of the PK preserves history. Weekly
recompute appends a new row per series; old rows stay queryable for
alpha-trajectory analytics.

Revision ID: 0017_benchmark_stats
Revises: 0016_unify_signal_events
Create Date: 2026-06-20
"""
from __future__ import annotations

from alembic import op


revision = "0017_benchmark_stats"
down_revision = "0016_unify_signal_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS stock_benchmark_stats (
            ticker_id        INTEGER NOT NULL
                REFERENCES tickers(ticker_id) ON DELETE CASCADE,
            -- 'INDUSTRY' (stock vs its own industry index) or
            -- 'NIFTY' (stock vs the NIFTY proxy, NIFTYBEES).
            benchmark_kind   VARCHAR(16) NOT NULL,
            -- Concrete benchmark identity: industry_name (for
            -- INDUSTRY) or the literal string 'NIFTY'.
            benchmark_id     VARCHAR(128) NOT NULL,
            -- 'SHORT' (26w) / 'MEDIUM' (52w) / 'LONG' (156w).
            window_label     VARCHAR(8) NOT NULL,
            -- Date of the LAST bar that fed the regression. With
            -- weekly Monday-anchored bars this is a Monday.
            as_of_date       DATE NOT NULL,
            -- Actual observation count used (≤ window length;
            -- shorter when the stock has thin history).
            n_weeks          INTEGER NOT NULL,

            -- Regression stats — NULL if n_weeks < 8 or degenerate
            -- (e.g. benchmark variance = 0).
            beta             DOUBLE PRECISION,
            alpha_annual     DOUBLE PRECISION,
            r_squared        DOUBLE PRECISION,
            sigma_annual     DOUBLE PRECISION,
            rs_pct           DOUBLE PRECISION,
            info_ratio       DOUBLE PRECISION,

            computed_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

            CONSTRAINT stock_benchmark_stats_kind_chk
                CHECK (benchmark_kind IN ('INDUSTRY', 'NIFTY')),
            CONSTRAINT stock_benchmark_stats_window_chk
                CHECK (window_label IN ('SHORT', 'MEDIUM', 'LONG')),
            CONSTRAINT stock_benchmark_stats_r2_chk
                CHECK (r_squared IS NULL
                       OR (r_squared >= 0 AND r_squared <= 1)),
            CONSTRAINT stock_benchmark_stats_nweeks_chk
                CHECK (n_weeks >= 0),

            PRIMARY KEY
                (ticker_id, benchmark_kind, window_label, as_of_date)
        );
    """)

    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_stock_benchmark_stats_latest
            ON stock_benchmark_stats
            (ticker_id, benchmark_kind, window_label, as_of_date DESC);
    """)

    op.execute("""
        CREATE TABLE IF NOT EXISTS industry_benchmark_stats (
            industry_name    VARCHAR(128) NOT NULL
                REFERENCES industry_index(industry_name) ON DELETE CASCADE,
            -- Always 'NIFTY' for Phase 6-3.
            benchmark_id     VARCHAR(128) NOT NULL,
            window_label     VARCHAR(8) NOT NULL,
            as_of_date       DATE NOT NULL,
            n_weeks          INTEGER NOT NULL,

            beta             DOUBLE PRECISION,
            alpha_annual     DOUBLE PRECISION,
            r_squared        DOUBLE PRECISION,
            sigma_annual     DOUBLE PRECISION,
            rs_pct           DOUBLE PRECISION,
            info_ratio       DOUBLE PRECISION,

            computed_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

            CONSTRAINT industry_benchmark_stats_window_chk
                CHECK (window_label IN ('SHORT', 'MEDIUM', 'LONG')),
            CONSTRAINT industry_benchmark_stats_r2_chk
                CHECK (r_squared IS NULL
                       OR (r_squared >= 0 AND r_squared <= 1)),
            CONSTRAINT industry_benchmark_stats_nweeks_chk
                CHECK (n_weeks >= 0),

            PRIMARY KEY
                (industry_name, benchmark_id, window_label, as_of_date)
        );
    """)

    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_industry_benchmark_stats_latest
            ON industry_benchmark_stats
            (industry_name, window_label, as_of_date DESC);
    """)

    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trip_etl_user') THEN
                GRANT SELECT, INSERT, UPDATE, DELETE
                    ON stock_benchmark_stats, industry_benchmark_stats
                    TO trip_etl_user;
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trip_gui_user') THEN
                GRANT SELECT
                    ON stock_benchmark_stats, industry_benchmark_stats
                    TO trip_gui_user;
            END IF;
        END$$;
    """)

    op.execute("""
        COMMENT ON TABLE stock_benchmark_stats IS
            'Per-(stock, benchmark, window) regression statistics for '
            'email ranking and GUI overlays (Phase 6-3). Recomputed '
            'weekly via run_benchmark_stats_for_all_tickers.';
        COMMENT ON TABLE industry_benchmark_stats IS
            'Per-(industry, NIFTY, window) regression statistics. Used '
            'to compute industry_RS_26w in the email rank_score '
            '(Phase 6-3).';
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS industry_benchmark_stats;")
    op.execute("DROP TABLE IF EXISTS stock_benchmark_stats;")
