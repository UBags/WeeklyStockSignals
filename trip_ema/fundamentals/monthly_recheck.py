"""Monthly re-baseline of the reference layer. First Saturday, markets closed.

WHY THIS IS A RE-CHECK AND NOT A CACHE

fundamentals_fetch_status records tickers the provider has told us it has nothing
for, and the gap-only sweep skips them — which is what turned ~95 doomed fetches
per run into zero. Left alone, that would quietly ossify into a permanent negative
cache, and a negative cache that never expires is a lie with a longer shelf life.
A microcap yfinance ignores today may gain coverage next quarter.

We already have proof: the first sweep after recording verdicts moved shares from
2238 to 2250 covered. Twelve tickers that had been "failing" every run were
transient all along, and the old joint `except (Transient, Permanent)` could not
tell. Absence is not permanent just because it was permanent once.

So once a month we deliberately re-ask every dark ticker. It costs ~95 + ~102
fetches at ~2.3s — about eight minutes — and it self-heals:

  * a ticker that now HAS data      -> clear() releases the verdict (success path)
  * a ticker still dark             -> mark_unavailable() bumps `attempts` and
                                       `last_checked_at`, PRESERVING first_seen_at
                                       so "how long has this been dark" survives

Nothing is deleted, nothing expires on a timer. The record simply tells the truth
about the last time we asked.

WHY FIRST SATURDAY

Markets are closed, so this never contends with the live orchestrator's four daily
NSE ticks, and a slow yfinance pass cannot delay a signal.

DUE-GATING

Cron runs this DAILY and `--if-due` makes it a no-op unless today really is the
first Saturday AND no shares refresh has completed for this exchange this calendar
month. That is deliberately more robust than encoding the schedule in cron itself:

  * cron's day-of-month and day-of-week fields are OR-ed when both are restricted,
    so the obvious `0 9 1-7 * 6` fires on EVERY day 1-7 *and* every Saturday. It
    is a classic trap and it silently runs the job seven times a month.
  * a machine that was down on the first Saturday would simply miss the month.
    Due-gating on the DATA (has a run completed this month?) rather than on the
    calendar means the job catches up on the next run instead of being skipped.
"""
from __future__ import annotations

import logging
from calendar import SATURDAY
from dataclasses import dataclass
from datetime import date

from sqlalchemy import text
from sqlalchemy.engine import Engine

from .data_fidelity import FidelityReport, audit_exchange
from ..domain.market_session import market_today

logger = logging.getLogger(__name__)


def is_first_saturday(d: date) -> bool:
    """First Saturday of the month: a Saturday falling on days 1-7."""
    return d.weekday() == SATURDAY and d.day <= 7


def _refreshed_this_month(db_engine: Engine, exchange: str, today: date) -> bool:
    """Has a shares refresh already COMPLETED for this exchange this month?

    Due-gate on the data, not the clock. A manual `refresh-reference` earlier in
    the month counts — and should: the point is that the reference layer was
    re-asked, not who asked it.
    """
    with db_engine.connect() as conn:
        row = conn.execute(
            text("""
                SELECT 1
                  FROM fundamentals_run
                 WHERE phase = 'shares'
                   AND exchange = :ex
                   AND completed_at IS NOT NULL
                   AND completed_at >= CAST(:month_start AS date)
                 LIMIT 1
            """),
            {"ex": exchange, "month_start": today.replace(day=1)},
        ).first()
    return row is not None


@dataclass(frozen=True)
class RecheckReport:
    exchange: str
    ran: bool
    reason: str
    before: FidelityReport | None = None
    after: FidelityReport | None = None

    def render(self) -> str:
        if not self.ran:
            return f"reference re-check — {self.exchange}: skipped ({self.reason})"

        lines = [f"reference re-check — {self.exchange}", ""]
        if self.before is None or self.after is None:
            return "\n".join(lines)

        b = {d.dataset: d for d in self.before.datasets}
        lines.append(
            f"  {'DATASET':<24} {'BEFORE':>16}  {'AFTER':>16}  DELTA"
        )
        lines.append(f"  {'-' * 24} {'-' * 16}  {'-' * 16}  {'-' * 24}")
        for a in self.after.datasets:
            prev = b.get(a.dataset)
            if prev is None or a.covered is None or prev.covered is None:
                continue
            d_cov = a.covered - prev.covered
            d_dark = a.unavailable - prev.unavailable
            bits = []
            if d_cov:
                # The whole point of re-asking: a dark ticker that came back.
                bits.append(f"{d_cov:+d} covered")
            if d_dark:
                bits.append(f"{d_dark:+d} unavailable")
            delta = ", ".join(bits) or "no change"
            lines.append(
                f"  {a.dataset:<24} "
                f"{prev.covered}/{prev.universe:<10} "
                f"{a.covered}/{a.universe:<10} {delta}"
            )
        lines.append("")
        lines.append(
            f"  READY: {'yes' if self.after.ready else 'NO'}"
            + ("" if self.after.ready else
               f"  ({len(self.after.gaps)} gap(s), "
               f"{len(self.after.blockers)} blocker(s))")
        )
        return "\n".join(lines)


def run_monthly_recheck(
    db_engine: Engine,
    config,
    exchange: str,
    *,
    if_due: bool = False,
    today: date | None = None,
) -> RecheckReport:
    """Audit, re-ask every dark ticker, audit again, report the delta.

    `if_due` makes this safe to run from a daily cron: it no-ops unless today is
    the first Saturday AND nothing has refreshed this month.
    """
    # Imported here rather than at module scope: runners imports the fundamentals
    # fetchers, which pull yfinance, and this module is imported by the CLI at
    # startup for every subcommand.
    from .runners import (
        run_financials_refresh,
        run_industry_refresh,
        run_shares_refresh,
    )

    today = today or market_today(exchange)

    if if_due:
        if not is_first_saturday(today):
            return RecheckReport(
                exchange, False,
                f"{today.isoformat()} is not the first Saturday of the month",
            )
        if _refreshed_this_month(db_engine, exchange, today):
            return RecheckReport(
                exchange, False,
                "a shares refresh already completed this month",
            )

    gap_days = config.etl.max_coverage_gap_days
    before = audit_exchange(db_engine, exchange, gap_days=gap_days)
    logger.info(
        "reference re-check starting for %s (%d dataset(s) with gaps)",
        exchange, len(before.gaps),
    )

    # (runner name, function, the audit dataset that mirrors it)
    #
    # Industry belongs here for the same reason the other two do, and its absence
    # was not a judgement call — the industry runner simply never learned to
    # record or skip a verdict, so there was nothing to re-ask. Now there is.
    # Ordering mirrors backfill(): industry precedes the index build, shares
    # precedes market cap.
    _RUNNERS = (
        ("industry", run_industry_refresh, "instrument_industry"),
        ("shares", run_shares_refresh, "instrument_shares"),
        ("financials", run_financials_refresh, "quarterly_financials"),
    )
    dark_before = {d.dataset: d.unavailable for d in before.datasets}

    for name, fn, audit_dataset in _RUNNERS:
        logger.info(
            "[%s] re-asking gaps INCLUDING %d ticker(s) previously recorded "
            "as unavailable upstream",
            name, dark_before.get(audit_dataset, 0),
        )
        fn(
            db_engine, exchange,
            force=True,
            only_missing=True,
            # THE POINT OF THIS JOB. The ordinary sweep skips known-dark tickers,
            # which is what makes it cheap. Here we deliberately do NOT skip them:
            # a provider's "no data" is a fact about last month, not a permanent
            # property of the company.
            recheck_unavailable=True,
        )

    after = audit_exchange(db_engine, exchange, gap_days=gap_days)
    report = RecheckReport(exchange, True, "completed", before, after)
    logger.info("reference re-check complete for %s", exchange)
    return report
