"""CLI command handlers for Phase 1 fundamentals.

The handlers themselves live here so trip_ema/etl/cli.py stays a
thin dispatcher. trip_ema/etl/cli.py imports these and registers
them in the same subparser table as the existing ETL commands.

Convention is identical to the etl handlers: take (args, config),
return exit code (0 success, 1 partial, 2 fatal).
"""
from __future__ import annotations

import logging
import sys
from typing import Optional

from sqlalchemy import Engine, text

from ..config import Config
from ..etl.exchanges import UnsupportedExchangeError, validate_exchange
from ..persistence import transaction
from ..persistence.connection import create_engine_from_config
from ..persistence.fundamentals_repo import (
    FundamentalsRunRepository,
    IndustryOverrideRepository,
    InstrumentIndustryRepository,
    InstrumentSharesRepository,
)
from .runners import run_industry_refresh

logger = logging.getLogger(__name__)


# ---------- refresh-industry ---------- #


def cmd_refresh_industry(args, config: Config) -> int:
    validate_exchange(args.exchange, config)
    db_engine = create_engine_from_config(config)
    try:
        run_id = run_industry_refresh(
            db_engine,
            args.exchange,
            resume_run_id=args.resume,
            force=args.force,
        )
        # Read back the run row to print a summary.
        with db_engine.connect() as conn:
            r = FundamentalsRunRepository(conn).get(run_id)
        if r is None:
            print(f"run_id={run_id} (no row found?)", file=sys.stderr)
            return 1
        print(
            f"run_id={r.run_id} status={r.status} "
            f"succeeded={r.succeeded} failed={r.failed} skipped={r.skipped}"
        )
        if r.status == "SUCCESS":
            return 0
        if r.status == "PARTIAL":
            return 1
        return 2
    finally:
        db_engine.dispose()


# ---------- set-industry-override ---------- #


def cmd_set_industry_override(args, config: Config) -> int:
    validate_exchange(args.exchange, config)
    db_engine = create_engine_from_config(config)
    try:
        # Look up the ticker_id.
        with db_engine.connect() as conn:
            row = conn.execute(text("""
                SELECT ticker_id FROM tickers
                WHERE exchange = :ex AND symbol = :sym
                  AND is_active = TRUE
            """), {"ex": args.exchange, "sym": args.symbol}).fetchone()
        if row is None:
            print(
                f"error: ticker {args.exchange}:{args.symbol} not found or inactive",
                file=sys.stderr,
            )
            return 2
        ticker_id = int(row[0])

        # Sector is optional (the user may know industry but not sector).
        # If not provided, default to a sensible placeholder.
        sector = args.sector or "Unclassified"

        with transaction(db_engine) as conn:
            IndustryOverrideRepository(conn).upsert(
                ticker_id,
                sector=sector,
                industry=args.industry,
                note=args.note,
                set_by=args.set_by,
            )
        print(
            f"override set: ticker_id={ticker_id} "
            f"{args.exchange}:{args.symbol} → "
            f"sector={sector!r} industry={args.industry!r}"
        )
        return 0
    finally:
        db_engine.dispose()


# ---------- delete-industry-override ---------- #


def cmd_delete_industry_override(args, config: Config) -> int:
    validate_exchange(args.exchange, config)
    db_engine = create_engine_from_config(config)
    try:
        with db_engine.connect() as conn:
            row = conn.execute(text("""
                SELECT ticker_id FROM tickers
                WHERE exchange = :ex AND symbol = :sym
            """), {"ex": args.exchange, "sym": args.symbol}).fetchone()
        if row is None:
            print(
                f"error: ticker {args.exchange}:{args.symbol} not found",
                file=sys.stderr,
            )
            return 2
        ticker_id = int(row[0])
        with transaction(db_engine) as conn:
            deleted = IndustryOverrideRepository(conn).delete(ticker_id)
        if deleted:
            print(f"override removed for ticker_id={ticker_id}")
            return 0
        print(
            f"no override existed for {args.exchange}:{args.symbol}",
            file=sys.stderr,
        )
        return 1
    finally:
        db_engine.dispose()


# ---------- fundamentals-status ---------- #


def cmd_fundamentals_status(args, config: Config) -> int:
    db_engine = create_engine_from_config(config)
    try:
        # Coverage section: classified counts per (exchange, instrument_type).
        # EQ and ETF have different shapes — equities aim for ~85%+ via
        # yfinance; ETFs are 100% deterministically bucketed once a
        # refresh has run.
        with db_engine.connect() as conn:
            counts = InstrumentIndustryRepository(conn).coverage_counts_by_type()
        if counts:
            print("Industry classification coverage (by instrument type):")
            print(
                f"  {'EXCHANGE':<10} {'TYPE':<6} {'TOTAL':>8} "
                f"{'CLASSIFIED':>12} {'%':>6}"
            )
            for ex, itype, total, classified in counts:
                pct = (classified * 100.0 / total) if total else 0.0
                print(
                    f"  {ex:<10} {itype:<6} {total:>8} "
                    f"{classified:>12} {pct:>5.1f}%"
                )
        else:
            print("No active tickers / no classification data yet.")

        # Shares-outstanding coverage section (Phase 2). Same shape
        # as industry coverage. with_shares counts DISTINCT tickers
        # that have at least one non-NULL shares_outstanding row;
        # the time-series shape means one ticker can have many rows.
        with db_engine.connect() as conn:
            shares_counts = InstrumentSharesRepository(conn).coverage_counts_by_type()
        if any(w > 0 for _, _, _, w in shares_counts):
            print()
            print("Shares-outstanding coverage (by instrument type):")
            print(
                f"  {'EXCHANGE':<10} {'TYPE':<6} {'TOTAL':>8} "
                f"{'WITH_SHARES':>12} {'%':>6}"
            )
            for ex, itype, total, with_shares in shares_counts:
                pct = (with_shares * 100.0 / total) if total else 0.0
                print(
                    f"  {ex:<10} {itype:<6} {total:>8} "
                    f"{with_shares:>12} {pct:>5.1f}%"
                )

        # Financials coverage section (Phase 3). Same shape as shares
        # but with TWO data columns (revenue, ebitda) because the
        # important quality signal is the revenue/EBITDA gap — yfinance
        # often has revenue but not EBITDA for Indian mid/small caps.
        from ..persistence.fundamentals_repo import QuarterlyFinancialsRepository
        with db_engine.connect() as conn:
            fin_counts = QuarterlyFinancialsRepository(conn).coverage_counts_by_type()
        if any(w_r > 0 or w_e > 0 for _, _, _, w_r, w_e in fin_counts):
            print()
            print("Quarterly financials coverage (by instrument type):")
            print(
                f"  {'EXCHANGE':<10} {'TYPE':<6} {'TOTAL':>8} "
                f"{'W/REVENUE':>10} {'W/EBITDA':>10}"
            )
            for ex, itype, total, with_rev, with_ebt in fin_counts:
                print(
                    f"  {ex:<10} {itype:<6} {total:>8} "
                    f"{with_rev:>10} {with_ebt:>10}"
                )

        # Daily market cap coverage section (Phase 4A). Per-date
        # breakdown of COMPUTED vs YFINANCE_HIST_MCAP rows. We
        # show only the most recent ~10 dates to keep the output
        # readable; the operator can query the table directly for
        # full history.
        from ..persistence.fundamentals_repo import DailyMarketCapRepository
        with db_engine.connect() as conn:
            mc_counts = DailyMarketCapRepository(conn).coverage_counts_by_date()
        if mc_counts:
            print()
            print("Daily market cap coverage (most recent 10 dates):")
            print(
                f"  {'DATE':<12} {'TOTAL':>8} "
                f"{'COMPUTED':>10} {'HIST_MCAP':>10}"
            )
            for td, total, computed, hist in mc_counts[:10]:
                print(
                    f"  {td!s:<12} {total:>8} "
                    f"{computed:>10} {hist:>10}"
                )

        # Recent runs section.
        with db_engine.connect() as conn:
            runs = FundamentalsRunRepository(conn).list_recent(
                phase=args.phase, exchange=args.exchange, limit=args.limit,
            )
        if runs:
            print()
            print(f"Recent fundamentals_run rows (most recent first, limit {args.limit}):")
            print(
                f"  {'RUN_ID':>7} {'PHASE':<10} {'EX':<5} {'STATUS':<8} "
                f"{'SUCC':>6} {'FAIL':>5} {'SKIP':>5} {'STARTED':<20}"
            )
            for r in runs:
                ex = r.exchange or "-"
                started = r.started_at.strftime("%Y-%m-%d %H:%M:%S")
                print(
                    f"  {r.run_id:>7} {r.phase:<10} {ex:<5} {r.status:<8} "
                    f"{r.succeeded:>6} {r.failed:>5} {r.skipped:>5} {started:<20}"
                )
        else:
            print()
            print("No fundamentals_run rows match the filter.")
        return 0
    finally:
        db_engine.dispose()


# ---------- refresh-shares (Phase 2) ---------- #


def cmd_refresh_shares(args, config: Config) -> int:
    """Yfinance shares-outstanding refresh for an exchange.

    Mirrors cmd_refresh_industry — same exit-code conventions, same
    --resume / --force semantics, same async-via-fundamentals_run
    audit trail. The runner serializes calls (one HTTP per ticker)
    so the wall-clock cost is comparable to refresh-industry.

    NOTE: ETFs are included. Their sharesOutstanding from yfinance
    is the count of ETF units issued, not equity shares — meaningful
    for AUM analytics, harmless if you ignore it.
    """
    from .runners import run_shares_refresh
    validate_exchange(args.exchange, config)
    db_engine = create_engine_from_config(config)
    try:
        run_id = run_shares_refresh(
            db_engine,
            args.exchange,
            resume_run_id=args.resume,
            force=args.force,
        )
        with db_engine.connect() as conn:
            r = FundamentalsRunRepository(conn).get(run_id)
        if r is None:
            print(f"run_id={run_id} (no row found?)", file=sys.stderr)
            return 1
        print(
            f"run_id={r.run_id} status={r.status} "
            f"succeeded={r.succeeded} failed={r.failed}"
        )
        if r.status == "SUCCESS":
            return 0
        if r.status == "PARTIAL":
            return 1
        return 2
    finally:
        db_engine.dispose()


# ---------- refresh-financials (Phase 3.A) ---------- #


def cmd_refresh_financials(args, config: Config) -> int:
    """Yfinance quarterly-financials refresh for an exchange.

    Mirrors cmd_refresh_industry / cmd_refresh_shares — same exit-
    code conventions, same --resume / --force semantics, same
    fundamentals_run audit trail. Per-ticker yfinance HTTP cost is
    the same as the others (~2s per ticker × 2300 tickers ≈ 60 min).
    """
    from .runners import run_financials_refresh
    validate_exchange(args.exchange, config)
    db_engine = create_engine_from_config(config)
    try:
        run_id = run_financials_refresh(
            db_engine,
            args.exchange,
            resume_run_id=args.resume,
            force=args.force,
        )
        with db_engine.connect() as conn:
            r = FundamentalsRunRepository(conn).get(run_id)
        if r is None:
            print(f"run_id={run_id} (no row found?)", file=sys.stderr)
            return 1
        print(
            f"run_id={r.run_id} status={r.status} "
            f"succeeded={r.succeeded} failed={r.failed}"
        )
        if r.status == "SUCCESS":
            return 0
        if r.status == "PARTIAL":
            return 1
        return 2
    finally:
        db_engine.dispose()


# ---------- compute-market-cap (Phase 4A) ---------- #


def cmd_compute_market_cap(args, config: Config) -> int:
    """Two modes:
      - Single-day compute (no --backfill-from): uses local
        ohlcv_bars × instrument_shares JOIN to compute COMPUTED /
        APPROX_TODAYS_SHARES rows for one trade_date. Cheap,
        no yfinance HTTP.
      - Backfill (--backfill-from given): walks the date range
        via run_market_cap_backfill, hitting yfinance once per
        ticker. ~60-90 min for 2300 NSE tickers depending on
        rate-limit behavior.
    """
    from datetime import date as _date
    from .market_cap_compute import compute_market_cap_for_date
    from .runners import run_market_cap_backfill

    validate_exchange(args.exchange, config)
    db_engine = create_engine_from_config(config)
    try:
        if args.backfill_from is not None:
            # Backfill mode.
            from_date = _date.fromisoformat(args.backfill_from)
            to_date = _date.fromisoformat(args.backfill_to) if args.backfill_to else _date.today()
            run_id = run_market_cap_backfill(
                db_engine, args.exchange,
                from_date=from_date,
                to_date=to_date,
                resume_run_id=args.resume,
                force=args.force,
            )
            with db_engine.connect() as conn:
                r = FundamentalsRunRepository(conn).get(run_id)
            if r is None:
                print(f"run_id={run_id} (no row found?)", file=sys.stderr)
                return 1
            print(
                f"run_id={r.run_id} status={r.status} "
                f"succeeded={r.succeeded} failed={r.failed}"
            )
            if r.status == "SUCCESS":
                return 0
            if r.status == "PARTIAL":
                return 1
            return 2

        # Single-day mode.
        trade_date = _date.fromisoformat(args.date) if args.date else _date.today()
        result = compute_market_cap_for_date(
            db_engine, args.exchange, trade_date,
        )
        print(
            f"date={result.trade_date} exchange={result.exchange} "
            f"computed={result.succeeded_computed} "
            f"approx={result.succeeded_approx} "
            f"skipped_no_close={result.skipped_no_close} "
            f"skipped_no_shares={result.skipped_no_shares}"
        )
        # Exit code policy: 0 if at least one row was written;
        # 1 if nothing was written (most likely cause: bars
        # haven't been ingested for the date yet).
        total_written = result.succeeded_computed + result.succeeded_approx
        return 0 if total_written > 0 else 1
    finally:
        db_engine.dispose()


# ---------- backfill-etf-classification ---------- #


def cmd_backfill_etf_classification(args, config: Config) -> int:
    """Re-classify ETFs against the ETF-specific path.

    Use case: after a normal `refresh-industry` was run before the
    ETF branch existed, the ~300 NSE ETFs were left with NULL or
    yfinance-junk classifications. Re-running `refresh-industry`
    works, but it also re-fetches all 2300+ equities (~90 minutes).
    This command does ONLY the ETF subset (~10 minutes).

    Selection criteria: active tickers where instrument_type='ETF'
    AND (no instrument_industry row exists OR
         classification_source != 'INSTRUMENT_TYPE' AND not overridden).

    Implementation reuses the existing _classify_etf() helper from
    the runner — same code path the main refresh uses — so behavior
    is identical.
    """
    from ..etl.exchanges import validate_exchange
    from .runners import _classify_etf

    validate_exchange(args.exchange, config)
    db_engine = create_engine_from_config(config)
    try:
        # Load the ETF candidates.
        with db_engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT t.ticker_id, t.symbol
                FROM tickers t
                LEFT JOIN instrument_industry ii ON ii.ticker_id = t.ticker_id
                LEFT JOIN industry_override io   ON io.ticker_id = t.ticker_id
                WHERE t.exchange = :ex
                  AND t.is_active = TRUE
                  AND t.delisted_at IS NULL
                  AND t.instrument_type = 'ETF'
                  AND io.ticker_id IS NULL
                  AND (
                      ii.ticker_id IS NULL
                      OR ii.classification_source != 'INSTRUMENT_TYPE'
                  )
                ORDER BY t.symbol
            """), {"ex": args.exchange}).fetchall()
        candidates = [{"id": int(r[0]), "symbol": r[1]} for r in rows]
        total = len(candidates)
        if total == 0:
            print(f"No ETFs need backfill on {args.exchange}. Nothing to do.")
            return 0
        print(
            f"Backfilling ETF classification for {total} tickers on "
            f"{args.exchange}. Estimated time: ~{total * 2}s."
        )

        # Open a fundamentals_run for audit trail.
        with transaction(db_engine) as conn:
            run_id = FundamentalsRunRepository(conn).open(
                phase="industry", exchange=args.exchange,
                notes=f"ETF backfill: {total} tickers",
            )

        succeeded = 0
        failed = 0
        try:
            for idx, t in enumerate(candidates):
                outcome = _classify_etf(
                    db_engine, t["id"], t["symbol"], args.exchange,
                    fetch_fn=None,  # _classify_etf doesn't use it
                )
                if outcome == "succeeded":
                    succeeded += 1
                else:
                    failed += 1
                if (idx + 1) % 25 == 0:
                    print(
                        f"  {idx + 1}/{total} processed "
                        f"(s={succeeded} f={failed})"
                    )
                    with transaction(db_engine) as conn:
                        FundamentalsRunRepository(conn).update_progress(
                            run_id,
                            succeeded=succeeded, failed=failed, skipped=0,
                        )
        except KeyboardInterrupt:
            with transaction(db_engine) as conn:
                FundamentalsRunRepository(conn).close(
                    run_id, status="PARTIAL",
                    succeeded=succeeded, failed=failed, skipped=0,
                    notes="Interrupted",
                )
            print(f"\nInterrupted. Closed run {run_id} as PARTIAL.")
            return 1

        status_value = "SUCCESS" if failed == 0 else (
            "PARTIAL" if succeeded > 0 else "FAILED"
        )
        with transaction(db_engine) as conn:
            FundamentalsRunRepository(conn).close(
                run_id, status=status_value,
                succeeded=succeeded, failed=failed, skipped=0,
            )
        print(
            f"\nETF backfill complete: run_id={run_id} "
            f"status={status_value} succeeded={succeeded} failed={failed}"
        )
        return 0 if status_value == "SUCCESS" else (
            1 if status_value == "PARTIAL" else 2
        )
    finally:
        db_engine.dispose()


# ---------- argparse registration helper ---------- #


def register_subparsers(sub, _add_exchange_arg) -> dict[str, callable]:
    """Add Phase 1 subcommands to the parent's subparsers. Returns
    the command→handler mapping for the dispatcher in cli.py.

    Called from trip_ema/etl/cli.py's `main()` so the user types
    `trip-ema-etl refresh-industry` exactly like the other ETL
    commands.
    """
    ri = sub.add_parser(
        "refresh-industry",
        help="Fetch sector/industry from yfinance for every active ticker",
    )
    _add_exchange_arg(ri)
    ri.add_argument(
        "--force", action="store_true",
        help="Re-run even if a successful refresh exists from the last 80 days",
    )
    ri.add_argument(
        "--resume", type=int, default=None, metavar="RUN_ID",
        help="Resume a previously-interrupted run from its checkpoint",
    )

    so = sub.add_parser(
        "set-industry-override",
        help="Manually set sector/industry for one ticker "
             "(beats yfinance auto-classification)",
    )
    _add_exchange_arg(so)
    so.add_argument("--symbol", required=True)
    so.add_argument("--industry", required=True)
    so.add_argument("--sector", default=None,
                    help="Optional. Defaults to 'Unclassified' if omitted.")
    so.add_argument("--note", default=None)
    so.add_argument("--set-by", default=None,
                    help="Operator name/identifier for audit trail.")

    do = sub.add_parser(
        "delete-industry-override",
        help="Remove an operator override (lets yfinance auto-classify again)",
    )
    _add_exchange_arg(do)
    do.add_argument("--symbol", required=True)

    fs = sub.add_parser(
        "fundamentals-status",
        help="Coverage report + recent fundamentals_run rows",
    )
    fs.add_argument("--exchange", default=None,
                    help="Filter recent runs by exchange (default: all)")
    fs.add_argument("--phase", default=None,
                    help="Filter recent runs by phase (default: all)")
    fs.add_argument("--limit", type=int, default=10)

    be = sub.add_parser(
        "backfill-etf-classification",
        help="Re-classify ETFs that were missed by earlier refresh-industry "
             "runs (e.g. runs before the ETF branch existed). Only touches "
             "tickers with instrument_type='ETF'; equities untouched.",
    )
    _add_exchange_arg(be)

    rs = sub.add_parser(
        "refresh-shares",
        help="Fetch shares-outstanding from yfinance for every active "
             "ticker. Time-series: each run inserts a new instrument_shares "
             "snapshot. Mirrors refresh-industry's semantics.",
    )
    _add_exchange_arg(rs)
    rs.add_argument(
        "--force", action="store_true",
        help="Re-run even if a successful refresh exists from the last 80 days",
    )
    rs.add_argument(
        "--resume", type=int, default=None, metavar="RUN_ID",
        help="Resume a previously-interrupted shares run from its checkpoint",
    )

    rf = sub.add_parser(
        "refresh-financials",
        help="Fetch quarterly revenue + EBITDA from yfinance for every "
             "active ticker. Time-series PK; up to 4 quarters per "
             "ticker per run. Phase 3.A stores yfinance's reported "
             "EBITDA, NULL when missing (no computation).",
    )
    _add_exchange_arg(rf)
    rf.add_argument(
        "--force", action="store_true",
        help="Re-run even if a successful refresh exists from the last 80 days",
    )
    rf.add_argument(
        "--resume", type=int, default=None, metavar="RUN_ID",
        help="Resume a previously-interrupted financials run from its checkpoint",
    )

    cmc = sub.add_parser(
        "compute-market-cap",
        help="Compute daily market cap for active EQ tickers. Single-"
             "day mode joins local ohlcv_bars × instrument_shares. "
             "Backfill mode (--backfill-from YYYY-MM-DD) walks a date "
             "range via yfinance historical mcap fetch.",
    )
    _add_exchange_arg(cmc)
    cmc.add_argument(
        "--date", default=None, metavar="YYYY-MM-DD",
        help="Single-day mode: trade_date to compute (default: today)",
    )
    cmc.add_argument(
        "--backfill-from", default=None, metavar="YYYY-MM-DD",
        help="Backfill mode start date. If given, walks dates via "
             "yfinance instead of single-day local compute",
    )
    cmc.add_argument(
        "--backfill-to", default=None, metavar="YYYY-MM-DD",
        help="Backfill mode end date. Default: today",
    )
    cmc.add_argument(
        "--force", action="store_true",
        help="Re-run backfill even if a successful one exists from the last 80 days",
    )
    cmc.add_argument(
        "--resume", type=int, default=None, metavar="RUN_ID",
        help="Resume an interrupted backfill",
    )

    return {
        "refresh-industry":             cmd_refresh_industry,
        "set-industry-override":        cmd_set_industry_override,
        "delete-industry-override":     cmd_delete_industry_override,
        "fundamentals-status":          cmd_fundamentals_status,
        "backfill-etf-classification":  cmd_backfill_etf_classification,
        "refresh-shares":               cmd_refresh_shares,
        "refresh-financials":           cmd_refresh_financials,
        "compute-market-cap":           cmd_compute_market_cap,
    }
