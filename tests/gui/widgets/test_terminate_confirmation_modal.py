"""Unit tests for TerminateInProgressConfirmationModal (Phase 1b).

Verifies:
  - Title format matches the operator spec:
      "Terminate In-process {kind} on Exchange : {exchange}"
  - Kind display maps 'intraday' -> 'Intraday', 'eod' -> 'EoD'
  - Cancel button is default focus (destructive action requires
    explicit click, not Enter)
  - Accept / reject return the expected QDialog result codes
"""
from __future__ import annotations

import pytest
from PySide6.QtWidgets import QDialog, QDialogButtonBox

from trip_ema.gui.views.widgets.terminate_confirmation_modal import (
    TerminateInProgressConfirmationModal,
    _display_kind,
)


# ============================================================
# _display_kind helper
# ============================================================

class TestDisplayKind:
    def test_intraday_maps_to_capitalized(self):
        assert _display_kind("intraday") == "Intraday"

    def test_eod_maps_to_camelcase(self):
        # Operator spec: "EoD", not "Eod" or "EOD".
        assert _display_kind("eod") == "EoD"

    def test_unknown_kind_passes_through_raw(self):
        # Fallback: don't silently swallow future kind values.
        assert _display_kind("weekly") == "weekly"


# ============================================================
# Title text
# ============================================================

class TestTitleText:
    def test_intraday_title_exact_format(self, qtbot):
        # Matches the operator spec verbatim:
        #   "Terminate In-process {Intraday|EoD} on Exchange : {ex}"
        m = TerminateInProgressConfirmationModal(
            exchange="NSE", kind="intraday",
        )
        qtbot.addWidget(m)
        assert m.title_text == "Terminate In-process Intraday on Exchange : NSE"

    def test_eod_title_exact_format(self, qtbot):
        m = TerminateInProgressConfirmationModal(
            exchange="NYSE", kind="eod",
        )
        qtbot.addWidget(m)
        assert m.title_text == "Terminate In-process EoD on Exchange : NYSE"

    def test_title_widget_contains_the_title_string(self, qtbot):
        m = TerminateInProgressConfirmationModal(
            exchange="BSE", kind="intraday",
        )
        qtbot.addWidget(m)
        # The title QLabel wraps the text in <b>...</b>; check that
        # the plain title string is a substring of the rendered text.
        assert m.title_text in m._title_label.text()


# ============================================================
# Property surface
# ============================================================

class TestProperties:
    def test_exchange_property(self, qtbot):
        m = TerminateInProgressConfirmationModal(
            exchange="NSE", kind="intraday",
        )
        qtbot.addWidget(m)
        assert m.exchange == "NSE"

    def test_kind_property_returns_raw_db_value(self, qtbot):
        m = TerminateInProgressConfirmationModal(
            exchange="NSE", kind="eod",
        )
        qtbot.addWidget(m)
        assert m.kind == "eod"

    def test_display_kind_property(self, qtbot):
        m = TerminateInProgressConfirmationModal(
            exchange="NSE", kind="eod",
        )
        qtbot.addWidget(m)
        assert m.display_kind == "EoD"


# ============================================================
# Focus + safety
# ============================================================

class TestSafety:
    def test_cancel_button_is_default(self, qtbot):
        """Enter key should NOT fire Terminate. Cancel must be the
        default focused button."""
        m = TerminateInProgressConfirmationModal(
            exchange="NSE", kind="intraday",
        )
        qtbot.addWidget(m)

        buttons = m.findChild(QDialogButtonBox)
        assert buttons is not None
        cancel_btn = buttons.button(QDialogButtonBox.Cancel)
        assert cancel_btn is not None
        assert cancel_btn.isDefault(), (
            "Cancel must be the default button so Enter doesn't "
            "trigger the destructive Terminate action."
        )

    def test_ok_button_labeled_terminate(self, qtbot):
        m = TerminateInProgressConfirmationModal(
            exchange="NSE", kind="intraday",
        )
        qtbot.addWidget(m)

        buttons = m.findChild(QDialogButtonBox)
        ok_btn = buttons.button(QDialogButtonBox.Ok)
        assert ok_btn.text() == "Terminate"


# ============================================================
# Accept / reject
# ============================================================

class TestAcceptReject:
    def test_accept_returns_accepted(self, qtbot):
        m = TerminateInProgressConfirmationModal(
            exchange="NSE", kind="intraday",
        )
        qtbot.addWidget(m)
        m.accept()
        assert m.result() == QDialog.Accepted

    def test_reject_returns_rejected(self, qtbot):
        m = TerminateInProgressConfirmationModal(
            exchange="NSE", kind="intraday",
        )
        qtbot.addWidget(m)
        m.reject()
        assert m.result() == QDialog.Rejected
