"""Phase 6-3: Benchmark stats runners (alpha/beta/RS/R²).

Orchestrate the fetch → compute → write loop for the
stock_benchmark_stats and industry_benchmark_stats tables. Data
sources:

  Stock weekly closes
    `_fetch_bars_at_timeframe(conn, ticker_id, "1W")` — 1D bars
    aggregated to 1W on the fly (same helper used by per-ticker
    indicator compute since Phase 6-2).

  Industry weekly values
    `industry_index_value` rows (daily) bucketed by IST-Monday-
    anchored week; last value in each week wins.

  NIFTY weekly closes
    `NIFTYBEES` (NSE/ETF) weekly bars via _fetch_bars_at_timeframe.
    Same instrument the IndustryPanel uses as its NIFTY-50 proxy
    (`_NIFTY50_FALLBACK`).

All three series are aligned on Monday-of-week dates via
align_series_by_date before regression. Per-window suffix slicing is
done last so the same aligned series feeds SHORT/MEDIUM/LONG.

Runners:
  run_benchmark_stats_for_ticker(engine, ticker_id, as_of_date=None)
      → writes up to 6 stock_benchmark_stats rows.
  run_benchmark_stats_for_industry(engine, name, as_of_date=None)
      → writes up to 3 industry_benchmark_stats rows.
  run_benchmark_stats_for_all_tickers(engine, *, exchange=None)
      → bulk wrapper, per-ticker error isolation.
  run_benchmark_stats_for_all_industries(engine)
      → bulk wrapper, per-industry error isolation.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, UTC

from sqlalchemy import text
from sqlalchemy.engine import Engine

from ..persistence import transaction
from . import NIFTY_BENCHMARK_ID
from .benchmark_stats_compute import (
    WINDOW_RETURNS,
    align_series_by_date,
    compute_regression_stats,
    slice_window,
)

logger = logging.getLogger(__name__)


# ---------- helpers ---------- #


def _fetch_industry_weekly_closes(conn, industry_name: str):
    """Aggregate industry_index_value (daily) into a list of
    (week_start_monday, last_value_in_week). Sorted asc."""
    rows = conn.execute(text("""
        SELECT trade_date, value
        FROM industry_index_value
        WHERE industry_name = :n
        ORDER BY trade_date ASC
    """), {"n": industry_name}).all()
    weekly: dict = {}
    for r in rows:
        d = r.trade_date
        week_start = d - timedelta(days=d.weekday())  # Monday of week
        weekly[week_start] = float(r.value)   # last in week wins (asc order)
    return sorted(weekly.items())


def _fetch_nifty_weekly_closes(conn):
    """NIFTYBEES weekly closes (NSE/ETF). Returns list[(date, close)]
    sorted asc, or [] if NIFTYBEES isn't in the ticker universe."""
    from .runners import _fetch_bars_at_timeframe
    nifty_row = conn.execute(text("""
        SELECT ticker_id FROM tickers
        WHERE exchange = 'NSE' AND symbol = 'NIFTYBEES'
          AND instrument = 'ETF'
    """)).first()
    if nifty_row is None:
        logger.warning("NIFTYBEES not in ticker universe; "
                       "NIFTY benchmark stats unavailable")
        return []
    bars = _fetch_bars_at_timeframe(conn, nifty_row.ticker_id, "1W")
    return [(b.timestamp.date(), float(b.close)) for b in bars]


def _industry_for_ticker(conn, ticker_id: int) -> str | None:
    """Resolve a ticker's industry classification. Returns None for
    unclassified tickers; returns 'ETF' for ETFs (caller skips
    INDUSTRY benchmark in that case)."""
    row = conn.execute(text("""
        SELECT industry FROM instrument_industry
        WHERE ticker_id = :tid
    """), {"tid": ticker_id}).first()
    return row.industry if row else None


# ---------- per-ticker runner ---------- #


def run_benchmark_stats_for_ticker(
    db_engine: Engine,
    ticker_id: int,
    *,
    as_of_date: date | None = None,
) -> int:
    """Compute and persist benchmark stats for one ticker.

    Writes up to 6 rows: 3 windows × {INDUSTRY, NIFTY}. The INDUSTRY
    benchmark is skipped for ETFs (industry = 'ETF') and for tickers
    whose industry has no industry_index_value rows (sub-threshold
    industries). The NIFTY benchmark is skipped if NIFTYBEES is the
    ticker itself (self-vs-self) or NIFTYBEES isn't in the universe.

    as_of_date: optional override. When None, each benchmark uses
    the latest date in its aligned series independently. For
    consistent cross-ticker ranking, pass the same explicit date
    when invoking from the bulk runner.

    Returns the number of rows written.
    """
    from .runners import _fetch_bars_at_timeframe
    from . import StockBenchmarkStats
    from ..persistence.fundamentals_repo import (
        StockBenchmarkStatsRepository,
    )

    rows_to_write: list = []
    now = datetime.now(UTC)

    with db_engine.connect() as conn:
        # 1. Stock weekly closes
        stock_bars = _fetch_bars_at_timeframe(conn, ticker_id, "1W")
        if not stock_bars:
            logger.info("run_benchmark_stats_for_ticker: ticker_id=%d "
                        "has no weekly bars; skipping", ticker_id)
            return 0
        stock_pairs = [(b.timestamp.date(), float(b.close))
                       for b in stock_bars]

        # 2. Industry benchmark series
        industry_name = _industry_for_ticker(conn, ticker_id)
        industry_pairs: list = []
        if industry_name and industry_name != "ETF":
            industry_pairs = _fetch_industry_weekly_closes(
                conn, industry_name,
            )

        # 3. NIFTY benchmark series. Skip self-vs-self for NIFTYBEES.
        ticker_row = conn.execute(text("""
            SELECT symbol, instrument FROM tickers WHERE ticker_id = :t
        """), {"t": ticker_id}).first()
        is_nifty_self = (
            ticker_row is not None
            and ticker_row.symbol == "NIFTYBEES"
            and ticker_row.instrument == "ETF"
        )
        nifty_pairs = [] if is_nifty_self else _fetch_nifty_weekly_closes(conn)

    # Helper: build rows for one benchmark
    def _stats_for_benchmark(benchmark_kind: str, benchmark_id: str,
                             bench_pairs: list) -> None:
        stock_aligned, bench_aligned = align_series_by_date(
            stock_pairs, bench_pairs,
        )
        if not stock_aligned:
            return
        effective_asof = (
            as_of_date if as_of_date is not None
            else stock_pairs[-1][0]
        )
        # If we have an explicit as_of_date, truncate to it.
        if as_of_date is not None:
            stock_dates = [d for d, _ in stock_pairs if d <= as_of_date]
            if not stock_dates:
                return
            # Simpler: rebuild from pairs with as_of cutoff.
            sp = [(d, c) for d, c in stock_pairs if d <= as_of_date]
            bp = [(d, c) for d, c in bench_pairs if d <= as_of_date]
            stock_aligned, bench_aligned = align_series_by_date(sp, bp)
            if not stock_aligned:
                return

        for window_label, n_returns in WINDOW_RETURNS.items():
            s_win, b_win = slice_window(
                stock_aligned, bench_aligned, n_returns,
            )
            stats = compute_regression_stats(s_win, b_win)
            rows_to_write.append(StockBenchmarkStats(
                ticker_id=ticker_id,
                benchmark_kind=benchmark_kind,
                benchmark_id=benchmark_id,
                window_label=window_label,
                as_of_date=effective_asof,
                n_weeks=stats.n_weeks,
                beta=stats.beta,
                alpha_annual=stats.alpha_annual,
                r_squared=stats.r_squared,
                sigma_annual=stats.sigma_annual,
                rs_pct=stats.rs_pct,
                info_ratio=stats.info_ratio,
                computed_at=now,
            ))

    if industry_pairs:
        _stats_for_benchmark("INDUSTRY", industry_name, industry_pairs)
    if nifty_pairs:
        _stats_for_benchmark("NIFTY", NIFTY_BENCHMARK_ID, nifty_pairs)

    if not rows_to_write:
        logger.info("run_benchmark_stats_for_ticker: ticker_id=%d "
                    "produced no rows (no benchmarks aligned)", ticker_id)
        return 0

    with transaction(db_engine) as conn:
        repo = StockBenchmarkStatsRepository(conn)
        n_written = repo.upsert_batch(rows_to_write)
    logger.info("run_benchmark_stats_for_ticker ticker_id=%d: "
                "wrote %d rows", ticker_id, n_written)
    return n_written


# ---------- per-industry runner ---------- #


def run_benchmark_stats_for_industry(
    db_engine: Engine,
    industry_name: str,
    *,
    as_of_date: date | None = None,
) -> int:
    """Compute and persist industry vs NIFTY benchmark stats.

    Writes up to 3 rows (one per window). Skipped entirely if the
    industry has no industry_index_value rows or NIFTYBEES isn't
    in the universe.
    """
    from . import IndustryBenchmarkStats
    from ..persistence.fundamentals_repo import (
        IndustryBenchmarkStatsRepository,
    )

    now = datetime.now(UTC)
    with db_engine.connect() as conn:
        industry_pairs = _fetch_industry_weekly_closes(conn, industry_name)
        nifty_pairs = _fetch_nifty_weekly_closes(conn)

    if not industry_pairs:
        logger.info("run_benchmark_stats_for_industry: %s has no "
                    "index_value rows; skipping", industry_name)
        return 0
    if not nifty_pairs:
        logger.warning("run_benchmark_stats_for_industry: NIFTY "
                       "series empty; skipping %s", industry_name)
        return 0

    if as_of_date is not None:
        industry_pairs = [(d, v) for d, v in industry_pairs if d <= as_of_date]
        nifty_pairs = [(d, v) for d, v in nifty_pairs if d <= as_of_date]
        if not industry_pairs:
            return 0

    industry_aligned, nifty_aligned = align_series_by_date(
        industry_pairs, nifty_pairs,
    )
    if not industry_aligned:
        return 0

    effective_asof = (
        as_of_date if as_of_date is not None
        else industry_pairs[-1][0]
    )

    rows_to_write: list = []
    for window_label, n_returns in WINDOW_RETURNS.items():
        i_win, n_win = slice_window(
            industry_aligned, nifty_aligned, n_returns,
        )
        stats = compute_regression_stats(i_win, n_win)
        rows_to_write.append(IndustryBenchmarkStats(
            industry_name=industry_name,
            benchmark_id=NIFTY_BENCHMARK_ID,
            window_label=window_label,
            as_of_date=effective_asof,
            n_weeks=stats.n_weeks,
            beta=stats.beta,
            alpha_annual=stats.alpha_annual,
            r_squared=stats.r_squared,
            sigma_annual=stats.sigma_annual,
            rs_pct=stats.rs_pct,
            info_ratio=stats.info_ratio,
            computed_at=now,
        ))

    with transaction(db_engine) as conn:
        repo = IndustryBenchmarkStatsRepository(conn)
        n_written = repo.upsert_batch(rows_to_write)
    logger.info("run_benchmark_stats_for_industry %s: wrote %d rows",
                industry_name, n_written)
    return n_written


# ---------- bulk runners ---------- #


def run_benchmark_stats_for_all_industries(
    db_engine: Engine,
    *,
    as_of_date: date | None = None,
) -> dict:
    """Compute benchmark stats for every active industry vs NIFTY.

    Per-industry error isolation. Returns
        {"succeeded": [(name, n_rows)],
         "skipped":   [(name, reason)],
         "failed":    [(name, error_message)]}
    """
    from ..persistence.fundamentals_repo import IndustryIndexRepository

    with db_engine.connect() as conn:
        indices = IndustryIndexRepository(conn).list_active()

    succeeded: list = []
    skipped: list = []
    failed: list = []

    for idx in indices:
        try:
            n = run_benchmark_stats_for_industry(
                db_engine, idx.industry_name, as_of_date=as_of_date,
            )
            if n > 0:
                succeeded.append((idx.industry_name, n))
            else:
                skipped.append((idx.industry_name, "insufficient data"))
        except Exception as e:
            logger.exception(
                "run_benchmark_stats_for_all_industries: failed for %s",
                idx.industry_name,
            )
            failed.append((idx.industry_name, str(e)))

    logger.info(
        "run_benchmark_stats_for_all_industries: succeeded=%d "
        "skipped=%d failed=%d",
        len(succeeded), len(skipped), len(failed),
    )
    return {"succeeded": succeeded, "skipped": skipped, "failed": failed}


def run_benchmark_stats_for_all_tickers(
    db_engine: Engine,
    *,
    exchange: str | None = None,
    as_of_date: date | None = None,
) -> dict:
    """Compute benchmark stats for every active ticker.

    Per-ticker error isolation. ETFs other than NIFTYBEES still get
    NIFTY stats but no INDUSTRY stats (skipped by the per-ticker
    runner).
    """
    from ..persistence.tickers_repo import TickersRepository

    with db_engine.connect() as conn:
        tickers = TickersRepository(conn).find_active()
    if exchange is not None:
        tickers = [t for t in tickers if t.ref.exchange == exchange]

    succeeded: list = []
    skipped: list = []
    failed: list = []

    for t in tickers:
        try:
            n = run_benchmark_stats_for_ticker(
                db_engine, t.ticker_id, as_of_date=as_of_date,
            )
            if n > 0:
                succeeded.append((t.ticker_id, t.ref.symbol, n))
            else:
                skipped.append((t.ticker_id, t.ref.symbol,
                                "no benchmarks aligned"))
        except Exception as e:
            logger.exception(
                "run_benchmark_stats_for_all_tickers: failed for "
                "ticker_id=%d symbol=%s",
                t.ticker_id, t.ref.symbol,
            )
            failed.append((t.ticker_id, t.ref.symbol, str(e)))

    logger.info(
        "run_benchmark_stats_for_all_tickers: succeeded=%d "
        "skipped=%d failed=%d",
        len(succeeded), len(skipped), len(failed),
    )
    return {"succeeded": succeeded, "skipped": skipped, "failed": failed}
