"""Repository for industry_indicator_state — V1 per-industry state.

Industry-side mirror of `StockIndicatorStateRepository`. Same
methods, same SQL shape, with `industry_name` replacing
`ticker_id` in the PK. Application-level validation mirrors the
DB CHECK constraints in migration 0020.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..domain.industry_indicator_state import (
    REGIME_VALUES,
    IndustryIndicatorState,
)


# Reused across the SELECT helpers — keep the column order stable
# because _row_to_state unpacks positionally.
_SELECT_COLS = (
    "industry_name, timeframe, bar_timestamp, "
    "med_ema, long_ema, longest_ema, atr, "
    "upper_inner_band, lower_inner_band, "
    "upper_mid_band, lower_mid_band, "
    "upper_outer_band, lower_outer_band, "
    "display_regime, computed_at"
)


_UPSERT_SQL = """
    INSERT INTO industry_indicator_state (
        industry_name, timeframe, bar_timestamp,
        med_ema, long_ema, longest_ema,
        atr,
        upper_inner_band, lower_inner_band,
        upper_mid_band, lower_mid_band,
        upper_outer_band, lower_outer_band,
        display_regime, computed_at
    ) VALUES (
        :ind, :tf, :bt,
        :med, :long, :longest,
        :atr,
        :uib, :lib, :umb, :lmb, :uob, :lob,
        :dr, :ca
    )
    ON CONFLICT (industry_name, timeframe, bar_timestamp)
    DO UPDATE SET
        med_ema = EXCLUDED.med_ema,
        long_ema = EXCLUDED.long_ema,
        longest_ema = EXCLUDED.longest_ema,
        atr = EXCLUDED.atr,
        upper_inner_band = EXCLUDED.upper_inner_band,
        lower_inner_band = EXCLUDED.lower_inner_band,
        upper_mid_band = EXCLUDED.upper_mid_band,
        lower_mid_band = EXCLUDED.lower_mid_band,
        upper_outer_band = EXCLUDED.upper_outer_band,
        lower_outer_band = EXCLUDED.lower_outer_band,
        display_regime = EXCLUDED.display_regime,
        computed_at = EXCLUDED.computed_at
"""


class IndustryIndicatorStateRepository:
    """V1 industry indicator state CRUD.

    Application-level validation in `upsert_one` / `upsert_many`:
      - display_regime ∈ {-1, 0, +1}
      - atr >= 0
      - EMA values > 0

    These are also enforced by DB CHECK constraints in migration
    0020; the repo-side checks give better error messages.
    """

    def __init__(self, conn: Connection):
        self._conn = conn

    # ---------- writes ---------- #

    def upsert_one(self, state: IndustryIndicatorState) -> None:
        """Upsert a single row. Idempotent on
        (industry_name, timeframe, bar_timestamp)."""
        self._validate(state)
        self._conn.execute(text(_UPSERT_SQL), self._params(state))

    def upsert_many(
        self, states: list[IndustryIndicatorState],
    ) -> int:
        """Upsert N rows in one round-trip via executemany.

        Validates each row first; raises on the first invalid row
        before any write. Returns the count written (== len(states)).
        """
        if not states:
            return 0
        for s in states:
            self._validate(s)
        self._conn.execute(
            text(_UPSERT_SQL),
            [self._params(s) for s in states],
        )
        return len(states)

    def delete_for_industry(
        self, industry_name: str, timeframe: str,
    ) -> int:
        """Wipe all rows for one (industry, timeframe). Used by
        full-recompute admin paths. Returns the row count deleted.
        """
        result = self._conn.execute(
            text("""
                DELETE FROM industry_indicator_state
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
    ) -> IndustryIndicatorState | None:
        """Exact-match lookup. Returns None if no row at that key."""
        row = self._conn.execute(
            text(f"""
                SELECT {_SELECT_COLS}
                FROM industry_indicator_state
                WHERE industry_name = :ind AND timeframe = :tf
                  AND bar_timestamp = :bt
            """),
            {"ind": industry_name, "tf": timeframe,
             "bt": bar_timestamp},
        ).fetchone()
        return _row_to_state(row) if row else None

    def get_at_or_before(
        self,
        industry_name: str,
        timeframe: str,
        bar_timestamp: datetime,
    ) -> IndustryIndicatorState | None:
        """Most recent row at or before `bar_timestamp`. Used by
        live-engine warm-start where we want the latest state we
        have for this industry as of a given clock instant."""
        row = self._conn.execute(
            text(f"""
                SELECT {_SELECT_COLS}
                FROM industry_indicator_state
                WHERE industry_name = :ind AND timeframe = :tf
                  AND bar_timestamp <= :bt
                ORDER BY bar_timestamp DESC
                LIMIT 1
            """),
            {"ind": industry_name, "tf": timeframe,
             "bt": bar_timestamp},
        ).fetchone()
        return _row_to_state(row) if row else None

    def get_latest_for_industry(
        self,
        industry_name: str,
        timeframe: str,
    ) -> IndustryIndicatorState | None:
        """The most recent row for this (industry, timeframe).
        Returns None when the industry has no rows."""
        row = self._conn.execute(
            text(f"""
                SELECT {_SELECT_COLS}
                FROM industry_indicator_state
                WHERE industry_name = :ind AND timeframe = :tf
                ORDER BY bar_timestamp DESC
                LIMIT 1
            """),
            {"ind": industry_name, "tf": timeframe},
        ).fetchone()
        return _row_to_state(row) if row else None

    def get_history_for_industry(
        self,
        industry_name: str,
        timeframe: str,
        *,
        from_dt: datetime | None = None,
        to_dt: datetime | None = None,
        limit: int | None = None,
    ) -> list[IndustryIndicatorState]:
        """Historical rows for this (industry, timeframe),
        newest-first.

        Optional `from_dt` / `to_dt` bound the bar_timestamp range
        (inclusive). Optional `limit` caps the result count.
        """
        sql_parts = [
            f"SELECT {_SELECT_COLS}",
            "FROM industry_indicator_state",
            "WHERE industry_name = :ind AND timeframe = :tf",
        ]
        params: dict = {"ind": industry_name, "tf": timeframe}
        if from_dt is not None:
            sql_parts.append("  AND bar_timestamp >= :fr")
            params["fr"] = from_dt
        if to_dt is not None:
            sql_parts.append("  AND bar_timestamp <= :to")
            params["to"] = to_dt
        sql_parts.append("ORDER BY bar_timestamp DESC")
        if limit is not None:
            sql_parts.append("LIMIT :lim")
            params["lim"] = limit
        rows = self._conn.execute(text("\n".join(sql_parts)), params)
        return [_row_to_state(r) for r in rows]

    def count_for_industry(
        self, industry_name: str, timeframe: str,
    ) -> int:
        """Row count for this (industry, timeframe). Used by
        admin/status endpoints to report coverage."""
        return self._conn.execute(
            text("""
                SELECT COUNT(*) FROM industry_indicator_state
                WHERE industry_name = :ind AND timeframe = :tf
            """),
            {"ind": industry_name, "tf": timeframe},
        ).scalar_one()

    # ---------- internals ---------- #

    @staticmethod
    def _validate(state: IndustryIndicatorState) -> None:
        if state.display_regime not in REGIME_VALUES:
            raise ValueError(
                f"display_regime must be in {REGIME_VALUES}, "
                f"got {state.display_regime}"
            )
        if state.atr < 0:
            raise ValueError(f"atr must be >= 0, got {state.atr}")
        if not (state.med_ema > 0 and state.long_ema > 0
                and state.longest_ema > 0):
            raise ValueError(
                f"EMA values must all be > 0, got "
                f"med={state.med_ema}, long={state.long_ema}, "
                f"longest={state.longest_ema}"
            )

    @staticmethod
    def _params(state: IndustryIndicatorState) -> dict:
        return {
            "ind": state.industry_name,
            "tf": state.timeframe,
            "bt": state.bar_timestamp,
            "med": state.med_ema,
            "long": state.long_ema,
            "longest": state.longest_ema,
            "atr": state.atr,
            "uib": state.upper_inner_band,
            "lib": state.lower_inner_band,
            "umb": state.upper_mid_band,
            "lmb": state.lower_mid_band,
            "uob": state.upper_outer_band,
            "lob": state.lower_outer_band,
            "dr": state.display_regime,
            "ca": state.computed_at,
        }


def _row_to_state(row) -> IndustryIndicatorState:
    """Unpack one SELECT row into an IndustryIndicatorState. Column
    order matches `_SELECT_COLS`."""
    return IndustryIndicatorState(
        industry_name=row[0],
        timeframe=row[1],
        bar_timestamp=row[2],
        med_ema=float(row[3]),
        long_ema=float(row[4]),
        longest_ema=float(row[5]),
        atr=float(row[6]),
        upper_inner_band=float(row[7]),
        lower_inner_band=float(row[8]),
        upper_mid_band=float(row[9]),
        lower_mid_band=float(row[10]),
        upper_outer_band=float(row[11]),
        lower_outer_band=float(row[12]),
        display_regime=int(row[13]),
        computed_at=row[14],
    )
