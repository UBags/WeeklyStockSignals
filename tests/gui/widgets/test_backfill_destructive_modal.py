"""Tests for BackfillDestructiveModal + estimate_runtime_minutes
(Step 13c)."""
from __future__ import annotations

from datetime import date

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog

from trip_ema.gui.views.widgets.backfill_destructive_modal import (
    BackfillDestructiveModal, estimate_runtime_minutes,
)


def _build(qtbot, **overrides):
    defaults = dict(
        exchange="NSE",
        date_from=date(2024, 1, 1),
        date_to=date(2026, 6, 28),
        n_stocks=0,
        stocks_preview=None,
        n_historical_to_delete=12_345,
        n_overview_to_stale=200,
        refetch_bars=False,
        runtime_estimate_minutes=18,
    )
    defaults.update(overrides)
    modal = BackfillDestructiveModal(**defaults)
    qtbot.addWidget(modal)
    return modal


# ============================================================
# Modal renders without exec()
# ============================================================

class TestRenders:

    def test_constructs(self, qtbot):
        modal = _build(qtbot)
        assert modal.windowTitle() == "Confirm Backfill"

    def test_is_modal(self, qtbot):
        modal = _build(qtbot)
        assert modal.isModal() is True

    def test_all_active_scope_shows_ALL_keyword(self, qtbot):
        modal = _build(qtbot, n_stocks=0, stocks_preview=None)
        # The plan-summary label should mention "ALL active"
        from PySide6.QtWidgets import QLabel
        all_text = " ".join(
            lbl.text() for lbl in modal.findChildren(QLabel)
        )
        assert "ALL" in all_text

    def test_explicit_scope_shows_preview(self, qtbot):
        modal = _build(
            qtbot, n_stocks=3,
            stocks_preview=["RELIANCE", "TCS", "INFY"],
        )
        from PySide6.QtWidgets import QLabel
        all_text = " ".join(
            lbl.text() for lbl in modal.findChildren(QLabel)
        )
        assert "RELIANCE" in all_text
        assert "TCS" in all_text

    def test_preview_truncates_at_5(self, qtbot):
        modal = _build(
            qtbot, n_stocks=8,
            stocks_preview=[
                "A", "B", "C", "D", "E", "F", "G", "H",
            ],
        )
        from PySide6.QtWidgets import QLabel
        all_text = " ".join(
            lbl.text() for lbl in modal.findChildren(QLabel)
        )
        # First 5 shown
        assert "A" in all_text
        # Ellipsis marker shows
        assert "…" in all_text

    def test_displays_delete_count(self, qtbot):
        modal = _build(
            qtbot, n_historical_to_delete=99_999,
        )
        from PySide6.QtWidgets import QLabel
        all_text = " ".join(
            lbl.text() for lbl in modal.findChildren(QLabel)
        )
        # Number rendered with thousands separator
        assert "99,999" in all_text

    def test_displays_stale_count(self, qtbot):
        modal = _build(
            qtbot, n_overview_to_stale=42,
        )
        from PySide6.QtWidgets import QLabel
        all_text = " ".join(
            lbl.text() for lbl in modal.findChildren(QLabel)
        )
        assert "42" in all_text
        assert "stale" in all_text.lower()


# ============================================================
# Confirm / Cancel
# ============================================================

class TestConfirmCancel:

    def test_accept_returns_accepted(self, qtbot):
        modal = _build(qtbot)
        modal.accept()
        assert modal.result() == QDialog.Accepted

    def test_reject_returns_rejected(self, qtbot):
        modal = _build(qtbot)
        modal.reject()
        assert modal.result() == QDialog.Rejected


# ============================================================
# estimate_runtime_minutes
# ============================================================

class TestEstimateRuntime:

    def test_grows_with_ticker_count(self):
        small = estimate_runtime_minutes(
            n_tickers=100, refetch_bars=False,
        )
        big = estimate_runtime_minutes(
            n_tickers=2667, refetch_bars=False,
        )
        assert big > small

    def test_refetch_costs_more(self):
        without = estimate_runtime_minutes(
            n_tickers=1000, refetch_bars=False,
        )
        with_refetch = estimate_runtime_minutes(
            n_tickers=1000, refetch_bars=True,
        )
        assert with_refetch > without

    def test_floor_is_one_minute(self):
        # Zero tickers + no refetch: still rounds to at least 1
        result = estimate_runtime_minutes(
            n_tickers=0, refetch_bars=False,
        )
        assert result >= 1
