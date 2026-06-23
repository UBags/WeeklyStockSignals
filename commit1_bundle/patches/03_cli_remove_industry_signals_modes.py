"""Commit 1, patch 03.

Edit `trip_ema/fundamentals/cli.py`:
  1. Replace the `cmd_compute_signals` header + arg-normalization +
     mode-counting block with ticker-only equivalents. Industry branches
     get deleted.
  2. Drop `--industry` and `--all-industries` argparse arguments from
     the `compute-signals` subparser.
  3. Update the `cmd_run_eod_pipeline` docstring step numbering — the
     "compute-signals (industries)" step is gone.

Industry indicator-state stays. Only the *signals* command and its CLI
flags lose their industry modes.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _patch_lib import edit, run as _run


TARGET = "trip_ema/fundamentals/cli.py"


# ---- Edit 1: cmd_compute_signals header through mode-count check ----
#
# Reaches from `def cmd_compute_signals(` through the closing
# `return 2` of the modes_set>1 branch, replacing the whole industry-
# aware preamble with a ticker-only one.

OLD_PREAMBLE = '''def cmd_compute_signals(args, config: Config) -> int:
    """Compute signal events for industries or tickers.

    Modes (mutually exclusive):
      --industry NAME              : one industry
      --all-industries             : every active industry with indicator_state
      --ticker SYMBOL --exchange X : one ticker (+ --timeframe required)
      --all-tickers --timeframe TF : all tickers on a timeframe
    """
    from .runners import (
        run_signal_events_for_all_industries,
        run_signal_events_for_all_tickers,
        run_signal_events_for_industry,
        run_signal_events_for_ticker,
    )
    from ..persistence.tickers_repo import TickersRepository
    from ..domain import TickerRef

    # Normalize args — older callers may not set per-ticker attrs.
    args.industry = getattr(args, "industry", None)
    args.all_industries = getattr(args, "all_industries", False)
    args.ticker = getattr(args, "ticker", None)
    args.all_tickers = getattr(args, "all_tickers", False)
    args.exchange = getattr(args, "exchange", None)
    args.timeframe = getattr(args, "timeframe", None)
    args.force = getattr(args, "force", False)

    modes_set = sum([
        bool(args.industry),
        bool(args.all_industries),
        bool(args.ticker),
        bool(args.all_tickers),
    ])
    if modes_set == 0:
        print(
            "error: must pass one of --industry, --all-industries, "
            "--ticker, or --all-tickers",
            file=sys.stderr,
        )
        return 2
    if modes_set > 1:
        print(
            "error: --industry, --all-industries, --ticker, and "
            "--all-tickers are mutually exclusive",
            file=sys.stderr,
        )
        return 2'''

NEW_PREAMBLE = '''def cmd_compute_signals(args, config: Config) -> int:
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
        return 2'''


# ---- Edit 2: delete the `if args.industry:` branch ----

OLD_INDUSTRY_BRANCH = '''        if args.industry:
            try:
                run_id = run_signal_events_for_industry(
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
                f"compute-signals {args.industry}: "
                f"run_id={r.run_id} status={r.status} "
                f"events_written={r.succeeded} skipped={r.skipped}"
            )
            return 0 if r.status == "SUCCESS" else 2

        if args.all_industries:
            report = run_signal_events_for_all_industries(
                db_engine, force=args.force,
            )
            print(f"compute-signals --all-industries:")
            print(
                f"  succeeded: {len(report['succeeded'])}, "
                f"skipped: {len(report['skipped'])}, "
                f"failed: {len(report['failed'])}"
            )
            if report["succeeded"]:
                print()
                print("Succeeded:")
                print(f"  {'INDUSTRY':<48} {'EVENTS':>8}")
                for name, count in report["succeeded"]:
                    print(f"  {name[:48]:<48} {count:>8}")
            if report["skipped"]:
                print()
                print("Skipped (run compute-indicator-state first):")
                for name, reason in report["skipped"]:
                    print(f"  {name[:48]:<48} {reason}")
            if report["failed"]:
                print()
                print("Failed:")
                for name, err in report["failed"]:
                    print(f"  {name[:48]:<48} {err[:60]}")
            return _exit_code_from_report(report)

        if args.ticker:'''

NEW_INDUSTRY_BRANCH = '''        if args.ticker:'''


# ---- Edit 3: drop --industry and --all-industries argparse args ----

OLD_ARGPARSE = '''    csg.add_argument(
        "--industry", default=None, metavar="NAME",
        help="Industry name (one industry).",
    )
    csg.add_argument(
        "--all-industries", action="store_true", dest="all_industries",
        help="Every active industry that has indicator_state rows.",
    )
    csg.add_argument(
        "--ticker", default=None, metavar="SYMBOL",
        help="Ticker symbol. Requires --exchange and --timeframe.",
    )'''

NEW_ARGPARSE = '''    csg.add_argument(
        "--ticker", default=None, metavar="SYMBOL",
        help="Ticker symbol. Requires --exchange and --timeframe.",
    )'''


# ---- Edit 4: update compute-signals subparser help text ----

OLD_CSG_HELP = '''    csg = sub.add_parser(
        "compute-signals",
        help="Compute BUY/SELL/EXIT_BUY/EXIT_SELL signal events from "
             "indicator_state.display_regime transitions, for "
             "industries or tickers.",
    )'''

NEW_CSG_HELP = '''    csg = sub.add_parser(
        "compute-signals",
        help="Compute BUY/SELL/EXIT_BUY/EXIT_SELL signal events from "
             "indicator_state.display_regime transitions, for tickers.",
    )'''


# ---- Edit 5: cmd_run_eod_pipeline docstring step list ----

OLD_EOD_STEPS_DOCSTRING = '''    Wires together 8 phases (10 with --include-tickers):
      1. refresh-industry
      2. refresh-shares
      3. refresh-financials
      4. compute-market-cap
      5. build-industry-indices
      6. compute-index-values
      7. compute-indicator-state (industries)
      8. compute-signals (industries)
      9. compute-indicator-state (tickers)   [opt-in]
     10. compute-signals (tickers)           [opt-in]'''

NEW_EOD_STEPS_DOCSTRING = '''    Wires together 7 phases (9 with --include-tickers):
      1. refresh-industry
      2. refresh-shares
      3. refresh-financials
      4. compute-market-cap
      5. build-industry-indices
      6. compute-index-values
      7. compute-indicator-state (industries)
      8. compute-indicator-state (tickers)   [opt-in]
      9. compute-signals (tickers)           [opt-in]'''


def patch(text: str) -> str:
    text = edit(text, OLD_PREAMBLE, NEW_PREAMBLE)
    text = edit(text, OLD_INDUSTRY_BRANCH, NEW_INDUSTRY_BRANCH)
    text = edit(text, OLD_ARGPARSE, NEW_ARGPARSE)
    text = edit(text, OLD_CSG_HELP, NEW_CSG_HELP)
    text = edit(text, OLD_EOD_STEPS_DOCSTRING, NEW_EOD_STEPS_DOCSTRING)
    return text


if __name__ == "__main__":
    _run(TARGET, patch)
