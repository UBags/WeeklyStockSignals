"""Tests for cycle_stdout_parser — parses cmd_cycle's stage
markers + failure summary out of QProcess stdout/stderr.
"""
from __future__ import annotations

import pytest

from trip_ema.gui.views.widgets.cycle_stdout_parser import (
    is_cycle_complete_line,
    labels_before,
    parse_cycle_failure_summary,
    parse_stage_line,
    LIVE_CYCLE_LABEL_ORDER,
    STAGE_NAME_TO_LABEL,
)


# ============================================================
# parse_stage_line
# ============================================================

class TestParseStageLine:

    def test_running_emit(self):
        result = parse_stage_line("  [fetch_bars] running")
        assert result == ("Fetch Stock OHLCV", "running", "running")

    def test_done_emit(self):
        result = parse_stage_line("  [stock_state] done")
        assert result == (
            "Compute Stock Indicators", "done", "done",
        )

    def test_done_with_extra_msg(self):
        result = parse_stage_line(
            "  [notify] done: skipped (no signals)",
        )
        assert result == (
            "Notify Subscribers",
            "done",
            "done: skipped (no signals)",
        )

    def test_failed_emit(self):
        result = parse_stage_line(
            "  [iiv] failed: connection refused",
        )
        assert result == (
            "Refresh Industry Index Values",
            "failed",
            "failed: connection refused",
        )

    def test_rotation_collapses_to_compute_signals(self):
        result = parse_stage_line("  [rotation] running")
        assert result[0] == "Compute Signals"

    def test_derive_collapses_to_compute_signals(self):
        result = parse_stage_line("  [derive] done")
        assert result[0] == "Compute Signals"

    def test_cache_rebuild_collapses_to_compute_signals(self):
        result = parse_stage_line("  [cache_rebuild] running")
        assert result[0] == "Compute Signals"

    def test_industry_state_label(self):
        result = parse_stage_line("  [industry_state] running")
        assert result == (
            "Compute Industry Indicators", "running", "running",
        )

    def test_iiv_maps_to_refresh_industry_index_values(self):
        result = parse_stage_line("  [iiv] running")
        assert result == (
            "Refresh Industry Index Values", "running", "running",
        )

    def test_non_stage_line_returns_none(self):
        assert parse_stage_line("V1 cycle starting: NSE") is None
        assert parse_stage_line("compute_runs row: run_id=42") is None
        assert parse_stage_line(
            "cycle complete (NSE, intraday)",
        ) is None
        assert parse_stage_line("") is None
        assert parse_stage_line("   ") is None
        assert parse_stage_line("[]") is None

    def test_unknown_internal_name_returns_none(self):
        assert parse_stage_line(
            "  [unknown_stage] running",
        ) is None
        assert parse_stage_line(
            "  [fetch_bars2] running",
        ) is None

    def test_unknown_status_returns_none(self):
        # cmd_cycle only emits running/done/failed, but parser
        # should be conservative on unknown statuses.
        assert parse_stage_line(
            "  [fetch_bars] pending",
        ) is None
        assert parse_stage_line(
            "  [fetch_bars] partial",
        ) is None

    def test_handles_leading_whitespace(self):
        # cmd_cycle emits with two-space leading indent, but the
        # parser strips first so any leading whitespace works.
        assert parse_stage_line(
            "    [fetch_bars] running",
        ) == ("Fetch Stock OHLCV", "running", "running")

    def test_handles_no_leading_whitespace(self):
        assert parse_stage_line(
            "[fetch_bars] running",
        ) == ("Fetch Stock OHLCV", "running", "running")


# ============================================================
# labels_before
# ============================================================

class TestLabelsBefore:

    def test_first_stage_returns_empty(self):
        assert labels_before("Fetch Stock OHLCV") == []

    def test_middle_stage(self):
        result = labels_before("Compute Signals")
        assert result == [
            "Fetch Stock OHLCV",
            "Compute Stock Indicators",
            "Compute Stock Enrichment",
        ]

    def test_last_stage(self):
        result = labels_before("Notify Subscribers")
        # All 7 stages before "Notify Subscribers"
        assert len(result) == 7
        assert "Fetch Stock OHLCV" in result
        assert "Compute Industry Enrichment" in result

    def test_unknown_label_returns_empty(self):
        assert labels_before("Not a real stage") == []
        assert labels_before("") == []


# ============================================================
# parse_cycle_failure_summary
# ============================================================

class TestParseCycleFailureSummary:

    def test_typical_failure_line(self):
        stderr = "cycle FAILED in iiv: connection refused\n"
        assert parse_cycle_failure_summary(stderr) == (
            "iiv", "connection refused",
        )

    def test_failure_with_complex_message(self):
        stderr = (
            "cycle FAILED in stock_state: "
            "SQLAlchemy connection pool exhausted\n"
        )
        assert parse_cycle_failure_summary(stderr) == (
            "stock_state",
            "SQLAlchemy connection pool exhausted",
        )

    def test_failure_with_colon_in_message(self):
        # message body contains colons (e.g. exception detail)
        stderr = (
            "cycle FAILED in derive: "
            "ValueError: bad ticker_id 0\n"
        )
        result = parse_cycle_failure_summary(stderr)
        assert result[0] == "derive"
        assert "ValueError: bad ticker_id 0" in result[1]

    def test_empty_stderr(self):
        assert parse_cycle_failure_summary("") == (None, "")

    def test_no_failure_line(self):
        assert parse_cycle_failure_summary(
            "INFO: some log line\n"
            "INFO: another line\n",
        ) == (None, "")

    def test_finds_last_failure_when_multiple(self):
        # If by some bug multiple lines appear, use the LAST one
        # (most-recent failure wins)
        stderr = (
            "cycle FAILED in fetch_bars: yfinance timeout\n"
            "some recovery log\n"
            "cycle FAILED in iiv: definitive failure\n"
        )
        assert parse_cycle_failure_summary(stderr) == (
            "iiv", "definitive failure",
        )


# ============================================================
# is_cycle_complete_line
# ============================================================

class TestIsCycleCompleteLine:

    def test_typical_success_line(self):
        assert is_cycle_complete_line(
            "cycle complete (exchange=NSE, kind=intraday, run_id=42)",
        )

    def test_with_leading_whitespace(self):
        assert is_cycle_complete_line(
            "  cycle complete (NSE)",
        )

    def test_failed_line_not_complete(self):
        assert not is_cycle_complete_line(
            "cycle FAILED in iiv: error",
        )

    def test_empty(self):
        assert not is_cycle_complete_line("")

    def test_other_lines_not_complete(self):
        assert not is_cycle_complete_line(
            "V1 cycle starting: NSE",
        )
        assert not is_cycle_complete_line("compute_runs row: 42")


# ============================================================
# Invariants — guard against the maps + tuples drifting apart
# ============================================================

class TestInvariants:

    def test_all_label_values_in_order_tuple(self):
        """Every value in STAGE_NAME_TO_LABEL must appear in
        LIVE_CYCLE_LABEL_ORDER."""
        for label in STAGE_NAME_TO_LABEL.values():
            assert label in LIVE_CYCLE_LABEL_ORDER, (
                f"label {label!r} not in canonical order tuple"
            )

    def test_label_order_has_8_stages(self):
        """The canonical order must have exactly 8 stages
        (matches LIVE_CYCLE_STAGES from stage_progress.py)."""
        assert len(LIVE_CYCLE_LABEL_ORDER) == 8

    def test_compute_signals_has_3_internal_names(self):
        """Three internal cmd_cycle stages collapse into
        'Compute Signals'."""
        substages = [
            name
            for name, label in STAGE_NAME_TO_LABEL.items()
            if label == "Compute Signals"
        ]
        assert set(substages) == {
            "rotation", "derive", "cache_rebuild",
        }
