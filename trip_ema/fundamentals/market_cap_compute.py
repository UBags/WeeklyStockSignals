"""Daily market cap computation for trade dates within our local
instrument_shares data range.

This module produces COMPUTED rows only — joining ohlcv_bars (daily
close prices) × instrument_shares (the as-of shares-outstanding for
the trade date) into daily_market_cap.

For trade dates OLDER than our earliest instrument_shares row, the
yfinance historical mcap fetcher takes over (Phase 4A still-to-build,
pending the operator's probe of yfinance's API shape — see
scripts/probe_yfinance_hist_mcap.py).

What this module does:
  - compute_market_cap_for_date(db_engine, exchange, trade_date)
    Iterates active EQ tickers on `exchange`. For each:
      - Look up the day's close from ohlcv_bars (timeframe='1d').
      - Look up shares as-of the trade_date from instrument_shares.
      - Multiply, write a COMPUTED row.
    Tickers without a close (no bar that day) are skipped.
    Tickers without shares for the date but with shares from a
    later date are written as APPROX_TODAYS_SHARES (low-trust),
    so the row exists for downstream Phase 4B even if approximate.
    Tickers with no shares at all are skipped (no value to record).

What it deliberately does NOT do:
  - Yfinance calls. The yfinance historical mcap path is the
    fetcher module's job (still pending the probe).
  - Multi-date batching (yet). compute_market_cap_for_date does ONE
    date per call. The runner module walks the date range.
  - ETF handling. ETFs are filtered out via instrument_type='EQ'
    in the selection query — Phase 4B doesn't index ETFs.

The function returns a ComputeResult summary so the caller can log
counts; it does NOT return the rows themselves (those live in the
DB after the call).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Optional

from sqlalchemy import Engine, text

from ..persistence import transaction
from ..persistence.fundamentals_repo import (
    DailyMarketCapRepository,
    InstrumentSharesRepository,
)

logger = logging.getLogger(__name__)


# ---------- result dataclass ---------- #


@dataclass(frozen=True, slots=True)
class ComputeResult:
    """Summary of one call to compute_market_cap_for_date.

    All counts are over the per-exchange active EQ ticker universe
    that the function attempted to compute for. `skipped_no_close`
    + `skipped_no_shares` + `succeeded_computed` +
    `succeeded_approx` = total tickers iterated.
    """
    trade_date: date
    exchange: str
    succeeded_computed: int  # rows written with source=COMPUTED
    succeeded_approx: int    # rows written with source=APPROX_TODAYS_SHARES
    skipped_no_close: int    # ticker had no bar for that day
    skipped_no_shares: int   # ticker had no shares for that day at all


# ---------- public function ---------- #


def compute_market_cap_for_date(
    db_engine: Engine,
    exchange: str,
    trade_date: date,
    *,
    allow_approx_fallback: bool = True,
) -> ComputeResult:
    """Compute and store daily_market_cap rows for all active EQ
    tickers on `exchange` for `trade_date`.

    The function is idempotent — re-running it for the same date
    overwrites the previously-stored rows (via the repo's
    upsert_snapshot ON CONFLICT logic).

    Parameters:
      db_engine, exchange, trade_date — as documented.
      allow_approx_fallback — if True (the default), tickers that
        have shares from a LATER date but not the trade_date itself
        get an APPROX_TODAYS_SHARES row. If False, those tickers
        are skipped (skipped_no_shares).

    The allow_approx_fallback default is True for production
    backfill (we'd rather have an approximate row than no row, so
    Phase 4B can build indices for the backfill period). Tests
    pass False to verify the strict-mode behavior.
    """
    # Load the universe in one query so we don't do per-ticker
    # database round-trips.
    with db_engine.connect() as conn:
        rows = conn.execute(
            text("""
                SELECT t.ticker_id, t.symbol
                FROM tickers t
                WHERE t.exchange = :ex
                  AND t.is_active = TRUE
                  AND t.instrument_type = 'EQ'
                  AND (t.delisted_at IS NULL OR t.delisted_at > :td)
                ORDER BY t.symbol
            """),
            {"ex": exchange, "td": trade_date},
        ).fetchall()
    universe = [(int(r[0]), r[1]) for r in rows]

    if not universe:
        logger.warning(
            "compute_market_cap_for_date: no active EQ tickers on %s — "
            "nothing to do for %s", exchange, trade_date,
        )
        return ComputeResult(
            trade_date=trade_date, exchange=exchange,
            succeeded_computed=0, succeeded_approx=0,
            skipped_no_close=0, skipped_no_shares=0,
        )

    # Load all closes for this date in one query. Using
    # bar_timestamp >= :start AND < :end so we don't tie ourselves
    # to a specific timezone — any 1d bar with a timestamp falling
    # within the trade_date counts. (Indian 1d bars are timestamped
    # at market close 15:30 IST; we just need to be permissive.)
    from datetime import datetime as _dt
    day_start = _dt.combine(trade_date, _dt.min.time())
    day_end = _dt.combine(trade_date, _dt.max.time())
    with db_engine.connect() as conn:
        close_rows = conn.execute(
            text("""
                SELECT b.ticker_id, b.close
                FROM ohlcv_bars b
                WHERE b.timeframe = '1d'
                  AND b.bar_timestamp >= :start
                  AND b.bar_timestamp <= :end
            """),
            {"start": day_start, "end": day_end},
        ).fetchall()
    closes: dict[int, float] = {
        int(r[0]): float(r[1]) for r in close_rows
    }

    # Iterate the universe; track outcomes per category.
    succeeded_computed = 0
    succeeded_approx = 0
    skipped_no_close = 0
    skipped_no_shares = 0

    with db_engine.connect() as conn:
        shares_repo = InstrumentSharesRepository(conn)

        # Pre-resolve shares per ticker. For each ticker we want:
        #   1. shares as-of trade_date (or earlier) → COMPUTED row.
        #   2. otherwise, shares from a LATER date (today's known
        #      shares) → APPROX_TODAYS_SHARES row (if allowed).
        #   3. otherwise, skip.
        ticker_shares_info: dict[int, tuple[Optional[int], str]] = {}
        for tid, _sym in universe:
            row = shares_repo.get_as_of(tid, trade_date)
            if row is not None and row.shares_outstanding is not None:
                ticker_shares_info[tid] = (row.shares_outstanding, "COMPUTED")
                continue
            # No shares as-of trade_date. Try the absolute latest.
            latest = shares_repo.get_latest(tid)
            if (
                latest is not None
                and latest.shares_outstanding is not None
                and allow_approx_fallback
            ):
                ticker_shares_info[tid] = (
                    latest.shares_outstanding, "APPROX_TODAYS_SHARES",
                )
                continue
            # Nothing we can use.
            ticker_shares_info[tid] = (None, "")

    # Write the rows in one transaction. Each upsert is one
    # statement; for ~2300 tickers this is ~2300 INSERT/UPSERT
    # statements per date which is fine for a single backfill
    # date, and the runner will paginate dates.
    with transaction(db_engine) as conn:
        mcap_repo = DailyMarketCapRepository(conn)
        for tid, _sym in universe:
            close_price = closes.get(tid)
            if close_price is None:
                skipped_no_close += 1
                continue
            shares, src = ticker_shares_info[tid]
            if shares is None:
                skipped_no_shares += 1
                continue
            mcap = close_price * shares
            mcap_repo.upsert_snapshot(
                tid,
                trade_date=trade_date,
                close_price=close_price,
                shares_used=shares,
                market_cap=mcap,
                source=src,
            )
            if src == "COMPUTED":
                succeeded_computed += 1
            else:
                succeeded_approx += 1

    logger.info(
        "compute_market_cap_for_date %s/%s: "
        "computed=%d approx=%d skipped_no_close=%d skipped_no_shares=%d",
        exchange, trade_date,
        succeeded_computed, succeeded_approx,
        skipped_no_close, skipped_no_shares,
    )
    return ComputeResult(
        trade_date=trade_date, exchange=exchange,
        succeeded_computed=succeeded_computed,
        succeeded_approx=succeeded_approx,
        skipped_no_close=skipped_no_close,
        skipped_no_shares=skipped_no_shares,
    )
