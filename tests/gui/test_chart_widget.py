"""Qt-aware tests for `trip_ema.gui.chart_widget.ChartWidget`.

These run under offscreen Qt (configured in tests/gui/conftest.py).
They cover the widget's externally-visible contract:

  - Each mode constructs without error.
  - render(payload) populates the cached state (bars indexed by
    position, datetime list parallel to it).
  - latest_bar() returns the rightmost bar in the rendered payload,
    or None when nothing has been rendered.
  - set_layer_visible() flips visibility in MAIN mode and is a no-op
    in the other modes.
  - MAIN mode emits bar_under_cursor_changed when the cursor is over
    a valid bar; INDUSTRY and MINI never emit.
  - render(None) clears the widget without crashing.
  - render({"bars": []}) is a clean no-op.
  - A second render() overwrites the first (used when the layer
    toggle re-renders).

Heavy rendering pixel correctness lives in the manual smoke tests
(screenshots from the offscreen plugin). These tests pin the
data-flow / signal contract.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from PySide6.QtCore import QPointF

from trip_ema.gui.chart_widget import ChartMode, ChartWidget


# ---------- bar fixture helper ---------- #


_T0 = datetime(2024, 1, 1, tzinfo=timezone.utc)


def _bar(week: int, *, regime: str = "BULLISH",
         close: float = 100.0, warmed: bool = True) -> dict:
    return {
        "bar_timestamp": (_T0 + timedelta(weeks=week)).isoformat(),
        "open": close - 1, "high": close + 1, "low": close - 2, "close": close,
        "volume": 100_000,
        "med_ema": close - 0.5,
        "long_ema": close - 1.0,
        "longest_ema": close - 1.5,
        "atr": 1.5,
        "upper_inner": close + 2, "lower_inner": close - 2,
        "upper_mid": close + 4,   "lower_mid":   close - 4,
        "upper_outer": close + 6, "lower_outer": close - 6,
        "display_bg": regime,
        "core_bg": regime,
        "is_warmed_up": warmed,
    }


def _payload(n: int = 20) -> dict:
    return {
        "exchange": "NSE", "symbol": "TEST", "instrument": "EQ",
        "timeframe": "1W",
        "bars": [_bar(i, close=100 + i) for i in range(n)],
        "signal_events": [],
        "warmup_bars_count": 0,
        "notes": "",
    }


# ---------- construction ---------- #


class TestConstruction:

    def test_main_mode_constructs(self, qtbot):
        w = ChartWidget(ChartMode.MAIN)
        qtbot.addWidget(w)
        assert w._mode == ChartMode.MAIN
        # MAIN has the volume sub-plot and axis labels
        assert w._volume_plot is not None
        assert w._price_axis_label is not None
        assert w._date_axis_label is not None

    def test_industry_mode_constructs_with_title(self, qtbot):
        w = ChartWidget(ChartMode.INDUSTRY, title="Oil & Gas")
        qtbot.addWidget(w)
        assert w._mode == ChartMode.INDUSTRY
        # INDUSTRY has no volume, no axis labels
        assert w._volume_plot is None
        assert w._price_axis_label is None
        assert w._date_axis_label is None
        assert w._title_label is not None
        assert w._title_label.text() == "Oil & Gas"

    def test_mini_mode_constructs_with_title(self, qtbot):
        w = ChartWidget(ChartMode.MINI, title="RELIANCE")
        qtbot.addWidget(w)
        assert w._mode == ChartMode.MINI
        assert w._volume_plot is None
        # MINI suppresses signal markers and ATR bands by default
        assert w._layer_visible["signals"] is False
        assert w._layer_visible["atr"] is False
        # EMAs + regime fill remain on
        assert w._layer_visible["emas"] is True
        assert w._layer_visible["regime"] is True

    def test_no_title_no_label(self, qtbot):
        w = ChartWidget(ChartMode.MAIN, title=None)
        qtbot.addWidget(w)
        assert w._title_label is None


# ---------- render() ---------- #


class TestRender:

    def test_render_none_clears(self, qtbot):
        w = ChartWidget(ChartMode.MAIN)
        qtbot.addWidget(w)
        w.render(_payload(5))
        assert w.latest_bar() is not None
        w.render(None)
        # After clearing, the cached state should be gone — latest_bar
        # returns None for an empty cache.
        assert w._last_payload is None

    def test_render_empty_bars_safe(self, qtbot):
        """A payload that exists but has no bars should not crash."""
        w = ChartWidget(ChartMode.MAIN)
        qtbot.addWidget(w)
        w.render({"bars": [], "signal_events": []})
        assert w.latest_bar() is None

    def test_render_populates_index_and_dt_lists(self, qtbot):
        w = ChartWidget(ChartMode.INDUSTRY, title="X")
        qtbot.addWidget(w)
        payload = _payload(7)
        w.render(payload)
        assert len(w._bars_by_index) == 7
        assert len(w._bar_index_to_dt) == 7
        # First and last bar timestamps round-trip via ISO parsing
        assert w._bar_index_to_dt[0] == _T0
        assert w._bar_index_to_dt[6] == _T0 + timedelta(weeks=6)

    def test_second_render_overwrites_first(self, qtbot):
        w = ChartWidget(ChartMode.MAIN)
        qtbot.addWidget(w)
        w.render(_payload(10))
        assert len(w._bars_by_index) == 10
        w.render(_payload(5))
        assert len(w._bars_by_index) == 5


class TestLatestBar:

    def test_latest_bar_returns_last_in_payload(self, qtbot):
        w = ChartWidget(ChartMode.MAIN)
        qtbot.addWidget(w)
        w.render(_payload(8))
        latest = w.latest_bar()
        assert latest is not None
        assert latest["close"] == 100 + 7   # bars: 100..107

    def test_latest_bar_none_before_render(self, qtbot):
        w = ChartWidget(ChartMode.MAIN)
        qtbot.addWidget(w)
        assert w.latest_bar() is None

    def test_latest_bar_none_after_render_none(self, qtbot):
        w = ChartWidget(ChartMode.MAIN)
        qtbot.addWidget(w)
        w.render(_payload(3))
        w.render(None)
        assert w.latest_bar() is None


# ---------- set_layer_visible ---------- #


class TestLayerToggle:

    def test_main_mode_toggle_flips_visibility(self, qtbot):
        w = ChartWidget(ChartMode.MAIN)
        qtbot.addWidget(w)
        w.render(_payload(5))
        assert w._layer_visible["emas"] is True
        w.set_layer_visible("emas", False)
        assert w._layer_visible["emas"] is False
        w.set_layer_visible("emas", True)
        assert w._layer_visible["emas"] is True

    def test_industry_mode_toggle_is_noop(self, qtbot):
        """INDUSTRY has no toggles; set_layer_visible must be a
        no-op so accidental wiring doesn't mutate state."""
        w = ChartWidget(ChartMode.INDUSTRY, title="X")
        qtbot.addWidget(w)
        w.render(_payload(5))
        before = dict(w._layer_visible)
        w.set_layer_visible("emas", False)
        assert w._layer_visible == before

    def test_mini_mode_toggle_is_noop(self, qtbot):
        w = ChartWidget(ChartMode.MINI, title="Y")
        qtbot.addWidget(w)
        w.render(_payload(5))
        before = dict(w._layer_visible)
        w.set_layer_visible("emas", False)
        assert w._layer_visible == before

    def test_unknown_layer_key_ignored(self, qtbot):
        w = ChartWidget(ChartMode.MAIN)
        qtbot.addWidget(w)
        before = dict(w._layer_visible)
        w.set_layer_visible("not_a_real_layer", False)
        assert w._layer_visible == before


# ---------- title API ---------- #


class TestTitle:

    def test_set_title_updates_label(self, qtbot):
        w = ChartWidget(ChartMode.MINI, title="old")
        qtbot.addWidget(w)
        w.set_title("new")
        assert w._title_label.text() == "new"

    def test_set_title_noop_when_no_label(self, qtbot):
        w = ChartWidget(ChartMode.MAIN, title=None)
        qtbot.addWidget(w)
        # Must not crash even though there's no label slot
        w.set_title("anything")


# ---------- crosshair signals ---------- #


class TestSignals:
    """The MAIN mode emits bar_under_cursor_changed when the cursor
    is over a valid bar. The signal contract is what IndustryPanel
    and SignalsView depend on; pin it down here.

    Driving the actual sigMouseMoved is fiddly under offscreen Qt
    (the scene needs to be visible). These tests reach into the
    _on_mouse_moved handler directly with a synthesized mouse
    position to verify the signal fires.
    """

    def test_main_emits_bar_under_cursor_for_valid_position(
        self, qtbot, monkeypatch,
    ):
        """The MAIN handler emits the signal when the cursor is over a
        valid bar. We monkey-patch sceneBoundingRect to bypass the
        offscreen-Qt layout flakiness (the real rect is only correct
        after a paint event, which doesn't reliably happen in tests)."""
        w = ChartWidget(ChartMode.MAIN)
        qtbot.addWidget(w)
        w.render(_payload(20))

        # Force the geometry check to pass and the view → data mapping
        # to land on bar index 5 regardless of actual widget layout.
        from PySide6.QtCore import QRectF
        big = QRectF(-1e9, -1e9, 2e9, 2e9)
        monkeypatch.setattr(
            w._price_plot, "sceneBoundingRect", lambda: big,
        )
        monkeypatch.setattr(
            w._price_plot.vb, "mapSceneToView",
            lambda _pos: QPointF(5, 100),
        )

        received = []
        w.bar_under_cursor_changed.connect(received.append)
        w._on_mouse_moved(QPointF(0, 0))

        assert len(received) == 1
        assert received[0]["close"] == 100 + 5

    def test_industry_never_emits(self, qtbot, monkeypatch):
        w = ChartWidget(ChartMode.INDUSTRY, title="X")
        qtbot.addWidget(w)
        w.render(_payload(20))
        from PySide6.QtCore import QRectF
        big = QRectF(-1e9, -1e9, 2e9, 2e9)
        monkeypatch.setattr(w._price_plot, "sceneBoundingRect", lambda: big)
        monkeypatch.setattr(
            w._price_plot.vb, "mapSceneToView",
            lambda _pos: QPointF(5, 100),
        )

        received = []
        w.bar_under_cursor_changed.connect(received.append)
        w._on_mouse_moved(QPointF(0, 0))
        assert received == []

    def test_mini_never_emits(self, qtbot, monkeypatch):
        w = ChartWidget(ChartMode.MINI, title="Y")
        qtbot.addWidget(w)
        w.render(_payload(20))
        from PySide6.QtCore import QRectF
        big = QRectF(-1e9, -1e9, 2e9, 2e9)
        monkeypatch.setattr(w._price_plot, "sceneBoundingRect", lambda: big)
        monkeypatch.setattr(
            w._price_plot.vb, "mapSceneToView",
            lambda _pos: QPointF(5, 100),
        )

        received = []
        w.bar_under_cursor_changed.connect(received.append)
        w._on_mouse_moved(QPointF(0, 0))
        assert received == []

    def test_main_emits_cursor_left_when_cursor_outside_chart(self, qtbot):
        """When the cursor leaves the chart's bounding rect, MAIN
        should emit cursor_left so the SignalsView reverts the bar
        detail readout to the latest bar."""
        w = ChartWidget(ChartMode.MAIN)
        qtbot.addWidget(w)
        w.render(_payload(20))
        w.resize(800, 600)
        w.show()
        qtbot.waitExposed(w)

        left_emissions = []
        w.cursor_left.connect(lambda: left_emissions.append(True))

        # A scene position far outside the price plot's bounding rect.
        # The handler hits the "not in chart" branch and emits.
        far_pos = QPointF(-1_000_000, -1_000_000)
        w._on_mouse_moved(far_pos)
        assert left_emissions == [True]


# ---------- defensive: no payload, no crash ---------- #


class TestNoPayloadSafety:

    def test_mouse_move_before_render_is_noop(self, qtbot):
        """The user can move the cursor over the chart area before
        any data has been loaded. Handler must not raise."""
        w = ChartWidget(ChartMode.MAIN)
        qtbot.addWidget(w)
        w.show()
        qtbot.waitExposed(w)
        # No payload set yet; handler should early-return
        w._on_mouse_moved(QPointF(100, 100))   # must not raise

    def test_set_layer_visible_before_render_is_safe(self, qtbot):
        """Toggling a layer before any payload is rendered should
        not crash and should be a clean no-op."""
        w = ChartWidget(ChartMode.MAIN)
        qtbot.addWidget(w)
        w.set_layer_visible("emas", False)   # must not raise
        assert w._layer_visible["emas"] is False