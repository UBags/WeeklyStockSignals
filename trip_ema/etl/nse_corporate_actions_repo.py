"""Repository for the nse_corporate_actions table.

Write path: the loader parses the nselib feed into atomic actions and upserts
them (idempotent — the dedupe index absorbs re-runs; a revised subject hashes
differently and lands as a new row). Read path: the reconcile pulls a ticker's
scale/censor events by ex_date.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import text
from sqlalchemy.engine import Connection

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class NseActionRecord:
    """One atomic corporate action, ready to persist."""

    isin: str
    symbol: str
    ex_date: date
    action_type: str
    subject: str
    ticker_id: int | None = None
    exchange: str = "NSE"
    detail: str | None = None
    ratio_numerator: int | None = None
    ratio_denominator: int | None = None
    price_factor: float | None = None
    censor: bool = False
    source: str = "nselib"
    raw_payload: dict | None = None


@dataclass(frozen=True, slots=True)
class FactorEvent:
    """A scale or censor event for one ticker, for the reconcile."""

    ex_date: date
    action_type: str
    price_factor: float | None
    censor: bool
    ratio_numerator: int | None
    ratio_denominator: int | None
    subject: str
    detected_at: datetime | None = None


def _json_safe(value):
    """Recursively replace non-finite floats (NaN / Inf) with None so the
    payload is valid JSON. Postgres jsonb rejects NaN, and the nselib feed
    uses NaN for missing numeric cells (e.g. faceVal). numpy float64 NaN is
    caught too (it subclasses float)."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


class NseCorporateActionsRepository:

    def __init__(self, conn: Connection):
        self._conn = conn

    # ---------- ticker resolution ---------- #

    def isin_to_ticker_id(self) -> dict[str, int]:
        """Map every populated tickers.isin to its ticker_id.

        char(12) is trimmed to a plain key so the loader can look up the feed's
        isin directly. Loaded once per refresh rather than one query per row.
        """
        rows = self._conn.execute(
            text(
                "SELECT TRIM(isin) AS isin, ticker_id "
                "FROM tickers WHERE isin IS NOT NULL"
            )
        )
        return {r.isin: r.ticker_id for r in rows}

    # ---------- write ---------- #

    def upsert_many(self, records: list[NseActionRecord]) -> int:
        """Idempotent insert of atomic actions. Returns the number of rows
        actually inserted (existing rows are left untouched — DO NOTHING).

        The conflict target is the dedupe index expression; NULLS NOT DISTINCT
        (set on the index) makes NULL ratios dedupe like values.
        """
        sql = text(
            """
            INSERT INTO nse_corporate_actions
                (ticker_id, isin, symbol, exchange, ex_date, action_type,
                 detail, ratio_numerator, ratio_denominator, price_factor,
                 censor, subject, source, raw_payload)
            VALUES
                (:ticker_id, :isin, :symbol, :exchange, :ex_date, :action_type,
                 :detail, :ratio_numerator, :ratio_denominator, :price_factor,
                 :censor, :subject, :source, CAST(:raw_payload AS jsonb))
            ON CONFLICT
                (isin, ex_date, action_type, ratio_numerator,
                 ratio_denominator, md5(subject))
            DO NOTHING
            """
        )
        inserted = 0
        for rec in records:
            result = self._conn.execute(
                sql,
                {
                    "ticker_id": rec.ticker_id,
                    "isin": rec.isin,
                    "symbol": rec.symbol,
                    "exchange": rec.exchange,
                    "ex_date": rec.ex_date,
                    "action_type": rec.action_type,
                    "detail": rec.detail,
                    "ratio_numerator": rec.ratio_numerator,
                    "ratio_denominator": rec.ratio_denominator,
                    "price_factor": rec.price_factor,
                    "censor": rec.censor,
                    "subject": rec.subject,
                    "source": rec.source,
                    "raw_payload": (
                        json.dumps(_json_safe(rec.raw_payload))
                        if rec.raw_payload else None
                    ),
                },
            )
            inserted += result.rowcount if result.rowcount and result.rowcount > 0 else 0
        return inserted

    # ---------- read (reconcile) ---------- #

    def factor_events_for_ticker(self, ticker_id: int) -> list[FactorEvent]:
        """A ticker's scale events (price_factor NOT NULL) AND censor events,
        oldest first. What the reconcile groups by ex_date."""
        rows = self._conn.execute(
            text(
                """
                SELECT ex_date, action_type, price_factor, censor,
                       ratio_numerator, ratio_denominator, subject, detected_at
                FROM nse_corporate_actions
                WHERE ticker_id = :tid
                  AND (price_factor IS NOT NULL OR censor = true)
                ORDER BY ex_date, action_type
                """
            ),
            {"tid": ticker_id},
        )
        return [
            FactorEvent(
                ex_date=r.ex_date,
                action_type=r.action_type,
                price_factor=(
                    float(r.price_factor) if r.price_factor is not None else None
                ),
                censor=r.censor,
                ratio_numerator=r.ratio_numerator,
                ratio_denominator=r.ratio_denominator,
                subject=r.subject,
                detected_at=r.detected_at,
            )
            for r in rows
        ]

    def count(self) -> int:
        return int(
            self._conn.execute(
                text("SELECT COUNT(*) AS n FROM nse_corporate_actions")
            ).scalar_one()
        )
