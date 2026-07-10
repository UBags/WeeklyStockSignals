"""sector_stock_leadlag persistence. rs_rank is upserted column-scoped so a
later §16 pass writing insync_corr/etc. on the same row is not clobbered."""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..domain.sector_leadlag_metric import SectorLeadlagMetric
from ..domain.sector_metric import SectorStockMetric

_INSERT = text(
    """
    INSERT INTO sector_stock_leadlag (
        ticker_id, asof_date, "window", industry_name, rs_rank, computed_at
    ) VALUES (:t, :d, :w, :ind, :rr, NOW())
    ON CONFLICT (ticker_id, asof_date, "window") DO UPDATE SET
        industry_name = EXCLUDED.industry_name,
        rs_rank = EXCLUDED.rs_rank,
        computed_at = NOW()
    """
)


_INSERT_LL = text(
    """
    INSERT INTO sector_stock_leadlag (
        ticker_id, asof_date, "window", industry_name, is_constituent,
        insync_corr, best_lag, lead_lag_corr, beta_sector, n_obs,
        coupling_class, computed_at
    ) VALUES (
        :t, :d, :w, :ind, :isc, :ic, :bl, :llc, :bs, :n, :cc, NOW()
    )
    ON CONFLICT (ticker_id, asof_date, "window") DO UPDATE SET
        industry_name = EXCLUDED.industry_name,
        is_constituent = EXCLUDED.is_constituent,
        insync_corr = EXCLUDED.insync_corr,
        best_lag = EXCLUDED.best_lag,
        lead_lag_corr = EXCLUDED.lead_lag_corr,
        beta_sector = EXCLUDED.beta_sector,
        n_obs = EXCLUDED.n_obs,
        coupling_class = EXCLUDED.coupling_class,
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

    def upsert_leadlag_many(self, rows: list[SectorLeadlagMetric]) -> int:
        if not rows:
            return 0
        self._conn.execute(
            _INSERT_LL, [self._params_ll(r) for r in rows],
        )
        return len(rows)

    @staticmethod
    def _params_ll(row: SectorLeadlagMetric) -> dict:
        return {
            "t": row.ticker_id, "d": row.asof_date, "w": row.window,
            "ind": row.industry_name, "isc": row.is_constituent,
            "ic": row.insync_corr, "bl": row.best_lag,
            "llc": row.lead_lag_corr, "bs": row.beta_sector,
            "n": row.n_obs, "cc": row.coupling_class,
        }

    def count(self) -> int:
        return int(self._conn.execute(
            text("SELECT count(*) FROM sector_stock_leadlag")
        ).scalar_one())
