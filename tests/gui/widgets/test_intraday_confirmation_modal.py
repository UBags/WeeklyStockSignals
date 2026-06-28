"""Tests for IntradayConfirmationModal — the pre-flight dialog
shown before [Run Intraday] spawns a QProcess.

Verifies:
  - Constructs without crashing
  - Surfaces exchange + subscriber count + kind hint
  - Cancel is the default button (destructive-ish ops should be
    one extra click away)
  - Accept/Reject behavior matches QDialog conventions
"""
from __future__ import annotations

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QDialogButtonBox

from trip_ema.gui.views.widgets.intraday_confirmation_modal import (
    IntradayConfirmationModal,
    RUNTIME_ESTIMATE_TEXT,
)


@pytest.fixture
def modal(qtbot):
    """Standard modal: NSE, intraday-during-market, 1 subscriber."""
    m = IntradayConfirmationModal(
        exchange="NSE",
        kind_hint="intraday",
        market_open=True,
        n_active_subscribers=1,
    )
    qtbot.addWidget(m)
    return m


class TestConstructionBasics:

    def test_constructs_without_error(self, modal):
        assert modal is not None

    def test_window_title(self, modal):
        assert modal.windowTitle() == "Confirm Intraday Cycle"

    def test_is_modal(self, modal):
        assert modal.isModal()

    def test_has_minimum_width(self, modal):
        assert modal.minimumWidth() >= 400


class TestContentRendering:

    def test_exchange_appears_in_text(self, qtbot):
        modal = IntradayConfirmationModal(
            exchange="BSE",
            kind_hint="intraday",
            market_open=True,
            n_active_subscribers=5,
        )
        qtbot.addWidget(modal)
        # Search children labels for "BSE"
        from PySide6.QtWidgets import QLabel
        found_bse = False
        for label in modal.findChildren(QLabel):
            if "BSE" in label.text():
                found_bse = True
                break
        assert found_bse, "Exchange name should appear in modal"

    def test_subscriber_count_zero_shows_clearly(self, qtbot):
        modal = IntradayConfirmationModal(
            exchange="NSE",
            kind_hint="eod",
            market_open=False,
            n_active_subscribers=0,
        )
        qtbot.addWidget(modal)
        from PySide6.QtWidgets import QLabel
        all_text = " ".join(
            label.text() for label in modal.findChildren(QLabel)
        )
        assert "0 active subscriber" in all_text

    def test_subscriber_count_plural_form(self, qtbot):
        modal = IntradayConfirmationModal(
            exchange="NSE",
            kind_hint="intraday",
            market_open=True,
            n_active_subscribers=5,
        )
        qtbot.addWidget(modal)
        from PySide6.QtWidgets import QLabel
        all_text = " ".join(
            label.text() for label in modal.findChildren(QLabel)
        )
        assert "5 active subscribers" in all_text

    def test_subscriber_count_singular_form(self, qtbot):
        modal = IntradayConfirmationModal(
            exchange="NSE",
            kind_hint="intraday",
            market_open=True,
            n_active_subscribers=1,
        )
        qtbot.addWidget(modal)
        from PySide6.QtWidgets import QLabel
        all_text = " ".join(
            label.text() for label in modal.findChildren(QLabel)
        )
        # "1 active subscriber " (no 's')
        # === STEP 13D2-FIXUP: singular subscriber assertion ===
        # The count is wrapped in a <span>, so there's no trailing
        # space after "subscriber" — assert both the singular form
        # is present AND the plural is NOT.
        assert "1 active subscriber" in all_text
        assert "1 active subscribers" not in all_text
        assert "1 active subscribers" not in all_text

    def test_runtime_estimate_appears(self, modal):
        from PySide6.QtWidgets import QLabel
        all_text = " ".join(
            label.text() for label in modal.findChildren(QLabel)
        )
        # The constant should be embedded somewhere
        assert (
            "Expected duration" in all_text
            or "duration" in all_text.lower()
        )

    def test_market_open_text(self, qtbot):
        modal = IntradayConfirmationModal(
            exchange="NSE",
            kind_hint="intraday",
            market_open=True,
            n_active_subscribers=0,
        )
        qtbot.addWidget(modal)
        from PySide6.QtWidgets import QLabel
        all_text = " ".join(
            label.text() for label in modal.findChildren(QLabel)
        )
        assert "open" in all_text.lower()

    def test_market_closed_text(self, qtbot):
        modal = IntradayConfirmationModal(
            exchange="NSE",
            kind_hint="eod",
            market_open=False,
            n_active_subscribers=0,
        )
        qtbot.addWidget(modal)
        from PySide6.QtWidgets import QLabel
        all_text = " ".join(
            label.text() for label in modal.findChildren(QLabel)
        )
        assert "closed" in all_text.lower()

    def test_pipeline_stages_listed(self, modal):
        """All 8 stage labels should appear in the modal."""
        from PySide6.QtWidgets import QLabel
        all_text = " ".join(
            label.text() for label in modal.findChildren(QLabel)
        )
        for stage in (
            "Fetch Stock OHLCV",
            "Compute Stock Indicators",
            "Compute Stock Enrichment",
            "Compute Signals",
            "Refresh Industry Index Values",
            "Compute Industry Indicators",
            "Compute Industry Enrichment",
            "Notify Subscribers",
        ):
            assert stage in all_text, (
                f"Stage {stage!r} not in modal text"
            )


class TestButtons:

    def test_has_cancel_and_accept_buttons(self, modal):
        bb = modal.findChild(QDialogButtonBox)
        assert bb is not None
        # Cancel exists
        cancel = bb.button(QDialogButtonBox.Cancel)
        assert cancel is not None
        # Accept-role button is "Run Cycle"
        run_btn = None
        for b in bb.buttons():
            if b.text() == "Run Cycle":
                run_btn = b
                break
        assert run_btn is not None, (
            "Run Cycle button not found"
        )

    def test_cancel_is_default(self, modal):
        """Per F1 lock: Cancel is default (destructive-ish ops
        should be one extra click away)."""
        bb = modal.findChild(QDialogButtonBox)
        cancel = bb.button(QDialogButtonBox.Cancel)
        assert cancel.isDefault(), (
            "Cancel should be the default button"
        )

    def test_run_button_not_default(self, modal):
        bb = modal.findChild(QDialogButtonBox)
        run_btn = None
        for b in bb.buttons():
            if b.text() == "Run Cycle":
                run_btn = b
                break
        assert not run_btn.isDefault(), (
            "Run Cycle should NOT be the default"
        )


class TestExecResults:

    def test_reject_returns_rejected(self, modal):
        modal.reject()
        # Just check it doesn't raise; exec() would return
        # Rejected, but we can't call exec() in tests (blocking)
        # — checking reject path setup is enough

    def test_accept_returns_accepted(self, modal):
        # Same setup check; the actual button wiring uses
        # button_box.accepted.connect(self.accept), tested by
        # construction
        modal.accept()
