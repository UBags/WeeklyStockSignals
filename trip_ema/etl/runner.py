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

Phase 14.6a — `exchange` is a required keyword argument on every
public entry point (`refresh_listings`, `recompute_tiers`, `run_eod`,
`run_intraday`, `run_backfill`, `unpark_all`). The engine parameter
is now named `db_engine` (was `engine`) to match the convention used
elsewhere in the codebase (admin routes, scheduler, app.py). Every
public entry point calls `validate_exchange(exchange, config)` before
doing any work — unknown or unconfigured exchanges raise
UnsupportedExchangeError. The `YFinanceSource` is now constructed
per-pass with `exchange=` so its symbol formatter is correct.
"""

from __future__ import annotations

from collections.abc import Callable

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, UTC
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

from ..config import Config
from ..domain import OHLCVBar, Timeframe
from ..observability import metrics as M
from ..observability.correlation import correlation_id, new_correlation_id
from ..persistence.bars_repo import BarsRepository
from ..persistence.exchange_state_repo import ExchangeStateRepository
from .corporate_actions_repo import CorporateActionsRepository
from .exchanges import UnsupportedExchangeError, validate_exchange
from .holidays_calendar import NseTradingCalendar
from .listings_sync import ListingsSync, SyncResult
from .nse_downloader import NseDownloader
from .nse_listings_parser import parse_equity_csv, parse_etf_csv
from .parked import ParkedEntry, append_parked
from .reconciler import _StoredBar, reconcile
from .status_repo import EtlStatusRepository
from .symbol_history_repo import SymbolHistoryRepository  # noqa: F401  -- re-exported
from .tiering import TickerAdv, assign_tiers
from .types import (
    FetchOutcome,  # noqa: F401  -- re-exported for callers
    RawBar,
    ReconcileResult,  # noqa: F401  -- re-exported
    RunKind,
    RunOutcome,
    Tier,
)
from .throttle import ThrottleConfig, ThrottleGate
from .yfinance_source import YFinanceSource
from ..domain.market_session import (
    bar_anchor,
    exchange_of_engine,
    market_today,
    trading_date_of,
)

logger = logging.getLogger(__name__)


# ---------- Listings refresh ---------- #


@dataclass(frozen=True, slots=True)
class RefreshListingsResult:
    sync_result: SyncResult
    run_id: int
    outcome: RunOutcome
    notes: str


def refresh_listings(
    db_engine: Engine,
    config: Config,
    *,
    exchange: str,
    downloader: NseDownloader | None = None,
) -> RefreshListingsResult:
    """Pull the listings CSVs for `exchange` and sync the tickers table.

    Phase 14.6a: `exchange` is a required keyword argument. Currently
    only `NSE` has a fully-implemented listings ingestion pipeline
    (the NseDownloader + nse_listings_parser); other exchanges are
    validated here but the downstream listings code will error if
    called with a non-NSE exchange. That asymmetry is the cleanest
    place to put the limitation until Phase 15 adds per-exchange
    listings sources.

    Single DB transaction — partial failures roll back. The CSV
    downloads are outside the transaction (they can take 10-30s and
    we don't want to hold a row lock).
    """
    validate_exchange(exchange, config)
    if exchange != "NSE":
        raise UnsupportedExchangeError(
            f"refresh_listings is currently implemented for NSE only; "
            f"got exchange={exchange!r}. Other exchanges are accepted "
            f"by validate_exchange but the listings downloader is "
            f"NSE-specific. Multi-exchange listings ingestion is a "
            f"Phase 15 deliverable.",
        )

    dl = downloader or NseDownloader()
    cid = new_correlation_id()

    with correlation_id(cid):
        with db_engine.begin() as conn:
            status_repo = EtlStatusRepository(conn)
            run_id = status_repo.open_run(
                RunKind.LISTINGS, exchange=exchange, correlation_id=cid,
            )

        t_start = time.monotonic()
        try:
            logger.info("ETL: downloading NSE equities CSV", extra={"exchange": exchange})
            equities_bytes = dl.fetch_equities()
            logger.info("ETL: downloading NSE ETFs CSV", extra={"exchange": exchange})
            etfs_bytes = dl.fetch_etfs()
        except Exception as e:
            logger.exception("Listings download failed")
            with db_engine.begin() as conn:
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
                "exchange": exchange,
                "equities": len(equity_listings),
                "etfs": len(etf_listings),
            },
        )

        with db_engine.begin() as conn:
            sync = ListingsSync(conn, exchange=exchange)
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
                    f"sync: exchange={exchange}, "
                    f"new={result.new_listings}, "
                    f"renamed={result.renamed_symbols}, "
                    f"enriched={result.enriched_existing}, "
                    f"delisted={result.delisted}, "
                    f"unchanged={result.unchanged}"
                ),
            )
            # Phase 14.6b: same-transaction update of exchange_state.
            # A successful etl_runs.close ⇒ a synchronized
            # exchange_state row, by transactional guarantee.
            ExchangeStateRepository(conn).record_listings_sync(
                exchange,
                run_at=datetime.now(UTC),
                run_id=run_id,
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


def recompute_tiers(
    db_engine: Engine,
    config: Config,
    *,
    exchange: str,
) -> int:
    """Refresh tickers.tier based on trailing-30-day ADV for `exchange`.

    Returns the number of tier reassignments that changed.

    Phase 14.6a: `exchange` is required. Only tickers belonging to
    this exchange are re-tiered; other exchanges' tier assignments
    are untouched.
    """
    validate_exchange(exchange, config)

    cid = new_correlation_id()
    with correlation_id(cid), db_engine.begin() as conn:
        status_repo = EtlStatusRepository(conn)
        run_id = status_repo.open_run(
            RunKind.TIER_RECOMPUTE, exchange=exchange, correlation_id=cid,
        )
        t_start = time.monotonic()

        # Aggregate 30-day ADV per ticker. The trailing window excludes
        # today (we use bars strictly before today, so weekends/holidays
        # don't bias things). 30 trading days ≈ 45 calendar days.
        rows = conn.execute(
            text("""
                WITH recent_bars AS (
                    SELECT ticker_id, close * volume AS daily_value
                    FROM ohlcv_bars
                    WHERE timeframe = '1D'
                      AND bar_timestamp >= NOW() - INTERVAL '45 days'
                      AND bar_timestamp <  NOW()
                )
                SELECT t.ticker_id,
                       COALESCE(AVG(w.daily_value), 0) AS avg_daily_value
                FROM tickers t
                LEFT JOIN recent_bars w ON t.ticker_id = w.ticker_id
                WHERE t.is_active = TRUE AND t.delisted_at IS NULL
                  AND t.exchange = :ex
                GROUP BY t.ticker_id
            """),
            {"ex": exchange},
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
            notes=f"exchange={exchange}, tier reassignments: {changes}",
        )
        # Phase 14.6b: state write in the same transaction as close_run.
        ExchangeStateRepository(conn).record_tier_recompute(
            exchange, run_at=datetime.now(UTC),
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
    listing_date: date | None = None
    """From NSE's `dateoflisting`. The ONLY thing that distinguishes a ticker
    that is short because it is NEW from one that is short because Yahoo
    truncated us. See _coverage_shortfall_days."""


def _select_tickers(
    db_engine: Engine,
    config: Config,
    *,
    exchange: str,
    tier_filter: set[Tier] | None = None,
) -> list[_TickerInfo]:
    """Active, non-delisted tickers for `exchange`, ordered by
    (tier ASC, symbol ASC). Tier 1 first so partial-run aborts lose
    the least important data.

    Phase 14.6a: `exchange` is required. Each market's fetch pass
    only selects its own tickers.
    """
    with db_engine.connect() as conn:
        query = """
            SELECT ticker_id, symbol, tier, listing_date
            FROM tickers
            WHERE is_active = TRUE AND delisted_at IS NULL
              AND exchange = :ex
        """
        params: dict = {"ex": exchange}
        if tier_filter:
            query += " AND tier = ANY(:tiers)"
            params["tiers"] = [t.value for t in tier_filter]
        query += " ORDER BY tier ASC, symbol ASC"
        rows = conn.execute(text(query), params)
        return [
            _TickerInfo(
                ticker_id=r.ticker_id, symbol=r.symbol, tier=Tier(r.tier),
                listing_date=r.listing_date,
            )
            for r in rows
        ]


def _fetch_existing_bars_for_tickers(
    db_engine: Engine,
    ticker_ids: list[int],
    timeframe: Timeframe,
    *,
    since: date | None = None,
) -> dict[int, list[_StoredBar]]:
    """For reconciliation. since=None pulls all history — only use when
    you really want it (backfill divergence detection on first run)."""
    if not ticker_ids:
        return {}
    with db_engine.connect() as conn:
        query = """
            SELECT ticker_id, bar_timestamp, open, high, low,
                   close, adj_close, volume
            FROM ohlcv_bars
            WHERE ticker_id = ANY(:tids) AND timeframe = :tf
        """
        params: dict[str, Any] = {"tids": ticker_ids, "tf": timeframe.value}
        _ex = exchange_of_engine(db_engine)
        if since is not None:
            query += " AND bar_timestamp >= :since"
            # The lower bound must be the SAME instant the writer would stamp for
            # that trading date, or the boundary day falls out of the window: the
            # anchor is local midnight (18:30Z the previous day for IST), and a
            # bound at 00:00Z sits five and a half hours LATER than the bar it is
            # meant to include.
            params["since"] = bar_anchor(since, _ex)
        rows = conn.execute(text(query), params)

        out: dict[int, list[_StoredBar]] = {tid: [] for tid in ticker_ids}
        for r in rows:
            out[r.ticker_id].append(_StoredBar(
                # NOT r.bar_timestamp.date(). psycopg hands back an aware datetime
                # in the SESSION zone, so a bare .date() silently reads a clock
                # nobody chose. Reconciliation keys on trading_date — get this
                # wrong and bars reconcile against the wrong day, which is the
                # quietest possible way to corrupt a price series.
                trading_date=trading_date_of(r.bar_timestamp, _ex),
                open=r.open, high=r.high, low=r.low, close=r.close,
                adj_close=r.adj_close,
                volume=r.volume,
            ))
        return out


def run_eod(
    db_engine: Engine,
    config: Config,
    *,
    exchange: str,
    source: YFinanceSource | None = None,
    days_lookback: int = 7,
) -> int:
    """Daily end-of-day pass: every active ticker for `exchange`.

    Fetches the trailing `days_lookback` calendar days. We don't
    need full history — just enough overlap to detect intraday
    revisions on the latest bar(s).

    Phase 14.6a: `exchange` is a required keyword argument. The
    MarketOrchestrator passes its own market's exchange code; the
    CLI passes whatever `--exchange` was specified.

    Returns the run_id so the GUI can show progress.
    """
    validate_exchange(exchange, config)
    return _run_fetch_pass(
        db_engine,
        config,
        exchange=exchange,
        kind=RunKind.EOD,
        tier_filter=None,
        timeframe=Timeframe.DAILY,
        source=source,
        days_lookback=days_lookback,
    )


def run_intraday(
    db_engine: Engine,
    config: Config,
    *,
    exchange: str,
    source: YFinanceSource | None = None,
) -> int:
    """Hourly full-universe pass for `exchange` during market hours.

    Yfinance returns hourly bars; we aggregate them to a daily
    PROVISIONAL bar (the latest one). The trip_ema scheduler's
    provisional path picks it up on its next cycle.

    Phase 14.6a: `exchange` is required.

    NOTE: The trading-day check still uses the NSE-specific calendar
    (`NseTradingCalendar`). For non-NSE exchanges the per-market
    orchestrator owns the trading-day decision via its own
    TradingCalendar and would call this function only on real
    trading days; the local check here is defensive for direct CLI
    invocations. When other exchanges' calendars land in Phase 15,
    select the right calendar by exchange.
    """
    validate_exchange(exchange, config)

    # NSE-only defensive holiday check. Other exchanges fall through
    # and trust the caller (typically the per-market orchestrator).
    if exchange == "NSE":
        cal = NseTradingCalendar()
        today_ist = _today_in_ist()
        if not cal.is_trading_day(today_ist):
            logger.info(
                "Skipping intraday run on non-trading day",
                extra={"exchange": exchange, "date": today_ist.isoformat()},
            )
            return -1

    return _run_fetch_pass(
        db_engine,
        config,
        exchange=exchange,
        kind=RunKind.INTRADAY,
        tier_filter=None,  # full universe — every run fetches all tickers
        timeframe=Timeframe.DAILY,  # We aggregate hourly → daily-provisional
        source=source,
        intraday=True,
    )


def fetch_bars_for_tickers(
    db_engine: Engine,
    config: Config,
    *,
    ticker_ids: list[int],
    start_date: date,
    end_date: date | None = None,
    source: YFinanceSource | None = None,
    execute_batches_fn: 'Callable | None' = None,
    validate_exchange_fn: 'Callable | None' = None,
) -> int:
    """Per-ticker yfinance daily-bar fetch for a date range.

    Step 11b1: closes the gap left by `run_backfill`, which is
    full-exchange only. For partial-ticker backfill (operator
    invokes `backfill --stocks SYM1,SYM2`), the pipeline needs to
    refetch only the listed tickers — not the whole exchange.

    All listed tickers must be on the same exchange (validated;
    raises ValueError on mixed-exchange input). Inactive / delisted
    tickers are silently dropped from the fetch.

    Args:
        db_engine: SQLAlchemy Engine.
        config: trip_ema Config (for yfinance batch + concurrency).
        ticker_ids: ticker_ids to fetch. Must be non-empty.
        start_date: inclusive lower bound on the fetch range.
        end_date: inclusive upper bound. None defaults to today.
        source: optional YFinanceSource override (mainly for tests
            with mocked fetches; production code lets this default
            so a fresh source per call gets the correct exchange).

    Returns:
        The `etl_runs.run_id` of the fetch operation, matching
        `run_backfill`'s return shape.

    Raises:
        ValueError: ticker_ids empty, no active tickers resolved, or
            mixed exchanges in the input.
    """
    if not ticker_ids:
        raise ValueError(
            "fetch_bars_for_tickers: ticker_ids must be non-empty"
        )

    # Resolve symbol + tier + exchange for each ticker_id.
    with db_engine.connect() as conn:
        rows = conn.execute(
            text("""
                SELECT ticker_id, exchange, symbol, tier, listing_date
                  FROM tickers
                 WHERE ticker_id = ANY(:ids)
                   AND is_active = TRUE
                   AND delisted_at IS NULL
                 ORDER BY tier ASC, symbol ASC
            """),
            {"ids": ticker_ids},
        ).all()

    if not rows:
        raise ValueError(
            f"fetch_bars_for_tickers: no active tickers found for "
            f"ticker_ids={ticker_ids!r}"
        )

    exchanges = {r.exchange for r in rows}
    if len(exchanges) > 1:
        raise ValueError(
            f"fetch_bars_for_tickers: ticker_ids span multiple "
            f"exchanges {exchanges!r}; call once per exchange"
        )
    exchange = exchanges.pop()
    (validate_exchange_fn or validate_exchange)(exchange, config)

    # This path used to be `YFinanceSource(exchange=exchange)` — no batch_size,
    # no request_delay. It therefore ran at the constructor defaults (delay 0.0)
    # and hammered Yahoo with no spacing at all, which is a remarkable property
    # for the code path whose entire job is to re-fetch tickers that a throttle
    # already cost us. It now reads the same config as every other fetch.
    src = source or YFinanceSource(
        exchange=exchange,
        batch_size=config.etl.yfinance_batch_size,
        request_delay_seconds=config.etl.yfinance_request_delay_seconds,
        throttle_backoff_seconds=config.etl.yfinance_throttle_backoff_seconds,
    )
    tickers = [
        _TickerInfo(
            ticker_id=r.ticker_id, symbol=r.symbol,
            tier=Tier(r.tier), listing_date=r.listing_date,
        )
        for r in rows
    ]

    if end_date is None:
        # `exchange` was resolved from the SCOPED tickers above and
        # validated. exchange_of_engine() asks what exchange the DATABASE
        # is, which is a different question and the wrong one here.
        end_date = market_today(exchange)

    cid = new_correlation_id()
    with correlation_id(cid):
        with db_engine.begin() as conn:
            run_id = EtlStatusRepository(conn).open_run(
                RunKind.BACKFILL,
                exchange=exchange,
                correlation_id=cid,
            )
        outcome, _failed_tickers = (execute_batches_fn or _execute_batches)(
            db_engine=db_engine,
            config=config,
            src=src,
            tickers=tickers,
            run_id=run_id,
            start=start_date,
            end=end_date,
            timeframe=Timeframe.DAILY,
            kind=RunKind.BACKFILL,
            intraday=False,
        )
        # Same single bounded solo-retry for the per-ticker path.
        if _failed_tickers:
            _solo_retry_once(
                db_engine, config,
                exchange=exchange,
                failed_tickers=_failed_tickers,
                start=start_date, end=end_date,
                timeframe=Timeframe.DAILY, intraday=False,
            )
        with db_engine.begin() as conn:
            EtlStatusRepository(conn).close_run(run_id, outcome)

    return run_id


def run_backfill(

    db_engine: Engine,
    config: Config,
    *,
    exchange: str,
    start_date: date,
    source: YFinanceSource | None = None,
    tier_filter: set[Tier] | None = None,
) -> int:
    """Historical backfill from `start_date` to today for `exchange`.

    The full universe is fetched in tier order (so a Ctrl-C partway
    through still leaves us with the most-important tickers done).

    Phase 14.6a: `exchange` is a required keyword argument.
    """
    validate_exchange(exchange, config)
    return _run_fetch_pass(
        db_engine,
        config,
        exchange=exchange,
        kind=RunKind.BACKFILL,
        tier_filter=tier_filter,
        timeframe=Timeframe.DAILY,
        source=source,
        start_date=start_date,
    )



# === STEP 13D1: progress_cb ===
def _emit_fetch_progress(
    cb,
    msg: str,
) -> None:
    """Stage-progress emit for the fetch_bars stage. No-op when
    cb is None. Stage name is always 'fetch_bars'.
    """
    if cb is not None:
        try:
            cb("fetch_bars", msg)
        except Exception:
            logger.exception(
                "progress_cb raised in _run_fetch_pass; "
                "continuing",
            )


def _run_fetch_pass(
    db_engine: Engine,
    config: Config,
    *,
    exchange: str,
    kind: RunKind,
    tier_filter: set[Tier] | None,
    timeframe: Timeframe,
    source: YFinanceSource | None,
    start_date: date | None = None,
    days_lookback: int = 7,
    intraday: bool = False,
    progress_cb: Callable[[str, str], None] | None = None,
    etl_status_repo_factory: 'Callable | None' = None,
) -> int:
    """Shared spine for backfill / eod / intraday. Differences:

      - exchange: which exchange's tickers to fetch (passed through
        to ticker selection and to the YFinanceSource symbol mapper).
      - tier_filter: None = all tickers (default for eod / intraday /
        backfill). A non-None set restricts to those tiers — used
        only by an explicit backfill --tier override.
      - start_date: backfill provides one; eod / intraday don't (they
        compute from days_lookback).
      - intraday: True → hourly fetch → aggregate to provisional
        daily. Full universe (no tier restriction).

    Caller is responsible for validating the exchange BEFORE calling
    this helper (the public entry points do that).
    """
    _EtlStatusRepository = etl_status_repo_factory or EtlStatusRepository
    cid = new_correlation_id()
    src = source or YFinanceSource(
        exchange=exchange,
        batch_size=config.etl.yfinance_batch_size,
        request_delay_seconds=config.etl.yfinance_request_delay_seconds,
        throttle_backoff_seconds=config.etl.yfinance_throttle_backoff_seconds,
    )

    # Sanity: if the caller supplied a source, ensure it matches the
    # exchange we're about to fetch for. A mismatch is a programmer
    # error (probably a test wiring bug).
    if source is not None and source.exchange != exchange:
        raise UnsupportedExchangeError(
            f"YFinanceSource exchange mismatch: source.exchange="
            f"{source.exchange!r}, requested exchange={exchange!r}. "
            f"Construct the source with the right exchange or omit "
            f"the kwarg so the runner builds one.",
        )

    with correlation_id(cid):
        # === STEP 13D1-FIXUP: emit relocated ===
        _emit_fetch_progress(progress_cb, "running")
        # Step 0: close any orphaned etl_runs rows for this exchange
        # left behind by an earlier mid-run process death. Idempotent;
        # cheap (single UPDATE). Runs at the start of every fetch
        # pass so EoD (~daily) naturally serves as a janitor for
        # this exchange, and the Phase 15b backtest concurrent-run
        # guard never sees a dead row as "still in flight". See
        # EtlStatusRepository.cleanup_stale_runs for the threshold
        # rationale.
        with db_engine.begin() as conn:
            n_orphans = _EtlStatusRepository(conn).cleanup_stale_runs(
                exchange=exchange,
            )
        if n_orphans > 0:
            logger.warning(
                "ETL: auto-closed %d orphaned etl_runs row(s) for exchange=%s "
                "(left dangling by an earlier mid-run process death)",
                n_orphans, exchange,
                extra={
                    "exchange": exchange, "kind": kind.value,
                    "n_orphans": n_orphans,
                },
            )

        # Open the run row first
        with db_engine.begin() as conn:
            run_id = _EtlStatusRepository(conn).open_run(
                kind, exchange=exchange, correlation_id=cid,
            )
            # Phase 14.6b: a backfill is the only ETL operation that
            # has a "started" lifecycle event we care about, since
            # `initial_load_started_at` is set on the first backfill
            # against an exchange. Done in the same transaction as
            # open_run so a crash between them is impossible.
            if kind is RunKind.BACKFILL:
                ExchangeStateRepository(conn).record_backfill_started(
                    exchange, started_at=datetime.now(UTC),
                )

        t_start = time.monotonic()
        tickers = _select_tickers(
            db_engine, config, exchange=exchange, tier_filter=tier_filter,
        )
        if not tickers:
            logger.warning(
                "No tickers to fetch",
                extra={"kind": kind.value, "exchange": exchange},
            )
            with db_engine.begin() as conn:
                EtlStatusRepository(conn).close_run(
                    run_id, RunOutcome.SUCCESS,
                    notes=f"no tickers for exchange={exchange}",
                )
                # Phase 14.6b: even a no-tickers run is a recorded
                # operational event — surface it in exchange_state so
                # the GUI doesn't claim the EoD never ran.
                _record_exchange_state_after_pass(
                    conn, kind=kind, exchange=exchange,
                    outcome=RunOutcome.SUCCESS, run_id=run_id,
                    data_date=None,  # no bars touched
                )
            _emit_fetch_progress(progress_cb, "done")
            return run_id

        logger.info(
            "ETL: fetch pass starting",
            extra={
                "kind": kind.value, "exchange": exchange,
                "ticker_count": len(tickers), "run_id": run_id,
            },
        )

        end_date = market_today(exchange)
        effective_start = start_date or (end_date - _timedelta_days(days_lookback))

        outcome, _failed_tickers = _execute_batches(
            db_engine=db_engine,
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
        # Single BOUNDED solo-retry of tickers that flaked in the
        # batched pass. yfinance drops random symbols under batch
        # load; solo re-fetch recovers them. See _solo_retry_once.
        if _failed_tickers:
            _solo_retry_once(
                db_engine, config,
                exchange=exchange,
                failed_tickers=_failed_tickers,
                start=effective_start, end=end_date,
                timeframe=timeframe, intraday=intraday,
            )

        with db_engine.begin() as conn:
            EtlStatusRepository(conn).close_run(run_id, outcome)
            parked_count = EtlStatusRepository(conn).count_parked()
            # Phase 14.6b: same-transaction state write.
            _record_exchange_state_after_pass(
                conn, kind=kind, exchange=exchange,
                outcome=outcome, run_id=run_id,
                data_date=end_date,
            )
        M.etl_parked_tickers.set(parked_count)
        M.etl_runs_total.labels(
            kind=kind.value, outcome=outcome.value,
        ).inc()
        M.etl_run_duration_seconds.labels(
            kind=kind.value,
        ).observe(time.monotonic() - t_start)

        _emit_fetch_progress(progress_cb, "done")
        return run_id


def _solo_retry_once(
    db_engine: Engine,
    config: Config,
    *,
    exchange: str,
    failed_tickers: list,
    start: date,
    end: date,
    timeframe: Timeframe,
    intraday: bool,
) -> None:
    """Re-fetch each flaked ticker ONCE, solo (batch_size=1).

    yfinance drops random symbols under batch load; a solo re-fetch is
    reliable. This runs exactly ONE pass over the failed set captured at
    entry — it is NOT a retry-until-success loop:

      * `snapshot` is fixed before the loop. New failures during the
        retry are recorded by _process_one_batch's own failure handling
        (counting toward the across-run park threshold) but are NOT
        added back here, so they are never retried again in this pass.
      * Plain `for` over the snapshot, hard-bounded by its length.
      * A genuinely-dataless ticker (delisted / ETF / renamed) simply
        fails its one solo attempt and is left alone. No infinite loop.
    """
    snapshot = list(failed_tickers)  # fixed; never appended to below
    if not snapshot:
        return

    # ONE gate for the whole recovery — both layers, every ticker. This is
    # the point of the exercise. Each solo fetch is a batch of one, and a
    # lone empty frame is ambiguous, so no single fetch here can tell a
    # delisted ticker from a rate-limit. Shared, they can: three consecutive
    # solo fetches returning nothing is a throttle, and the gate makes every
    # subsequent fetch in the pass sit out the backoff. Before this, the solo
    # passes walked the entire snapshot at full speed against a closed door
    # and wrote every ticker off as dataless.
    gate = ThrottleGate(
        ThrottleConfig(
            base_seconds=config.etl.yfinance_throttle_backoff_seconds,
        ),
    )

    def _solo_pass(pass_snapshot, *, repair):
        """One bounded solo pass. Returns (recovered, still_failed).
        Plain `for` over a fixed snapshot; no `while`, hard-bounded —
        cannot loop. still_failed = tickers with bs.succeeded == 0."""
        src = YFinanceSource(
            exchange=exchange,
            batch_size=1,
            request_delay_seconds=(
                config.etl.yfinance_request_delay_seconds
            ),
            throttle_backoff_seconds=config.etl.yfinance_throttle_backoff_seconds,
            repair=repair,
            # The shared gate. Every solo fetch in both layers corroborates
            # every other one.
            throttle=gate,
        )
        bound = len(pass_snapshot)
        recovered = 0
        still_failed = []
        for i, ticker in enumerate(pass_snapshot):
            assert i < bound, "solo-retry exceeded its bound"
            try:
                bs = _process_one_batch(
                    db_engine=db_engine, config=config, src=src,
                    batch=[ticker], start=start, end=end,
                    timeframe=timeframe, intraday=intraday,
                )
                if bs.succeeded > 0:
                    recovered += 1
                else:
                    still_failed.append(ticker)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "solo-retry failed for ticker_id=%s",
                    ticker.ticker_id,
                )
                still_failed.append(ticker)
        return recovered, still_failed

    # Layer 2: repair=True — batch-flakes recover WITH repair's fixing.
    rec_a, survivors = _solo_pass(snapshot, repair=True)
    # Layer 3: repair=False over ONLY Layer-2's survivors — rescues the
    # IntCastingNaNError repair-casualties. survivors is fixed before
    # this pass; no re-add, so no loop.
    rec_b = 0
    still_empty: list = []
    if survivors:
        rec_b, still_empty = _solo_pass(survivors, repair=False)
    logger.info(
        "solo-retry: %d recovered (repair=True), %d recovered "
        "(repair=False) of %d flaked",
        rec_a, rec_b, len(snapshot),
    )
    if still_empty:
        # Survived both solo passes. Either genuinely dataless (delisted,
        # rights entitlement, never-traded) or Yahoo was throttling for the
        # whole window. Named, not silent.
        logger.warning(
            "solo-retry: %d ticker(s) STILL returned no data after Layer 2 "
            "and Layer 3: %s",
            len(still_empty),
            ", ".join(
                sorted(f"{t.symbol}(id={t.ticker_id})" for t in still_empty)
            ),
        )


def _execute_batches(
    *,
    db_engine: Engine,
    config: Config,
    src: YFinanceSource,
    tickers: list[_TickerInfo],
    run_id: int,
    start: date,
    end: date,
    timeframe: Timeframe,
    kind: RunKind,
    intraday: bool,
) -> tuple[RunOutcome, list]:
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
    all_failed_tickers: list = []
    all_empty_tickers: list = []
    all_short_tickers: list = []

    with ThreadPoolExecutor(max_workers=config.etl.yfinance_concurrency) as ex:
        futures = [
            ex.submit(
                _process_one_batch,
                db_engine=db_engine, config=config, src=src,
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
            all_failed_tickers.extend(bs.failed_tickers)
            all_empty_tickers.extend(bs.empty_tickers)
            all_short_tickers.extend(bs.short_tickers)
            # Update the run counters incrementally so the GUI sees progress.
            with db_engine.begin() as conn:
                EtlStatusRepository(conn).increment_counters(
                    run_id,
                    attempted=bs.attempted, succeeded=bs.succeeded,
                    failed=bs.failed, bars_written=bs.bars_written,
                )

    M.etl_bars_written_total.inc(total_bars_written)

    # The retry set is failures PLUS zero-bar "successes" PLUS truncated ones.
    # All three go to the bounded solo passes; only failures count toward the
    # run outcome.
    retry_tickers = all_failed_tickers + all_empty_tickers + all_short_tickers
    if all_empty_tickers:
        logger.warning(
            "fetch: %d ticker(s) returned zero bars without erroring "
            "(throttle or delisting) — routing to solo-retry",
            len(all_empty_tickers),
        )
    if all_short_tickers:
        logger.warning(
            "fetch: %d ticker(s) returned TRUNCATED history (first bar far "
            "later than max(requested_start, listing_date)) — routing to "
            "solo-retry: %s",
            len(all_short_tickers),
            ", ".join(sorted(t.symbol for t in all_short_tickers)[:25]),
        )

    if total_attempted == 0:
        return RunOutcome.FAILED, retry_tickers
    if total_succeeded == 0:
        # Every ticker errored or came back empty. Previously an all-empty
        # pass reported SUCCESS; it now fails loudly.
        return RunOutcome.FAILED, retry_tickers
    if total_failed > 0:
        return RunOutcome.PARTIAL, retry_tickers
    return RunOutcome.SUCCESS, retry_tickers


@dataclass(frozen=True, slots=True)
class _BatchStats:
    attempted: int = 0
    succeeded: int = 0
    failed: int = 0
    bars_written: int = 0
    # Identities of tickers that failed this batch — used by the
    # single bounded solo-retry in _run_fetch_pass.
    failed_tickers: tuple = ()
    # Tickers whose fetch did not error but returned ZERO bars. yfinance
    # emits an all-NaN column (no exception) when it throttles us, so these
    # look like successes and are not. Not counted as failures — that would
    # false-park a live-but-halted ticker — but they ARE retry candidates
    # and join failed_tickers in the set handed to _solo_retry_once.
    empty_tickers: tuple = ()
    # Tickers whose fetch returned SOME bars, but starting far later than
    # max(requested_start, listing_date) — i.e. Yahoo truncated us. The bars we
    # got are still written; what is withheld is the verdict "success". Same
    # retry path as empty_tickers.
    short_tickers: tuple = ()


def _coverage_shortfall_days(
    ticker: _TickerInfo, bars, requested_start: date,
) -> int:
    """How many days LATE the earliest returned bar is. 0 means complete.

    expected_first_bar = max(requested_start, listing_date)

    We asked Yahoo for `requested_start` onward. The earliest bar that could
    legitimately exist is the later of that and the day the stock actually listed.
    A first bar materially after that means Yahoo did not return what we asked
    for.

    listing_date is what makes this safe. Without it, any short history looks like
    a failure, and we would false-flag every genuine new listing — ELCID took its
    NSE listing on 2026-04-20 and its .NS history correctly starts there, despite
    the company being decades old. With it, the test is exact: ELCID's expected
    first bar IS 2026-04-20, so it passes.

    Returns 0 (never negative, never a false alarm) when listing_date is NULL,
    since we then have no ground truth to judge against.
    """
    if not bars or ticker.listing_date is None:
        return 0
    expected_first = max(requested_start, ticker.listing_date)
    actual_first = min(b.trading_date for b in bars)
    return max(0, (actual_first - expected_first).days)


def _process_one_batch(
    *,
    db_engine: Engine,
    config: Config,
    src: YFinanceSource,
    batch: list[_TickerInfo],
    start: date,
    end: date,
    timeframe: Timeframe,
    intraday: bool,
) -> _BatchStats:
    """Fetch one batch, reconcile, write bars, update status. Runs in
    a worker thread. Each call uses its own DB transaction(s).

    The `src` carries the exchange; symbol formatting is handled
    inside YFinanceSource.
    """
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
    by_id = {t.ticker_id: t for t in batch}  # noqa: F841 -- handy for debugging
    existing = _fetch_existing_bars_for_tickers(
        db_engine, ticker_ids, timeframe, since=start,
    )

    attempted = len(batch)
    succeeded = 0
    failed = 0
    bars_written = 0
    failed_tickers_local: list = []
    empty_tickers_local: list = []
    short_tickers_local: list = []

    for ticker in batch:
        outcome = results_by_symbol.get(ticker.symbol)
        if outcome is None or not outcome.succeeded:
            err = (
                (outcome.error_message or "<unknown error>")
                if outcome else "no result"
            )
            _handle_ticker_failure(db_engine, config, ticker, err)
            failed += 1
            failed_tickers_local.append(ticker)
            M.etl_tickers_fetched_total.labels(
                tier=str(ticker.tier.value), outcome="failed",
            ).inc()
            continue

        if not outcome.bars:
            # Fetch did not error but returned nothing. yfinance returns the
            # symbol's column all-NaN — without raising — both when it is
            # throttling us and when a ticker is genuinely delisted. We cannot
            # tell the two apart here, so we do neither of the wrong things:
            #   * no _handle_ticker_failure — consecutive_failures untouched,
            #     so a live-but-halted ticker is never false-parked;
            #   * no record_success — writing "success" on a ticker that
            #     returned zero bars is the lie that hid this whole class.
            # It becomes a retry candidate instead: _solo_retry_once re-fetches
            # it solo (Layer 2), and if it is STILL empty, again with
            # repair=False (Layer 3). Genuinely-dataless tickers survive both
            # and are named in the Layer 3 WARNING.
            empty_tickers_local.append(ticker)
            M.etl_tickers_fetched_total.labels(
                tier=str(ticker.tier.value), outcome="empty",
            ).inc()
            continue

        new_bars_for_engine = _aggregate_if_intraday(outcome.bars, intraday)
        plan = reconcile(
            new_bars_for_engine,
            existing.get(ticker.ticker_id, []),
            today=market_today(exchange_of_engine(db_engine)),
            rolling_window_days=config.etl.reconcile_window_days,
        )

        _ex = exchange_of_engine(db_engine)
        with db_engine.begin() as conn:
            bars_repo = BarsRepository(conn)
            ca_repo = CorporateActionsRepository(conn)
            status_repo = EtlStatusRepository(conn)

            for w in plan.writes:
                # THE ANCHOR. Local midnight of the trading date, in the market's
                # own zone — not midnight UTC.
                #
                # Midnight-UTC is invertible only for zones EAST of UTC: for NSE,
                # 00:00Z reads back as 05:30 IST, same calendar day, so it worked.
                # For NYSE, 00:00Z reads back as 20:00 ET the PREVIOUS day — every
                # bar shifts back one day and every Monday bar files into the
                # previous week. Wrong week, wrong weekly OHLC, wrong EMA, wrong
                # signal, no exception, no gap, no audit finding.
                #
                # bar_anchor() is invertible in both hemispheres. See L1.
                ts = bar_anchor(w.trading_date, _ex)
                bars_repo.upsert_bar(
                    ticker.ticker_id,
                    timeframe,
                    OHLCVBar(
                        timestamp=ts, open=w.open, high=w.high,
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

            # Did we get what we ASKED for, or merely something?
            #
            # Everything above this line judges the fetch by whether it errored.
            # Nothing judged it by whether it was COMPLETE — which is how a stock
            # listed in 1999, backfilled from 2006, came back with five bars from
            # last week and was recorded as a clean success. The bars are real, so
            # we keep them; what we withhold is the verdict.
            shortfall = 0 if intraday else _coverage_shortfall_days(
                ticker, outcome.bars, start,
            )
            if shortfall > config.etl.max_coverage_gap_days:
                short_tickers_local.append(ticker)
                # NOT record_success — that call is the lie.
                # NOT record_failure either: consecutive_failures must stay put,
                # or a truncated fetch would park a perfectly live large cap.
                # We leave a breadcrumb and re-fetch it solo instead.
                status_repo.record_coverage_gap(
                    ticker.ticker_id,
                    f"truncated: first bar {shortfall}d after expected "
                    f"(listing_date={ticker.listing_date}, requested={start})",
                )
                M.etl_tickers_fetched_total.labels(
                    tier=str(ticker.tier.value), outcome="short",
                ).inc()
            else:
                status_repo.record_success(ticker.ticker_id, last_bar_date)
                M.etl_tickers_fetched_total.labels(
                    tier=str(ticker.tier.value), outcome="success",
                ).inc()

        bars_written += len(plan.writes)
        succeeded += 1

    return _BatchStats(
        attempted=attempted,
        succeeded=succeeded,
        failed=failed,
        bars_written=bars_written,
        failed_tickers=tuple(failed_tickers_local),
        empty_tickers=tuple(empty_tickers_local),
        short_tickers=tuple(short_tickers_local),
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
    db_engine: Engine,
    config: Config,
    ticker: _TickerInfo,
    error_message: str,
) -> None:
    """One ticker errored. Update etl_ticker_status; if the failure
    count crosses the park threshold, append to the parked CSV."""
    with db_engine.begin() as conn:
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
                parked_at=datetime.now(UTC),
                reason=f"{config.etl.park_failure_threshold} consecutive failures",
                last_error=error_message,
            ),
            today=market_today(exchange_of_engine(db_engine)),
            base_dir=Path(config.etl.parked_dir),
        )


# ---------- Unpark ---------- #


def unpark_all(
    db_engine: Engine,
    config: Config,
    *,
    exchange: str,
) -> int:
    """Clear the parked flag for every parked ticker on `exchange`.
    Doesn't refetch them — the next EoD run will. Returns the count
    of unparked tickers.

    Phase 14.6a: `exchange` is required. Unparking is scoped to one
    exchange's tickers so a BSE operator-action doesn't accidentally
    clear NSE parks.
    """
    validate_exchange(exchange, config)

    with db_engine.begin() as conn:
        repo = EtlStatusRepository(conn)
        parked = repo.list_parked()
        # The repository's list_parked doesn't currently filter by
        # exchange; we filter here against the tickers table.
        if parked:
            # Pull the parked tickers' exchanges in one query and
            # filter the unpark list to this exchange's IDs.
            parked_ids = [p.ticker_id for p in parked]
            rows = conn.execute(
                text("""
                    SELECT ticker_id FROM tickers
                    WHERE ticker_id = ANY(:ids) AND exchange = :ex
                """),
                {"ids": parked_ids, "ex": exchange},
            ).fetchall()
            allowed_ids = {r.ticker_id for r in rows}
            parked = [p for p in parked if p.ticker_id in allowed_ids]

        for t in parked:
            repo.unpark(t.ticker_id)
        # Update the gauge with the post-unpark total (across all
        # exchanges, since the gauge isn't exchange-labelled).
        M.etl_parked_tickers.set(
            EtlStatusRepository(conn).count_parked(),
        )
        return len(parked)


# ---------- Helpers ---------- #


def _record_exchange_state_after_pass(
    conn,
    *,
    kind: RunKind,
    exchange: str,
    outcome: RunOutcome,
    run_id: int,
    data_date: date | None,
) -> None:
    """Phase 14.6b dispatch: pick the right ExchangeStateRepository
    write for each RunKind. Called from inside the same transaction
    that closes the corresponding etl_runs row, so a successful
    close ⇒ a synchronized exchange_state.

    Tier-recompute is NOT routed here — recompute_tiers writes its
    state directly inline because it doesn't go through _run_fetch_pass.
    Listings refresh is also handled inline in refresh_listings for
    the same reason.

    Failed/partial outcomes still update last_eod_outcome (operators
    need to see "the EoD ran but partially failed today" distinct
    from "the EoD didn't run today"); backfill completion only sets
    initial_load_completed_at on a clean success.
    """
    repo = ExchangeStateRepository(conn)
    now = datetime.now(UTC)

    if kind is RunKind.EOD:
        repo.record_eod(
            exchange,
            run_at=now,
            outcome=outcome.value,
            data_date=data_date,
            run_id=run_id,
        )
    elif kind is RunKind.INTRADAY:
        # Intraday isn't tracked per-outcome — only timestamp.
        # An intraday failure is visible via etl_runs; the
        # exchange_state field is just "when did we last run".
        repo.record_intraday(exchange, run_at=now)
    elif kind is RunKind.BACKFILL:
        # Backfill always advances the last_eod_* fields too: a
        # backfill that covers up to today is, by definition, a
        # superset of what an EoD does. This keeps the "is this
        # exchange caught up to today" question a single field
        # lookup rather than max(last_eod_data_date, max-bar-from-backfill).
        repo.record_eod(
            exchange,
            run_at=now,
            outcome=outcome.value,
            data_date=data_date,
            run_id=run_id,
        )
        repo.record_backfill_completed(
            exchange,
            completed_at=now,
            outcome=outcome.value,
        )
    # Other RunKinds (LISTINGS, TIER_RECOMPUTE) aren't routed here —
    # their entry points write to exchange_state directly. Silently
    # ignoring them rather than asserting keeps this helper safe to
    # extend with new kinds in the future.


def _today_in_ist() -> date:
    """Today's date in NSE local time."""
    from datetime import timedelta
    return (datetime.now(UTC) + timedelta(hours=5, minutes=30)).date()


def _timedelta_days(n: int):
    from datetime import timedelta
    return timedelta(days=n)
