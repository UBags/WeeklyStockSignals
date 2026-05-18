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
    engine = create_engine_from_config(config)
    try:
        result = refresh_listings(engine, config)
    finally:
        engine.dispose()
    print(
        f"refresh-listings: new={result.sync_result.new_listings} "
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
    engine = create_engine_from_config(config)
    try:
        run_id = run_backfill(
            engine, config,
            start_date=start, tier_filter=tier_filter,
        )
    finally:
        engine.dispose()
    print(f"backfill complete (run_id={run_id})")
    return 0


def cmd_incremental_eod(args, config: Config) -> int:
    engine = create_engine_from_config(config)
    try:
        run_id = run_eod(engine, config, days_lookback=args.lookback_days)
    finally:
        engine.dispose()
    print(f"eod complete (run_id={run_id})")
    return 0


def cmd_incremental_intraday(args, config: Config) -> int:
    engine = create_engine_from_config(config)
    try:
        run_id = run_intraday(engine, config)
    finally:
        engine.dispose()
    if run_id < 0:
        print("intraday: skipped (non-trading day)")
        return 0
    print(f"intraday complete (run_id={run_id})")
    return 0


def cmd_recompute_tiers(args, config: Config) -> int:
    engine = create_engine_from_config(config)
    try:
        changes = recompute_tiers(engine, config)
    finally:
        engine.dispose()
    print(f"tier reassignments: {changes}")
    return 0


def cmd_status(args, config: Config) -> int:
    engine = create_engine_from_config(config)
    try:
        with engine.connect() as conn:
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
        engine.dispose()
    return 0


def cmd_unpark(args, config: Config) -> int:
    engine = create_engine_from_config(config)
    try:
        count = unpark_all(engine, config)
    finally:
        engine.dispose()
    print(f"unparked {count} tickers (next EoD run will retry them)")
    return 0


def cmd_approve_action(args, config: Config) -> int:
    engine = create_engine_from_config(config)
    try:
        with engine.begin() as conn:
            repo = CorporateActionsRepository(conn)
            ok = repo.approve(args.action_id, reviewed_by="cli", notes=args.notes)
    finally:
        engine.dispose()
    if not ok:
        print(
            f"action_id {args.action_id} not found or not in pending_review",
            file=sys.stderr,
        )
        return 1
    print(f"approved action_id={args.action_id}")
    return 0


# ---------- argparse setup ---------- #


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="trip-ema-etl",
        description="ETL pipeline for trip_ema (NSE equities + ETFs from yfinance).",
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

    sub.add_parser(
        "refresh-listings",
        help="Pull NSE equity + ETF CSVs and sync tickers table",
    )

    bk = sub.add_parser(
        "backfill", help="Historical OHLCV load",
    )
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
    eod.add_argument(
        "--lookback-days", type=int, default=7,
        help="Calendar days of overlap to fetch (default 7).",
    )

    sub.add_parser(
        "incremental-intraday",
        help="Hourly Tier-1-only ETL during market hours",
    )

    sub.add_parser(
        "recompute-tiers",
        help="Refresh tickers.tier from 30-day ADV",
    )

    status = sub.add_parser("status", help="Recent runs + parked count")
    status.add_argument("--limit", type=int, default=10)

    sub.add_parser("unpark", help="Clear parked flag for all parked tickers")

    appr = sub.add_parser("approve-action", help="Approve a pending corporate_action")
    appr.add_argument("action_id", type=int)
    appr.add_argument("--notes", help="Optional reviewer notes")

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
    }
    handler = handlers.get(args.command)
    if handler is None:
        parser.error(f"unknown command: {args.command}")
        return 2

    try:
        return handler(args, config)
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
