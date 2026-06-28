"""Compute + upsert the market_overview_daily snapshot.

Step 13a: powers the Overall Market Overview footer + the 90-day
trend dialogs in the ETL Status GUI.

One snapshot row per (snapshot_date, exchange). The metrics are
"how the universe looks AS-OF snapshot_date, anchored to the
in-progress weekly bar containing that date":

  - Signal counts (n_buys, n_sells, n_exit_buys, n_exit_sells)
    sourced from current_signals rows whose bar_timestamp equals
    the snapshot date's IST-Monday. After rotation rolls a row out
    of current_signals, it stops contributing to today's signal
    counts; the historical record persists in market_overview_daily.

  - Regime counts (green=1, red=-1, neutral=0) sourced from
    stock_indicator_state for the in-progress weekly bar (= Monday
    of the IST week containing snapshot_date).

  - EMA-envelope counts (above_highest, below_lowest, between)
    compute the per-ticker max+min of (med_ema, long_ema, longest_
    ema) for the in-progress weekly bar, then compare to the daily
    close at snapshot_date.

LIVE PATH (current implementation):
  snapshot_for_today(exchange) — reads existing state for "now".
  Cheap, single-SQL. Called by the EoD live tick (16:30 IST).

BACKFILL PATH (deferred to a later bundle):
  For past dates, we'd need to re-aggregate daily bars and re-run
  the indicator pipeline as-of each historical day — expensive,
  ~3-4 hours for 90 days × 2667 tickers. Not in 13a. The 90-day
  trend dialog accumulates naturally as the live cycle writes one
  snapshot per EoD; after 90 calendar days of operation it's full.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class MarketOverviewCounts:
    """The 11 aggregate counts that go in one snapshot row."""
    n_buys: int
    n_sells: int
    n_exit_buys: int
    n_exit_sells: int
    n_green_regime: int
    n_red_regime: int
    n_neutral_regime: int
    n_above_highest_ema: int
    n_below_lowest_ema: int
    n_between_emas: int

    @property
    def total_signals(self) -> int:
        return (
            self.n_buys + self.n_sells
            + self.n_exit_buys + self.n_exit_sells
        )

    @property
    def total_regimes(self) -> int:
        """Sanity: should equal the active universe size when every
        ticker has a state row for the in-progress bar."""
        return (
            self.n_green_regime + self.n_red_regime
            + self.n_neutral_regime
        )


class MarketOverviewSnapshotter:
    """Computes one (snapshot_date, exchange) row of
    market_overview_daily.

    Stateless aside from the db_engine handle — instances are
    cheap to construct and safe to share across threads (no shared
    mutable state).
    """

    def __init__(self, db_engine: Engine) -> None:
        self._engine = db_engine

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def snapshot_for_today(
        self, *, exchange: str = "NSE",
        snapshot_date: date | None = None,
    ) -> MarketOverviewCounts:
        """Compute the 11 counts and UPSERT one row to
        market_overview_daily.

        `snapshot_date` defaults to today's IST date (server's
        current wall clock interpreted in Asia/Kolkata). Override
        for tests or to fill in a recent gap.

        UPSERT semantics: if a row already exists for
        (snapshot_date, exchange), every column is replaced and
        computed_at is updated to NOW(). Operators re-running the
        snapshot for the same day see the latest numbers, not a
        stale snapshot.
        """
        with self._engine.begin() as conn:
            counts = self._compute_counts(
                conn, exchange=exchange,
                snapshot_date=snapshot_date,
            )
            self._upsert(
                conn, exchange=exchange,
                snapshot_date=snapshot_date,
                counts=counts,
            )
        logger.info(
            "MarketOverview snapshot[%s] for %s: "
            "signals=%d (B%d/S%d/Eb%d/Es%d), "
            "regimes G%d/R%d/N%d, "
            "EMA envelope above=%d/below=%d/between=%d",
            exchange,
            snapshot_date or "today (IST)",
            counts.total_signals,
            counts.n_buys, counts.n_sells,
            counts.n_exit_buys, counts.n_exit_sells,
            counts.n_green_regime, counts.n_red_regime,
            counts.n_neutral_regime,
            counts.n_above_highest_ema, counts.n_below_lowest_ema,
            counts.n_between_emas,
        )
        return counts

    def delete_range(
        self, *, exchange: str, date_from: date, date_to: date,
    ) -> int:
        """DELETE rows for the closed range [date_from, date_to].
        Used by backfill stage 7 (extended to clear snapshots) +
        when a future backfill stage 10 lands. Returns number of
        rows deleted."""
        with self._engine.begin() as conn:
            result = conn.execute(
                text("""
                    DELETE FROM market_overview_daily
                     WHERE exchange = :ex
                       AND snapshot_date BETWEEN :df AND :dt
                """),
                {"ex": exchange, "df": date_from, "dt": date_to},
            )
            return int(result.rowcount or 0)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _compute_counts(
        self,
        conn: Connection,
        *,
        exchange: str,
        snapshot_date: date | None,
    ) -> MarketOverviewCounts:
        """The aggregation SQL. Single pass over the universe; one
        query gives all 11 counts via FILTER clauses."""
        # The Postgres expression `date_trunc('week', X)` returns the
        # Monday of X's ISO 8601 week (Mon-Sun). When X is given as
        # an IST-local date, the trunc'd value is "Monday 00:00 IST"
        # as a timestamp (no tz); we then convert back to UTC to
        # match the bar_timestamp shape used in stock_indicator_state
        # / current_signals for weekly bars (Monday 00:00 UTC of the
        # IST week).
        # Step 13a fixup: CAST(:snap_date AS date) instead of
        # the postfix :snap_date::date — SQLAlchemy's text() bind
        # regex has a `(?!:)` lookahead that refuses to substitute
        # a bind param when immediately followed by another colon
        # (which is PG's cast operator). The CAST() form avoids the
        # adjacency.
        if snapshot_date is None:
            today_clause = (
                "(NOW() AT TIME ZONE 'Asia/Kolkata')::date"
            )
            params: dict = {"ex": exchange}
        else:
            today_clause = "CAST(:snap_date AS date)"
            params = {"ex": exchange, "snap_date": snapshot_date}

        sql = text(f"""
            WITH
                -- Step 13a fixup-fixup: monday_ts anchored in UTC.
                -- The terminal `::timestamptz` cast on a timestamp-
                -- without-tz interprets it in the SESSION timezone,
                -- not UTC. On the NUC (session TZ = Asia/Kolkata),
                -- this puts monday_ts at "Monday 00:00 IST" =
                -- "Sunday 18:30 UTC", 5:30 hours before the actual
                -- Monday-00:00-UTC anchor used by
                -- stock_indicator_state and current_signals. Using
                -- `AT TIME ZONE 'UTC'` instead makes the conversion
                -- explicit + session-TZ-independent.
                week_anchor AS (
                    SELECT
                        ({today_clause})                            AS today_d,
                        (date_trunc(
                            'week', ({today_clause})::timestamp
                        )::date)::timestamp
                        AT TIME ZONE 'UTC'                          AS monday_ts
                ),
                active AS (
                    SELECT ticker_id
                      FROM tickers
                     WHERE exchange = :ex
                       AND is_active = TRUE
                       AND delisted_at IS NULL
                ),
                state AS (
                    SELECT
                        sis.ticker_id,
                        sis.display_regime,
                        GREATEST(sis.med_ema, sis.long_ema,
                                 sis.longest_ema)                   AS highest_ema,
                        LEAST(sis.med_ema, sis.long_ema,
                              sis.longest_ema)                      AS lowest_ema
                      FROM stock_indicator_state sis
                      JOIN active USING (ticker_id)
                      JOIN week_anchor wa
                        ON sis.bar_timestamp = wa.monday_ts
                     WHERE sis.timeframe = '1W'
                ),
                close_today AS (
                    SELECT
                        o.ticker_id, o.close
                      FROM ohlcv_bars o
                      JOIN active USING (ticker_id)
                      JOIN week_anchor wa
                        ON o.bar_timestamp::date = wa.today_d
                     WHERE o.timeframe = '1D'
                ),
                sigs AS (
                    SELECT cs.ticker_id, cs.signal_type
                      FROM current_signals cs
                      JOIN active USING (ticker_id)
                      JOIN week_anchor wa
                        ON cs.bar_timestamp = wa.monday_ts
                ),
                per_ticker AS (
                    SELECT
                        a.ticker_id,
                        s.display_regime,
                        s.highest_ema, s.lowest_ema,
                        ct.close,
                        sg.signal_type
                      FROM active     a
                      LEFT JOIN state       s  USING (ticker_id)
                      LEFT JOIN close_today ct USING (ticker_id)
                      LEFT JOIN sigs        sg USING (ticker_id)
                )
            SELECT
                COUNT(*) FILTER (WHERE signal_type = 'BUY')              AS n_buys,
                COUNT(*) FILTER (WHERE signal_type = 'SELL')             AS n_sells,
                COUNT(*) FILTER (WHERE signal_type = 'EXIT_FROM_BUY')    AS n_exit_buys,
                COUNT(*) FILTER (WHERE signal_type = 'EXIT_FROM_SELL')   AS n_exit_sells,
                COUNT(*) FILTER (WHERE display_regime =  1)              AS n_green_regime,
                COUNT(*) FILTER (WHERE display_regime = -1)              AS n_red_regime,
                COUNT(*) FILTER (WHERE display_regime =  0)              AS n_neutral_regime,
                COUNT(*) FILTER (
                    WHERE close IS NOT NULL
                      AND highest_ema IS NOT NULL
                      AND close >= highest_ema
                ) AS n_above_highest_ema,
                COUNT(*) FILTER (
                    WHERE close IS NOT NULL
                      AND lowest_ema IS NOT NULL
                      AND close <= lowest_ema
                ) AS n_below_lowest_ema,
                COUNT(*) FILTER (
                    WHERE close IS NOT NULL
                      AND highest_ema IS NOT NULL
                      AND lowest_ema  IS NOT NULL
                      AND close >  lowest_ema
                      AND close <  highest_ema
                ) AS n_between_emas
              FROM per_ticker
        """)

        row = conn.execute(sql, params).first()
        if row is None:
            # Theoretically unreachable — aggregation always
            # returns one row, even over an empty universe.
            return MarketOverviewCounts(
                0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
            )
        return MarketOverviewCounts(
            n_buys=int(row.n_buys),
            n_sells=int(row.n_sells),
            n_exit_buys=int(row.n_exit_buys),
            n_exit_sells=int(row.n_exit_sells),
            n_green_regime=int(row.n_green_regime),
            n_red_regime=int(row.n_red_regime),
            n_neutral_regime=int(row.n_neutral_regime),
            n_above_highest_ema=int(row.n_above_highest_ema),
            n_below_lowest_ema=int(row.n_below_lowest_ema),
            n_between_emas=int(row.n_between_emas),
        )

    def _upsert(
        self,
        conn: Connection,
        *,
        exchange: str,
        snapshot_date: date | None,
        counts: MarketOverviewCounts,
    ) -> None:
        """UPSERT one row. Uses ON CONFLICT DO UPDATE on the
        (snapshot_date, exchange) PK. Idempotent — multiple snaps
        on the same day yield the most recent numbers."""
        # Step 13a fixup: see _compute_counts for the rationale —
        # CAST() avoids the SQLAlchemy bind-regex `(?!:)` lookahead.
        if snapshot_date is None:
            date_clause = "(NOW() AT TIME ZONE 'Asia/Kolkata')::date"
            params: dict = {
                "ex": exchange,
                "b": counts.n_buys,
                "s": counts.n_sells,
                "eb": counts.n_exit_buys,
                "es": counts.n_exit_sells,
                "g": counts.n_green_regime,
                "r": counts.n_red_regime,
                "n": counts.n_neutral_regime,
                "ah": counts.n_above_highest_ema,
                "bl": counts.n_below_lowest_ema,
                "bt": counts.n_between_emas,
            }
        else:
            date_clause = "CAST(:snap_date AS date)"
            params = {
                "ex": exchange,
                "snap_date": snapshot_date,
                "b": counts.n_buys,
                "s": counts.n_sells,
                "eb": counts.n_exit_buys,
                "es": counts.n_exit_sells,
                "g": counts.n_green_regime,
                "r": counts.n_red_regime,
                "n": counts.n_neutral_regime,
                "ah": counts.n_above_highest_ema,
                "bl": counts.n_below_lowest_ema,
                "bt": counts.n_between_emas,
            }

        conn.execute(text(f"""
            INSERT INTO market_overview_daily (
                snapshot_date, exchange,
                n_buys, n_sells, n_exit_buys, n_exit_sells,
                n_green_regime, n_red_regime, n_neutral_regime,
                n_above_highest_ema, n_below_lowest_ema,
                n_between_emas,
                computed_at
            ) VALUES (
                {date_clause}, :ex,
                :b, :s, :eb, :es,
                :g, :r, :n,
                :ah, :bl, :bt,
                NOW()
            )
            ON CONFLICT (snapshot_date, exchange) DO UPDATE SET
                n_buys              = EXCLUDED.n_buys,
                n_sells             = EXCLUDED.n_sells,
                n_exit_buys         = EXCLUDED.n_exit_buys,
                n_exit_sells        = EXCLUDED.n_exit_sells,
                n_green_regime      = EXCLUDED.n_green_regime,
                n_red_regime        = EXCLUDED.n_red_regime,
                n_neutral_regime    = EXCLUDED.n_neutral_regime,
                n_above_highest_ema = EXCLUDED.n_above_highest_ema,
                n_below_lowest_ema  = EXCLUDED.n_below_lowest_ema,
                n_between_emas      = EXCLUDED.n_between_emas,
                computed_at         = NOW()
        """), params)
