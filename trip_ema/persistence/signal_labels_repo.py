"""signal_labels persistence: one row per enriched signal, upserted on
the signal_enrichment_id FK."""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..domain.signal_label import SignalLabel

_INSERT = text(
    """
    INSERT INTO signal_labels (
        signal_enrichment_id, fwd_ret_4w, fwd_ret_8w, fwd_ret_13w,
        fwd_max_dd, fwd_rr, horizon_complete, computed_at
    ) VALUES (
        :seid, :r4, :r8, :r13, :dd, :rr, :hc, NOW()
    )
    ON CONFLICT (signal_enrichment_id) DO UPDATE SET
        fwd_ret_4w = EXCLUDED.fwd_ret_4w,
        fwd_ret_8w = EXCLUDED.fwd_ret_8w,
        fwd_ret_13w = EXCLUDED.fwd_ret_13w,
        fwd_max_dd = EXCLUDED.fwd_max_dd,
        fwd_rr = EXCLUDED.fwd_rr,
        horizon_complete = EXCLUDED.horizon_complete,
        computed_at = NOW()
    """
)


class SignalLabelsRepository:
    def __init__(self, conn: Connection):
        self._conn = conn

    @staticmethod
    def _params(row: SignalLabel) -> dict:
        return {
            "seid": row.signal_enrichment_id,
            "r4": row.fwd_ret_4w, "r8": row.fwd_ret_8w,
            "r13": row.fwd_ret_13w, "dd": row.fwd_max_dd,
            "rr": row.fwd_rr,
            "hc": row.horizon_complete,
        }

    def upsert_many(self, rows: list[SignalLabel]) -> int:
        if not rows:
            return 0
        self._conn.execute(_INSERT, [self._params(r) for r in rows])
        return len(rows)

    def count(self) -> int:
        return int(
            self._conn.execute(
                text("SELECT count(*) FROM signal_labels")
            ).scalar_one()
        )
