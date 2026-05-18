"""ETL runner.

The functions here turn parsed configuration + a DB engine + a
yfinance source into actual ETL passes. Each pass is its own function
so the CLI can compose them.

Boundaries:
  - The runner doesn't open the DB engine — it accepts one.
  - The runner doesn't decide WHEN to run (cron / hourly poll / manual
    trigger) — the CLI or the GUI scheduler decides.
  - The runner DOES manage transactions, concurrency, the etl_runs
    row lifecycle, and the parked-failures plumbing.

Concurrency model:
  - ThreadPoolExecutor(max_workers=N) where N = config.etl.yfinance_concurrency.
  - One worker = one batch of `batch_size` tickers from `yf.download`.
  - Tier ordering is preserved at submit time (Tier 1 batches submitted
    first); within a tier, batches finish in arbitrary order.
  - Each worker holds its own DB connection so writes don't serialize.
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from sqlalchemy import text
from sqlalchemy.engine import Engine

from ..config import Config
from ..domain import OHLCVBar, Timeframe
from ..observability import metrics as M
from ..observability.correlation import correlation_id, new_correlation_id
from ..persistence.bars_repo import BarsRepository
from .corporate_actions_repo import CorporateActionsRepository
from .holidays_calendar import NseTradingCalendar
from .listings_sync import ListingsSync, SyncResult
from .nse_downloader import NseDownloader
from .nse_listings_parser import parse_equity_csv, parse_etf_csv
from .parked import ParkedEntry, append_parked
from .reconciler import _StoredBar, reconcile
from .status_repo import EtlStatusRepository
from .symbol_history_repo import SymbolHistoryRepository
from .tiering import TickerAdv, assign_tiers
from .types import (
    FetchOutcome,
    RawBar,
    ReconcileResult,
    RunKind,
    RunOutcome,
    Tier,
)
from .yfinance_source import YFinanceSource

logger = logging.getLogger(__name__)


# ---------- Listings refresh ---------- #


@dataclass(frozen=True, slots=True)
class RefreshListingsResult:
    sync_result: SyncResult
    run_id: int
    outcome: RunOutcome
    notes: str


def refresh_listings(
    engine: Engine,
    config: Config,
    *,
    downloader: Optional[NseDownloader] = None,
) -> RefreshListingsResult:
    """Pull the NSE equity + ETF CSVs and sync the tickers table.

    Single DB transaction — partial failures roll back. The CSV
    downloads are outside the transaction (they can take 10-30s and
    we don't want to hold a row lock).
    """
    dl = downloader or NseDownloader()
    cid = new_correlation_id()

    with correlation_id(cid):
        with engine.begin() as conn:
            status_repo = EtlStatusRepository(conn)
            run_id = status_repo.open_run(
                RunKind.LISTINGS, correlation_id=cid,
            )

        t_start = time.monotonic()
        try:
            logger.info("ETL: downloading NSE equities CSV")
            equities_bytes = dl.fetch_equities()
            logger.info("ETL: downloading NSE ETFs CSV")
            etfs_bytes = dl.fetch_etfs()
        except Exception as e:
            logger.exception("Listings download failed")
            with engine.begin() as conn:
                EtlStatusRepository(conn).close_run(
                    run_id, RunOutcome.FAILED,
                    notes=f"download failed: {e}",
                )
            M.etl_runs_total.labels(
                kind=RunKind.LISTINGS.value,
                outcome=RunOutcome.FAILED.value,
            ).inc()
            M.etl_run_duration_seconds.labels(
                kind=RunKind.LISTINGS.value,
            ).observe(time.monotonic() - t_start)
            raise

        equity_listings = parse_equity_csv(equities_bytes)
        etf_listings = parse_etf_csv(etfs_bytes)
        all_listings = equity_listings + etf_listings
        logger.info(
            "ETL: parsed listings",
            extra={
                "equities": len(equity_listings),
                "etfs": len(etf_listings),
            },
        )

        with engine.begin() as conn:
            sync = ListingsSync(conn, exchange=config.etl.exchange)
            # We sync the combined list with delisting detection enabled.
            # A ticker missing from BOTH the equity and ETF lists is
            # genuinely delisted; one in just the equity list might be
            # in the ETF list (rare but possible).
            result = sync.sync(all_listings, detect_delistings=True)
            status_repo = EtlStatusRepository(conn)
            status_repo.close_run(
                run_id,
                RunOutcome.SUCCESS,
                notes=(
                    f"sync: new={result.new_listings}, "
                    f"renamed={result.renamed_symbols}, "
                    f"enriched={result.enriched_existing}, "
                    f"delisted={result.delisted}, "
                    f"unchanged={result.unchanged}"
                ),
            )

        M.etl_runs_total.labels(
            kind=RunKind.LISTINGS.value,
            outcome=RunOutcome.SUCCESS.value,
        ).inc()
        M.etl_run_duration_seconds.labels(
            kind=RunKind.LISTINGS.value,
        ).observe(time.monotonic() - t_start)

        return RefreshListingsResult(
            sync_result=result,
            run_id=run_id,
            outcome=RunOutcome.SUCCESS,
            notes="OK",
        )


# ---------- Tier recompute ---------- #


def recompute_tiers(engine: Engine, config: Config) -> int:
    """Refresh tickers.tier based on trailing-30-day ADV. Returns the
    number of tier reassignments that changed."""
    cid = new_correlation_id()
    with correlation_id(cid), engine.begin() as conn:
        status_repo = EtlStatusRepository(conn)
        run_id = status_repo.open_run(
            RunKind.TIER_RECOMPUTE, correlation_id=cid,
        )
        t_start = time.monotonic()

        # Aggregate 30-day ADV per ticker. The trailing window excludes
        # today (we use bars strictly before today, so weekends/holidays
        # don't bias things). 30 trading days ≈ 45 calendar days.
        rows = conn.execute(
            text("""
                WITH window AS (
                    SELECT ticker_id, close * volume AS daily_value
                    FROM ohlcv_bars
                    WHERE timeframe = '1D'
                      AND bar_timestamp >= NOW() - INTERVAL '45 days'
                      AND bar_timestamp <  NOW()
                )
                SELECT t.ticker_id,
                       COALESCE(AVG(w.daily_value), 0) AS avg_daily_value
                FROM tickers t
                LEFT JOIN window w ON t.ticker_id = w.ticker_id
                WHERE t.is_active = TRUE AND t.delisted_at IS NULL
                  AND t.exchange = :ex
                GROUP BY t.ticker_id
            """),
            {"ex": config.etl.exchange},
        )
        advs = [
            TickerAdv(ticker_id=r.ticker_id, avg_daily_value=float(r.avg_daily_value))
            for r in rows
        ]
        assignments = assign_tiers(
            advs,
            tier_1_size=config.etl.tier_1_size,
            tier_2_size=config.etl.tier_2_size,
        )

        # Apply only the tickers whose tier actually changed.
        changes = 0
        for a in assignments:
            result = conn.execute(
                text("""
                    UPDATE tickers SET tier = :t
                    WHERE ticker_id = :tid AND tier <> :t
                """),
                {"t": a.tier.value, "tid": a.ticker_id},
            )
            changes += result.rowcount

        status_repo.close_run(
            run_id,
            RunOutcome.SUCCESS,
            notes=f"tier reassignments: {changes}",
        )
        M.etl_runs_total.labels(
            kind=RunKind.TIER_RECOMPUTE.value,
            outcome=RunOutcome.SUCCESS.value,
        ).inc()
        M.etl_run_duration_seconds.labels(
            kind=RunKind.TIER_RECOMPUTE.value,
        ).observe(time.monotonic() - t_start)
        return changes


# ---------- Bar-fetch passes (backfill, EoD, intraday) ---------- #


@dataclass(frozen=True, slots=True)
class _TickerInfo:
    """Compact projection used by the runner to dispatch work."""
    ticker_id: int
    symbol: str
    tier: Tier


def _select_tickers(
    engine: Engine,
    config: Config,
    *,
    tier_filter: Optional[set[Tier]] = None,
) -> list[_TickerInfo]:
    """Active, non-delisted, NSE tickers ordered by (tier ASC, symbol ASC).
    Tier 1 first so partial-run aborts lose the least important data."""
    with engine.connect() as conn:
        query = """
            SELECT ticker_id, symbol, tier
            FROM tickers
            WHERE is_active = TRUE AND delisted_at IS NULL
              AND exchange = :ex
        """
        params = {"ex": config.etl.exchange}
        if tier_filter:
            query += " AND tier = ANY(:tiers)"
            params["tiers"] = [t.value for t in tier_filter]
        query += " ORDER BY tier ASC, symbol ASC"
        rows = conn.execute(text(query), params)
        return [
            _TickerInfo(ticker_id=r.ticker_id, symbol=r.symbol, tier=Tier(r.tier))
            for r in rows
        ]


def _fetch_existing_bars_for_tickers(
    engine: Engine,
    ticker_ids: list[int],
    timeframe: Timeframe,
    *,
    since: Optional[date] = None,
) -> dict[int, list[_StoredBar]]:
    """For reconciliation. since=None pulls all history — only use when
    you really want it (backfill divergence detection on first run)."""
    if not ticker_ids:
        return {}
    with engine.connect() as conn:
        query = """
            SELECT ticker_id, bar_timestamp, open, high, low,
                   close, adj_close, volume
            FROM ohlcv_bars
            WHERE ticker_id = ANY(:tids) AND timeframe = :tf
        """
        params = {"tids": ticker_ids, "tf": timeframe.value}
        if since is not None:
            query += " AND bar_timestamp >= :since"
            params["since"] = datetime.combine(
                since, datetime.min.time(), tzinfo=timezone.utc,
            )
        rows = conn.execute(text(query), params)

        out: dict[int, list[_StoredBar]] = {tid: [] for tid in ticker_ids}
        for r in rows:
            out[r.ticker_id].append(_StoredBar(
                trading_date=r.bar_timestamp.date(),
                open=r.open, high=r.high, low=r.low, close=r.close,
                adj_close=r.adj_close,
                volume=r.volume,
            ))
        return out


def run_eod(
    engine: Engine,
    config: Config,
    *,
    source: Optional[YFinanceSource] = None,
    days_lookback: int = 7,
) -> int:
    """Daily end-of-day pass: every active NSE ticker.

    Fetches the trailing `days_lookback` calendar days. We don't
    need full history — just enough overlap to detect intraday
    revisions on the latest bar(s).

    Returns the run_id so the GUI can show progress.
    """
    return _run_fetch_pass(
        engine,
        config,
        kind=RunKind.EOD,
        tier_filter=None,
        timeframe=Timeframe.DAILY,
        source=source,
        days_lookback=days_lookback,
    )


def run_intraday(
    engine: Engine,
    config: Config,
    *,
    source: Optional[YFinanceSource] = None,
) -> int:
    """Hourly Tier-1-only pass during market hours.

    Yfinance returns hourly bars; we aggregate them to a daily
    PROVISIONAL bar (the latest one). The trip_ema scheduler's
    provisional path picks it up on its next cycle.
    """
    # Holiday check: skip on non-trading days outright.
    cal = NseTradingCalendar()
    today_ist = _today_in_ist()
    if not cal.is_trading_day(today_ist):
        logger.info(
            "Skipping intraday run on non-trading day",
            extra={"date": today_ist.isoformat()},
        )
        return -1

    return _run_fetch_pass(
        engine,
        config,
        kind=RunKind.INTRADAY,
        tier_filter={Tier.TIER_1},
        timeframe=Timeframe.DAILY,  # We aggregate hourly → daily-provisional
        source=source,
        intraday=True,
    )


def run_backfill(
    engine: Engine,
    config: Config,
    *,
    start_date: date,
    source: Optional[YFinanceSource] = None,
    tier_filter: Optional[set[Tier]] = None,
) -> int:
    """Historical backfill from `start_date` to today.

    The full universe is fetched in tier order (so a Ctrl-C partway
    through still leaves us with the most-important tickers done).
    """
    return _run_fetch_pass(
        engine,
        config,
        kind=RunKind.BACKFILL,
        tier_filter=tier_filter,
        timeframe=Timeframe.DAILY,
        source=source,
        start_date=start_date,
    )


def _run_fetch_pass(
    engine: Engine,
    config: Config,
    *,
    kind: RunKind,
    tier_filter: Optional[set[Tier]],
    timeframe: Timeframe,
    source: Optional[YFinanceSource],
    start_date: Optional[date] = None,
    days_lookback: int = 7,
    intraday: bool = False,
) -> int:
    """Shared spine for backfill / eod / intraday. Differences:

      - tier_filter: None = all tickers, {Tier.TIER_1} = intraday.
      - start_date: backfill provides one; eod / intraday don't (they
        compute from days_lookback).
      - intraday: True → hourly fetch (Tier 1 only) → aggregate to
        provisional daily.
    """
    cid = new_correlation_id()
    src = source or YFinanceSource(
        batch_size=config.etl.yfinance_batch_size,
        request_delay_seconds=config.etl.yfinance_request_delay_seconds,
    )

    with correlation_id(cid):
        # Open the run row first
        with engine.begin() as conn:
            run_id = EtlStatusRepository(conn).open_run(kind, correlation_id=cid)

        t_start = time.monotonic()
        tickers = _select_tickers(engine, config, tier_filter=tier_filter)
        if not tickers:
            logger.warning("No tickers to fetch", extra={"kind": kind.value})
            with engine.begin() as conn:
                EtlStatusRepository(conn).close_run(
                    run_id, RunOutcome.SUCCESS, notes="no tickers",
                )
            return run_id

        logger.info(
            "ETL: fetch pass starting",
            extra={"kind": kind.value, "ticker_count": len(tickers), "run_id": run_id},
        )

        end_date = date.today()
        effective_start = start_date or (end_date - _timedelta_days(days_lookback))

        outcome = _execute_batches(
            engine=engine,
            config=config,
            src=src,
            tickers=tickers,
            run_id=run_id,
            start=effective_start,
            end=end_date,
            timeframe=timeframe,
            kind=kind,
            intraday=intraday,
        )

        with engine.begin() as conn:
            EtlStatusRepository(conn).close_run(run_id, outcome)
            parked_count = EtlStatusRepository(conn).count_parked()
        M.etl_parked_tickers.set(parked_count)
        M.etl_runs_total.labels(
            kind=kind.value, outcome=outcome.value,
        ).inc()
        M.etl_run_duration_seconds.labels(
            kind=kind.value,
        ).observe(time.monotonic() - t_start)

        return run_id


def _execute_batches(
    *,
    engine: Engine,
    config: Config,
    src: YFinanceSource,
    tickers: list[_TickerInfo],
    run_id: int,
    start: date,
    end: date,
    timeframe: Timeframe,
    kind: RunKind,
    intraday: bool,
) -> RunOutcome:
    """Submit all the batches to a ThreadPoolExecutor, collect results.

    Returns SUCCESS if no tickers parked, PARTIAL if some parked,
    FAILED if every ticker errored (rare — that means yfinance is down).
    """
    batch_size = config.etl.yfinance_batch_size
    batches: list[list[_TickerInfo]] = [
        tickers[i:i + batch_size]
        for i in range(0, len(tickers), batch_size)
    ]
    total_attempted = 0
    total_succeeded = 0
    total_failed = 0
    total_bars_written = 0

    with ThreadPoolExecutor(max_workers=config.etl.yfinance_concurrency) as ex:
        futures = [
            ex.submit(
                _process_one_batch,
                engine=engine, config=config, src=src,
                batch=batch, start=start, end=end,
                timeframe=timeframe, intraday=intraday,
            )
            for batch in batches
        ]
        for fut in as_completed(futures):
            try:
                bs = fut.result()
            except Exception:
                logger.exception("Batch worker raised")
                continue
            total_attempted += bs.attempted
            total_succeeded += bs.succeeded
            total_failed += bs.failed
            total_bars_written += bs.bars_written
            # Update the run counters incrementally so the GUI sees progress.
            with engine.begin() as conn:
                EtlStatusRepository(conn).increment_counters(
                    run_id,
                    attempted=bs.attempted, succeeded=bs.succeeded,
                    failed=bs.failed, bars_written=bs.bars_written,
                )

    M.etl_bars_written_total.inc(total_bars_written)

    if total_attempted == 0:
        return RunOutcome.FAILED
    if total_succeeded == 0:
        return RunOutcome.FAILED
    if total_failed > 0:
        return RunOutcome.PARTIAL
    return RunOutcome.SUCCESS


@dataclass(frozen=True, slots=True)
class _BatchStats:
    attempted: int = 0
    succeeded: int = 0
    failed: int = 0
    bars_written: int = 0


def _process_one_batch(
    *,
    engine: Engine,
    config: Config,
    src: YFinanceSource,
    batch: list[_TickerInfo],
    start: date,
    end: date,
    timeframe: Timeframe,
    intraday: bool,
) -> _BatchStats:
    """Fetch one batch, reconcile, write bars, update status. Runs in
    a worker thread. Each call uses its own DB transaction(s)."""
    symbols = [t.symbol for t in batch]

    # Fetch
    if intraday:
        results_by_symbol = src.fetch_hourly_recent(
            symbols, lookback_days=config.etl.intraday_lookback_days,
        )
    else:
        results_by_symbol = src.fetch_daily_history(
            symbols, start=start, end=end,
        )

    # Index DB-stored bars for the reconciler
    ticker_ids = [t.ticker_id for t in batch]
    by_id = {t.ticker_id: t for t in batch}
    existing = _fetch_existing_bars_for_tickers(
        engine, ticker_ids, timeframe, since=start,
    )

    attempted = len(batch)
    succeeded = 0
    failed = 0
    bars_written = 0

    for ticker in batch:
        outcome = results_by_symbol.get(ticker.symbol)
        if outcome is None or not outcome.succeeded:
            err = (outcome.error_message if outcome else "no result")
            _handle_ticker_failure(engine, config, ticker, err)
            failed += 1
            M.etl_tickers_fetched_total.labels(
                tier=str(ticker.tier.value), outcome="failed",
            ).inc()
            continue

        new_bars_for_engine = _aggregate_if_intraday(outcome.bars, intraday)
        plan = reconcile(
            new_bars_for_engine,
            existing.get(ticker.ticker_id, []),
            today=date.today(),
            rolling_window_days=config.etl.reconcile_window_days,
        )

        with engine.begin() as conn:
            bars_repo = BarsRepository(conn)
            ca_repo = CorporateActionsRepository(conn)
            status_repo = EtlStatusRepository(conn)

            for w in plan.writes:
                ts_utc = datetime.combine(
                    w.trading_date, datetime.min.time(),
                    tzinfo=timezone.utc,
                )
                bars_repo.upsert_bar(
                    ticker.ticker_id,
                    timeframe,
                    OHLCVBar(
                        timestamp=ts_utc, open=w.open, high=w.high,
                        low=w.low, close=w.close, volume=w.volume,
                    ),
                    adj_close=w.adj_close,
                )
            divergence_count = ca_repo.record_divergence_candidates(
                ticker.ticker_id, list(plan.divergences),
            )
            if divergence_count > 0:
                M.etl_divergences_flagged_total.inc(divergence_count)

            last_bar_date = (
                max(b.trading_date for b in outcome.bars)
                if outcome.bars else None
            )
            status_repo.record_success(ticker.ticker_id, last_bar_date)

        bars_written += len(plan.writes)
        succeeded += 1
        M.etl_tickers_fetched_total.labels(
            tier=str(ticker.tier.value), outcome="success",
        ).inc()

    return _BatchStats(
        attempted=attempted,
        succeeded=succeeded,
        failed=failed,
        bars_written=bars_written,
    )


def _aggregate_if_intraday(
    raw_bars: tuple[RawBar, ...] | list[RawBar],
    intraday: bool,
) -> list[RawBar]:
    """Intraday path: hourly bars → one provisional daily bar per
    trading day. We take the LAST observed open of the day, the day's
    high/low across all hours, the latest close, and the sum of volumes.

    For the daily path we just pass through.
    """
    if not intraday:
        return list(raw_bars)

    # Group by trading_date — yfinance hourly timestamps land in UTC;
    # the .date() of each gives us the calendar date.
    by_date: dict[date, list[RawBar]] = {}
    for b in raw_bars:
        by_date.setdefault(b.trading_date, []).append(b)
    out: list[RawBar] = []
    for d, bars in sorted(by_date.items()):
        bars_sorted = sorted(bars, key=lambda x: x.trading_date)
        first_o = bars_sorted[0].open
        last_c = bars_sorted[-1].close
        last_ac = bars_sorted[-1].adj_close
        out.append(RawBar(
            trading_date=d,
            open=first_o,
            high=max(b.high for b in bars),
            low=min(b.low for b in bars),
            close=last_c,
            adj_close=last_ac,
            volume=sum(b.volume for b in bars),
        ))
    return out


def _handle_ticker_failure(
    engine: Engine,
    config: Config,
    ticker: _TickerInfo,
    error_message: str,
) -> None:
    """One ticker errored. Update etl_ticker_status; if the failure
    count crosses the park threshold, append to the parked CSV."""
    with engine.begin() as conn:
        repo = EtlStatusRepository(conn)
        parked = repo.record_failure(
            ticker.ticker_id, error_message,
            park_threshold=config.etl.park_failure_threshold,
        )
    if parked:
        append_parked(
            ParkedEntry(
                ticker_symbol=ticker.symbol,
                ticker_id=ticker.ticker_id,
                parked_at=datetime.now(timezone.utc),
                reason=f"{config.etl.park_failure_threshold} consecutive failures",
                last_error=error_message,
            ),
            today=date.today(),
            base_dir=Path(config.etl.parked_dir),
        )


# ---------- Unpark ---------- #


def unpark_all(engine: Engine, config: Config) -> int:
    """Clear the parked flag for every parked ticker. Doesn't refetch
    them — the next EoD run will. Returns the count of unparked
    tickers."""
    with engine.begin() as conn:
        repo = EtlStatusRepository(conn)
        parked = repo.list_parked()
        for t in parked:
            repo.unpark(t.ticker_id)
        M.etl_parked_tickers.set(0)
        return len(parked)


# ---------- Helpers ---------- #


def _today_in_ist() -> date:
    """Today's date in NSE local time."""
    from datetime import timedelta
    return (datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)).date()


def _timedelta_days(n: int):
    from datetime import timedelta
    return timedelta(days=n)
