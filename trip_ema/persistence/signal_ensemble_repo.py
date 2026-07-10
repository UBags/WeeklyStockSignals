"""signal_ensemble_features persistence (spec §17.5)."""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..domain.signal_ensemble_features import SignalEnsembleFeatures

_INSERT = text(
    """
    INSERT INTO signal_ensemble_features (
        signal_enrichment_id, variant, ensemble_score, ensemble_confirms,
        ensemble_agreement, n_leaders_bull, n_leaders_bear, computed_at
    ) VALUES (:sid, :v, :sc, :cf, :ag, :lb, :le, NOW())
    ON CONFLICT (signal_enrichment_id, variant) DO UPDATE SET
        ensemble_score = EXCLUDED.ensemble_score,
        ensemble_confirms = EXCLUDED.ensemble_confirms,
        ensemble_agreement = EXCLUDED.ensemble_agreement,
        n_leaders_bull = EXCLUDED.n_leaders_bull,
        n_leaders_bear = EXCLUDED.n_leaders_bear,
        computed_at = NOW()
    """
)


class SignalEnsembleFeaturesRepository:
    def __init__(self, conn: Connection):
        self._conn = conn

    def upsert_many(self, rows: list[SignalEnsembleFeatures]) -> int:
        if not rows:
            return 0
        self._conn.execute(_INSERT, [
            {
                "sid": r.signal_enrichment_id, "v": r.variant,
                "sc": r.ensemble_score, "cf": r.ensemble_confirms,
                "ag": r.ensemble_agreement, "lb": r.n_leaders_bull,
                "le": r.n_leaders_bear,
            }
            for r in rows
        ])
        return len(rows)
