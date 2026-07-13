"""Read-side repository for market_overview_daily.

Step 13b: powers the ETL Status GUI's Market Overview footer +
the 90-day trend dialogs.

Writes go through MarketOverviewSnapshotter (in trip_ema/snapshots/).
This module is purely for the GUI's read paths.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import text
from sqlalchemy.engine import Connection
from ..domain.market_session import (
    market_tz_name,
)


@dataclass(frozen=True, slots=True)
class MarketOverviewRow:
    """One snapshot row. Mirrors the market_overview_daily schema."""
    snapshot_date: date
    exchange: str
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
    computed_at: datetime


class MarketOverviewRepository:
    """Read methods over market_overview_daily."""

    def __init__(self, conn: Connection) -> None:
        self._conn = conn

    def latest(
        self, *, exchange: str,
    ) -> MarketOverviewRow | None:
        """The most-recent snapshot row for `exchange`, or None
        if the exchange has no snapshots yet."""
        row = self._conn.execute(
            text("""
                SELECT snapshot_date, exchange,
                       n_buys, n_sells, n_exit_buys, n_exit_sells,
                       n_green_regime, n_red_regime,
                       n_neutral_regime,
                       n_above_highest_ema, n_below_lowest_ema,
                       n_between_emas,
                       computed_at
                  FROM market_overview_daily
                 WHERE exchange = :ex
                 ORDER BY snapshot_date DESC
                 LIMIT 1
            """),
            {"ex": exchange},
        ).first()
        return _row_to_dc(row) if row else None

    def range(
        self, *, exchange: str, n_days: int = 90,
    ) -> list[MarketOverviewRow]:
        """The last `n_days` of snapshots, oldest first, for plotting.

        Returns up to n_days rows. If fewer snapshots exist, returns
        whatever is available (caller should handle empty / sparse
        data gracefully — e.g. show 'no data' rather than an empty
        chart)."""
        if n_days <= 0:
            return []
        rows = self._conn.execute(
            text("""
                SELECT snapshot_date, exchange,
                       n_buys, n_sells, n_exit_buys, n_exit_sells,
                       n_green_regime, n_red_regime,
                       n_neutral_regime,
                       n_above_highest_ema, n_below_lowest_ema,
                       n_between_emas,
                       computed_at
                  FROM market_overview_daily
                 WHERE exchange = :ex
                   AND snapshot_date
                       >= (NOW() AT TIME ZONE :tz)::date
                          - CAST(:days AS integer)
                 ORDER BY snapshot_date ASC
            """),
            # snapshot_date is WRITTEN as (NOW() AT TIME ZONE :tz)::date — the
            # market's calendar. It was READ against CURRENT_DATE — the session's.
            # Two clocks for one column, agreeing only while the server's TZ
            # happens to equal the market's.
            {"ex": exchange, "days": n_days, "tz": market_tz_name(exchange)},
        ).all()
        return [_row_to_dc(r) for r in rows]

    def get(
        self, *, exchange: str, snapshot_date: date,
    ) -> MarketOverviewRow | None:
        """One-row lookup by composite PK."""
        row = self._conn.execute(
            text("""
                SELECT snapshot_date, exchange,
                       n_buys, n_sells, n_exit_buys, n_exit_sells,
                       n_green_regime, n_red_regime,
                       n_neutral_regime,
                       n_above_highest_ema, n_below_lowest_ema,
                       n_between_emas,
                       computed_at
                  FROM market_overview_daily
                 WHERE exchange = :ex AND snapshot_date = :d
            """),
            {"ex": exchange, "d": snapshot_date},
        ).first()
        return _row_to_dc(row) if row else None


def _row_to_dc(row) -> MarketOverviewRow:
    return MarketOverviewRow(
        snapshot_date=row.snapshot_date,
        exchange=row.exchange,
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
        computed_at=row.computed_at,
    )
