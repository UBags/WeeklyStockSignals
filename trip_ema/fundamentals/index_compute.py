"""Index constituent picker + modified-Laspeyres value computer.

Phase 4B-2. Two pure functions and one shared dataclass; the runner
in runners.py wires these into rebalance and value-backfill loops.

Design overview (see docs/FUNDAMENTALS_PHASES_1_TO_8.md §20):

  Selection:
    Top-N tickers by daily_market_cap.market_cap on rebalance_date,
    scoped to:
      - instrument_industry.industry = industry_name (resolving
        industry_override beats yfinance auto-classification)
      - tickers.instrument_type = 'EQ' (ETFs excluded)
      - tickers.is_active = TRUE and not delisted as of
        rebalance_date.
      - market_cap > 0 on rebalance_date.

  Weighting:
    weight_i = mcap_i / Σ mcap_j across the picked top-N. Fixed at
    rebalance.

  Daily value:
    value(t) = base_value × Σ_i [
        weight_i × close_i(t) / close_i(rebalance_date)
    ]
    where the sum is over CONSTITUENTS-PRESENT-ON-DAY-t. Missing
    constituents (delisted or no mcap row that day) are dropped
    and the surviving constituents' weights are re-normalized via
    division by Σ surviving weights. `renormalized=True` is
    recorded on the value row for those days.

What this module deliberately does NOT do:
  - DB writes from the public API. Selection returns
    SelectedConstituent dataclasses; the runner writes.
    Computation returns a ComputedValue dataclass; the runner
    writes. This keeps the compute logic testable in isolation.
  - Auto-discovery (which industries qualify). That's the runner's
    job — it iterates instrument_industry counts and calls into
    here per qualifying industry.
  - Currency conversion. All math is in INR (per Phase 1 design).
    A multi-currency universe would need a conversion step before
    this module sees the inputs.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Optional

from sqlalchemy import Engine, text

from ..persistence.fundamentals_repo import (
    DailyMarketCapRepository,
    IndustryIndexConstituentRepository,
    IndustryIndexRepository,
)

logger = logging.getLogger(__name__)


# ---------- result dataclasses ---------- #


@dataclass(frozen=True, slots=True)
class SelectedConstituent:
    """One constituent picked at a rebalance.

    weight is computed at selection time as
    mcap / Σ mcap across the top-N picked. Sum of weights across a
    selection result is 1.0 modulo float rounding.

    close_price is the close on rebalance_date for this ticker,
    pulled from daily_market_cap. Stored alongside so the runner
    can persist it without re-querying.
    """
    ticker_id: int
    symbol: str
    market_cap: float
    close_price: float
    weight: float


@dataclass(frozen=True, slots=True)
class ComputedValue:
    """One day's index value plus the metadata needed to persist it.

    constituents_used + constituents_missing equals the total
    constituents active on the effective rebalance. `renormalized`
    is True if any were missing. None means "not enough data to
    compute" — the runner skips writing for these.
    """
    industry_name: str
    trade_date: date
    value: float
    constituents_used: int
    constituents_missing: int
    renormalized: bool
    effective_rebalance_date: date


# ---------- selection ---------- #


def select_top_n_constituents(
    db_engine: Engine,
    industry_name: str,
    rebalance_date: date,
    *,
    n: int = 10,
    exchange: Optional[str] = None,
) -> list[SelectedConstituent]:
    """Pick the top-N tickers by market cap for `industry_name` on
    `rebalance_date`.

    The selection joins:
      - tickers (filters to active EQ on the rebalance_date)
      - effective industry classification (industry_override beats
        instrument_industry; matches Phase 1's read semantics)
      - daily_market_cap (filters to rows ON rebalance_date)

    If fewer than `n` tickers qualify, returns whatever's there.
    The caller checks against min_constituents before treating
    the result as a valid rebalance.

    Notes on the SQL:
      - We pre-resolve "what's the effective industry per ticker"
        with a subquery rather than a CTE, because Phase 1's
        existing get-by-industry logic uses the same shape and
        we want consistency.
      - The 'EQ' filter on instrument_type excludes ETFs.
      - delisted_at IS NULL OR delisted_at > rebalance_date allows
        tickers that delisted AFTER the rebalance (they were valid
        constituents at picking time).
    """
    # Build the WHERE clauses. Exchange is optional — None means
    # "any exchange the operator has classified into this industry".
    where_clauses = [
        "t.is_active = TRUE",
        "t.instrument_type = 'EQ'",
        "(t.delisted_at IS NULL OR t.delisted_at > :rd)",
        "effective_industry.industry = :ind",
        "mc.trade_date = :rd",
        "mc.market_cap > 0",
    ]
    params: dict = {"ind": industry_name, "rd": rebalance_date, "n": n}
    if exchange is not None:
        where_clauses.append("t.exchange = :ex")
        params["ex"] = exchange

    where_sql = " AND ".join(where_clauses)

    # The effective industry per ticker: override takes precedence
    # over the auto-classified row. COALESCE in the subquery handles
    # this — if industry_override has a row, its industry wins;
    # otherwise instrument_industry's industry is used.
    sql = f"""
        WITH effective_industry AS (
            SELECT
                t.ticker_id,
                COALESCE(io.industry, ii.industry) AS industry
            FROM tickers t
            LEFT JOIN industry_override io
              ON io.ticker_id = t.ticker_id
            LEFT JOIN instrument_industry ii
              ON ii.ticker_id = t.ticker_id
        )
        SELECT
            t.ticker_id,
            t.symbol,
            mc.market_cap,
            mc.close_price
        FROM tickers t
        JOIN effective_industry
          ON effective_industry.ticker_id = t.ticker_id
        JOIN daily_market_cap mc
          ON mc.ticker_id = t.ticker_id
        WHERE {where_sql}
        ORDER BY mc.market_cap DESC
        LIMIT :n
    """

    with db_engine.connect() as conn:
        rows = conn.execute(text(sql), params).fetchall()

    if not rows:
        return []

    # Compute weights. Sum of market caps over the picked set
    # forms the denominator.
    total_mcap = sum(float(r[2]) for r in rows)
    if total_mcap <= 0:
        # Defensive: shouldn't happen since we filtered market_cap > 0
        # in the SQL, but a numerical edge case (e.g. all mcaps near
        # floating-point zero) could produce zero. Skip rather than
        # divide-by-zero.
        return []

    return [
        SelectedConstituent(
            ticker_id=int(r[0]),
            symbol=r[1],
            market_cap=float(r[2]),
            close_price=float(r[3]),
            weight=float(r[2]) / total_mcap,
        )
        for r in rows
    ]


# ---------- value computation ---------- #


def compute_index_value_for_date(
    db_engine: Engine,
    industry_name: str,
    trade_date: date,
) -> Optional[ComputedValue]:
    """Compute the modified-Laspeyres index value for one day.

    Returns None if:
      - The index doesn't exist.
      - trade_date is before base_date (pre-base — no weights).
      - No constituents were active on trade_date (e.g. the
        rebalance was empty or all constituents have been
        delisted).
      - Every active constituent is missing mcap on this day
        (can't compute anything).

    Otherwise returns a ComputedValue.

    The formula:
      value(t) = base_value
                 × Σ_i_present [
                     (weight_i / Σ_j_present weight_j)
                     × close_i(t) / close_i(rebalance_date)
                   ]

    When all constituents are present, Σ_j_present weight_j = 1.0
    and the formula degenerates to the textbook modified Laspeyres.
    When some are missing, the inner division renormalizes — that's
    the "drop and renormalize" behavior.
    """
    # 1. Look up the index definition.
    with db_engine.connect() as conn:
        idx = IndustryIndexRepository(conn).get(industry_name)
    if idx is None:
        logger.warning(
            "compute_index_value_for_date: index %r does not exist",
            industry_name,
        )
        return None

    # 2. Pre-base check.
    if trade_date < idx.base_date:
        # Silently skip — the runner walks date ranges that may
        # include pre-base dates and we don't want a flood of
        # warnings.
        return None

    # 3. Find the rebalance whose effective period contains
    # trade_date. list_as_of returns all constituents active on
    # that day — their `rebalance_date` field tells us which
    # rebalance defined them.
    with db_engine.connect() as conn:
        constituents = IndustryIndexConstituentRepository(conn).list_as_of(
            industry_name, trade_date,
        )
    if not constituents:
        logger.debug(
            "compute_index_value_for_date: no active constituents for "
            "%r on %s", industry_name, trade_date,
        )
        return None

    # All constituents picked at the same rebalance share a
    # rebalance_date. In practice they always will (the runner
    # rebalances them as a batch). Take the first one as canonical;
    # validate that all match.
    effective_rebalance_date = constituents[0].rebalance_date
    if any(c.rebalance_date != effective_rebalance_date for c in constituents):
        # This would indicate corrupt data — different constituents
        # of the same active period claiming different rebalances.
        # Log and continue with the most-recent rebalance_date.
        logger.warning(
            "compute_index_value_for_date: mixed rebalance_dates in "
            "active constituents for %r on %s — picking max",
            industry_name, trade_date,
        )
        effective_rebalance_date = max(
            c.rebalance_date for c in constituents
        )

    # 4. Look up close prices for each constituent on trade_date.
    # We do this as one SQL — list_for_date(trade_date) filtered to
    # the constituent ticker_ids — rather than per-ticker lookups,
    # because Phase 5+ will compute these for many days in a row
    # and per-ticker lookups would dominate the cost.
    constituent_ids = {c.ticker_id for c in constituents}
    with db_engine.connect() as conn:
        mcap_rows = DailyMarketCapRepository(conn).list_for_date(
            trade_date,
            # No exchange filter — constituents might span exchanges
            # in theory; the rebalance picked them anyway.
        )
    # Build ticker_id → close_price for trade_date.
    close_on_t: dict[int, float] = {
        r.ticker_id: r.close_price
        for r in mcap_rows
        if r.ticker_id in constituent_ids
    }

    # 5. For each constituent: present if we have a close, missing
    # otherwise. Compute weighted return contributions.
    present: list[tuple[float, float]] = []  # (weight, close_ratio)
    missing_count = 0
    for c in constituents:
        close_t = close_on_t.get(c.ticker_id)
        if close_t is None:
            missing_count += 1
            continue
        # close_at_rebalance is guaranteed > 0 by the schema check.
        ratio = close_t / c.close_at_rebalance
        present.append((c.weight_at_rebalance, ratio))

    used_count = len(present)
    if used_count == 0:
        logger.warning(
            "compute_index_value_for_date: ALL %d constituents missing "
            "mcap for %r on %s — cannot compute",
            len(constituents), industry_name, trade_date,
        )
        return None

    # 6. Renormalize if any were missing.
    renormalized = missing_count > 0
    sum_present_weights = sum(w for w, _ in present)
    if sum_present_weights <= 0:
        # Defensive: weights are guaranteed >= 0 by the schema; this
        # branch handles the all-zero-weights edge case.
        return None

    # Modified Laspeyres sum with renormalization:
    # value = base × Σ_i_present [(w_i / Σ_w_present) × ratio_i]
    weighted_sum = sum(
        (w / sum_present_weights) * ratio
        for w, ratio in present
    )
    value = idx.base_value * weighted_sum

    if value <= 0:
        # Defensive: the value should be > 0 unless all closes are
        # at-or-below zero (which would mean delisted-at-zero
        # constituents that somehow made it past the schema check).
        logger.warning(
            "compute_index_value_for_date: computed value %s for %r "
            "on %s; skipping write", value, industry_name, trade_date,
        )
        return None

    return ComputedValue(
        industry_name=industry_name,
        trade_date=trade_date,
        value=value,
        constituents_used=used_count,
        constituents_missing=missing_count,
        renormalized=renormalized,
        effective_rebalance_date=effective_rebalance_date,
    )
