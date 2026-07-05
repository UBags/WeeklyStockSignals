"""Repository for industry_indicator_enrichment — V1 enrichment.

Industry-side mirror of `StockIndicatorEnrichmentRepository`. Same
methods, same SQL shape, with `industry_name` replacing `ticker_id`
in the PK.

Application-level validation in `upsert_one`:
  - Pattern labels ∈ PATTERN_LABELS

The DB has no CHECK on pattern_label (legacy convention — the label
set could grow), so app-level validation is the only gate. The
boolean and integer/float fields are constrained by their Python
types; no further DB CHECKs needed.

FK: each enrichment row requires its matching industry_indicator_state
row to exist (ON DELETE CASCADE). The runner is responsible for
sequencing (state first, enrichment second).
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..domain.industry_indicator_enrichment import (
    PATTERN_LABELS,
    IndustryIndicatorEnrichment,
)


class IndustryIndicatorEnrichmentRepository:
    """V1 industry indicator enrichment CRUD."""

    def __init__(self, conn: Connection):
        self._conn = conn

    # ---------- writes ---------- #

    def upsert_one(
        self, enrichment: IndustryIndicatorEnrichment,
    ) -> None:
        """Upsert a single row. Idempotent on
        (industry_name, timeframe, bar_timestamp). The parent state
        row must already exist — the DB FK enforces this."""
        self._validate(enrichment)
        self._conn.execute(
            text(_UPSERT_SQL), self._params(enrichment),
        )

    def upsert_many(
        self, enrichments: list[IndustryIndicatorEnrichment],
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

    def delete_for_industry(
        self, industry_name: str, timeframe: str,
    ) -> int:
        """Wipe all rows for one (industry, timeframe). Used by
        full-recompute admin paths. Returns the row count deleted.
        """
        result = self._conn.execute(
            text("""
                DELETE FROM industry_indicator_enrichment
                WHERE industry_name = :ind AND timeframe = :tf
            """),
            {"ind": industry_name, "tf": timeframe},
        )
        return result.rowcount

    # ---------- reads ---------- #

    def get_at(
        self,
        industry_name: str,
        timeframe: str,
        bar_timestamp: datetime,
    ) -> IndustryIndicatorEnrichment | None:
        """Exact-match lookup."""
        row = self._conn.execute(
            text(_SELECT_SQL + " WHERE industry_name = :ind "
                 "AND timeframe = :tf AND bar_timestamp = :bt"),
            {"ind": industry_name, "tf": timeframe,
             "bt": bar_timestamp},
        ).fetchone()
        return _row_to_enrichment(row) if row else None

    def get_at_or_before(
        self,
        industry_name: str,
        timeframe: str,
        bar_timestamp: datetime,
    ) -> IndustryIndicatorEnrichment | None:
        """Most recent row at or before `bar_timestamp`."""
        row = self._conn.execute(
            text(_SELECT_SQL + " WHERE industry_name = :ind "
                 "AND timeframe = :tf AND bar_timestamp <= :bt "
                 "ORDER BY bar_timestamp DESC, date DESC LIMIT 1"),
            {"ind": industry_name, "tf": timeframe,
             "bt": bar_timestamp},
        ).fetchone()
        return _row_to_enrichment(row) if row else None

    def get_latest_for_industry(
        self, industry_name: str, timeframe: str,
    ) -> IndustryIndicatorEnrichment | None:
        """The most recent row for this (industry, timeframe)."""
        row = self._conn.execute(
            text(_SELECT_SQL + " WHERE industry_name = :ind "
                 "AND timeframe = :tf "
                 "ORDER BY bar_timestamp DESC, date DESC LIMIT 1"),
            {"ind": industry_name, "tf": timeframe},
        ).fetchone()
        return _row_to_enrichment(row) if row else None

    def get_history_for_industry(
        self,
        industry_name: str,
        timeframe: str,
        *,
        from_dt: datetime | None = None,
        to_dt: datetime | None = None,
        limit: int | None = None,
    ) -> list[IndustryIndicatorEnrichment]:
        """Historical rows for this (industry, timeframe),
        newest-first."""
        parts = [
            _SELECT_SQL,
            "WHERE industry_name = :ind AND timeframe = :tf",
        ]
        params: dict = {"ind": industry_name, "tf": timeframe}
        if from_dt is not None:
            parts.append("  AND bar_timestamp >= :fr")
            params["fr"] = from_dt
        if to_dt is not None:
            parts.append("  AND bar_timestamp <= :to")
            params["to"] = to_dt
        parts.append("ORDER BY bar_timestamp DESC, date DESC")
        if limit is not None:
            parts.append("LIMIT :lim")
            params["lim"] = limit
        rows = self._conn.execute(text("\n".join(parts)), params)
        return [_row_to_enrichment(r) for r in rows]

    def count_for_industry(
        self, industry_name: str, timeframe: str,
    ) -> int:
        """Row count for this (industry, timeframe)."""
        return self._conn.execute(
            text("""
                SELECT COUNT(*) FROM industry_indicator_enrichment
                WHERE industry_name = :ind AND timeframe = :tf
            """),
            {"ind": industry_name, "tf": timeframe},
        ).scalar_one()

    # ---------- internals ---------- #

    @staticmethod
    def _validate(e: IndustryIndicatorEnrichment) -> None:
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
    def _params(e: IndustryIndicatorEnrichment) -> dict:
        return {
            "ind": e.industry_name,
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
# SQL constants
# ====================================================================

_SELECT_SQL = """SELECT industry_name, timeframe, bar_timestamp, date,
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
       computed_at
FROM industry_indicator_enrichment"""


_UPSERT_SQL = """
    INSERT INTO industry_indicator_enrichment (
        industry_name, timeframe, bar_timestamp, date,
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
        computed_at
    ) VALUES (
        :ind, :tf, :bt, :dt,
        :pu, :pl,
        :dm, :dl, :dlx,
        :mdm, :mdl, :mdlx,
        :bsm, :bsl, :bslx,
        :pcm, :pcl, :pclx,
        :mpl, :lpl, :lxpl,
        :mtb, :mts, :ltb, :lts, :lxtb, :lxts,
        :ca
    )
    ON CONFLICT (industry_name, timeframe, date)
    DO UPDATE SET
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


def _row_to_enrichment(row) -> IndustryIndicatorEnrichment:
    """Unpack one SELECT row into an IndustryIndicatorEnrichment.
    Column order matches _SELECT_SQL."""
    return IndustryIndicatorEnrichment(
        industry_name=row[0],
        timeframe=row[1],
        bar_timestamp=row[2],
        date=row[3],
        pierces_upper_mid=bool(row[4]),
        pierces_lower_mid=bool(row[5]),
        dist_to_med_in_hl_units=(
            float(row[6]) if row[6] is not None else None
        ),
        dist_to_long_in_hl_units=(
            float(row[7]) if row[7] is not None else None
        ),
        dist_to_longest_in_hl_units=(
            float(row[8]) if row[8] is not None else None
        ),
        mean_dist_to_med_in_hl_units=(
            float(row[9]) if row[9] is not None else None
        ),
        mean_dist_to_long_in_hl_units=(
            float(row[10]) if row[10] is not None else None
        ),
        mean_dist_to_longest_in_hl_units=(
            float(row[11]) if row[11] is not None else None
        ),
        bars_since_med_turn=(
            int(row[12]) if row[12] is not None else None
        ),
        bars_since_long_turn=(
            int(row[13]) if row[13] is not None else None
        ),
        bars_since_longest_turn=(
            int(row[14]) if row[14] is not None else None
        ),
        pct_change_since_med_turn=(
            float(row[15]) if row[15] is not None else None
        ),
        pct_change_since_long_turn=(
            float(row[16]) if row[16] is not None else None
        ),
        pct_change_since_longest_turn=(
            float(row[17]) if row[17] is not None else None
        ),
        med_pattern_label=row[18],
        long_pattern_label=row[19],
        longest_pattern_label=row[20],
        med_turn_buy=bool(row[21]),
        med_turn_sell=bool(row[22]),
        long_turn_buy=bool(row[23]),
        long_turn_sell=bool(row[24]),
        longest_turn_buy=bool(row[25]),
        longest_turn_sell=bool(row[26]),
        computed_at=row[27],
    )
