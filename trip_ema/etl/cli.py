"""`trip-ema-etl` command-line interface.

Subcommands:
  refresh-listings     — pull NSE CSVs, sync tickers table
  backfill             — V1 backfill (full pipeline: bars + state + signals)
  incremental-eod      — daily EoD fetch (all tickers, tier-ordered)
  incremental-intraday — hourly Tier-1-only fetch during market hours
  recompute-tiers      — refresh tickers.tier from 30-day ADV
  status               — recent etl_runs + parked count + per-tier counts
  unpark               — clear parked flag for all parked tickers
  approve-action       — promote a corporate_actions row to approved

Note: historical signal regeneration is provided by
`trip-ema-etl compute-signals --all-tickers --force --timeframe 1W`
(in the fundamentals sub-CLI). The Phase 15a `generate-signals`
command was removed in Step 10 — its underlying signal_generator
module was deleted during the demolition work.

Phase 14.6a: every ETL subcommand that touches ticker data requires
`--exchange NAME`. NAME must be:
  1. Known to the codebase (NSE, BSE, NYSE, NASDAQ, TSE, HKSE, SSE,
     SZSE, LSE — see trip_ema/etl/exchanges.py); AND
  2. Present in config.yaml's `market_hours:` block (the operator
     has enabled it).

If either check fails, the CLI prints a single line to stderr and
exits 2 — no stack trace.

Conventions:
  - argparse (no new dep)
  - Exit 0 success, 1 partial-failure, 2 fatal
  - Logging level via --verbose / -v
  - Config path via --config / -c (defaults to config.yaml in CWD)
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

from ..config import Config
from ..config.loader import load_config
from ..observability.logging_config import configure_logging
from ..persistence.connection import create_engine_from_config
from .corporate_actions_repo import CorporateActionsRepository
from .exchanges import UnsupportedExchangeError
from .runner import (
    recompute_tiers,
    refresh_listings,
    run_backfill,
    run_eod,
    run_intraday,
    unpark_all,
)
from .status_repo import EtlStatusRepository
from .types import RunOutcome, Tier


logger = logging.getLogger(__name__)


# ---------- Command handlers ---------- #


def cmd_refresh_listings(args, config: Config) -> int:
    db_engine = create_engine_from_config(config)
    try:
        result = refresh_listings(db_engine, config, exchange=args.exchange)
    finally:
        db_engine.dispose()
    print(
        f"refresh-listings ({args.exchange}): "
        f"new={result.sync_result.new_listings} "
        f"renamed={result.sync_result.renamed_symbols} "
        f"enriched={result.sync_result.enriched_existing} "
        f"delisted={result.sync_result.delisted} "
        f"unchanged={result.sync_result.unchanged}"
    )
    return 0 if result.outcome == RunOutcome.SUCCESS else 1


def cmd_backfill(args, config: Config) -> int:
    """V1 backfill — Step 11b2 rewrite.

    The legacy implementation called `run_backfill()` which was a
    pure yfinance bar-fetch. The V1 cycle covers the full pipeline:
    bar fetch + stock state + enrichment + industry stages +
    historical_signals + current_signals rebuild. See
    `trip_ema.backfill.pipeline.backfill` for stage details.

    Args (post-argparse):
        --date_from YYYY-MM-DD (required unless --release-lock is
            given alone)
        --date_to YYYY-MM-DD (defaults to today)
        --stocks 'SYM1,SYM2' / 'SYM1 SYM2 SYM3' (defaults to ALL)
        --exchange NSE / BSE / NYSE (default NSE)
        --no_fetch_bars (skip stage 1)
        --release-lock (clear stale backfill_runs orphans first;
            then continue with the backfill if date_from given,
            else exit 0)
    """
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    from ..backfill import backfill
    from ..persistence import transaction
    from ..persistence.backfill_runs_repo import (
        BackfillRunsRepository,
    )

    # ---- 1. Release-lock first (operator-explicit cleanup) ----
    db_engine = create_engine_from_config(config)
    try:
        if args.release_lock:
            with transaction(db_engine) as conn:
                n = BackfillRunsRepository(conn).release_locks()
            if n == 0:
                print("--release-lock: no active marker rows found.")
            else:
                print(
                    f"--release-lock: cleared {n} orphan marker "
                    f"row(s) in backfill_runs."
                )
            if args.date_from is None:
                # Cleanup-only mode. Print clear guidance about
                # what arguments to provide for a proper backfill.
                print()
                print(
                    "To run a backfill, provide --date_from "
                    "(YYYY-MM-DD, required) and optionally:"
                )
                print(
                    "  --date_to YYYY-MM-DD   (default: today)"
                )
                print(
                    "  --stocks SYM1,SYM2     (default: ALL "
                    "active tickers on the exchange)"
                )
                print(
                    "  --exchange NSE|BSE|NYSE  (default: NSE)"
                )
                print(
                    "  --no_fetch_bars        (skip yfinance "
                    "stage 1; useful for state-only rebuild)"
                )
                return 0
        elif args.date_from is None:
            print(
                "error: --date_from is required (YYYY-MM-DD), "
                "or use --release-lock for cleanup-only mode",
                file=sys.stderr,
            )
            return 2

        # ---- 2. Parse + validate dates ----
        date_from = _parse_date(args.date_from)
        if date_from is None:
            print(
                f"error: --date_from must be YYYY-MM-DD, "
                f"got {args.date_from!r}",
                file=sys.stderr,
            )
            return 2

        date_to = None
        if args.date_to:
            date_to = _parse_date(args.date_to)
            if date_to is None:
                print(
                    f"error: --date_to must be YYYY-MM-DD, "
                    f"got {args.date_to!r}",
                    file=sys.stderr,
                )
                return 2

        # ---- 3. Resolve --stocks symbols → ticker_ids ----
        stocks: list[int] | None = None
        if args.stocks:
            # Accept both 'A,B,C' and 'A B C' shapes.
            symbols: list[str] = []
            for item in args.stocks:
                for part in item.split(","):
                    p = part.strip().upper()
                    if p:
                        symbols.append(p)
            if not symbols:
                print(
                    "error: --stocks must list at least one symbol",
                    file=sys.stderr,
                )
                return 2

            with db_engine.connect() as conn:
                rows = conn.execute(
                    text("""
                        SELECT ticker_id, symbol FROM tickers
                         WHERE symbol = ANY(:syms)
                           AND exchange = :ex
                           AND is_active = TRUE
                           AND delisted_at IS NULL
                    """),
                    {"syms": symbols, "ex": args.exchange},
                ).all()
            found = {r.symbol: r.ticker_id for r in rows}
            missing = [s for s in symbols if s not in found]
            if missing:
                print(
                    f"error: symbol(s) not found / inactive / wrong "
                    f"exchange on {args.exchange}: "
                    f"{', '.join(missing)}",
                    file=sys.stderr,
                )
                return 2
            stocks = sorted(found.values())

        # ---- 4. Print plan + rough ETA ----
        refetch = not args.no_fetch_bars
        _print_backfill_plan(
            date_from=date_from, date_to=date_to,
            exchange=args.exchange, stocks=stocks,
            refetch_bars=refetch, db_engine=db_engine,
        )

        # ---- 5. Run the pipeline ----
        def _progress(stage: str, message: str) -> None:
            print(f"  [{stage}] {message}", flush=True)

        try:
            result = backfill(
                db_engine, config,
                date_from=date_from,
                date_to=date_to,
                exchange=args.exchange,
                stocks=stocks,
                refetch_bars=refetch,
                progress_cb=_progress,
            )
        except IntegrityError:
            print(
                "error: another backfill appears to be active "
                "(backfill_runs.uq_one_active fired). If a previous "
                "run was Ctrl-C'd or crashed, use "
                "`backfill --release-lock` to clear the orphan, "
                "then re-run.",
                file=sys.stderr,
            )
            return 1

        # ---- 6. Report ----
        return _report_backfill_result(result)

    finally:
        db_engine.dispose()


def _print_backfill_plan(
    *,
    date_from, date_to, exchange, stocks,
    refetch_bars, db_engine,
) -> None:
    """Print operator-facing summary + rough ETA before the
    pipeline starts. ETA is a back-of-envelope estimate based on
    typical yfinance + recompute rates."""
    from sqlalchemy import text

    if stocks is None:
        with db_engine.connect() as conn:
            n_tickers = conn.execute(text("""
                SELECT COUNT(*) FROM tickers
                 WHERE exchange = :ex
                   AND is_active = TRUE
                   AND delisted_at IS NULL
            """), {"ex": exchange}).scalar_one()
    else:
        n_tickers = len(stocks)

    print(f"V1 Backfill — {exchange}")
    print(f"  date_from:     {date_from.isoformat()}")
    if date_to:
        print(f"  date_to:       {date_to.isoformat()}")
    else:
        print(f"  date_to:       (defaults to today)")
    if stocks is None:
        print(f"  stocks:        ALL ({n_tickers} active)")
    else:
        print(
            f"  stocks:        {n_tickers} specified "
            f"({','.join(str(t) for t in stocks[:5])}"
            f"{'...' if n_tickers > 5 else ''})"
        )
    print(f"  refetch_bars:  {refetch_bars}")
    print()
    # Rough ETA — back of envelope, just to give operator a sense
    # whether to expect minutes or hours.
    seconds_est = _estimate_runtime_seconds(
        n_tickers=n_tickers, refetch_bars=refetch_bars,
    )
    minutes_est = max(1, round(seconds_est / 60))
    print(
        f"  Estimated runtime: ~{minutes_est} min "
        f"(rough; per-ticker cost dominates)."
    )
    print()


def _estimate_runtime_seconds(
    *, n_tickers: int, refetch_bars: bool,
) -> float:
    """Back-of-envelope estimator. Numbers are approximate;
    actual runtime depends on network, CPU, DB load."""
    yfinance_per_ticker = 0.4 if refetch_bars else 0.0
    stock_state_per_ticker = 0.1
    stock_enrichment_per_ticker = 0.1
    industry_stages_total = 30.0
    per_ticker = (
        yfinance_per_ticker
        + stock_state_per_ticker
        + stock_enrichment_per_ticker
    )
    return n_tickers * per_ticker + industry_stages_total


def _report_backfill_result(result) -> int:
    """Print the BackfillResult summary. Returns exit code: 0 on
    success, 1 on stage failure."""
    print()
    print("=" * 60)
    if result.failed_stage is not None:
        print(
            f"backfill FAILED in {result.failed_stage}: "
            f"{result.error_message}"
        )
        print(
            f"  run_id:      {result.run_id} "
            f"(marker finished; live cycle resumes)"
        )
    else:
        print("backfill complete")
        print(f"  run_id:      {result.run_id}")
    print(f"  range:       {result.date_from} .. {result.date_to}")
    print(
        f"  scope:       {result.ticker_scope_size} tickers "
        f"({result.ticker_scope_label[:50]}"
        f"{'...' if len(result.ticker_scope_label) > 50 else ''})"
    )
    if result.bars_fetch_run_id is not None:
        print(
            f"  bars fetch:  etl_runs.run_id={result.bars_fetch_run_id}"
        )
    print(
        f"  stock state: {result.stock_state_tickers_done} "
        f"done, {len(result.stock_state_failed)} failed"
    )
    print(
        f"  enrichment:  {result.stock_enrichment_tickers_done} "
        f"done, {len(result.stock_enrichment_failed)} failed"
    )
    print(
        f"  industries:  iiv={result.iiv_industries_done}, "
        f"state={result.industry_state_done}, "
        f"enrich={result.industry_enrichment_done}"
    )
    print(
        f"  signals:     deleted={result.historical_signals_deleted}, "
        f"inserted={result.historical_signals_inserted}, "
        f"mondays_walked={result.mondays_walked}"
    )
    print(
        f"  current:     upserted={result.current_signals_upserted}, "
        f"deleted={result.current_signals_deleted}"
    )
    return 0 if result.failed_stage is None else 1


def cmd_incremental_eod(args, config: Config) -> int:
    db_engine = create_engine_from_config(config)
    try:
        run_id = run_eod(
            db_engine, config,
            exchange=args.exchange,
            days_lookback=args.lookback_days,
        )
    finally:
        db_engine.dispose()
    print(f"eod complete ({args.exchange}, run_id={run_id})")
    return 0


def cmd_incremental_intraday(args, config: Config) -> int:
    db_engine = create_engine_from_config(config)
    try:
        run_id = run_intraday(db_engine, config, exchange=args.exchange)
    finally:
        db_engine.dispose()
    if run_id < 0:
        print(f"intraday ({args.exchange}): skipped (non-trading day)")
        return 0
    print(f"intraday complete ({args.exchange}, run_id={run_id})")
    return 0


def cmd_recompute_tiers(args, config: Config) -> int:
    db_engine = create_engine_from_config(config)
    try:
        changes = recompute_tiers(db_engine, config, exchange=args.exchange)
    finally:
        db_engine.dispose()
    print(f"tier reassignments ({args.exchange}): {changes}")
    return 0


def cmd_status(args, config: Config) -> int:
    db_engine = create_engine_from_config(config)
    try:
        with db_engine.connect() as conn:
            status_repo = EtlStatusRepository(conn)
            recent = status_repo.list_recent_runs(limit=args.limit)
            parked = status_repo.list_parked()
        print(f"Recent ETL runs (last {len(recent)}):")
        for r in recent:
            print(
                f"  run_id={r.run_id} kind={r.kind:18} "
                f"outcome={r.outcome or 'in-flight':10} "
                f"started={r.started_at.isoformat(timespec='seconds')} "
                f"attempted={r.tickers_attempted} ok={r.tickers_succeeded} "
                f"fail={r.tickers_failed} bars={r.bars_written}"
            )
        print()
        print(f"Parked tickers: {len(parked)}")
        if parked and args.verbose:
            for t in parked[:20]:
                print(
                    f"  ticker_id={t.ticker_id} "
                    f"parked_at={t.parked_at.isoformat() if t.parked_at else 'n/a'} "
                    f"reason={t.parked_reason}"
                )
    finally:
        db_engine.dispose()
    return 0


def cmd_unpark(args, config: Config) -> int:
    db_engine = create_engine_from_config(config)
    try:
        count = unpark_all(db_engine, config, exchange=args.exchange)
    finally:
        db_engine.dispose()
    print(
        f"unparked {count} {args.exchange} tickers "
        f"(next EoD run will retry them)"
    )
    return 0


def cmd_approve_action(args, config: Config) -> int:
    db_engine = create_engine_from_config(config)
    try:
        with db_engine.begin() as conn:
            repo = CorporateActionsRepository(conn)
            ok = repo.approve(args.action_id, reviewed_by="cli", notes=args.notes)
    finally:
        db_engine.dispose()
    if not ok:
        print(
            f"action_id {args.action_id} not found or not in pending_review",
            file=sys.stderr,
        )
        return 1
    print(f"approved action_id={args.action_id}")
    return 0


# ---------- argparse setup ---------- #


def _add_exchange_arg(parser: argparse.ArgumentParser) -> None:
    """All ETL-action subcommands take a required --exchange flag.
    Centralized here so the help text and constraints stay
    consistent across subcommands."""
    parser.add_argument(
        "--exchange",
        required=True,
        help=(
            "Exchange to operate on (e.g. NSE, BSE, NYSE). Must be "
            "(1) supported by the codebase and (2) configured in "
            "config.yaml's `market_hours:` block. See "
            "trip_ema/etl/exchanges.py for the known list."
        ),
    )


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="trip-ema-etl",
        description=(
            "ETL pipeline for trip_ema. Each ETL action requires "
            "an explicit --exchange (NSE, BSE, NYSE, NASDAQ, TSE, "
            "HKSE, SSE, SZSE, LSE). Exchanges other than NSE may "
            "not have full listings ingestion implemented yet — see "
            "the runner module for current state."
        ),
    )
    p.add_argument(
        "--config", "-c",
        default="config.yaml",
        help="Path to config.yaml (default: ./config.yaml)",
    )
    p.add_argument(
        "--verbose", "-v",
        action="count", default=0,
        help="Increase log verbosity (use -vv for DEBUG)",
    )
    sub = p.add_subparsers(dest="command", required=True)

    rl = sub.add_parser(
        "refresh-listings",
        help="Pull listings CSVs and sync tickers table",
    )
    _add_exchange_arg(rl)

    bk = sub.add_parser(
        "backfill",
        help=(
            "V1 backfill: re-run the full pipeline (bars + state + "
            "enrichment + industries + historical_signals + "
            "current_signals) for a date range and ticker scope."
        ),
    )
    _add_exchange_arg(bk)
    bk.add_argument(
        "--date_from",
        help=(
            "Start date YYYY-MM-DD (ISO 8601). Required unless "
            "--release-lock is used alone. Rounds DOWN to Monday."
        ),
    )
    bk.add_argument(
        "--date_to",
        help=(
            "End date YYYY-MM-DD. Defaults to today. The walk "
            "goes up through monday_of(date_to) inclusive."
        ),
    )
    bk.add_argument(
        "--stocks", nargs="+", default=None,
        help=(
            "Symbols to backfill. Accepts both 'RELIANCE,TCS' "
            "(comma-separated) and 'RELIANCE TCS' (space-separated). "
            "Default: ALL active tickers on the exchange."
        ),
    )
    bk.add_argument(
        "--no_fetch_bars", action="store_true",
        help=(
            "Skip stage 1 (yfinance bar refetch). Useful when the "
            "OHLCV table is already populated and only the V1 "
            "derivations need rebuilding."
        ),
    )
    bk.add_argument(
        "--release-lock", action="store_true",
        dest="release_lock",
        help=(
            "Clear any orphan rows in backfill_runs (finished_at "
            "IS NULL) before proceeding. Use after a prior backfill "
            "was Ctrl-C'd or crashed. With --date_from also given, "
            "continues with the backfill; alone, exits after cleanup."
        ),
    )

    eod = sub.add_parser("incremental-eod", help="Daily EoD ETL pass")
    _add_exchange_arg(eod)
    eod.add_argument(
        "--lookback-days", type=int, default=7,
        help="Calendar days of overlap to fetch (default 7).",
    )

    intraday = sub.add_parser(
        "incremental-intraday",
        help="Hourly Tier-1-only ETL during market hours",
    )
    _add_exchange_arg(intraday)

    rt = sub.add_parser(
        "recompute-tiers",
        help="Refresh tickers.tier from 30-day ADV",
    )
    _add_exchange_arg(rt)

    status = sub.add_parser(
        "status", help="Recent runs + parked count",
    )
    # Status is read-only and exchange-agnostic — no --exchange filter
    # in this slice. Add it if/when exchange-scoped status displays
    # become useful (future GUI work).
    status.add_argument("--limit", type=int, default=10)

    unpark = sub.add_parser(
        "unpark", help="Clear parked flag for all parked tickers on an exchange",
    )
    _add_exchange_arg(unpark)

    appr = sub.add_parser(
        "approve-action", help="Approve a pending corporate_action",
    )
    # Corporate actions are tracked per-ticker, not per-exchange at
    # the CLI level — the action_id is already specific.
    appr.add_argument("action_id", type=int)
    appr.add_argument("--notes", help="Optional reviewer notes")

    # Phase 1 fundamentals commands. Registered here so the user
    # types `trip-ema-etl refresh-industry` exactly like the other
    # ETL commands. The fundamentals package's CLI module owns the
    # argparse wiring AND the handler functions; we only need to
    # merge the returned handler map into the dispatcher below.
    from ..fundamentals.cli import register_subparsers as _register_fundamentals
    # Stash the handler map on the ArgumentParser instance so the
    # dispatcher below can find it. ArgumentParser has no such
    # attribute in its public interface; deliberate runtime hack.
    p._fundamentals_handlers = _register_fundamentals(sub, _add_exchange_arg)  # type: ignore[attr-defined]

    return p


# ---------- entrypoint ---------- #


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    level = logging.WARNING
    if args.verbose >= 2:
        level = logging.DEBUG
    elif args.verbose == 1:
        level = logging.INFO
    configure_logging(level=level, json_output=False)

    config_path = Path(args.config)
    if not config_path.exists():
        print(f"error: config not found at {config_path}", file=sys.stderr)
        return 2

    try:
        config = load_config(config_path)
    except Exception as e:
        print(f"error: failed to load config: {e}", file=sys.stderr)
        return 2

    handlers = {
        "refresh-listings":     cmd_refresh_listings,
        "backfill":             cmd_backfill,
        "incremental-eod":      cmd_incremental_eod,
        "incremental-intraday": cmd_incremental_intraday,
        "recompute-tiers":      cmd_recompute_tiers,
        "status":               cmd_status,
        "unpark":               cmd_unpark,
        "approve-action":       cmd_approve_action,
    }
    # Merge Phase 1 fundamentals handlers (registered by
    # _build_parser via fundamentals.cli.register_subparsers).
    handlers.update(getattr(parser, "_fundamentals_handlers", {}))
    handler = handlers.get(args.command)
    if handler is None:
        parser.error(f"unknown command: {args.command}")
        return 2

    try:
        return handler(args, config)
    except UnsupportedExchangeError as e:
        # Clean stderr line, no stack trace — these are user-facing
        # validation errors, not bugs.
        print(f"error: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\ninterrupted by user", file=sys.stderr)
        return 130
    except Exception as e:
        logger.exception("Unhandled error in command %s", args.command)
        print(f"error: {e}", file=sys.stderr)
        return 2


def _parse_date(s: str) -> date | None:
    try:
        return date.fromisoformat(s)
    except (TypeError, ValueError):
        return None


def _parse_tier_filter(value: str) -> set[Tier] | None:
    if value == "all":
        return None
    return {Tier(int(value))}


if __name__ == "__main__":
    sys.exit(main())
