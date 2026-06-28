"""Tests for BackfillFailureDialog + parse_failure_summary
(Step 13c)."""
from __future__ import annotations

import pytest
from PySide6.QtWidgets import QPlainTextEdit, QPushButton

from trip_ema.gui.views.widgets.backfill_failure_dialog import (
    BackfillFailureDialog, parse_failure_summary,
)


def _build(qtbot, **overrides):
    defaults = dict(
        exit_code=1,
        failed_stage=None,
        error_message=None,
        stderr_tail="",
        full_stderr="",
    )
    defaults.update(overrides)
    dlg = BackfillFailureDialog(**defaults)
    qtbot.addWidget(dlg)
    return dlg


# ============================================================
# parse_failure_summary
# ============================================================

class TestParseFailureSummary:

    def test_empty_input(self):
        stage, msg = parse_failure_summary("")
        assert stage is None
        assert msg is None

    def test_no_failure_marker(self):
        stage, msg = parse_failure_summary(
            "backfill complete\n  run_id: 42",
        )
        assert stage is None
        assert msg is None

    def test_extracts_stage_and_message(self):
        stdout = """V1 Backfill — NSE
  date_from:     2024-01-01
  stocks:        ALL (2667 active)

  [stage_fetch_bars] fetching 2667 tickers...
  [stage_stock_state] computing states...

============================================================
backfill FAILED in stage_industry_state: ZeroDivisionError: integer division by zero
  run_id:      42 (marker finished; live cycle resumes)
"""
        stage, msg = parse_failure_summary(stdout)
        assert stage == "stage_industry_state"
        assert (
            msg == "ZeroDivisionError: integer division by zero"
        )

    def test_first_match_only(self):
        # If 'backfill FAILED in' appears more than once we take
        # the first hit (regex.search with default flags)
        stdout = (
            "backfill FAILED in stage_a: msg_a\n"
            "backfill FAILED in stage_b: msg_b\n"
        )
        stage, msg = parse_failure_summary(stdout)
        assert stage == "stage_a"
        assert msg == "msg_a"


# ============================================================
# Dialog renders
# ============================================================

class TestRenders:

    def test_constructs_with_minimal_args(self, qtbot):
        dlg = _build(qtbot, exit_code=1)
        assert dlg.windowTitle() == "Backfill Failed"

    def test_displays_exit_code(self, qtbot):
        dlg = _build(qtbot, exit_code=42)
        from PySide6.QtWidgets import QLabel
        all_text = " ".join(
            lbl.text() for lbl in dlg.findChildren(QLabel)
        )
        assert "42" in all_text

    def test_displays_failed_stage(self, qtbot):
        dlg = _build(
            qtbot, exit_code=1,
            failed_stage="stage_industry_state",
        )
        from PySide6.QtWidgets import QLabel
        all_text = " ".join(
            lbl.text() for lbl in dlg.findChildren(QLabel)
        )
        assert "stage_industry_state" in all_text

    def test_displays_error_message(self, qtbot):
        dlg = _build(
            qtbot, exit_code=1,
            error_message="ZeroDivisionError: nope",
        )
        from PySide6.QtWidgets import QLabel
        all_text = " ".join(
            lbl.text() for lbl in dlg.findChildren(QLabel)
        )
        assert "ZeroDivisionError" in all_text

    def test_displays_stderr_tail(self, qtbot):
        dlg = _build(
            qtbot, exit_code=1,
            stderr_tail="line1\nline2\nline3",
        )
        # Find the tail QPlainTextEdit (not the hidden full-stderr)
        tail_widgets = dlg.findChildren(QPlainTextEdit)
        assert any(
            "line1" in w.toPlainText() for w in tail_widgets
        )

    def test_release_lock_hint_appears_for_lock_error(self, qtbot):
        dlg = _build(
            qtbot, exit_code=1,
            error_message=(
                "error: another backfill appears to be active"
            ),
        )
        from PySide6.QtWidgets import QLabel
        all_text = " ".join(
            lbl.text() for lbl in dlg.findChildren(QLabel)
        )
        assert "release-lock" in all_text

    def test_no_release_lock_hint_for_other_errors(self, qtbot):
        dlg = _build(
            qtbot, exit_code=1,
            error_message="random other error",
            full_stderr="random other stderr",
        )
        from PySide6.QtWidgets import QLabel
        all_text = " ".join(
            lbl.text() for lbl in dlg.findChildren(QLabel)
        )
        assert "release-lock" not in all_text


# ============================================================
# Full-stderr toggle
# ============================================================

class TestFullStderrToggle:

    def test_full_stderr_hidden_initially(self, qtbot):
        dlg = _build(
            qtbot, exit_code=1,
            stderr_tail="tail",
            full_stderr="line1\nline2\nline3\nline4\ntail",
        )
        # The full-stderr widget starts hidden
        assert dlg._full_stderr_widget.isHidden() is True

    def test_toggle_button_shows_full_stderr(self, qtbot):
        dlg = _build(
            qtbot, exit_code=1,
            stderr_tail="tail",
            full_stderr="line1\nline2\nline3\nline4\ntail",
        )
        # Find the "Show full stderr" button
        toggle_btns = [
            b for b in dlg.findChildren(QPushButton)
            if "stderr" in b.text()
        ]
        assert len(toggle_btns) >= 1
        toggle_btns[0].click()
        assert dlg._full_stderr_widget.isHidden() is False
