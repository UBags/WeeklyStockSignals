"""Commit 1, patch 06.

Fix `tests/fundamentals/test_eod_pipeline.py::_patch_all_runners` —
the helper still monkeypatched the now-deleted
`run_signal_events_for_all_industries`. 13 tests across
TestFailurePropagation, TestProgressCallback, TestIndexBuildSteadyState,
and TestMarketCapNoTradingDay called the helper and failed at setup
with AttributeError.

This patch:
  1. Drops `signals_industries_report=None` kwarg from the helper
     signature (no caller passes it explicitly — verified).
  2. Drops the matching `if signals_industries_report is None: ...`
     default-value block.
  3. Drops the `mocks["signals_industries"]` MagicMock + the
     `monkeypatch.setattr` call that targets the deleted function.
  4. Drops the leading `# Phase 6 — signals (industries).` comment
     so the section structure stays clean.

The 6 skipped `TestOrchestration` tests stay skipped (their step-list
expectation was already stale pre-Commit 1 and still is). The 13
previously-failing tests should now pass because the helper is
internally consistent with the post-Commit-1 runners.py surface.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _patch_lib import edit, run as _run


TARGET = "tests/fundamentals/test_eod_pipeline.py"


# ---- Edit 1: drop kwarg from signature ----

OLD_SIGNATURE = '''def _patch_all_runners(
    monkeypatch,
    *,
    industry_status="SUCCESS",
    shares_status="SUCCESS",
    financials_status="SUCCESS",
    market_cap_succeeded=100,
    index_build_report=None,
    index_value_status="SUCCESS",
    indicator_industries_report=None,
    signals_industries_report=None,
    indicator_tickers_report=None,
    signals_tickers_report=None,
):'''

NEW_SIGNATURE = '''def _patch_all_runners(
    monkeypatch,
    *,
    industry_status="SUCCESS",
    shares_status="SUCCESS",
    financials_status="SUCCESS",
    market_cap_succeeded=100,
    index_build_report=None,
    index_value_status="SUCCESS",
    indicator_industries_report=None,
    indicator_tickers_report=None,
    signals_tickers_report=None,
):'''


# ---- Edit 2: drop the default-value block ----

OLD_DEFAULT_BLOCK = '''    if signals_industries_report is None:
        signals_industries_report = {
            "succeeded": [("IT - Services", 17)],
            "skipped": [],
            "failed": [],
        }
    if indicator_tickers_report is None:'''

NEW_DEFAULT_BLOCK = '''    if indicator_tickers_report is None:'''


# ---- Edit 3: drop the monkeypatch.setattr block + section comment ----

OLD_MONKEYPATCH_BLOCK = '''    # Phase 6 — signals (industries).
    mocks["signals_industries"] = MagicMock(
        return_value=signals_industries_report
    )
    monkeypatch.setattr(
        "trip_ema.fundamentals.runners.run_signal_events_for_all_industries",
        mocks["signals_industries"],
    )

    # Phase 7 — per-ticker indicator + signals.'''

NEW_MONKEYPATCH_BLOCK = '''    # Phase 7 — per-ticker indicator + signals.'''


def patch(text: str) -> str:
    text = edit(text, OLD_SIGNATURE, NEW_SIGNATURE)
    text = edit(text, OLD_DEFAULT_BLOCK, NEW_DEFAULT_BLOCK)
    text = edit(text, OLD_MONKEYPATCH_BLOCK, NEW_MONKEYPATCH_BLOCK)
    return text


if __name__ == "__main__":
    _run(TARGET, patch)
