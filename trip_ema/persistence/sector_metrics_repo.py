"""sector_stock_leadlag persistence. rs_rank is upserted column-scoped so a
later §16 pass writing insync_corr/etc. on the same row is not clobbered."""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..domain.sector_metric import SectorStockMetric

_INSERT = text(
    """
    INSERT INTO sector_stock_leadlag (
        ticker_id, asof_date, window, industry_name, rs_rank, computed_at
    ) VALUES (:t, :d, :w, :ind, :rr, NOW())
    ON CONFLICT (ticker_id, asof_date, window) DO UPDATE SET
        industry_name = EXCLUDED.industry_name,
        rs_rank = EXCLUDED.rs_rank,
        computed_at = NOW()
    """
)


class SectorStockLeadlagRepository:
    def __init__(self, conn: Connection):
        self._conn = conn

    @staticmethod
    def _params(row: SectorStockMetric) -> dict:
        return {
            "t": row.ticker_id, "d": row.asof_date, "w": row.window,
            "ind": row.industry_name, "rr": row.rs_rank,
        }

    def upsert_many(self, rows: list[SectorStockMetric]) -> int:
        if not rows:
            return 0
        self._conn.execute(_INSERT, [self._params(r) for r in rows])
        return len(rows)

    def count(self) -> int:
        return int(self._conn.execute(
            text("SELECT count(*) FROM sector_stock_leadlag")
        ).scalar_one())
