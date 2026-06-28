"""Tests for RecomputeTiersConfirmationModal (Step 13d-3)."""
from __future__ import annotations

import pytest
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QLabel

from trip_ema.gui.views.widgets.recompute_tiers_confirmation_modal import (
    RecomputeTiersConfirmationModal,
)


class TestConstruction:

    def test_constructs_with_exchange(self, qtbot):
        modal = RecomputeTiersConfirmationModal(exchange="NSE")
        qtbot.addWidget(modal)
        assert modal.exchange == "NSE"

    def test_window_title(self, qtbot):
        modal = RecomputeTiersConfirmationModal(exchange="NSE")
        qtbot.addWidget(modal)
        assert "Recompute Tiers" in modal.windowTitle()

    def test_is_modal(self, qtbot):
        modal = RecomputeTiersConfirmationModal(exchange="NSE")
        qtbot.addWidget(modal)
        assert modal.isModal()


class TestContent:

    def test_title_contains_exchange(self, qtbot):
        modal = RecomputeTiersConfirmationModal(exchange="NSE")
        qtbot.addWidget(modal)
        all_text = " ".join(
            label.text() for label in modal.findChildren(QLabel)
        )
        assert "NSE" in all_text
        assert "Recompute tiers" in all_text

    def test_mentions_adv_and_tiers(self, qtbot):
        modal = RecomputeTiersConfirmationModal(exchange="NSE")
        qtbot.addWidget(modal)
        all_text = " ".join(
            label.text() for label in modal.findChildren(QLabel)
        )
        assert "ADV" in all_text
        assert "Tier 1" in all_text
        assert "Tier 2" in all_text
        assert "Tier 3" in all_text

    def test_mentions_duration(self, qtbot):
        modal = RecomputeTiersConfirmationModal(exchange="NSE")
        qtbot.addWidget(modal)
        all_text = " ".join(
            label.text() for label in modal.findChildren(QLabel)
        )
        assert "duration" in all_text.lower()


class TestButtons:

    def test_proceed_button_present(self, qtbot):
        modal = RecomputeTiersConfirmationModal(exchange="NSE")
        qtbot.addWidget(modal)
        bb = modal.findChild(QDialogButtonBox)
        ok_btn = bb.button(QDialogButtonBox.Ok)
        assert "Proceed" in ok_btn.text()

    def test_cancel_is_default(self, qtbot):
        modal = RecomputeTiersConfirmationModal(exchange="NSE")
        qtbot.addWidget(modal)
        bb = modal.findChild(QDialogButtonBox)
        cancel_btn = bb.button(QDialogButtonBox.Cancel)
        assert cancel_btn.isDefault()


class TestAcceptReject:

    def test_accept(self, qtbot):
        modal = RecomputeTiersConfirmationModal(exchange="NSE")
        qtbot.addWidget(modal)
        modal.accept()
        assert modal.result() == QDialog.Accepted

    def test_reject(self, qtbot):
        modal = RecomputeTiersConfirmationModal(exchange="NSE")
        qtbot.addWidget(modal)
        modal.reject()
        assert modal.result() == QDialog.Rejected
