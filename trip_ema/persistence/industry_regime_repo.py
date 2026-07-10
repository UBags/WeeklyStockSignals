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


_INSERT_BREADTH = text(
    """
    INSERT INTO industry_regime_breadth (
        industry_name, asof_date, breadth, computed_at
    ) VALUES (:ind, :d, :b, NOW())
    ON CONFLICT (industry_name, asof_date) DO UPDATE SET
        breadth = EXCLUDED.breadth, computed_at = NOW()
    """
)

_INSERT_ENS = text(
    """
    INSERT INTO influential_industry_ensemble (
        industry_i, member_j, variant, corr, best_lag, is_leader, sign,
        asof_date, computed_at
    ) VALUES (:i, :j, :v, :c, :bl, :isl, :sg, :d, NOW())
    """
)


class IndustryBreadthRepository:
    def __init__(self, conn: Connection):
        self._conn = conn

    def upsert_many(self, rows) -> int:
        if not rows:
            return 0
        self._conn.execute(_INSERT_BREADTH, [
            {"ind": r.industry_name, "d": r.asof_date, "b": r.breadth}
            for r in rows
        ])
        return len(rows)


class InfluentialEnsembleRepository:
    def __init__(self, conn: Connection):
        self._conn = conn

    def replace_all(self, rows) -> int:
        """Full rebuild — membership changes between runs, so clear and
        reinsert (safe: the regime phase is a single global pass)."""
        self._conn.execute(text("DELETE FROM influential_industry_ensemble"))
        if not rows:
            return 0
        self._conn.execute(_INSERT_ENS, [
            {
                "i": r.industry_i, "j": r.member_j, "v": r.variant,
                "c": r.corr, "bl": r.best_lag, "isl": r.is_leader,
                "sg": r.sign, "d": r.asof_date,
            }
            for r in rows
        ])
        return len(rows)
