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
            SignalEventRepository,
        )
        with db_engine.connect() as conn:
            indices = IndustryIndexRepository(conn).list_all()
            value_summary = IndustryIndexValueRepository(conn).coverage_summary()
            indicator_summary = IndicatorStateRepository(
                conn,
            ).coverage_summary_for_industries()
            signal_summary = SignalEventRepository(
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

        # Signal events section (Phase 6-1). Per-industry event
        # count and latest event date.
        if signal_summary:
            sig_by_name = {
                name: (rows, earliest, latest)
                for name, rows, earliest, latest in signal_summary
            }
            print()
            print(
                f"Signal events coverage ({len(signal_summary)} "
                f"industries):"
            )
            print(
                f"  {'INDUSTRY':<48} {'LATEST':<12} {'EVENTS':>6}"
            )
            for name in sorted(sig_by_name):
                rows, _earliest, latest = sig_by_name[name]
                latest_s = str(latest) if latest else "(none)"
                print(
                    f"  {name[:48]:<48} {latest_s:<12} {rows:>6}"
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


# ---------- ADDITION 1: cmd_backfill_shares ---------- #

def cmd_backfill_shares(args, config):
    """Yfinance historical-shares-outstanding backfill for an exchange.

    Differs from refresh-shares: instead of inserting ONE row per
    ticker with as_of_date=today, this fetches yfinance's full
    historical shares series via get_shares_full(start=...) and
    inserts MANY rows per ticker — one per quarterly filing date
    that yfinance reports.

    Once instrument_shares has historical rows, compute-market-cap's
    asof lookup (already date-aware) produces COMPUTED rows for the
    full backfill range — no more APPROX_TODAYS_SHARES gaps.

    Wall-clock cost: ~2s per ticker × 2600 tickers ≈ 90 min.
    Resumable: a Ctrl-C closes the run as PARTIAL with a checkpoint;
    re-run with --resume RUN_ID to pick up where you left off.
    """
    from .runners import run_shares_backfill
    from datetime import date as _date

    validate_exchange(args.exchange, config)  # noqa: F821
    db_engine = create_engine_from_config(config)  # noqa: F821
    try:
        start_date = _date.fromisoformat(args.start_date)
    except ValueError as e:
        print(
            f"Invalid --start-date {args.start_date!r}: {e}",
            file=sys.stderr,  # noqa: F821
        )
        return 2

    try:
        run_id = run_shares_backfill(
            db_engine,
            args.exchange,
            start_date=start_date,
            resume_run_id=args.resume,
            force=args.force,
        )
        with db_engine.connect() as conn:
            r = FundamentalsRunRepository(conn).get(run_id)  # noqa: F821
        if r is None:
            print(f"run_id={run_id} (no row found?)", file=sys.stderr)  # noqa: F821
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
    """Compute indicator state for one industry, all industries,
    one ticker, or all tickers for a given timeframe.

    Modes (mutually exclusive):
      --industry NAME              : one industry
      --all-industries             : every active industry with values
      --ticker SYMBOL --exchange X : one ticker (+ --timeframe required)
      --all-tickers --timeframe TF : all tickers on a timeframe
                                     (+ optional --exchange filter)

    Per-ticker compute reads from ohlcv_bars; per-industry reads
    from industry_index_value. Both write to indicator_state.
    """
    from .runners import (
        run_indicator_state_for_all_industries,
        run_indicator_state_for_all_tickers,
        run_indicator_state_for_industry,
        run_indicator_state_for_ticker,
    )
    from ..persistence.tickers_repo import TickersRepository
    from ..domain import TickerRef

    # Normalize args — older callers may not set the per-ticker attrs.
    args.industry = getattr(args, "industry", None)
    args.all_industries = getattr(args, "all_industries", False)
    args.ticker = getattr(args, "ticker", None)
    args.all_tickers = getattr(args, "all_tickers", False)
    args.exchange = getattr(args, "exchange", None)
    args.timeframe = getattr(args, "timeframe", None)
    args.force = getattr(args, "force", False)

    # Mode detection.
    modes_set = sum([
        bool(args.industry),
        bool(args.all_industries),
        bool(args.ticker),
        bool(args.all_tickers),
    ])
    if modes_set == 0:
        print(
            "error: must pass one of --industry NAME, --all-industries, "
            "--ticker SYMBOL, or --all-tickers",
            file=sys.stderr,
        )
        return 2
    if modes_set > 1:
        print(
            "error: --industry, --all-industries, --ticker, and "
            "--all-tickers are mutually exclusive",
            file=sys.stderr,
        )
        return 2

    db_engine = create_engine_from_config(config)
    try:
        # ---- per-industry single ----
        if args.industry:
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
            return 0 if r.status == "SUCCESS" else (
                1 if r.status == "PARTIAL" else 2
            )

        # ---- per-industry bulk ----
        if args.all_industries:
            report = run_indicator_state_for_all_industries(
                db_engine, force=args.force,
            )
            _print_indicator_industry_report(report)
            return _exit_code_from_report(report)

        # ---- per-ticker single ----
        if args.ticker:
            if not args.timeframe:
                print(
                    "error: --ticker requires --timeframe",
                    file=sys.stderr,
                )
                return 2
            # Resolve ticker_id from (exchange, symbol).
            if not args.exchange:
                print(
                    "error: --ticker requires --exchange",
                    file=sys.stderr,
                )
                return 2
            ref = TickerRef(args.exchange, args.ticker, "EQ")
            with db_engine.connect() as conn:
                ticker = TickersRepository(conn).get_by_ref(ref)
            if ticker is None:
                print(
                    f"error: ticker {args.exchange}:{args.ticker}:EQ "
                    f"not found",
                    file=sys.stderr,
                )
                return 2
            try:
                run_id = run_indicator_state_for_ticker(
                    db_engine, ticker.ticker_id, args.timeframe,
                    force=args.force,
                )
            except ValueError as e:
                print(f"error: {e}", file=sys.stderr)
                return 2
            with db_engine.connect() as conn:
                r = FundamentalsRunRepository(conn).get(run_id)
            print(
                f"compute-indicator-state ticker={args.ticker} "
                f"tf={args.timeframe}: run_id={r.run_id} "
                f"status={r.status} succeeded={r.succeeded} "
                f"skipped={r.skipped}"
            )
            return 0 if r.status == "SUCCESS" else 2

        # ---- per-ticker bulk ----
        if args.all_tickers:
            if not args.timeframe:
                print(
                    "error: --all-tickers requires --timeframe",
                    file=sys.stderr,
                )
                return 2
            try:
                report = run_indicator_state_for_all_tickers(
                    db_engine, args.timeframe,
                    exchange=args.exchange, force=args.force,
                )
            except ValueError as e:
                print(f"error: {e}", file=sys.stderr)
                return 2
            _print_indicator_ticker_report(report, args.timeframe)
            return _exit_code_from_report(report)
    finally:
        db_engine.dispose()


def _print_indicator_industry_report(report):
    print("compute-indicator-state --all-industries:")
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


def _print_indicator_ticker_report(report, timeframe):
    print(f"compute-indicator-state --all-tickers --timeframe {timeframe}:")
    print(
        f"  succeeded: {len(report['succeeded'])}, "
        f"skipped: {len(report['skipped'])}, "
        f"failed: {len(report['failed'])}"
    )
    if report["succeeded"]:
        # Only show top 10 + "...and N more" for brevity; bulk ticker
        # runs can have thousands of rows.
        print()
        print("Succeeded (top 10 by row count):")
        print(f"  {'SYMBOL':<16} {'WRITTEN':>8} {'SKIPPED':>8}")
        top = sorted(
            report["succeeded"], key=lambda x: -x[2],
        )[:10]
        for _, sym, written, skipped in top:
            print(f"  {sym[:16]:<16} {written:>8} {skipped:>8}")
        if len(report["succeeded"]) > 10:
            print(f"  ...and {len(report['succeeded']) - 10} more")
    if report["skipped"]:
        print()
        print(f"Skipped ({len(report['skipped'])} tickers):")
        # Show only first 5 to avoid overwhelming output.
        for _, sym, reason in report["skipped"][:5]:
            print(f"  {sym[:16]:<16} {reason[:60]}")
        if len(report["skipped"]) > 5:
            print(f"  ...and {len(report['skipped']) - 5} more")
    if report["failed"]:
        print()
        print("Failed:")
        for _, sym, err in report["failed"][:10]:
            print(f"  {sym[:16]:<16} {err[:60]}")
        if len(report["failed"]) > 10:
            print(f"  ...and {len(report['failed']) - 10} more")


def _exit_code_from_report(report) -> int:
    if report["failed"]:
        if report["succeeded"]:
            return 1
        return 2
    return 0


# ---------- compute-benchmark-stats (Phase 6-3) ---------- #


def cmd_compute_benchmark_stats(args, config) -> int:
    """Compute alpha/beta/RS regression statistics for stocks and
    industries vs their benchmarks (INDUSTRY index + NIFTY).

    Writes to stock_benchmark_stats and industry_benchmark_stats.
    Idempotent on (ticker/industry, benchmark, window, as_of_date)
    so --force is a no-op (kept for symmetry with sibling commands).

    Modes:
      --all-tickers      → 2,267 × up to 6 rows
      --all-industries   → 60 × 3 rows
      --ticker / --exchange → single ticker (6 rows)
      --industry NAME    → single industry (3 rows)
    """
    from datetime import date as _date
    from sqlalchemy import text as _text

    from .benchmark_stats_runners import (
        run_benchmark_stats_for_all_industries,
        run_benchmark_stats_for_all_tickers,
        run_benchmark_stats_for_industry,
        run_benchmark_stats_for_ticker,
    )
    from ..persistence.connection import create_engine_from_config

    as_of: _date | None = None
    if getattr(args, "as_of_date", None):
        as_of = _date.fromisoformat(args.as_of_date)

    engine = create_engine_from_config(config)

    # Industry-only mode
    if getattr(args, "all_industries", False):
        result = run_benchmark_stats_for_all_industries(
            engine, as_of_date=as_of,
        )
        print("compute-benchmark-stats --all-industries:")
        print(f"  succeeded: {len(result['succeeded'])}, "
              f"skipped: {len(result['skipped'])}, "
              f"failed: {len(result['failed'])}")
        for name, err in result["failed"][:10]:
            print(f"  FAILED  {name:50s} {err[:80]}")
        return 0 if not result["failed"] else 1

    # Ticker-only mode
    if getattr(args, "all_tickers", False):
        result = run_benchmark_stats_for_all_tickers(
            engine,
            exchange=getattr(args, "exchange", None),
            as_of_date=as_of,
        )
        print("compute-benchmark-stats --all-tickers:")
        print(f"  succeeded: {len(result['succeeded'])}, "
              f"skipped: {len(result['skipped'])}, "
              f"failed: {len(result['failed'])}")
        for tid, sym, err in result["failed"][:10]:
            print(f"  FAILED  ticker_id={tid} {sym:12s} {err[:80]}")
        return 0 if not result["failed"] else 1

    # Single-industry mode
    if getattr(args, "industry", None):
        n = run_benchmark_stats_for_industry(
            engine, args.industry, as_of_date=as_of,
        )
        print(f"compute-benchmark-stats {args.industry}: wrote {n} rows")
        return 0 if n > 0 else 1

    # Single-ticker mode
    if getattr(args, "ticker", None):
        if not getattr(args, "exchange", None):
            print("ERROR: --ticker requires --exchange")
            return 2
        with engine.connect() as conn:
            row = conn.execute(_text("""
                SELECT ticker_id FROM tickers
                WHERE exchange = :ex AND symbol = :sym
            """), {"ex": args.exchange, "sym": args.ticker}).first()
        if row is None:
            print(f"ERROR: {args.exchange}:{args.ticker} not found")
            return 2
        n = run_benchmark_stats_for_ticker(
            engine, row.ticker_id, as_of_date=as_of,
        )
        print(f"compute-benchmark-stats {args.exchange}:{args.ticker}: "
              f"wrote {n} rows")
        return 0 if n > 0 else 1

    print("ERROR: must pass one of "
          "--all-tickers / --all-industries / --ticker / --industry")
    return 2


# ---------- compute-signals (Phase 6-1) ---------- #


def cmd_compute_signals(args, config: Config) -> int:
    """Compute signal events for tickers.

    Industries are non-tradeable regime indicators and no longer emit
    signal events (their regimes still appear in indicator_state).

    Modes (mutually exclusive):
      --ticker SYMBOL --exchange X : one ticker (+ --timeframe required)
      --all-tickers --timeframe TF : all tickers on a timeframe
    """
    from .runners import (
        run_signal_events_for_all_tickers,
        run_signal_events_for_ticker,
    )
    from ..persistence.tickers_repo import TickersRepository
    from ..domain import TickerRef

    # Normalize args.
    args.ticker = getattr(args, "ticker", None)
    args.all_tickers = getattr(args, "all_tickers", False)
    args.exchange = getattr(args, "exchange", None)
    args.timeframe = getattr(args, "timeframe", None)
    args.force = getattr(args, "force", False)

    modes_set = sum([
        bool(args.ticker),
        bool(args.all_tickers),
    ])
    if modes_set == 0:
        print(
            "error: must pass one of --ticker or --all-tickers",
            file=sys.stderr,
        )
        return 2
    if modes_set > 1:
        print(
            "error: --ticker and --all-tickers are mutually exclusive",
            file=sys.stderr,
        )
        return 2

    db_engine = create_engine_from_config(config)
    try:
        if args.ticker:
            if not args.timeframe or not args.exchange:
                print(
                    "error: --ticker requires --exchange and --timeframe",
                    file=sys.stderr,
                )
                return 2
            ref = TickerRef(args.exchange, args.ticker, "EQ")
            with db_engine.connect() as conn:
                ticker = TickersRepository(conn).get_by_ref(ref)
            if ticker is None:
                print(
                    f"error: ticker {args.exchange}:{args.ticker}:EQ "
                    f"not found",
                    file=sys.stderr,
                )
                return 2
            try:
                run_id = run_signal_events_for_ticker(
                    db_engine, ticker.ticker_id, args.timeframe,
                    force=args.force,
                )
            except ValueError as e:
                print(f"error: {e}", file=sys.stderr)
                return 2
            with db_engine.connect() as conn:
                r = FundamentalsRunRepository(conn).get(run_id)
            print(
                f"compute-signals ticker={args.ticker} "
                f"tf={args.timeframe}: run_id={r.run_id} "
                f"status={r.status} events_written={r.succeeded} "
                f"skipped={r.skipped}"
            )
            return 0 if r.status == "SUCCESS" else 2

        if args.all_tickers:
            if not args.timeframe:
                print(
                    "error: --all-tickers requires --timeframe",
                    file=sys.stderr,
                )
                return 2
            try:
                report = run_signal_events_for_all_tickers(
                    db_engine, args.timeframe,
                    exchange=args.exchange, force=args.force,
                )
            except ValueError as e:
                print(f"error: {e}", file=sys.stderr)
                return 2
            print(
                f"compute-signals --all-tickers --timeframe "
                f"{args.timeframe}:"
            )
            print(
                f"  succeeded: {len(report['succeeded'])}, "
                f"skipped: {len(report['skipped'])}, "
                f"failed: {len(report['failed'])}"
            )
            if report["succeeded"]:
                print()
                print("Succeeded (top 10 by event count):")
                print(f"  {'SYMBOL':<16} {'EVENTS':>8}")
                top = sorted(
                    report["succeeded"], key=lambda x: -x[2],
                )[:10]
                for _, sym, count in top:
                    print(f"  {sym[:16]:<16} {count:>8}")
                if len(report["succeeded"]) > 10:
                    print(f"  ...and {len(report['succeeded']) - 10} more")
            if report["skipped"]:
                print()
                print(f"Skipped ({len(report['skipped'])} tickers):")
                for _, sym, reason in report["skipped"][:5]:
                    print(f"  {sym[:16]:<16} {reason[:60]}")
                if len(report["skipped"]) > 5:
                    print(f"  ...and {len(report['skipped']) - 5} more")
            if report["failed"]:
                print()
                print("Failed:")
                for _, sym, err in report["failed"][:10]:
                    print(f"  {sym[:16]:<16} {err[:60]}")
                if len(report["failed"]) > 10:
                    print(f"  ...and {len(report['failed']) - 10} more")
            return _exit_code_from_report(report)
    finally:
        db_engine.dispose()


# ---------- run-eod-pipeline (Phase 4C) ---------- #


def cmd_run_eod_pipeline(args, config: Config) -> int:
    """Run the full EoD fundamentals pipeline for one exchange.

    Wires together 7 phases (9 with --include-tickers):
      1. refresh-industry
      2. refresh-shares
      3. refresh-financials
      4. compute-market-cap
      5. build-industry-indices
      6. compute-index-values
      7. compute-indicator-state (industries)
      8. compute-indicator-state (tickers)   [opt-in]
      9. compute-signals (tickers)           [opt-in]

    Each step's skip-if-recent guard means re-running daily is
    cheap — Phase 1-3 (yfinance) only do real work every 80+ days;
    Phase 4-6 only re-do work if their inputs have changed.
    """
    from datetime import datetime
    from .eod_pipeline import run_eod_pipeline, StepStatus

    validate_exchange(args.exchange, config)
    db_engine = create_engine_from_config(config)
    try:
        print(
            f"Running EoD pipeline on {args.exchange} "
            f"(include_tickers={not args.skip_tickers}, "
            f"timeframe={args.timeframe or '1W'}, force={args.force})"
        )
        print()

        def progress(step_result):
            # Print one line per completed step.
            status_str = step_result.status.value
            dur_str = (
                f"{step_result.duration_seconds:.1f}s"
                if step_result.duration_seconds is not None else "—"
            )
            counts = (
                f"s={step_result.succeeded} "
                f"f={step_result.failed} "
                f"sk={step_result.skipped}"
            )
            print(
                f"  [{status_str:<8}] {step_result.name:<42} "
                f"{dur_str:>8}  {counts}"
            )
            if step_result.notes:
                print(f"             {step_result.notes}")
            if step_result.error:
                print(f"             ERROR: {step_result.error}")

        result = run_eod_pipeline(
            db_engine, args.exchange,
            include_tickers=not args.skip_tickers,
            timeframe=args.timeframe or "1W",
            force=args.force,
            progress_callback=progress,
        )

        print()
        print(
            f"Pipeline {result.overall_status} in "
            f"{result.duration_seconds:.1f}s "
            f"({len(result.steps)} steps)"
        )

        if result.overall_status == "SUCCESS":
            return 0
        if result.overall_status == "PARTIAL":
            return 1
        return 2
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

    bs = sub.add_parser(
        "backfill-shares",
        help="Fetch historical shares-outstanding from yfinance for every "
             "active ticker. Inserts MANY snapshots per ticker (one per "
             "filing date). Once populated, compute-market-cap will produce "
             "COMPUTED rows for the full historical range. Takes ~90 min.",
    )
    _add_exchange_arg(bs)
    bs.add_argument(
        "--start-date", required=True, metavar="YYYY-MM-DD",
        help="Earliest date to backfill shares for (e.g. 2016-06-01). "
             "yfinance typically has ~16 months of history per ticker; "
             "older requests return whatever's available.",
    )
    bs.add_argument(
        "--force", action="store_true",
        help="Re-run even if a successful backfill exists from the last 80 days",
    )
    bs.add_argument(
        "--resume", type=int, default=None, metavar="RUN_ID",
        help="Resume a previously-interrupted backfill from its checkpoint",
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
        help="Compute Triple-EMA indicator state for industries or "
             "tickers. Pass one of --industry, --all-industries, "
             "--ticker, or --all-tickers.",
    )
    cis.add_argument(
        "--industry", default=None, metavar="NAME",
        help="Industry name (one industry).",
    )
    cis.add_argument(
        "--all-industries", action="store_true", dest="all_industries",
        help="Every active industry index that has value rows.",
    )
    cis.add_argument(
        "--ticker", default=None, metavar="SYMBOL",
        help="Ticker symbol (one ticker). Requires --exchange and "
             "--timeframe.",
    )
    cis.add_argument(
        "--all-tickers", action="store_true", dest="all_tickers",
        help="Every active ticker. Requires --timeframe. Optional "
             "--exchange filter.",
    )
    cis.add_argument(
        "--exchange", default=None, metavar="NSE|BSE",
        help="Exchange (required for --ticker; optional filter for "
             "--all-tickers).",
    )
    cis.add_argument(
        "--timeframe", default=None, metavar="1W|1D|1h|...",
        help="Timeframe (required for --ticker and --all-tickers).",
    )
    cis.add_argument(
        "--force", action="store_true",
        help="Re-compute even if recent state exists (within 80 days).",
    )

    # ---------- Phase 6-1 command ---------- #

    csg = sub.add_parser(
        "compute-signals",
        help="Compute BUY/SELL/EXIT_BUY/EXIT_SELL signal events from "
             "indicator_state.display_regime transitions, for tickers.",
    )
    csg.add_argument(
        "--ticker", default=None, metavar="SYMBOL",
        help="Ticker symbol. Requires --exchange and --timeframe.",
    )
    csg.add_argument(
        "--all-tickers", action="store_true", dest="all_tickers",
        help="Every active ticker. Requires --timeframe.",
    )
    csg.add_argument(
        "--exchange", default=None, metavar="NSE|BSE",
        help="Exchange (for --ticker; or filter for --all-tickers).",
    )
    csg.add_argument(
        "--timeframe", default=None, metavar="1W|1D|1h|...",
        help="Timeframe (required for --ticker and --all-tickers).",
    )
    csg.add_argument(
        "--force", action="store_true",
        help="Re-compute even if recent events exist (within 80 days).",
    )

    # ---------- Phase 4C command ---------- #

    eod = sub.add_parser(
        "run-eod-pipeline",
        help="Run the full EoD fundamentals pipeline for one "
             "exchange: refresh-industry → refresh-shares → "
             "refresh-financials → compute-market-cap → "
             "build-industry-indices → compute-index-values → "
             "compute-indicator-state → compute-signals. "
             "Optionally also runs per-ticker compute.",
    )
    eod.add_argument(
        "--exchange", required=True, metavar="NSE|BSE",
        help="Exchange to run the per-exchange phases for. "
             "Cross-exchange phases process all data regardless.",
    )
    eod.add_argument(
        "--include-tickers", action="store_true",
        dest="include_tickers",
        help="DEPRECATED no-op kept for backward compat. The per-stock "
             "phases are now always part of the pipeline. Use "
             "--skip-tickers to opt out.",
    )
    eod.add_argument(
        "--skip-tickers", action="store_true",
        dest="skip_tickers",
        help="Skip the per-stock indicator+signal phases. Use only when "
             "you want an industries-only re-run; the canonical pipeline "
             "order is per-stock first, then per-industry.",
    )
    eod.add_argument(
        "--timeframe", default=None, metavar="1W|1D|...",
        help="Timeframe for per-ticker compute (only used with "
             "--include-tickers). Default 1W.",
    )
    eod.add_argument(
        "--force", action="store_true",
        help="Pass force=True to every step's runner, bypassing "
             "all skip-if-recent guards. Use sparingly — re-runs "
             "the full history compute for every phase.",
    )


    p_bench = sub.add_parser(
        "compute-benchmark-stats",
        help="Compute alpha/beta/RS regression stats vs benchmarks "
             "(Phase 6-3). Writes to stock_benchmark_stats and "
             "industry_benchmark_stats.",
    )
    mode = p_bench.add_mutually_exclusive_group(required=True)
    mode.add_argument("--all-tickers", action="store_true",
                       help="Recompute for every active ticker.")
    mode.add_argument("--all-industries", action="store_true",
                       help="Recompute for every active industry.")
    mode.add_argument("--ticker", type=str,
                       help="Single ticker symbol (requires --exchange).")
    mode.add_argument("--industry", type=str,
                       help="Single industry name.")
    # --exchange is required only with --ticker; the other modes
    # (--all-tickers / --all-industries / --industry) ignore it.
    # Validated in cmd_compute_benchmark_stats.
    p_bench.add_argument(
        "--exchange", type=str, required=False,
        help="Exchange for --ticker (e.g. NSE). Ignored in other modes.",
    )
    p_bench.add_argument(
        "--as-of-date", type=str,
        help="ISO date (YYYY-MM-DD). Defaults to latest aligned "
             "series date. Use to backfill historical snapshots.",
    )
    p_bench.add_argument(
        "--force", action="store_true",
        help="No-op (every upsert is idempotent on PK); kept for "
             "symmetry with sibling compute commands.",
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
        "backfill-shares":              cmd_backfill_shares,
        "compute-indicator-state":      cmd_compute_indicator_state,
        "compute-signals":              cmd_compute_signals,
        "compute-benchmark-stats":      cmd_compute_benchmark_stats,
        "run-eod-pipeline":             cmd_run_eod_pipeline,
    }
