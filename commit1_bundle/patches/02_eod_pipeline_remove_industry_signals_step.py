"""Commit 1, patch 02.

Edit `trip_ema/fundamentals/eod_pipeline.py`:
  1. Drop `run_signal_events_for_all_industries` from the import block.
  2. Delete `_step_signals_industries` nested function definition.
  3. Remove the `("compute-signals (industries)", ...)` tuple from the
     `steps_to_run.extend([...])` call.

Industry indicator-state step stays (industries still get regimes
computed). Only the *signals* step is removed.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _patch_lib import edit, run as _run


TARGET = "trip_ema/fundamentals/eod_pipeline.py"


# ---- Edit 1: import block ----

OLD_IMPORT_BLOCK = """    from .runners import (
        run_financials_refresh,
        run_indicator_state_for_all_industries,
        run_indicator_state_for_all_tickers,
        run_industry_index_build,
        run_industry_refresh,
        run_index_value_backfill,
        run_shares_refresh,
        run_signal_events_for_all_industries,
        run_signal_events_for_all_tickers,
    )"""

NEW_IMPORT_BLOCK = """    from .runners import (
        run_financials_refresh,
        run_indicator_state_for_all_industries,
        run_indicator_state_for_all_tickers,
        run_industry_index_build,
        run_industry_refresh,
        run_index_value_backfill,
        run_shares_refresh,
        run_signal_events_for_all_tickers,
    )"""


# ---- Edit 2: _step_signals_industries function ----

OLD_STEP_FUNC = '''    def _step_signals_industries():
        report = run_signal_events_for_all_industries(
            db_engine, force=force,
        )
        succeeded = len(report["succeeded"])
        failed = len(report["failed"])
        skipped = len(report["skipped"])
        status = _status_from_counts(succeeded, failed, skipped)
        return (status, succeeded, failed, skipped,
                f"compute-signals for {succeeded} industries "
                f"(skipped {skipped}, failed {failed})")

'''  # Trailing blank line eaten with the function so we don't leave a triple blank


# ---- Edit 3: steps_to_run tuple ----

OLD_STEPS_EXTEND = """    steps_to_run.extend([
        ("build-industry-indices", _step_index_build),
        ("compute-index-values", _step_index_values),
        ("compute-indicator-state (industries)",
         _step_indicator_state_industries),
        ("compute-signals (industries)", _step_signals_industries),
    ])"""

NEW_STEPS_EXTEND = """    steps_to_run.extend([
        ("build-industry-indices", _step_index_build),
        ("compute-index-values", _step_index_values),
        ("compute-indicator-state (industries)",
         _step_indicator_state_industries),
    ])"""


def patch(text: str) -> str:
    text = edit(text, OLD_IMPORT_BLOCK, NEW_IMPORT_BLOCK)
    # The function block is matched by raw text (not normalized) because
    # the trailing blank line matters. Use edit() with empty new-block.
    text = edit(text, OLD_STEP_FUNC, "")
    text = edit(text, OLD_STEPS_EXTEND, NEW_STEPS_EXTEND)
    return text


if __name__ == "__main__":
    _run(TARGET, patch)
