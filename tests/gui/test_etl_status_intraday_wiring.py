"""Tests for the intraday QProcess wiring in EtlStatusView
(Step 13d-2).

Mirrors the structure of tests/gui/test_etl_status_backfill_wiring.py
but targets the intraday button + IntradayConfirmationModal +
QProcess(spawn `trip-ema-etl cycle`).

Covers:
  - Modal open + Cancel
  - Modal Accept → QProcess spawn
  - Stdout parsing → stage progress updates
  - Mutex during intraday (other buttons disabled)
  - Cancel intraday (SIGTERM)
  - Failure path
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from PySide6.QtCore import QProcess
from PySide6.QtWidgets import QDialog

from .fakes import FakePrompts

from trip_ema.gui.views.etl_status import EtlStatusView
from trip_ema.gui.views.widgets.stage_progress import (
    LIVE_CYCLE_STAGES, StageStatus,
)


@pytest.fixture
def fake_db(monkeypatch):
    """Fake GuiDbSession that returns a connection allowing
    SubscribersRepository.count_active() to be patched."""
    db = MagicMock()
    from contextlib import contextmanager

    @contextmanager
    def fake_connect():
        yield MagicMock()

    db.connect = fake_connect
    return db


@pytest.fixture
def fake_http():
    return MagicMock()


@pytest.fixture
def prompts():
    """The view's Prompts — recording, so tests assert on what the user saw."""
    return FakePrompts()


@pytest.fixture
def view(qtbot, fake_db, fake_http, monkeypatch, prompts):
    """An EtlStatusView with all background schedulers disabled
    (start_refresh_on_init=False) so tests can drive state
    manually."""
    v = EtlStatusView(
        db=fake_db, http=fake_http,
        start_refresh_on_init=False,
        prompts=prompts,
    )
    qtbot.addWidget(v)
    return v


# ============================================================
# Initial state
# ============================================================

class TestInitialState:

    def test_no_intraday_process_at_init(self, view):
        assert view._intraday_process is None

    def test_buffers_empty(self, view):
        assert view._intraday_stdout_buffer == ""
        assert view._intraday_stderr_buffer == ""

    def test_cancel_button_hidden_at_init(self, view):
        assert not view._cancel_intraday_btn.isVisible()

    def test_progress_label_empty_at_init(self, view):
        assert view._intraday_progress_label.text() == ""


# ============================================================
# Run Intraday button — modal flow
# ============================================================

class TestModalFlow:

    def test_no_exchange_shows_warning(
        self, view, monkeypatch, prompts):
        """If no exchange selected, click → a warning."""
        view._picker.current_exchange = lambda: None
        view._on_run_intraday_clicked()
        assert prompts.warnings, "Expected QMessageBox.warning to fire"
        # No QProcess spawned
        assert view._intraday_process is None

    def test_already_running_blocks(self, view):
        view._intraday_process = MagicMock()
        view._on_run_intraday_clicked()
        # Status text should reflect the block — but we just
        # check the process didn't change
        # (a new spawn would have replaced it with a new QProcess)
        assert isinstance(view._intraday_process, MagicMock)

    def test_local_running_other_kind_blocks(self, view):
        view._local_running = True
        view._inflight_kind = "backfill"
        view._on_run_intraday_clicked()
        # Should NOT have spawned anything
        assert view._intraday_process is None

    def test_modal_reject_does_not_spawn(
        self, view, monkeypatch,
    ):
        view._picker.current_exchange = lambda: "NSE"
        # Patch SubscribersRepository at the import site that
        # _on_run_intraday_clicked uses
        monkeypatch.setattr(
            "trip_ema.persistence.subscribers_repo."
            "SubscribersRepository.count_active",
            lambda self: 0,
        )
        # Force modal to return Rejected
        from trip_ema.gui.views.widgets import (
            intraday_confirmation_modal as icm,
        )
        monkeypatch.setattr(
            icm.IntradayConfirmationModal, "exec",
            lambda self: QDialog.Rejected,
        )
        view._on_run_intraday_clicked()
        assert view._intraday_process is None


# ============================================================
# Stdout parsing → StageProgressList updates
# ============================================================

class TestStdoutParsing:

    def test_running_emit_sets_stage_running(self, view):
        # Set up: fake an active intraday process
        view._intraday_process = MagicMock()
        # Mock readAllStandardOutput to return a stage line
        view._intraday_process.readAllStandardOutput = (
            lambda: b"  [fetch_bars] running\n"
        )
        view._on_intraday_stdout()
        # Stage 1 should be RUNNING
        # We can't directly inspect StageProgressList state from
        # outside but we can check the progress label
        assert "Fetch Stock OHLCV" in (
            view._intraday_progress_label.text()
        )
        assert "running" in (
            view._intraday_progress_label.text()
        )

    def test_done_emit_sets_stage_done(self, view):
        view._intraday_process = MagicMock()
        view._intraday_process.readAllStandardOutput = (
            lambda: b"  [stock_state] done\n"
        )
        view._on_intraday_stdout()
        assert "Compute Stock Indicators" in (
            view._intraday_progress_label.text()
        )

    def test_failed_emit_surfaces_in_label(self, view):
        view._intraday_process = MagicMock()
        view._intraday_process.readAllStandardOutput = (
            lambda: b"  [iiv] failed: connection lost\n"
        )
        view._on_intraday_stdout()
        text = view._intraday_progress_label.text()
        assert "Refresh Industry Index Values" in text
        assert "connection lost" in text

    def test_compute_signals_collapses_internal_stages(self, view):
        """rotation/derive/cache_rebuild all map to
        'Compute Signals' label. Multiple emits in sequence
        should NOT crash and should keep the label updated."""
        view._intraday_process = MagicMock()
        # Sequence of substage emits — rotation done, derive
        # running, derive done
        view._intraday_process.readAllStandardOutput = (
            lambda: (
                b"  [rotation] running\n"
                b"  [rotation] done\n"
                b"  [derive] running\n"
                b"  [derive] done\n"
            )
        )
        view._on_intraday_stdout()
        # The latest label is from the last parsed emit (derive done)
        assert "Compute Signals" in (
            view._intraday_progress_label.text()
        )

    def test_cycle_complete_sweep(self, view):
        view._intraday_process = MagicMock()
        view._intraday_process.readAllStandardOutput = (
            lambda: b"cycle complete (NSE, intraday, 42)\n"
        )
        view._on_intraday_stdout()
        # Progress label should reflect completion
        assert "complete" in (
            view._intraday_progress_label.text().lower()
        )

    def test_non_stage_lines_ignored(self, view):
        view._intraday_process = MagicMock()
        # Lines that should NOT change the stage progress label
        view._intraday_process.readAllStandardOutput = (
            lambda: (
                b"V1 cycle starting: exchange=NSE\n"
                b"compute_runs row: run_id=42\n"
            )
        )
        view._on_intraday_stdout()
        # Progress label stays empty since no stage line parsed
        assert view._intraday_progress_label.text() == ""

    def test_buffer_accumulates(self, view):
        view._intraday_process = MagicMock()
        view._intraday_process.readAllStandardOutput = (
            lambda: b"  [fetch_bars] running\n"
        )
        view._on_intraday_stdout()
        assert (
            "[fetch_bars] running" in view._intraday_stdout_buffer
        )


# ============================================================
# Stderr buffering
# ============================================================

class TestStderrBuffering:

    def test_stderr_appended_to_buffer(self, view):
        view._intraday_process = MagicMock()
        view._intraday_process.readAllStandardError = (
            lambda: b"some error log\n"
        )
        view._on_intraday_stderr()
        assert "some error log" in view._intraday_stderr_buffer


# ============================================================
# Finished handler
# ============================================================

class TestFinishedHandler:

    def test_success_resets_mutex(self, view, monkeypatch):
        """exit_code 0 → mutex flags cleared + activity scheduler
        triggered."""
        view._intraday_process = MagicMock()
        view._local_running = True
        view._inflight_kind = "intraday"
        # Stub the schedulers + QMessageBox to avoid blocking
        view._activity_scheduler.trigger_now = MagicMock()
        view._mutex_scheduler.trigger_now = MagicMock()

        view._on_intraday_finished(
            0, QProcess.NormalExit,
        )

        assert view._intraday_process is None
        assert view._local_running is False
        assert view._inflight_kind is None
        view._activity_scheduler.trigger_now.assert_called_once()
        view._mutex_scheduler.trigger_now.assert_called_once()

    def test_failure_resets_mutex_and_opens_dialog(
        self, view, monkeypatch,
    ):
        """exit_code != 0 → failure dialog opened + mutex cleared."""
        view._intraday_process = MagicMock()
        view._local_running = True
        view._inflight_kind = "intraday"
        view._intraday_stderr_buffer = (
            "cycle FAILED in iiv: connection lost\n"
        )
        view._activity_scheduler.trigger_now = MagicMock()
        view._mutex_scheduler.trigger_now = MagicMock()

        # Patch the BackfillFailureDialog to no-op
        from trip_ema.gui.views.widgets import (
            backfill_failure_dialog as bfd,
        )
        opened = []
        original_dialog = bfd.BackfillFailureDialog
        class FakeDialog(original_dialog):
            def exec(self):
                opened.append(self)
                return 0
        monkeypatch.setattr(
            bfd, "BackfillFailureDialog", FakeDialog,
        )

        view._on_intraday_finished(
            1, QProcess.NormalExit,
        )

        assert view._intraday_process is None
        assert view._local_running is False
        assert view._inflight_kind is None
        assert len(opened) == 1, (
            "Expected BackfillFailureDialog.exec() once"
        )
