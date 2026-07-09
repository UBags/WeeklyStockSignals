"""Persistence for signal_enrichment (per-signal ML feature store, §19).

Mirrors the stock_indicator_state_repo pattern: explicit-column
INSERT ... ON CONFLICT (natural key) DO UPDATE. `features` is bound as a
json.dumps string and CAST to JSONB in SQL (the codebase's JSONB write
convention). Idempotent on (ticker_id, timeframe, signal_date,
signal_type).
"""
from __future__ import annotations

import json

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..domain.signal_enrichment import SignalEnrichment

_COLUMNS = """
    ticker_id, timeframe, signal_date, signal_type,
    industry_id, features, feature_spec_version,
    fwd_ret_4w, fwd_ret_8w, fwd_ret_13w, fwd_max_dd,
    quality_class, computed_at
"""
_VALUES = """
    :tid, :tf, :sd, :st,
    :ind, CAST(:feat AS JSONB), :ver,
    :r4, :r8, :r13, :dd,
    :qc, NOW()
"""
_ON_CONFLICT = """
    ON CONFLICT (ticker_id, timeframe, signal_date, signal_type)
    DO UPDATE SET
        industry_id = EXCLUDED.industry_id,
        features = EXCLUDED.features,
        feature_spec_version = EXCLUDED.feature_spec_version,
        fwd_ret_4w = EXCLUDED.fwd_ret_4w,
        fwd_ret_8w = EXCLUDED.fwd_ret_8w,
        fwd_ret_13w = EXCLUDED.fwd_ret_13w,
        fwd_max_dd = EXCLUDED.fwd_max_dd,
        quality_class = EXCLUDED.quality_class,
        computed_at = NOW()
"""
_INSERT = (
    f"INSERT INTO signal_enrichment ({_COLUMNS}) "
    f"VALUES ({_VALUES}) {_ON_CONFLICT}"
)


class SignalEnrichmentRepository:
    def __init__(self, conn: Connection):
        self._conn = conn

    def upsert_one(self, row: SignalEnrichment) -> None:
        self._conn.execute(text(_INSERT), self._params(row))

    def upsert_many(self, rows: list[SignalEnrichment]) -> int:
        if not rows:
            return 0
        self._conn.execute(text(_INSERT), [self._params(r) for r in rows])
        return len(rows)

    def get_for_signal(
        self, ticker_id: int, timeframe: str, signal_date, signal_type: str,
    ) -> SignalEnrichment | None:
        row = self._conn.execute(
            text(
                "SELECT ticker_id, timeframe, signal_date, signal_type, "
                "industry_id, features, feature_spec_version, "
                "fwd_ret_4w, fwd_ret_8w, fwd_ret_13w, fwd_max_dd, "
                "quality_class FROM signal_enrichment "
                "WHERE ticker_id = :tid AND timeframe = :tf "
                "AND signal_date = :sd AND signal_type = :st"
            ),
            {"tid": ticker_id, "tf": timeframe, "sd": signal_date,
             "st": signal_type},
        ).fetchone()
        if row is None:
            return None
        feats = row.features
        if isinstance(feats, str):
            feats = json.loads(feats)
        return SignalEnrichment(
            ticker_id=row.ticker_id, timeframe=row.timeframe,
            signal_date=row.signal_date, signal_type=row.signal_type,
            features=feats, feature_spec_version=row.feature_spec_version,
            industry_id=row.industry_id,
            fwd_ret_4w=row.fwd_ret_4w, fwd_ret_8w=row.fwd_ret_8w,
            fwd_ret_13w=row.fwd_ret_13w, fwd_max_dd=row.fwd_max_dd,
            quality_class=row.quality_class,
        )

    def count(self) -> int:
        return int(self._conn.execute(
            text("SELECT COUNT(*) FROM signal_enrichment")
        ).scalar())

    @staticmethod
    def _params(row: SignalEnrichment) -> dict:
        return {
            "tid": row.ticker_id, "tf": row.timeframe,
            "sd": row.signal_date, "st": row.signal_type,
            "ind": row.industry_id,
            "feat": json.dumps(row.features),
            "ver": row.feature_spec_version,
            "r4": row.fwd_ret_4w, "r8": row.fwd_ret_8w,
            "r13": row.fwd_ret_13w, "dd": row.fwd_max_dd,
            "qc": row.quality_class,
        }
