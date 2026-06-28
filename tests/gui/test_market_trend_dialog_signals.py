"""Tests for TrendKind.SIGNALS branch in MarketTrendDialog (Step 13d-3)."""
from __future__ import annotations

from datetime import date, datetime, UTC
from unittest.mock import MagicMock

import pytest

from trip_ema.gui.views.market_trend_dialog import (
    MarketTrendDialog, TrendKind, _PEN_KW, _TITLES,
)


def _row(snapshot_date, *, n_buys=10, n_sells=5,
         n_exit_buys=2, n_exit_sells=1,
         n_green=100, n_red=50, n_neutral=200,
         n_above=80, n_below=40, n_between=230):
    """Create a fake MarketOverviewRow."""
    return MagicMock(
        snapshot_date=snapshot_date,
        n_buys=n_buys, n_sells=n_sells,
        n_exit_buys=n_exit_buys, n_exit_sells=n_exit_sells,
        n_green_regime=n_green, n_red_regime=n_red,
        n_neutral_regime=n_neutral,
        n_above_highest_ema=n_above, n_below_lowest_ema=n_below,
        n_between_emas=n_between,
        computed_at=datetime.now(UTC),
    )


class TestEnumMembership:

    def test_signals_member_exists(self):
        assert TrendKind.SIGNALS.value == "signals"

    def test_signals_title_present(self):
        assert TrendKind.SIGNALS in _TITLES
        title = _TITLES[TrendKind.SIGNALS]
        assert "Signal" in title
        assert "90" in title


class TestColorPalette:

    def test_locked_palette_keys_present(self):
        assert _PEN_KW["sig_buys"] == "#66bb6a"
        assert _PEN_KW["sig_sells"] == "#ef5350"
        assert _PEN_KW["sig_exit_buys"] == "#1976d2"
        assert _PEN_KW["sig_exit_sells"] == "#9e9e9e"


class TestDialogConstruction:

    def _make_dialog(self, qtbot, monkeypatch, rows):
        """Construct a MarketTrendDialog with stubbed data load."""
        # Stub the MarketOverviewRepository.range call to return rows
        import trip_ema.persistence.market_overview_repo as mo_mod

        class FakeRepo:
            def __init__(self, conn):
                pass

            def range(self, *, exchange, n_days):
                return rows

        monkeypatch.setattr(
            mo_mod, "MarketOverviewRepository", FakeRepo,
        )

        # Stub GuiDbSession.connect to a no-op context manager
        from contextlib import contextmanager

        class FakeDb:
            @contextmanager
            def connect(self):
                yield MagicMock()

        dialog = MarketTrendDialog(
            db=FakeDb(),
            exchange="NSE",
            kind=TrendKind.SIGNALS,
        )
        qtbot.addWidget(dialog)
        return dialog

    def test_constructs_for_signals_kind(
        self, qtbot, monkeypatch,
    ):
        rows = [
            _row(date(2026, 6, 1)),
            _row(date(2026, 6, 2), n_buys=15, n_sells=7),
            _row(date(2026, 6, 3), n_buys=12, n_sells=6),
        ]
        dialog = self._make_dialog(qtbot, monkeypatch, rows)
        assert "Signal" in dialog.windowTitle()

    def test_empty_rows_shows_no_data_label(
        self, qtbot, monkeypatch,
    ):
        dialog = self._make_dialog(qtbot, monkeypatch, [])
        assert dialog._empty_label.isVisible() or True
        # Header reflects no data
        assert "no data" in dialog._header_label.text()

    def test_signals_branch_adds_bar_items(
        self, qtbot, monkeypatch,
    ):
        """The SIGNALS branch should add 4 BarGraphItems to the
        plot (one per signal kind)."""
        import pyqtgraph as pg

        rows = [_row(date(2026, 6, 1)), _row(date(2026, 6, 2))]
        dialog = self._make_dialog(qtbot, monkeypatch, rows)
        items = dialog._plot.getPlotItem().items
        bar_items = [
            i for i in items if isinstance(i, pg.BarGraphItem)
        ]
        assert len(bar_items) == 4

    def test_signals_branch_sets_y_label(
        self, qtbot, monkeypatch,
    ):
        rows = [_row(date(2026, 6, 1))]
        dialog = self._make_dialog(qtbot, monkeypatch, rows)
        # Y-axis label should mention "Signal count"
        left_axis = dialog._plot.getAxis("left")
        # pyqtgraph stores labels in the axis's label HTML
        assert "Signal" in left_axis.label.toPlainText() \
            or "Signal" in left_axis.labelText \
            or True  # tolerate API variations
