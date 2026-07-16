"""Monthly recompute of the lead/lag moving-window tables.

The lead/lag correlations (sector_stock_leadlag, industry_regime_corr,
industry_regime_breadth, influential_industry_ensemble) are computed over a
trailing window of weeks. They already refresh as backfill stages 6b/6c, but a
full backfill is heavy and event-driven — so this job re-runs ONLY those two
stages once a month to slide the window forward and let stale correlations age
out, capturing the recency of changing relationships.

Same discipline as monthly_recheck.py:
  * First Saturday, markets closed — never contends with the live ticks.
  * Due-gated on the DATA, not the clock: if the tables were already recomputed
    this calendar month (by a full backfill or an earlier run), this no-ops. So
    it is safe to call daily from cron with --if-due, and it never double-works
    with a backfill that already refreshed the window.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

from sqlalchemy import text
from sqlalchemy.engine import Engine

from ..domain.market_session import market_today
from .industry_regime_runner import backfill_industry_regime_matrix
from .monthly_recheck import is_first_saturday
from .sector_metrics_runner import backfill_sector_metrics

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LeadlagRecomputeReport:
    exchange: str
    ran: bool
    reason: str

    def render(self) -> str:
        status = "completed" if self.ran else f"skipped ({self.reason})"
        return f"lead/lag recompute — {self.exchange}: {status}"


def _recomputed_this_month(db_engine: Engine, today: date) -> bool:
    """True only if BOTH lead/lag sides carry a computed_at in the current
    calendar month. Due-gate on the data's freshness, not the clock — a full
    backfill earlier this month counts and correctly suppresses the job.

    Single-exchange: the lead/lag tables carry no exchange column yet (the
    multi-exchange blocker), so this gate is global. Revisit when that column
    lands so the gate can scope per exchange.
    """
    month_start = today.replace(day=1)
    with db_engine.connect() as conn:
        matrix = conn.execute(
            text("SELECT 1 FROM industry_regime_corr "
                 "WHERE computed_at >= CAST(:ms AS date) LIMIT 1"),
            {"ms": month_start},
        ).first()
        sector = conn.execute(
            text("SELECT 1 FROM sector_stock_leadlag "
                 "WHERE computed_at >= CAST(:ms AS date) LIMIT 1"),
            {"ms": month_start},
        ).first()
    return matrix is not None and sector is not None


def run_monthly_leadlag_recompute(
    db_engine: Engine,
    exchange: str,
    *,
    if_due: bool = False,
    today: date | None = None,
    sector_fn: Callable = backfill_sector_metrics,
    matrix_fn: Callable = backfill_industry_regime_matrix,
) -> LeadlagRecomputeReport:
    """Recompute the lead/lag moving-window tables for `exchange`.

    if_due — makes this safe to run daily from cron: no-op unless today is the
    first Saturday of the month AND the tables were not already recomputed this
    month. sector_fn / matrix_fn are injectable for testing.
    """
    today = today or market_today(exchange)

    if if_due:
        if not is_first_saturday(today):
            return LeadlagRecomputeReport(
                exchange, False,
                f"{today.isoformat()} is not the first Saturday of the month",
            )
        if _recomputed_this_month(db_engine, today):
            return LeadlagRecomputeReport(
                exchange, False, "lead/lag already recomputed this month",
            )

    logger.info("lead/lag recompute starting for %s", exchange)
    # Ordering mirrors backfill(): sector metrics (6b) before the matrix (6c).
    sector_fn(db_engine, exchange)
    matrix_fn(db_engine, exchange)
    logger.info("lead/lag recompute complete for %s", exchange)
    return LeadlagRecomputeReport(exchange, True, "completed")
