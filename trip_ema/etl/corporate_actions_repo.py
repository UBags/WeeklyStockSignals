"""Repository for the corporate_actions table.

Two write paths:
  1. The reconciler emits DivergenceFlags for old bars that differ
     from current yfinance values. The runner writes these as
     `candidate_revision` rows with status='pending_review'.
  2. yfinance's `Ticker.actions` API returns real splits/dividends.
     These are written as `split` or `dividend` rows with
     status='recorded' (no manual review needed).

The approve workflow:
  - `trip-ema-etl approve-action <id>` promotes a pending_review row
    to 'approved' and triggers a reprime of the affected ticker.
  - The PySide GUI (Slice 2) will surface pending rows for review.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .reconciler import DivergenceFlag

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CorporateActionRecord:
    action_id: int
    ticker_id: int
    action_type: str
    effective_date: date
    ratio_numerator: float | None
    ratio_denominator: float | None
    amount: float | None
    source: str
    status: str
    detected_at: datetime
    reviewed_at: datetime | None
    reviewed_by: str | None
    notes: str | None


class CorporateActionsRepository:

    def __init__(self, conn: Connection):
        self._conn = conn

    # ---------- divergence flagging ---------- #

    def record_divergence_candidates(
        self,
        ticker_id: int,
        divergences: list[DivergenceFlag] | tuple[DivergenceFlag, ...],
    ) -> int:
        """Write one corporate_actions row per divergence with
        status='pending_review' and action_type='candidate_revision'.

        Returns the number of rows written. Uses INSERT ON CONFLICT
        DO NOTHING so re-running the same ETL pass doesn't duplicate
        pending rows (uniqueness by ticker_id + effective_date +
        action_type='candidate_revision' is enforced at the app
        level — duplicate inserts get filtered).

        We do the dedup with a NOT EXISTS check rather than a unique
        constraint because real splits/bonuses CAN repeat on the same
        date (rare but possible — a stock can have a bonus AND a
        dividend ex-date on the same day)."""
        if not divergences:
            return 0
        written = 0
        for d in divergences:
            payload = {
                "stored_close": d.stored_close,
                "new_close": d.new_close,
                "stored_adj_close": d.stored_adj_close,
                "new_adj_close": d.new_adj_close,
                "delta_pct": d.delta_pct,
            }
            result = self._conn.execute(
                text("""
                    INSERT INTO corporate_actions
                        (ticker_id, action_type, effective_date,
                         source, status, raw_payload, notes)
                    SELECT :tid, 'candidate_revision', :ed,
                           'reconciler', 'pending_review',
                           CAST(:payload AS jsonb),
                           :notes
                    WHERE NOT EXISTS (
                        SELECT 1 FROM corporate_actions
                        WHERE ticker_id = :tid
                          AND effective_date = :ed
                          AND action_type = 'candidate_revision'
                          AND status = 'pending_review'
                    )
                    RETURNING action_id
                """),
                {
                    "tid": ticker_id,
                    "ed": d.trading_date,
                    "payload": json.dumps(payload),
                    "notes": (
                        f"close diverged: stored={d.stored_close:.4f}, "
                        f"new={d.new_close:.4f} ({d.delta_pct * 100:+.2f}%)"
                    ),
                },
            )
            if result.rowcount > 0:
                written += 1
        return written

    # ---------- real corporate actions (splits, dividends) ---------- #

    def record_action(
        self,
        ticker_id: int,
        action_type: str,
        effective_date: date,
        *,
        ratio_numerator: float | None = None,
        ratio_denominator: float | None = None,
        amount: float | None = None,
        source: str = "yfinance",
        raw_payload: dict | None = None,
    ) -> int | None:
        """Record a real split/bonus/dividend from yfinance's actions
        API. Returns the new action_id, or None if a row for the same
        (ticker_id, action_type, effective_date) already exists."""
        result = self._conn.execute(
            text("""
                INSERT INTO corporate_actions
                    (ticker_id, action_type, effective_date,
                     ratio_numerator, ratio_denominator, amount,
                     source, status, raw_payload)
                SELECT :tid, :at, :ed, :rn, :rd, :amt, :src, 'recorded',
                       CAST(:payload AS jsonb)
                WHERE NOT EXISTS (
                    SELECT 1 FROM corporate_actions
                    WHERE ticker_id = :tid
                      AND action_type = :at
                      AND effective_date = :ed
                )
                RETURNING action_id
            """),
            {
                "tid": ticker_id, "at": action_type, "ed": effective_date,
                "rn": ratio_numerator, "rd": ratio_denominator,
                "amt": amount, "src": source,
                "payload": json.dumps(raw_payload) if raw_payload else None,
            },
        )
        row = result.first()
        return row.action_id if row else None

    # ---------- review workflow ---------- #

    def list_pending(self, limit: int = 100) -> list[CorporateActionRecord]:
        result = self._conn.execute(
            text("""
                SELECT action_id, ticker_id, action_type, effective_date,
                       ratio_numerator, ratio_denominator, amount,
                       source, status, detected_at, reviewed_at,
                       reviewed_by, notes
                FROM corporate_actions
                WHERE status = 'pending_review'
                ORDER BY detected_at ASC
                LIMIT :lim
            """),
            {"lim": limit},
        )
        return [self._row_to_record(r) for r in result]

    def get(self, action_id: int) -> CorporateActionRecord | None:
        result = self._conn.execute(
            text("""
                SELECT action_id, ticker_id, action_type, effective_date,
                       ratio_numerator, ratio_denominator, amount,
                       source, status, detected_at, reviewed_at,
                       reviewed_by, notes
                FROM corporate_actions WHERE action_id = :aid
            """),
            {"aid": action_id},
        ).first()
        return self._row_to_record(result) if result else None

    def approve(
        self,
        action_id: int,
        *,
        reviewed_by: str = "cli",
        notes: str | None = None,
    ) -> bool:
        """Promote pending_review → approved. Returns True if a row
        was actually transitioned (False if action_id doesn't exist
        or wasn't in pending_review)."""
        result = self._conn.execute(
            text("""
                UPDATE corporate_actions SET
                    status      = 'approved',
                    reviewed_at = NOW(),
                    reviewed_by = :by,
                    notes       = CASE
                        WHEN :notes IS NULL THEN notes
                        WHEN notes IS NULL THEN :notes
                        ELSE notes || E'\n[approved] ' || :notes
                    END
                WHERE action_id = :aid AND status = 'pending_review'
            """),
            {"aid": action_id, "by": reviewed_by, "notes": notes},
        )
        return result.rowcount > 0

    def reject(
        self,
        action_id: int,
        *,
        reviewed_by: str = "cli",
        notes: str | None = None,
    ) -> bool:
        result = self._conn.execute(
            text("""
                UPDATE corporate_actions SET
                    status      = 'rejected',
                    reviewed_at = NOW(),
                    reviewed_by = :by,
                    notes       = CASE
                        WHEN :notes IS NULL THEN notes
                        WHEN notes IS NULL THEN :notes
                        ELSE notes || E'\n[rejected] ' || :notes
                    END
                WHERE action_id = :aid AND status = 'pending_review'
            """),
            {"aid": action_id, "by": reviewed_by, "notes": notes},
        )
        return result.rowcount > 0

    # ---------- helpers ---------- #

    @staticmethod
    def _row_to_record(row) -> CorporateActionRecord:
        return CorporateActionRecord(
            action_id=row.action_id,
            ticker_id=row.ticker_id,
            action_type=row.action_type,
            effective_date=row.effective_date,
            ratio_numerator=(
                float(row.ratio_numerator) if row.ratio_numerator is not None else None
            ),
            ratio_denominator=(
                float(row.ratio_denominator) if row.ratio_denominator is not None else None
            ),
            amount=float(row.amount) if row.amount is not None else None,
            source=row.source,
            status=row.status,
            detected_at=row.detected_at,
            reviewed_at=row.reviewed_at,
            reviewed_by=row.reviewed_by,
            notes=row.notes,
        )
