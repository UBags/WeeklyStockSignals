"""industry_regime_corr persistence (spec §17). Single current snapshot;
upsert on (industry_i, industry_j, window) overwrites with the latest."""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..domain.industry_regime_corr import IndustryRegimeCorr

_INSERT = text(
    """
    INSERT INTO industry_regime_corr (
        industry_i, industry_j, "window", asof_date, corr, best_lag,
        lead_lag_corr, n_obs, computed_at
    ) VALUES (:i, :j, :w, :d, :c, :bl, :llc, :n, NOW())
    ON CONFLICT (industry_i, industry_j, "window") DO UPDATE SET
        asof_date = EXCLUDED.asof_date,
        corr = EXCLUDED.corr,
        best_lag = EXCLUDED.best_lag,
        lead_lag_corr = EXCLUDED.lead_lag_corr,
        n_obs = EXCLUDED.n_obs,
        computed_at = NOW()
    """
)


class IndustryRegimeCorrRepository:
    def __init__(self, conn: Connection):
        self._conn = conn

    def upsert_many(self, rows: list[IndustryRegimeCorr]) -> int:
        if not rows:
            return 0
        self._conn.execute(_INSERT, [
            {
                "i": r.industry_i, "j": r.industry_j, "w": r.window,
                "d": r.asof_date, "c": r.corr, "bl": r.best_lag,
                "llc": r.lead_lag_corr, "n": r.n_obs,
            }
            for r in rows
        ])
        return len(rows)

    def count(self) -> int:
        return int(self._conn.execute(
            text("SELECT count(*) FROM industry_regime_corr")
        ).scalar_one())
