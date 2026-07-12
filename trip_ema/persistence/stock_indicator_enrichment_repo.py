"""Repository for stock_indicator_enrichment — V1 enrichment fields.

CRUD over the 23-enrichment-field table introduced in migration 0021.
Mirrors the Phase 1-4 repository pattern.

Application-level validation in `upsert_one`:
  - Pattern labels ∈ PATTERN_LABELS

The DB has no CHECK on pattern_label (legacy convention — "the label
set could grow"), so app-level validation is the only gate. The
boolean and integer/float fields are constrained by their Python
types; no further DB CHECKs needed.

FK: each enrichment row requires its matching state row to exist
(ON DELETE CASCADE). The runner is responsible for sequencing
(state first, enrichment second).
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..domain.stock_indicator_enrichment import (
    PATTERN_LABELS,
    StockIndicatorEnrichment,
)


class StockIndicatorEnrichmentRepository:
    """V1 stock indicator enrichment CRUD."""

    def __init__(self, conn: Connection):
        self._conn = conn

    # ---------- writes ---------- #

    def upsert_one(self, enrichment: StockIndicatorEnrichment) -> None:
        """Upsert a single row. Idempotent on
        (ticker_id, timeframe, bar_timestamp). The parent state row
        must already exist — the DB FK enforces this."""
        self._validate(enrichment)
        self._conn.execute(text(_UPSERT_SQL), self._params(enrichment))

    def upsert_many(
        self, enrichments: list[StockIndicatorEnrichment],
    ) -> int:
        """Upsert N rows in one round-trip via executemany. Validates
        each row first; raises on the first invalid row before any
        write. Returns the count written (== len(enrichments))."""
        if not enrichments:
            return 0
        for e in enrichments:
            self._validate(e)
        self._conn.execute(
            text(_UPSERT_SQL),
            [self._params(e) for e in enrichments],
        )
        return len(enrichments)

    def delete_for_ticker(self, ticker_id: int, timeframe: str) -> int:
        """Wipe all rows for one (ticker, timeframe). Used by full-
        recompute admin paths. Returns the row count deleted."""
        result = self._conn.execute(
            text("""
                DELETE FROM stock_indicator_enrichment
                WHERE ticker_id = :tid AND timeframe = :tf
            """),
            {"tid": ticker_id, "tf": timeframe},
        )
        return result.rowcount

    # ---------- reads ---------- #

    def get_at(
        self,
        ticker_id: int,
        timeframe: str,
        bar_timestamp: datetime,
    ) -> StockIndicatorEnrichment | None:
        """Exact-match lookup."""
        row = self._conn.execute(
            text(_SELECT_SQL + " WHERE ticker_id = :tid "
                 "AND timeframe = :tf AND bar_timestamp = :bt"),
            {"tid": ticker_id, "tf": timeframe, "bt": bar_timestamp},
        ).fetchone()
        return _row_to_enrichment(row) if row else None

    def get_at_or_before(
        self,
        ticker_id: int,
        timeframe: str,
        bar_timestamp: datetime,
        as_of_date: date | None = None,
    ) -> StockIndicatorEnrichment | None:
        """Most recent row at or before `bar_timestamp`.

        DAILY-GRAIN-BOUND: pass `as_of_date` when you mean "as of day D".
        bar_timestamp is the IST-Monday WEEK anchor, shared by every daily row
        of the week, so `bar_timestamp <= :bt` ALONE admits the rest of that
        week and `ORDER BY date DESC` then returns the week's LAST row -- a day
        in the FUTURE relative to :bt. A week anchor cannot bound a day.
        """
        row = self._conn.execute(
            text(_SELECT_SQL + " WHERE ticker_id = :tid "
                 "AND timeframe = :tf AND bar_timestamp <= :bt "
                 "AND (:aod IS NULL OR date <= :aod) "
                 "ORDER BY date DESC LIMIT 1"),
            {"tid": ticker_id, "tf": timeframe, "bt": bar_timestamp,
             "aod": as_of_date},
        ).fetchone()
        return _row_to_enrichment(row) if row else None

    def get_latest_for_ticker(
        self, ticker_id: int, timeframe: str,
    ) -> StockIndicatorEnrichment | None:
        """The most recent row for this (ticker, timeframe)."""
        row = self._conn.execute(
            text(_SELECT_SQL + " WHERE ticker_id = :tid "
                 "AND timeframe = :tf "
                 "ORDER BY date DESC LIMIT 1"),
            {"tid": ticker_id, "tf": timeframe},
        ).fetchone()
        return _row_to_enrichment(row) if row else None

    def get_history_for_ticker(
        self,
        ticker_id: int,
        timeframe: str,
        *,
        from_dt: datetime | None = None,
        to_dt: datetime | None = None,
        limit: int | None = None,
    ) -> list[StockIndicatorEnrichment]:
        """Historical rows for this (ticker, timeframe), newest-first."""
        parts = [
            _SELECT_SQL,
            "WHERE ticker_id = :tid AND timeframe = :tf",
        ]
        params: dict = {"tid": ticker_id, "tf": timeframe}
        if from_dt is not None:
            parts.append("  AND bar_timestamp >= :fr")
            params["fr"] = from_dt
        if to_dt is not None:
            parts.append("  AND bar_timestamp <= :to")
            params["to"] = to_dt
        parts.append("ORDER BY date DESC")
        if limit is not None:
            parts.append("LIMIT :lim")
            params["lim"] = limit
        rows = self._conn.execute(text("\n".join(parts)), params)
        return [_row_to_enrichment(r) for r in rows]

    def count_for_ticker(self, ticker_id: int, timeframe: str) -> int:
        """Row count for this (ticker, timeframe)."""
        return self._conn.execute(
            text("""
                SELECT COUNT(*) FROM stock_indicator_enrichment
                WHERE ticker_id = :tid AND timeframe = :tf
            """),
            {"tid": ticker_id, "tf": timeframe},
        ).scalar_one()

    # ---------- internals ---------- #

    @staticmethod
    def _validate(e: StockIndicatorEnrichment) -> None:
        for label_field in (
            "med_pattern_label", "long_pattern_label",
            "longest_pattern_label",
        ):
            value = getattr(e, label_field)
            if value not in PATTERN_LABELS:
                raise ValueError(
                    f"{label_field} must be one of {PATTERN_LABELS}, "
                    f"got {value!r}"
                )

    @staticmethod
    def _params(e: StockIndicatorEnrichment) -> dict:
        return {
            "tid": e.ticker_id,
            "tf": e.timeframe,
            "bt": e.bar_timestamp,
            "dt": e.date,
            "pu": e.pierces_upper_mid,
            "pl": e.pierces_lower_mid,
            "dm": e.dist_to_med_in_hl_units,
            "dl": e.dist_to_long_in_hl_units,
            "dlx": e.dist_to_longest_in_hl_units,
            "mdm": e.mean_dist_to_med_in_hl_units,
            "mdl": e.mean_dist_to_long_in_hl_units,
            "mdlx": e.mean_dist_to_longest_in_hl_units,
            "bsm": e.bars_since_med_turn,
            "bsl": e.bars_since_long_turn,
            "bslx": e.bars_since_longest_turn,
            "pcm": e.pct_change_since_med_turn,
            "pcl": e.pct_change_since_long_turn,
            "pclx": e.pct_change_since_longest_turn,
            "mpl": e.med_pattern_label,
            "lpl": e.long_pattern_label,
            "lxpl": e.longest_pattern_label,
            "mtb": e.med_turn_buy,
            "mts": e.med_turn_sell,
            "ltb": e.long_turn_buy,
            "lts": e.long_turn_sell,
            "lxtb": e.longest_turn_buy,
            "lxts": e.longest_turn_sell,
            "ca": e.computed_at,
        }


# ====================================================================
# SQL constants (long enough to factor out)
# ====================================================================

_SELECT_SQL = """SELECT ticker_id, timeframe, bar_timestamp,
       pierces_upper_mid, pierces_lower_mid,
       dist_to_med_in_hl_units, dist_to_long_in_hl_units,
       dist_to_longest_in_hl_units,
       mean_dist_to_med_in_hl_units, mean_dist_to_long_in_hl_units,
       mean_dist_to_longest_in_hl_units,
       bars_since_med_turn, bars_since_long_turn,
       bars_since_longest_turn,
       pct_change_since_med_turn, pct_change_since_long_turn,
       pct_change_since_longest_turn,
       med_pattern_label, long_pattern_label, longest_pattern_label,
       med_turn_buy, med_turn_sell,
       long_turn_buy, long_turn_sell,
       longest_turn_buy, longest_turn_sell,
       computed_at, date
FROM stock_indicator_enrichment"""


_UPSERT_SQL = """
    INSERT INTO stock_indicator_enrichment (
        ticker_id, timeframe, bar_timestamp,
        pierces_upper_mid, pierces_lower_mid,
        dist_to_med_in_hl_units, dist_to_long_in_hl_units,
        dist_to_longest_in_hl_units,
        mean_dist_to_med_in_hl_units, mean_dist_to_long_in_hl_units,
        mean_dist_to_longest_in_hl_units,
        bars_since_med_turn, bars_since_long_turn,
        bars_since_longest_turn,
        pct_change_since_med_turn, pct_change_since_long_turn,
        pct_change_since_longest_turn,
        med_pattern_label, long_pattern_label, longest_pattern_label,
        med_turn_buy, med_turn_sell,
        long_turn_buy, long_turn_sell,
        longest_turn_buy, longest_turn_sell,
        computed_at, date
    ) VALUES (
        :tid, :tf, :bt,
        :pu, :pl,
        :dm, :dl, :dlx,
        :mdm, :mdl, :mdlx,
        :bsm, :bsl, :bslx,
        :pcm, :pcl, :pclx,
        :mpl, :lpl, :lxpl,
        :mtb, :mts, :ltb, :lts, :lxtb, :lxts,
        :ca, :dt
    )
    ON CONFLICT (ticker_id, timeframe, date)
    DO UPDATE SET
        bar_timestamp = EXCLUDED.bar_timestamp,
        pierces_upper_mid = EXCLUDED.pierces_upper_mid,
        pierces_lower_mid = EXCLUDED.pierces_lower_mid,
        dist_to_med_in_hl_units = EXCLUDED.dist_to_med_in_hl_units,
        dist_to_long_in_hl_units = EXCLUDED.dist_to_long_in_hl_units,
        dist_to_longest_in_hl_units = EXCLUDED.dist_to_longest_in_hl_units,
        mean_dist_to_med_in_hl_units = EXCLUDED.mean_dist_to_med_in_hl_units,
        mean_dist_to_long_in_hl_units = EXCLUDED.mean_dist_to_long_in_hl_units,
        mean_dist_to_longest_in_hl_units = EXCLUDED.mean_dist_to_longest_in_hl_units,
        bars_since_med_turn = EXCLUDED.bars_since_med_turn,
        bars_since_long_turn = EXCLUDED.bars_since_long_turn,
        bars_since_longest_turn = EXCLUDED.bars_since_longest_turn,
        pct_change_since_med_turn = EXCLUDED.pct_change_since_med_turn,
        pct_change_since_long_turn = EXCLUDED.pct_change_since_long_turn,
        pct_change_since_longest_turn = EXCLUDED.pct_change_since_longest_turn,
        med_pattern_label = EXCLUDED.med_pattern_label,
        long_pattern_label = EXCLUDED.long_pattern_label,
        longest_pattern_label = EXCLUDED.longest_pattern_label,
        med_turn_buy = EXCLUDED.med_turn_buy,
        med_turn_sell = EXCLUDED.med_turn_sell,
        long_turn_buy = EXCLUDED.long_turn_buy,
        long_turn_sell = EXCLUDED.long_turn_sell,
        longest_turn_buy = EXCLUDED.longest_turn_buy,
        longest_turn_sell = EXCLUDED.longest_turn_sell,
        computed_at = EXCLUDED.computed_at
"""


def _row_to_enrichment(row) -> StockIndicatorEnrichment:
    """Unpack one SELECT row into a StockIndicatorEnrichment. Column
    order matches _SELECT_SQL."""
    return StockIndicatorEnrichment(
        ticker_id=int(row[0]),
        timeframe=row[1],
        bar_timestamp=row[2],
        pierces_upper_mid=bool(row[3]),
        pierces_lower_mid=bool(row[4]),
        dist_to_med_in_hl_units=(
            float(row[5]) if row[5] is not None else None
        ),
        dist_to_long_in_hl_units=(
            float(row[6]) if row[6] is not None else None
        ),
        dist_to_longest_in_hl_units=(
            float(row[7]) if row[7] is not None else None
        ),
        mean_dist_to_med_in_hl_units=(
            float(row[8]) if row[8] is not None else None
        ),
        mean_dist_to_long_in_hl_units=(
            float(row[9]) if row[9] is not None else None
        ),
        mean_dist_to_longest_in_hl_units=(
            float(row[10]) if row[10] is not None else None
        ),
        bars_since_med_turn=(
            int(row[11]) if row[11] is not None else None
        ),
        bars_since_long_turn=(
            int(row[12]) if row[12] is not None else None
        ),
        bars_since_longest_turn=(
            int(row[13]) if row[13] is not None else None
        ),
        pct_change_since_med_turn=(
            float(row[14]) if row[14] is not None else None
        ),
        pct_change_since_long_turn=(
            float(row[15]) if row[15] is not None else None
        ),
        pct_change_since_longest_turn=(
            float(row[16]) if row[16] is not None else None
        ),
        med_pattern_label=row[17],
        long_pattern_label=row[18],
        longest_pattern_label=row[19],
        med_turn_buy=bool(row[20]),
        med_turn_sell=bool(row[21]),
        long_turn_buy=bool(row[22]),
        long_turn_sell=bool(row[23]),
        longest_turn_buy=bool(row[24]),
        longest_turn_sell=bool(row[25]),
        computed_at=row[26],
        date=row[27],
    )
