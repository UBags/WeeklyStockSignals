"""Tests for RefreshListingsConfirmationModal (Step 13d-3)."""
from __future__ import annotations

import pytest
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QLabel

from trip_ema.gui.views.widgets.refresh_listings_confirmation_modal import (
    RefreshListingsConfirmationModal,
)


class TestConstruction:

    def test_constructs_with_exchange(self, qtbot):
        modal = RefreshListingsConfirmationModal(exchange="NSE")
        qtbot.addWidget(modal)
        assert modal.exchange == "NSE"

    def test_window_title(self, qtbot):
        modal = RefreshListingsConfirmationModal(exchange="NSE")
        qtbot.addWidget(modal)
        assert "Refresh Listings" in modal.windowTitle()

    def test_is_modal(self, qtbot):
        modal = RefreshListingsConfirmationModal(exchange="NSE")
        qtbot.addWidget(modal)
        assert modal.isModal()


class TestContent:

    def test_title_contains_exchange(self, qtbot):
        modal = RefreshListingsConfirmationModal(exchange="NSE")
        qtbot.addWidget(modal)
        all_text = " ".join(
            label.text() for label in modal.findChildren(QLabel)
        )
        assert "NSE" in all_text
        assert "Refresh listings" in all_text

    def test_mentions_new_listings_delisted(self, qtbot):
        modal = RefreshListingsConfirmationModal(exchange="NSE")
        qtbot.addWidget(modal)
        all_text = " ".join(
            label.text() for label in modal.findChildren(QLabel)
        )
        assert "New listings" in all_text
        assert "Delisted" in all_text

    def test_mentions_duration(self, qtbot):
        modal = RefreshListingsConfirmationModal(exchange="NSE")
        qtbot.addWidget(modal)
        all_text = " ".join(
            label.text() for label in modal.findChildren(QLabel)
        )
        assert "duration" in all_text.lower()


class TestButtons:

    def test_proceed_button_present(self, qtbot):
        modal = RefreshListingsConfirmationModal(exchange="NSE")
        qtbot.addWidget(modal)
        bb = modal.findChild(QDialogButtonBox)
        assert bb is not None
        ok_btn = bb.button(QDialogButtonBox.Ok)
        assert ok_btn is not None
        assert "Proceed" in ok_btn.text()

    def test_cancel_is_default(self, qtbot):
        modal = RefreshListingsConfirmationModal(exchange="NSE")
        qtbot.addWidget(modal)
        bb = modal.findChild(QDialogButtonBox)
        cancel_btn = bb.button(QDialogButtonBox.Cancel)
        assert cancel_btn.isDefault()


class TestAcceptReject:

    def test_accept_returns_accepted(self, qtbot):
        modal = RefreshListingsConfirmationModal(exchange="NSE")
        qtbot.addWidget(modal)
        modal.accept()
        assert modal.result() == QDialog.Accepted

    def test_reject_returns_rejected(self, qtbot):
        modal = RefreshListingsConfirmationModal(exchange="NSE")
        qtbot.addWidget(modal)
        modal.reject()
        assert modal.result() == QDialog.Rejected
