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

        # Industry indices section (Phase 4B-3). Shows per-index
        # coverage: name, sector, status, base date, latest value
        # date, value row count. Only printed if any indices exist.
        from ..persistence.fundamentals_repo import (
            IndicatorStateRepository,
            IndustryIndexRepository,
            IndustryIndexValueRepository,
        )
        with db_engine.connect() as conn:
            indices = IndustryIndexRepository(conn).list_all()
            value_summary = IndustryIndexValueRepository(conn).coverage_summary()
            indicator_summary = IndicatorStateRepository(
                conn,
            ).coverage_summary_for_industries()
        if indices:
            # Build name → (rows, earliest, latest) from the summary
            # so we can join in-memory.
            value_by_name = {
                name: (rows, earliest, latest)
                for name, rows, earliest, latest in value_summary
            }
            print()
            print(f"Industry indices ({len(indices)} total):")
            print(
                f"  {'INDUSTRY':<42} {'STATUS':<8} "
                f"{'BASE DATE':<12} {'LATEST':<12} {'ROWS':>5}"
            )
            for idx in indices:
                rows, earliest, latest = value_by_name.get(
                    idx.industry_name, (0, None, None)
                )
                latest_s = str(latest) if latest else "(none)"
                print(
                    f"  {idx.industry_name[:42]:<42} {idx.status:<8} "
                    f"{idx.base_date!s:<12} {latest_s:<12} {rows:>5}"
                )

        # Indicator state section (Phase 5-4). Per-industry row
        # count of indicator_state, plus latest trade date.
        if indicator_summary:
            ind_by_name = {
                name: (rows, earliest, latest)
                for name, rows, earliest, latest in indicator_summary
            }
            print()
            print(
                f"Indicator state coverage ({len(indicator_summary)} "
                f"industries):"
            )
            print(
                f"  {'INDUSTRY':<48} {'LATEST':<12} {'ROWS':>5}"
            )
            for name in sorted(ind_by_name):
                rows, _earliest, latest = ind_by_name[name]
                latest_s = str(latest) if latest else "(none)"
                print(
                    f"  {name[:48]:<48} {latest_s:<12} {rows:>5}"
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


# ---------- build-industry-indices (Phase 4B-3) ---------- #


def cmd_build_industry_indices(args, config: Config) -> int:
    """Auto-discover qualifying industries and create indices for
    those with ≥ min_constituents active EQ tickers.

    Idempotent: indices that already exist with constituents are
    left untouched. Newly-qualifying industries get an initial
    rebalance at the next semiannual trading day (April 1 or
    October 1) at or after the earliest mcap date.

    Reports a tabular summary of qualified / unqualified / indexed /
    failed industries.
    """
    from .runners import run_industry_index_build

    validate_exchange(args.exchange, config)
    db_engine = create_engine_from_config(config)
    try:
        report = run_industry_index_build(
            db_engine, args.exchange,
            min_constituents=args.min_constituents,
            n=args.n,
            base_value=args.base_value,
        )
    finally:
        db_engine.dispose()

    # Print a tabular summary. The output is intentionally
    # operator-readable rather than machine-parseable — automation
    # should use the HTTP endpoint instead.
    print(
        f"build-industry-indices on {args.exchange} "
        f"(min_constituents={args.min_constituents}, n={args.n}):"
    )
    print()
    print(f"  Qualified industries (≥{args.min_constituents} EQ tickers): {len(report['qualified'])}")
    print(f"  Unqualified industries: {len(report['unqualified'])}")
    print(f"  Newly indexed this run: {len(report['indexed'])}")
    print(f"  Failed: {len(report['failed'])}")

    if report["indexed"]:
        print()
        print("Newly indexed industries:")
        print(f"  {'INDUSTRY':<48} {'BASE DATE':<12} {'PICKED':>6}")
        for industry, base_date, picked in report["indexed"]:
            print(f"  {industry[:48]:<48} {base_date!s:<12} {picked:>6}")

    if report["failed"]:
        print()
        print("Failed (most common reason: below min_constituents on rebalance date):")
        for industry, reason in report["failed"]:
            print(f"  {industry[:48]:<48} {reason[:60]}")

    # Exit code policy:
    #   0 — all qualified industries indexed successfully OR no
    #       industries qualified (clean either way).
    #   1 — some qualified industries failed to index.
    if report["failed"]:
        return 1
    return 0


# ---------- compute-index-values (Phase 4B-3) ---------- #


def cmd_compute_index_values(args, config: Config) -> int:
    """Backfill daily index values for one industry or all active
    industries.

    Two modes:
      - Single-industry (--industry NAME): backfill that index
        across the requested date range.
      - All-active (--all-active): backfill every active index.
        Uses each index's own base_date as the start unless
        --from is given.

    Date range defaults: from = max(index.base_date, --from);
    to = --to or today.
    """
    from datetime import date as _date
    from .runners import run_index_value_backfill
    from ..persistence.fundamentals_repo import IndustryIndexRepository

    if not args.industry and not args.all_active:
        print(
            "error: must pass either --industry NAME or --all-active",
            file=sys.stderr,
        )
        return 2
    if args.industry and args.all_active:
        print(
            "error: --industry and --all-active are mutually exclusive",
            file=sys.stderr,
        )
        return 2

    cli_from = _date.fromisoformat(args.from_) if args.from_ else None
    cli_to = _date.fromisoformat(args.to) if args.to else _date.today()

    db_engine = create_engine_from_config(config)
    try:
        # Resolve the list of indices to process.
        with db_engine.connect() as conn:
            ir = IndustryIndexRepository(conn)
            if args.industry:
                idx = ir.get(args.industry)
                if idx is None:
                    print(
                        f"error: industry {args.industry!r} is not indexed. "
                        f"Run `build-industry-indices` first.",
                        file=sys.stderr,
                    )
                    return 2
                indices = [idx]
            else:
                indices = ir.list_active()
                if not indices:
                    print(
                        "No active industry indices exist. Run "
                        "`build-industry-indices` first.",
                        file=sys.stderr,
                    )
                    return 1

        total_indices = len(indices)
        # Counters across the loop. Resume is per-call (single
        # --resume RUN_ID makes sense only with --industry).
        successes = 0
        partials = 0
        failures = 0

        for i, idx in enumerate(indices, start=1):
            # from_date is the later of CLI --from and the index's
            # own base_date. We can't compute values before
            # base_date anyway.
            from_date = (
                max(cli_from, idx.base_date) if cli_from else idx.base_date
            )
            if from_date > cli_to:
                print(
                    f"  [{i}/{total_indices}] {idx.industry_name}: "
                    f"from_date {from_date} > to_date {cli_to}; skipping",
                )
                continue
            try:
                run_id = run_index_value_backfill(
                    db_engine, idx.industry_name,
                    from_date=from_date,
                    to_date=cli_to,
                    resume_run_id=args.resume,
                    force=args.force,
                )
                with db_engine.connect() as conn:
                    r = FundamentalsRunRepository(conn).get(run_id)
                status = r.status if r else "?"
                print(
                    f"  [{i}/{total_indices}] {idx.industry_name}: "
                    f"run_id={run_id} status={status} "
                    f"s={r.succeeded if r else '?'} "
                    f"sk={r.skipped if r else '?'} "
                    f"f={r.failed if r else '?'}"
                )
                if status == "SUCCESS":
                    successes += 1
                elif status == "PARTIAL":
                    partials += 1
                else:
                    failures += 1
            except Exception as e:
                logger.exception(
                    "compute-index-values for %s failed: %s",
                    idx.industry_name, e,
                )
                print(
                    f"  [{i}/{total_indices}] {idx.industry_name}: "
                    f"FAILED ({e})",
                )
                failures += 1

        # Summary line.
        print()
        print(
            f"compute-index-values done: "
            f"{successes} SUCCESS, {partials} PARTIAL, {failures} FAILED "
            f"(of {total_indices} indices)"
        )
        if failures > 0:
            return 2
        if partials > 0:
            return 1
        return 0
    finally:
        db_engine.dispose()


# ---------- rebalance-industry-index (Phase 4B-3) ---------- #


def cmd_rebalance_industry_index(args, config: Config) -> int:
    """Manually run a rebalance for one industry on a specific date.

    Normal lifecycle: auto-rebalance happens at every semiannual
    target via the build-industry-indices flow. This command is for
    operator intervention — e.g. recovering from a partial rebalance,
    or re-running a rebalance with different inputs (after operator
    overrides change the universe).
    """
    from datetime import date as _date
    from .runners import run_rebalance

    rebalance_date = _date.fromisoformat(args.date)
    db_engine = create_engine_from_config(config)
    try:
        result = run_rebalance(
            db_engine, args.industry, rebalance_date,
            n=args.n,
            exchange=args.exchange,
            min_constituents=args.min_constituents,
        )
    except ValueError as e:
        # Most likely: industry doesn't exist as an index. We give
        # a friendly hint rather than a stack trace.
        print(f"error: {e}", file=sys.stderr)
        return 2
    finally:
        db_engine.dispose()

    if result.rebalance_skipped:
        print(
            f"rebalance SKIPPED for {result.industry_name} on "
            f"{result.rebalance_date}: {result.skip_reason}"
        )
        return 1

    print(
        f"rebalance done for {result.industry_name} on "
        f"{result.rebalance_date}: picked={result.constituents_picked} "
        f"closed_prior={result.constituents_closed}"
    )
    return 0


# ---------- compute-indicator-state (Phase 5-4) ---------- #


def cmd_compute_indicator_state(args, config: Config) -> int:
    """Compute indicator state for one industry or all industries.

    The compute is whole-history per industry (regime state machines
    are sequential through time, so partial backfills aren't
    meaningful). For each industry:

      1. Read full industry_index_value history.
      2. Run the indicator pipeline (EMAs, ATR, bands, regimes).
      3. Upsert all indicator-ready rows into indicator_state.

    Either --industry NAME or --all-industries is required.
    """
    from .runners import (
        run_indicator_state_for_all_industries,
        run_indicator_state_for_industry,
    )

    if not args.industry and not args.all_industries:
        print(
            "error: must pass either --industry NAME or --all-industries",
            file=sys.stderr,
        )
        return 2
    if args.industry and args.all_industries:
        print(
            "error: --industry and --all-industries are mutually exclusive",
            file=sys.stderr,
        )
        return 2

    db_engine = create_engine_from_config(config)
    try:
        if args.industry:
            # Single-industry path.
            try:
                run_id = run_indicator_state_for_industry(
                    db_engine, args.industry, force=args.force,
                )
            except ValueError as e:
                print(f"error: {e}", file=sys.stderr)
                return 2
            with db_engine.connect() as conn:
                r = FundamentalsRunRepository(conn).get(run_id)
            if r is None:
                print(f"run_id={run_id} (no row found?)", file=sys.stderr)
                return 1
            print(
                f"compute-indicator-state {args.industry}: "
                f"run_id={r.run_id} status={r.status} "
                f"succeeded={r.succeeded} failed={r.failed} "
                f"skipped={r.skipped}"
            )
            if r.status == "SUCCESS":
                return 0
            if r.status == "PARTIAL":
                return 1
            return 2

        # All-industries path.
        report = run_indicator_state_for_all_industries(
            db_engine, force=args.force,
        )
        print(
            f"compute-indicator-state --all-industries:"
        )
        print(
            f"  succeeded: {len(report['succeeded'])}, "
            f"skipped: {len(report['skipped'])}, "
            f"failed: {len(report['failed'])}"
        )

        if report["succeeded"]:
            print()
            print("Succeeded:")
            print(f"  {'INDUSTRY':<48} {'WRITTEN':>8} {'SKIPPED':>8}")
            for name, written, skipped in report["succeeded"]:
                print(f"  {name[:48]:<48} {written:>8} {skipped:>8}")

        if report["skipped"]:
            print()
            print("Skipped (no value rows; run compute-index-values first):")
            for name, reason in report["skipped"]:
                print(f"  {name[:48]:<48} {reason}")

        if report["failed"]:
            print()
            print("Failed:")
            for name, err in report["failed"]:
                print(f"  {name[:48]:<48} {err[:60]}")

        # Exit code:
        #   0 — clean run (or every industry was already up-to-date)
        #   1 — some industries succeeded, some failed
        #   2 — fatal (no industries succeeded but some failed)
        if report["failed"]:
            if report["succeeded"]:
                return 1
            return 2
        return 0
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

    # ---------- Phase 4B-3 commands ---------- #

    bii = sub.add_parser(
        "build-industry-indices",
        help="Auto-discover qualifying yfinance industries on an "
             "exchange and create modified-Laspeyres indices for those "
             "with ≥ min_constituents active EQ tickers. Idempotent.",
    )
    _add_exchange_arg(bii)
    bii.add_argument(
        "--min-constituents", type=int, default=10,
        metavar="N",
        help="Minimum tickers required to index an industry "
             "(default: 10). Industries below this threshold are "
             "reported as unqualified.",
    )
    bii.add_argument(
        "--n", type=int, default=10, metavar="N",
        help="Number of constituents to pick per index (default: 10). "
             "Typically equals --min-constituents.",
    )
    bii.add_argument(
        "--base-value", type=float, default=1000.0, metavar="V",
        help="Base index value at first rebalance (default: 1000.0, "
             "NIFTY convention).",
    )

    civ = sub.add_parser(
        "compute-index-values",
        help="Backfill daily index values for one industry or all "
             "active industries. Walks trading days from "
             "daily_market_cap, computes modified-Laspeyres value "
             "per day. Pre-base dates are skipped silently.",
    )
    civ.add_argument(
        "--industry", default=None, metavar="NAME",
        help="Industry name to backfill (e.g. \"Information "
             "Technology Services\"). Mutually exclusive with "
             "--all-active.",
    )
    civ.add_argument(
        "--all-active", action="store_true", dest="all_active",
        help="Backfill every active industry index. Mutually "
             "exclusive with --industry.",
    )
    civ.add_argument(
        "--from", default=None, metavar="YYYY-MM-DD",
        dest="from_",  # 'from' is a Python keyword
        help="Start date. Default: each index's own base_date.",
    )
    civ.add_argument(
        "--to", default=None, metavar="YYYY-MM-DD",
        help="End date. Default: today.",
    )
    civ.add_argument(
        "--force", action="store_true",
        help="Re-run even if recent values exist (within 80 days).",
    )
    civ.add_argument(
        "--resume", type=int, default=None, metavar="RUN_ID",
        help="Resume an interrupted backfill (single-industry only).",
    )

    rii = sub.add_parser(
        "rebalance-industry-index",
        help="Manually run a rebalance for one industry on a specific "
             "date. For operator intervention; normal auto-rebalances "
             "happen via build-industry-indices.",
    )
    rii.add_argument(
        "--industry", required=True, metavar="NAME",
        help="Industry name to rebalance.",
    )
    rii.add_argument(
        "--date", required=True, metavar="YYYY-MM-DD",
        help="Rebalance date.",
    )
    rii.add_argument(
        "--n", type=int, default=10, metavar="N",
        help="Number of constituents to pick (default: 10).",
    )
    rii.add_argument(
        "--min-constituents", type=int, default=None, metavar="N",
        help="Override the index's min_constituents threshold for "
             "this run (default: use the index's own setting).",
    )
    rii.add_argument(
        "--exchange", default=None, metavar="NSE|BSE",
        help="Restrict picking to one exchange (default: no restriction).",
    )

    # ---------- Phase 5-4 command ---------- #

    cis = sub.add_parser(
        "compute-indicator-state",
        help="Compute Triple-EMA indicator state (3 EMAs, ATR, "
             "bands, regimes) for one industry index or all "
             "industries. Reads from industry_index_value, writes "
             "to indicator_state. Whole-history per industry "
             "(regime state machines are sequential).",
    )
    cis.add_argument(
        "--industry", default=None, metavar="NAME",
        help="Industry name to compute (e.g. \"Information "
             "Technology Services\"). Mutually exclusive with "
             "--all-industries.",
    )
    cis.add_argument(
        "--all-industries", action="store_true", dest="all_industries",
        help="Compute indicator state for every active industry "
             "index that has value rows. Mutually exclusive with "
             "--industry.",
    )
    cis.add_argument(
        "--force", action="store_true",
        help="Re-compute even if recent state exists "
             "(within 80 days).",
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
        "build-industry-indices":       cmd_build_industry_indices,
        "compute-index-values":         cmd_compute_index_values,
        "rebalance-industry-index":     cmd_rebalance_industry_index,
        "compute-indicator-state":      cmd_compute_indicator_state,
    }
