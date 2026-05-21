"""`trip-ema-etl` command-line interface.

Subcommands:
  refresh-listings     — pull NSE CSVs, sync tickers table
  backfill             — historical OHLCV load from --start
  incremental-eod      — daily EoD fetch (all tickers, tier-ordered)
  incremental-intraday — hourly Tier-1-only fetch during market hours
  recompute-tiers      — refresh tickers.tier from 30-day ADV
  status               — recent etl_runs + parked count + per-tier counts
  unpark               — clear parked flag for all parked tickers
  approve-action       — promote a corporate_actions row to approved

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
from typing import Optional

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
    start = _parse_date(args.start)
    if start is None:
        print(f"error: --start must be YYYY-MM-DD, got {args.start!r}", file=sys.stderr)
        return 2
    tier_filter = _parse_tier_filter(args.tier)
    db_engine = create_engine_from_config(config)
    try:
        run_id = run_backfill(
            db_engine, config,
            exchange=args.exchange,
            start_date=start, tier_filter=tier_filter,
        )
    finally:
        db_engine.dispose()
    print(f"backfill complete ({args.exchange}, run_id={run_id})")
    return 0


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


def cmd_generate_signals(args, config: Config) -> int:
    """Phase 15a — generate historical signals for a whole exchange's
    universe across the requested timeframes. Writes to
    `generated_signals/{EXCHANGE}.json` (atomic temp+rename).

    Synchronous: the CLI invocation blocks until the run completes.
    The HTTP equivalent (Phase 15b) submits the same function to
    the etl_executor for async operation.

    Resume semantics: a second invocation without --force-full picks
    up where the first left off per ticker; tickers that have no
    new bars are skipped. --force-full wipes resume rows for the
    exchange in a small transaction at run start and recomputes
    every ticker from earliest history.
    """
    # Lazy import — heavy module pulls in the engine, indicators,
    # etc.; keep the CLI's startup time short for other subcommands.
    from .signal_generator import generate_historical_signals
    from ..domain import Timeframe

    try:
        tfs = [Timeframe(t) for t in args.timeframe]
    except ValueError as e:
        print(f"error: invalid timeframe: {e}", file=sys.stderr)
        return 2

    db_engine = create_engine_from_config(config)
    try:
        result = generate_historical_signals(
            db_engine, config,
            exchange=args.exchange,
            timeframes=tfs,
            force_full=args.force_full,
        )
    finally:
        db_engine.dispose()

    print(result.summary_line())
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

    bk = sub.add_parser("backfill", help="Historical OHLCV load")
    _add_exchange_arg(bk)
    bk.add_argument(
        "--start", required=True,
        help="Start date (YYYY-MM-DD). e.g., 2010-01-01 for 15-year backfill",
    )
    bk.add_argument(
        "--tier", choices=("1", "2", "3", "all"),
        default="all",
        help="Restrict to one tier (default: all). 'all' fetches every active ticker.",
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

    gs = sub.add_parser(
        "generate-signals",
        help="Phase 15a — generate historical signals for backtesting",
    )
    _add_exchange_arg(gs)
    gs.add_argument(
        "--timeframe",
        action="append",
        default=None,
        choices=("1D", "1W"),
        help=(
            "Timeframe to generate (repeatable). Defaults to ['1W'] "
            "when omitted, matching the live system's primary."
        ),
    )
    gs.add_argument(
        "--force-full",
        action="store_true",
        help=(
            "Wipe resume tracking rows for this exchange and "
            "recompute every ticker from earliest history. Use when "
            "engine params or exchange_overrides have changed. "
            "May take 30+ minutes for a full NSE universe."
        ),
    )

    # Phase 1 fundamentals commands. Registered here so the user
    # types `trip-ema-etl refresh-industry` exactly like the other
    # ETL commands. The fundamentals package's CLI module owns the
    # argparse wiring AND the handler functions; we only need to
    # merge the returned handler map into the dispatcher below.
    from ..fundamentals.cli import register_subparsers as _register_fundamentals
    p._fundamentals_handlers = _register_fundamentals(sub, _add_exchange_arg)

    return p


# ---------- entrypoint ---------- #


def main(argv: Optional[list[str]] = None) -> int:
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
        "generate-signals":     cmd_generate_signals,
    }
    # Merge Phase 1 fundamentals handlers (registered by
    # _build_parser via fundamentals.cli.register_subparsers).
    handlers.update(getattr(parser, "_fundamentals_handlers", {}))
    # `--timeframe` with action='append' starts as None, not [].
    # Apply the documented default (`['1W']`) here so subcommand
    # handlers don't have to repeat the logic.
    if args.command == "generate-signals" and not args.timeframe:
        args.timeframe = ["1W"]
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


def _parse_date(s: str) -> Optional[date]:
    try:
        return date.fromisoformat(s)
    except (TypeError, ValueError):
        return None


def _parse_tier_filter(value: str) -> Optional[set[Tier]]:
    if value == "all":
        return None
    return {Tier(int(value))}


if __name__ == "__main__":
    sys.exit(main())
