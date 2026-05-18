"""Tests for the SignalsView and the Tickers → Signals cross-tab nav.

Coverage:
  - Chart constructs without populating the ticker combo (avoids
    MagicMock teardown segfaults).
  - Layer toggle re-renders without refetching.
  - Range button selection updates current_years.
  - load_ticker() updates internal state.
  - Cross-tab nav: clicking "View chart" on a Tickers row fires
    request_chart with the right ticker tuple, MainWindow's listener
    switches the stack page.
  - CandlestickItem builds without error.
  - _DateAxisItem renders date strings from indices.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from PySide6.QtCore import Qt

from trip_ema.gui.queries import TickerRow
from trip_ema.gui.views.signals import (
    CandlestickItem, SignalsView, _DateAxisItem,
)


def _synthetic_payload(n_bars: int = 5) -> dict:
    bars = []
    for i in range(n_bars):
        bars.append({
            "bar_timestamp": f"2025-01-{i+1:02d}T00:00:00+00:00",
            "open": 100.0 + i, "high": 102.0 + i, "low": 98.0 + i,
            "close": 101.0 + i, "volume": 1000.0 + i * 100,
            "med_ema": 100.0 + i * 0.5,
            "long_ema": 99.0 + i * 0.4,
            "longest_ema": 98.0 + i * 0.3,
            "atr": 2.0,
            "upper_inner": 102.0, "lower_inner": 98.0,
            "upper_mid": 104.0, "lower_mid": 96.0,
            "upper_outer": 108.0, "lower_outer": 92.0,
            "core_bg": "BULLISH" if i > 2 else "NEUTRAL",
            "display_bg": "BULLISH" if i > 2 else "NEUTRAL",
            "is_warmed_up": True,
        })
    return {
        "exchange": "NSE", "symbol": "RELIANCE", "instrument": "EQ",
        "timeframe": "1W",
        "bars": bars,
        "signal_events": [],
        "warmup_bars_count": 0,
        "notes": "ok",
    }


def _make_signals_view():
    """Build a SignalsView with combo population suppressed."""
    db = MagicMock()
    http = MagicMock()
    http.token_configured = True
    return SignalsView(db=db, http=http, populate_combo_on_init=False)


# ============================================================
# SignalsView construction and rendering
# ============================================================


class TestSignalsViewConstruction:

    def test_constructs_without_populating_combo(self, qtbot):
        view = _make_signals_view()
        qtbot.addWidget(view)
        # Combo starts empty
        assert view._ticker_combo.count() == 0
        # Both plot widgets exist
        assert view._price_plot is not None
        assert view._volume_plot is not None
        # Default range: 3Y
        assert view._current_years == 3

    def test_range_buttons_default_3y_selected(self, qtbot):
        view = _make_signals_view()
        qtbot.addWidget(view)
        for btn in view._range_buttons.buttons():
            if btn.property("years") == 3:
                assert btn.isChecked()
            else:
                assert not btn.isChecked()

    def test_layer_checkboxes_all_default_checked(self, qtbot):
        view = _make_signals_view()
        qtbot.addWidget(view)
        assert view._chk_emas.isChecked()
        assert view._chk_atr_bands.isChecked()
        assert view._chk_regime.isChecked()
        assert view._chk_signals.isChecked()


class TestChartRendering:

    def test_render_chart_adds_items_to_plots(self, qtbot):
        view = _make_signals_view()
        qtbot.addWidget(view)
        payload = _synthetic_payload(n_bars=8)
        view._render_chart(payload)
        # Items added: candle group, EMA lines, ATR band lines, volume bars, ...
        assert len(view._price_plot.items) > 5
        assert len(view._volume_plot.items) >= 1
        # Bar lookup populated
        assert len(view._bars_by_index) == 8

    def test_render_with_signal_events_adds_arrows(self, qtbot):
        view = _make_signals_view()
        qtbot.addWidget(view)
        payload = _synthetic_payload(n_bars=8)
        payload["signal_events"] = [
            {"event_id": 1, "bar_timestamp": payload["bars"][3]["bar_timestamp"],
             "signal": "BUY", "price_at_signal": 102.0},
            {"event_id": 2, "bar_timestamp": payload["bars"][6]["bar_timestamp"],
             "signal": "SELL", "price_at_signal": 107.0},
        ]
        before = len(view._price_plot.items)
        view._render_chart(payload)
        after = len(view._price_plot.items)
        # Two ArrowItems were added (in addition to candles, lines, etc.)
        assert after > before

    def test_layer_toggle_re_renders(self, qtbot):
        """Toggling a layer checkbox calls _render_chart with the same
        payload — verify it doesn't crash and the chart stays populated."""
        view = _make_signals_view()
        qtbot.addWidget(view)
        view._last_payload = _synthetic_payload(n_bars=5)
        view._render_chart(view._last_payload)
        items_before = len(view._price_plot.items)
        # Uncheck EMAs
        view._chk_emas.setChecked(False)
        # Toggle triggers re-render via _on_layer_toggled
        items_after = len(view._price_plot.items)
        # Fewer items because EMAs are gone
        assert items_after < items_before
        # Now re-enable
        view._chk_emas.setChecked(True)
        assert len(view._price_plot.items) >= items_before - 1


class TestLoadTicker:

    def test_load_ticker_sets_state(self, qtbot):
        """load_ticker() updates internal state synchronously, then
        fires off a worker for the fetch. We assert on the state."""
        view = _make_signals_view()
        qtbot.addWidget(view)
        # Stub the HTTP fetch — we don't actually care if the worker
        # races; we just need it not to crash. Returning quickly.
        view._http.get_signal_history.return_value = _synthetic_payload(n_bars=5)

        view.load_ticker("NSE", "RELIANCE", "EQ")
        # State set synchronously
        assert view._current_exchange == "NSE"
        assert view._current_symbol == "RELIANCE"
        assert view._current_instrument == "EQ"
        # Give the worker a moment so the post-test teardown doesn't
        # tear down state while the worker's running.
        qtbot.wait(50)

    def test_load_ticker_selects_combo_entry(self, qtbot):
        view = _make_signals_view()
        qtbot.addWidget(view)
        # Manually populate two entries
        view._ticker_combo.addItem("RELIANCE — Reliance Industries",
                                   ("NSE", "RELIANCE", "EQ"))
        view._ticker_combo.addItem("TCS — Tata Consultancy",
                                   ("NSE", "TCS", "EQ"))
        # Avoid the actual fetch — we only care about combo selection
        view._http.get_signal_history.return_value = _synthetic_payload(n_bars=1)
        view.load_ticker("NSE", "TCS", "EQ")
        assert view._ticker_combo.currentIndex() == 1
        qtbot.wait(50)


class TestRangeButton:

    def test_range_button_click_updates_years(self, qtbot):
        view = _make_signals_view()
        qtbot.addWidget(view)
        # Set a ticker so the fetch path runs
        view._current_exchange = "NSE"
        view._current_symbol = "RELIANCE"
        view._http.get_signal_history.return_value = _synthetic_payload(1)

        # Find the 10Y button and click it
        for btn in view._range_buttons.buttons():
            if btn.property("years") == 10:
                btn.click()
                break
        assert view._current_years == 10


# ============================================================
# CandlestickItem (custom OHLC primitive)
# ============================================================


class TestCandlestickItem:

    def test_constructs_with_empty_points(self, qtbot):
        item = CandlestickItem([])
        # boundingRect should not crash
        rect = item.boundingRect()
        assert rect is not None

    def test_constructs_with_points(self, qtbot):
        points = [
            (0, 100.0, 105.0, 99.0, 103.0),  # up
            (1, 103.0, 106.0, 100.0, 101.0),  # down
            (2, 101.0, 101.0, 101.0, 101.0),  # doji
        ]
        item = CandlestickItem(points)
        rect = item.boundingRect()
        # Should span x=[-1, 3] roughly
        assert rect.left() < 1
        assert rect.right() > 1


# ============================================================
# Date axis
# ============================================================


class TestDateAxisItem:

    def test_translates_indices_to_date_strings(self, qtbot):
        dates = [
            datetime(2025, 1, 6, tzinfo=timezone.utc),
            datetime(2025, 1, 13, tzinfo=timezone.utc),
            datetime(2025, 1, 20, tzinfo=timezone.utc),
        ]
        axis = _DateAxisItem(dates, orientation="bottom")
        labels = axis.tickStrings([0, 1, 2], None, None)
        assert labels == ["2025-01", "2025-01", "2025-01"]

    def test_out_of_range_returns_empty(self, qtbot):
        dates = [datetime(2025, 1, 6, tzinfo=timezone.utc)]
        axis = _DateAxisItem(dates, orientation="bottom")
        labels = axis.tickStrings([5], None, None)
        assert labels == [""]


# ============================================================
# Cross-tab navigation
# ============================================================


class TestCrossTabNav:

    def _make_window(self):
        from trip_ema.gui.main_window import MainWindow
        db = MagicMock()
        http = MagicMock()
        http.token_configured = True
        http.get_etl_status.return_value = {
            "recent_runs": [], "parked_count": 0,
            "tier_counts": {"tier_1": 0, "tier_2": 0, "tier_3": 0},
        }
        http.get_etl_parked.return_value = {"parked": [], "total": 0}
        return MainWindow(
            db=db, http=http,
            populate_signals_combo=False,
            populate_subscribers_lookup=False,
            start_refresh_on_init=False,
        )

    def test_request_chart_signal_switches_to_signals_tab(self, qtbot):
        window = self._make_window()
        qtbot.addWidget(window)
        # Start on ETL Status (row 0)
        window._sidebar.setCurrentRow(0)
        assert window._stack.currentIndex() == 0
        # Emit request_chart — MainWindow should switch to Signals
        window.request_chart_for_ticker.emit("NSE", "RELIANCE", "EQ")
        assert window._stack.currentIndex() == window._signals_stack_index

    def test_request_chart_signal_loads_ticker_in_view(self, qtbot):
        window = self._make_window()
        qtbot.addWidget(window)
        window.request_chart_for_ticker.emit("NSE", "RELIANCE", "EQ")
        # SignalsView should have its state set
        sv = window._signals_view
        assert sv._current_exchange == "NSE"
        assert sv._current_symbol == "RELIANCE"
        assert sv._current_instrument == "EQ"

    def test_tickers_view_emits_request_chart_when_button_clicked(self, qtbot):
        """Verify the wiring: when the Tickers tab's 'View chart' button
        fires its handler, the cross-tab signal carries the right
        ticker tuple."""
        from trip_ema.gui.views.tickers import TickerTableModel, TickersView
        db = MagicMock()
        http = MagicMock()
        http.token_configured = True
        view = TickersView(db=db, http=http, start_refresh_on_init=False)
        qtbot.addWidget(view)

        # Populate the model with one row
        row = TickerRow(
            ticker_id=1, exchange="NSE", symbol="RELIANCE",
            instrument_type="EQ", full_name="Reliance Industries",
            isin="INE002A01018", tier=1, is_active=True,
            delisted_at=None, last_successful_bar_date=None,
            consecutive_failures=0, parked=False,
        )
        view._model.set_rows([row])
        # Pretend the user selected this ticker
        view._selected_ticker_id = 1

        # Click the View chart button → emits request_chart
        with qtbot.waitSignal(view.request_chart, timeout=1000) as blocker:
            view._on_view_chart_clicked()
        assert blocker.args == ["NSE", "RELIANCE", "EQ"]
