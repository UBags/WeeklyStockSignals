"""Reusable Triple-EMA chart widget.

Same rendering code path as the existing SignalsView chart, but
parameterized for three call sites:

  ChartMode.MAIN — full chart used by SignalsView's right panel.
    - Layer toggles (EMAs / ATR / Regime / Signals) drive
      visibility via set_layer_visible(); the widget caches the
      payload and re-renders on toggle.
    - TradingView-style pinned crosshair labels on the right
      axis (price) and bottom axis (date).
    - Emits bar_under_cursor_changed(bar) / cursor_left() so the
      enclosing view can drive its side-panel bar-detail readout.
    - Has the volume sub-plot underneath.

  ChartMode.INDUSTRY — used by IndustryPanel for the top-of-left
    industry index chart.
    - All layers always visible; no toggles.
    - Crosshair lines but NO axis labels (per spec: minis only
      get crosshair, no labels). Industry chart matches mini
      look for consistency.
    - No volume sub-plot (industries don't have volume).
    - Title bar at top shows the industry name.

  ChartMode.MINI — used by IndustryPanel for the 12 thumbnails.
    - All layers visible (EMAs + candles + regime fill); no
      toggles, no signal markers (too noisy at thumb size).
    - Crosshair only, no labels.
    - Title bar shows the ticker symbol.
    - No volume.

Common payload shape: matches /v1/signals/.../history's
SignalHistoryPayload. The same dict structure is reused for both
ticker and industry charts so this widget doesn't branch on
source.

Crosshair model: mouse moves emit bar_under_cursor_changed for
MAIN. INDUSTRY and MINI handle their own crosshair update without
emitting (no consumers).
"""

from __future__ import annotations

import logging
from datetime import datetime
from enum import Enum
from typing import Optional

import pyqtgraph as pg
from PySide6.QtCore import Qt, QPointF, Signal
from PySide6.QtGui import QBrush, QColor, QPainter, QPen, QPicture
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from .signals_helpers import format_date_label, format_price_label

logger = logging.getLogger(__name__)


# --- palette (shared with signals.py — keep in sync) --- #
COLOR_BG = "#1e1e1e"
COLOR_AXIS = "#666"
COLOR_GRID = "#333"

COLOR_CANDLE_UP = "#1f9c5e"
COLOR_CANDLE_DOWN = "#c84a4a"

COLOR_EMA_MED = "#5cb6e8"
COLOR_EMA_LONG = "#ed8a3a"
COLOR_EMA_LONGEST = "#a26adb"

COLOR_BAND_INNER = "#777"
COLOR_BAND_MID = "#c8aa28"
COLOR_BAND_OUTER = "#c64242"

COLOR_REGIME_BULL = "#1f9c5e"
COLOR_REGIME_BEAR = "#c84a4a"
COLOR_REGIME_NEUTRAL = "#555"

COLOR_SIGNAL_BUY = "#1f9c5e"
COLOR_SIGNAL_SELL = "#c84a4a"

COLOR_AXIS_LABEL_BG = "#5cb6e8"
COLOR_AXIS_LABEL_FG = "#1e1e1e"


class ChartMode(Enum):
    MAIN = "main"
    INDUSTRY = "industry"
    MINI = "mini"


class _Candlestick(pg.GraphicsObject):
    """OHLC candle plot. Extracted from SignalsView's
    CandlestickItem; identical body. Bars indexed numerically
    along X so weekend / holiday gaps don't widen the chart."""

    def __init__(self, ohlc_points: list[tuple[int, float, float, float, float]]):
        super().__init__()
        self._points = ohlc_points
        self._picture = QPicture()
        self._generate_picture()

    def _generate_picture(self) -> None:
        p = QPainter(self._picture)
        try:
            width = 0.6
            for x, o, h, l, c in self._points:
                color = COLOR_CANDLE_UP if c >= o else COLOR_CANDLE_DOWN
                pen = QPen(QColor(color))
                pen.setWidthF(0.0)
                p.setPen(pen)
                p.setBrush(QBrush(QColor(color)))
                p.drawLine(QPointF(x, l), QPointF(x, h))
                top = max(o, c)
                bot = min(o, c)
                if abs(top - bot) < 1e-9:
                    p.drawLine(QPointF(x - width, o), QPointF(x + width, o))
                else:
                    p.drawRect(pg.QtCore.QRectF(
                        x - width, bot, 2 * width, top - bot,
                    ))
        finally:
            p.end()

    def paint(self, painter, option, widget=None) -> None:
        painter.drawPicture(0, 0, self._picture)

    def boundingRect(self):
        if not self._points:
            return pg.QtCore.QRectF()
        xs = [p[0] for p in self._points]
        lows = [p[3] for p in self._points]
        highs = [p[2] for p in self._points]
        return pg.QtCore.QRectF(
            min(xs) - 1, min(lows),
            max(xs) - min(xs) + 2, max(highs) - min(lows),
        )


class _DateAxis(pg.AxisItem):
    """Maps numeric bar indices to YYYY-MM tick strings."""

    def __init__(self, index_to_dt: list[datetime], *, single_mid: bool = False, **kwargs):
        super().__init__(**kwargs)
        self._index_to_dt = index_to_dt
        self._single_mid = single_mid

    def tickValues(self, minVal, maxVal, size):
        if self._single_mid and self._index_to_dt:
            # (d) For mini charts, show exactly 1 tick at the midpoint.
            mid = int((minVal + maxVal) / 2)
            mid = max(0, min(mid, len(self._index_to_dt) - 1))
            return [(1, [mid])]
        return super().tickValues(minVal, maxVal, size)

    def tickStrings(self, values, scale, spacing):
        out = []
        for v in values:
            i = int(round(v))
            if 0 <= i < len(self._index_to_dt):
                out.append(self._index_to_dt[i].strftime("%Y-%m"))
            else:
                out.append("")
        return out


class ChartWidget(QWidget):
    """Reusable triple-EMA chart. See module docstring for modes."""

    # Emitted whenever the cursor hovers a bar in MAIN mode. The
    # enclosing view connects this to its side-panel readout.
    # Non-MAIN modes don't emit (the signal still exists so the
    # connection is a no-op rather than an attribute error if
    # someone wires it up unconditionally).
    bar_under_cursor_changed = Signal(object)  # bar dict
    cursor_left = Signal()

    def __init__(
        self,
        mode: ChartMode,
        *,
        title: Optional[str] = None,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self._mode = mode
        self._title = title

        self._last_payload: Optional[dict] = None
        self._bars_by_index: dict[int, dict] = {}
        self._bar_index_to_dt: list[datetime] = []

        # Layer visibility — only meaningful for MAIN; INDUSTRY and
        # MINI always render all four (signal markers excluded for
        # mini to keep thumbnails readable).
        self._layer_visible = {
            "emas":   True,
            "atr":    True,
            "regime": True,
            "signals": True,
        }
        if mode == ChartMode.MINI:
            self._layer_visible["signals"] = False
            self._layer_visible["atr"] = False

        self._build_ui()

    # ---------- UI construction ---------- #

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(2)

        if self._title is not None:
            self._title_label = QLabel(self._title)
            self._title_label.setAlignment(Qt.AlignCenter)
            self._title_label.setStyleSheet(
                "color: #cccccc; font-size: 11px; font-weight: bold;"
            )
            outer.addWidget(self._title_label)
        else:
            self._title_label = None

        self._gl = pg.GraphicsLayoutWidget()
        self._gl.setBackground(COLOR_BG)

        # --- Price plot --- #
        self._price_plot = self._gl.addPlot(row=0, col=0)
        self._price_plot.showGrid(x=True, y=True, alpha=0.15)
        self._price_plot.getAxis("left").setTextPen(COLOR_AXIS)
        self._price_plot.getAxis("bottom").setTextPen(COLOR_AXIS)

        # Crosshair lines (all modes)
        crosshair_pen = pg.mkPen("#888", width=1, style=Qt.DashLine)
        self._vline_price = pg.InfiniteLine(
            angle=90, movable=False, pen=crosshair_pen,
        )
        self._hline_price = pg.InfiniteLine(
            angle=0, movable=False, pen=crosshair_pen,
        )
        self._price_plot.addItem(self._vline_price, ignoreBounds=True)
        self._price_plot.addItem(self._hline_price, ignoreBounds=True)
        self._vline_price.hide()
        self._hline_price.hide()

        # --- Volume plot (MAIN only) --- #
        if self._mode == ChartMode.MAIN:
            self._volume_plot = self._gl.addPlot(row=1, col=0)
            self._volume_plot.showGrid(x=True, y=True, alpha=0.1)
            self._volume_plot.getAxis("left").setTextPen(COLOR_AXIS)
            self._volume_plot.getAxis("bottom").setTextPen(COLOR_AXIS)
            self._volume_plot.setMaximumHeight(150)
            self._volume_plot.setXLink(self._price_plot)
            self._vline_vol = pg.InfiniteLine(
                angle=90, movable=False, pen=crosshair_pen,
            )
            self._volume_plot.addItem(self._vline_vol, ignoreBounds=True)
            self._vline_vol.hide()
            # Hide bottom axis on price plot (visible on volume below)
            self._price_plot.hideAxis("bottom")
        else:
            self._volume_plot = None
            self._vline_vol = None

        # --- TradingView-style pinned axis labels (all modes) --- #
        self._price_axis_label = pg.TextItem(
            text="", anchor=(1.0, 0.5),
            color=COLOR_AXIS_LABEL_FG,
            fill=pg.mkBrush(COLOR_AXIS_LABEL_BG),
            border=pg.mkPen(COLOR_AXIS_LABEL_BG),
        )
        self._price_plot.addItem(self._price_axis_label, ignoreBounds=True)
        self._price_axis_label.hide()

        self._date_axis_label = pg.TextItem(
            text="", anchor=(0.5, 1.0),
            color=COLOR_AXIS_LABEL_FG,
            fill=pg.mkBrush(COLOR_AXIS_LABEL_BG),
            border=pg.mkPen(COLOR_AXIS_LABEL_BG),
        )
        # Bottom axis lives on the volume plot in MAIN mode,
        # on the price plot otherwise.
        if self._volume_plot is not None:
            self._volume_plot.addItem(self._date_axis_label, ignoreBounds=True)
        else:
            self._price_plot.addItem(self._date_axis_label, ignoreBounds=True)
        self._date_axis_label.hide()

        # Stretch — give price more vertical space than volume
        if self._volume_plot is not None:
            self._gl.ci.layout.setRowStretchFactor(0, 4)
            self._gl.ci.layout.setRowStretchFactor(1, 1)

        # Mouse-move handler
        self._price_plot.scene().sigMouseMoved.connect(self._on_mouse_moved)

        outer.addWidget(self._gl)

    # ---------- Public API ---------- #

    def render(self, payload: Optional[dict]) -> None:
        """Render from a SignalHistoryPayload-shaped dict. None or
        an empty payload clears the chart AND the cached bar lookup
        so latest_bar() correctly returns None afterward."""
        self._last_payload = payload
        self._clear_plots()
        if not payload:
            # Wipe the index/dt caches so latest_bar() doesn't return
            # stale data after a clear. Without this, a SignalsView
            # that does render(None) followed by latest_bar() would
            # silently get the bar from the previous payload.
            self._bars_by_index = {}
            self._bar_index_to_dt = []
            return
        bars = payload.get("bars", [])
        if not bars:
            self._bars_by_index = {}
            self._bar_index_to_dt = []
            return

        self._bar_index_to_dt = [
            datetime.fromisoformat(b["bar_timestamp"])
            if isinstance(b["bar_timestamp"], str) else b["bar_timestamp"]
            for b in bars
        ]
        self._bars_by_index = {i: b for i, b in enumerate(bars)}

        # Bottom date axis — install on the volume plot for MAIN,
        # on the price plot otherwise.
        use_single_mid = (self._mode == ChartMode.MINI)
        date_axis = _DateAxis(
            self._bar_index_to_dt, orientation="bottom",
            single_mid=use_single_mid,
        )
        if self._volume_plot is not None:
            self._volume_plot.setAxisItems({"bottom": date_axis})
        else:
            self._price_plot.setAxisItems({"bottom": date_axis})

        # Layers, in z-order back to front
        if self._layer_visible["regime"]:
            self._render_regime_fill(bars)
        if self._mode == ChartMode.INDUSTRY:
            # Industry index: colored dots instead of candles
            self._render_industry_dots(bars)
        else:
            candles = [
                (i, b["open"], b["high"], b["low"], b["close"])
                for i, b in enumerate(bars)
            ]
            self._price_plot.addItem(_Candlestick(candles))
        if self._layer_visible["emas"]:
            self._render_emas(bars)
        if self._layer_visible["atr"]:
            self._render_atr_bands(bars)
        if self._layer_visible["signals"]:
            self._render_signal_markers(payload, bars)
        if self._volume_plot is not None:
            self._render_volume(bars)

        # Re-add crosshair items that survived clear via ignoreBounds
        # but were removed by plot.clear() above.
        self._price_plot.addItem(self._vline_price, ignoreBounds=True)
        self._price_plot.addItem(self._hline_price, ignoreBounds=True)
        if self._vline_vol is not None:
            self._volume_plot.addItem(self._vline_vol, ignoreBounds=True)
        self._price_plot.addItem(self._price_axis_label, ignoreBounds=True)
        if self._volume_plot is not None:
            self._volume_plot.addItem(self._date_axis_label, ignoreBounds=True)
        else:
            self._price_plot.addItem(self._date_axis_label, ignoreBounds=True)

        self._price_plot.enableAutoRange()
        if self._volume_plot is not None:
            self._volume_plot.enableAutoRange()

    def set_layer_visible(self, layer: str, visible: bool) -> None:
        """For MAIN mode toggles. Cached payload is re-rendered.
        No-op outside MAIN."""
        if self._mode != ChartMode.MAIN:
            return
        if layer not in self._layer_visible:
            return
        self._layer_visible[layer] = visible
        if self._last_payload is not None:
            self.render(self._last_payload)

    def latest_bar(self) -> Optional[dict]:
        """Return the most recent (rightmost) bar from the rendered
        payload, or None if no payload has been rendered. Used by
        SignalsView to default the bar-detail readout."""
        if not self._bars_by_index:
            return None
        last_idx = max(self._bars_by_index)
        return self._bars_by_index[last_idx]

    def set_title(self, title: str) -> None:
        """Update the title label. No-op when constructed without
        a title slot."""
        if self._title_label is not None:
            self._title_label.setText(title)

    # ---------- Internals ---------- #

    def _clear_plots(self) -> None:
        """Wipe both plots so a re-render starts from scratch."""
        self._price_plot.clear()
        if self._volume_plot is not None:
            self._volume_plot.clear()
        if self._vline_price is not None:
            self._vline_price.hide()
        if self._hline_price is not None:
            self._hline_price.hide()
        if self._vline_vol is not None:
            self._vline_vol.hide()
        self._price_axis_label.hide()
        self._date_axis_label.hide()

    def _render_regime_fill(self, bars: list[dict]) -> None:
        if not bars:
            return
        run_start = 0
        current = bars[0].get("display_bg") or "NEUTRAL"

        def regime_color(r: str) -> str:
            return {
                "BULLISH": COLOR_REGIME_BULL,
                "BEARISH": COLOR_REGIME_BEAR,
                "NEUTRAL": COLOR_REGIME_NEUTRAL,
            }.get(r, COLOR_REGIME_NEUTRAL)

        runs = []
        for i in range(1, len(bars)):
            r = bars[i].get("display_bg") or "NEUTRAL"
            if r != current:
                runs.append((run_start, i, current))
                run_start = i
                current = r
        runs.append((run_start, len(bars), current))

        # Mode-aware alpha: candle charts (MAIN/MINI) have rectangular
        # bodies competing for visual space, so a subtle 80/255 alpha
        # reads as a colored band between candles. INDUSTRY charts use
        # 3px scatter dots leaving most of the canvas empty; the same
        # 80 alpha disappears into the dark background and the regime
        # is effectively invisible. Bump to 140 (~55%) for INDUSTRY so
        # the green/red is clearly perceived without overpowering the
        # dot+EMA layers.
        regime_alpha = 140 if self._mode == ChartMode.INDUSTRY else 80
        for x0, x1, regime in runs:
            color = QColor(regime_color(regime))
            color.setAlpha(regime_alpha)
            region = pg.LinearRegionItem(
                values=[x0 - 0.5, x1 - 0.5],
                brush=pg.mkBrush(color),
                movable=False, pen=pg.mkPen(None),
            )
            region.setZValue(-10)
            self._price_plot.addItem(region)

    def _render_emas(self, bars: list[dict]) -> None:
        def line(field: str):
            xs, ys = [], []
            for i, b in enumerate(bars):
                v = b.get(field)
                if v is not None:
                    xs.append(i)
                    ys.append(v)
            return xs, ys
        for field, color in [
            ("med_ema", COLOR_EMA_MED),
            ("long_ema", COLOR_EMA_LONG),
            ("longest_ema", COLOR_EMA_LONGEST),
        ]:
            x, y = line(field)
            if x:
                self._price_plot.plot(x, y, pen=pg.mkPen(color, width=1.5))

    def _render_atr_bands(self, bars: list[dict]) -> None:
        def line(field: str):
            xs, ys = [], []
            for i, b in enumerate(bars):
                v = b.get(field)
                if v is not None:
                    xs.append(i)
                    ys.append(v)
            return xs, ys
        for field, color in [
            ("upper_inner", COLOR_BAND_INNER),
            ("lower_inner", COLOR_BAND_INNER),
            ("upper_mid", COLOR_BAND_MID),
            ("lower_mid", COLOR_BAND_MID),
            ("upper_outer", COLOR_BAND_OUTER),
            ("lower_outer", COLOR_BAND_OUTER),
        ]:
            x, y = line(field)
            if x:
                style = Qt.DashLine if "outer" in field else Qt.SolidLine
                self._price_plot.plot(
                    x, y, pen=pg.mkPen(QColor(color), width=1.0, style=style),
                )

    def _render_signal_markers(self, payload: dict, bars: list[dict]) -> None:
        events = payload.get("signal_events", [])
        if not events:
            return
        ts_to_idx = {b["bar_timestamp"]: i for i, b in enumerate(bars)}
        for e in events:
            i = ts_to_idx.get(e["bar_timestamp"])
            if i is None:
                continue
            bar = bars[i]
            signal = e["signal"].upper()
            offset = (bar["high"] - bar["low"]) * 0.5
            # BUY / EXIT_FROM_SELL: green ▲ below candle
            if signal in ("BUY", "EXIT_FROM_SELL"):
                y = bar["low"] - offset
                arrow = pg.ArrowItem(
                    pos=(i, y), angle=90,
                    headLen=12, tipAngle=40,
                    brush=COLOR_SIGNAL_BUY,
                    pen=pg.mkPen(COLOR_SIGNAL_BUY),
                )
                self._price_plot.addItem(arrow)
            # SELL / EXIT_FROM_BUY: red ▼ above candle
            elif signal in ("SELL", "EXIT_FROM_BUY"):
                y = bar["high"] + offset
                arrow = pg.ArrowItem(
                    pos=(i, y), angle=-90,
                    headLen=12, tipAngle=40,
                    brush=COLOR_SIGNAL_SELL,
                    pen=pg.mkPen(COLOR_SIGNAL_SELL),
                )
                self._price_plot.addItem(arrow)

    def _render_volume(self, bars: list[dict]) -> None:
        x, y, brushes = [], [], []
        for i, b in enumerate(bars):
            x.append(i)
            y.append(b.get("volume", 0))
            up = b["close"] >= b["open"]
            color = QColor(COLOR_CANDLE_UP if up else COLOR_CANDLE_DOWN)
            color.setAlpha(180)
            brushes.append(color)
        if x:
            self._volume_plot.addItem(pg.BarGraphItem(
                x=x, height=y, width=0.7,
                brushes=brushes, pen=pg.mkPen(None),
            ))

    def _render_industry_dots(self, bars: list[dict]) -> None:
        """Render the industry index as colored scatter dots.

        Each point is the weekly close value, colored green if
        close > previous close, red otherwise. Size = 3px.
        Industry indices are synthetic single-value series, so
        candles are misleading — dots show the actual data shape.
        """
        if not bars:
            return
        xs, ys, brushes = [], [], []
        prev_close = None
        for i, b in enumerate(bars):
            c = b["close"]
            xs.append(i)
            ys.append(c)
            if prev_close is not None and c >= prev_close:
                brushes.append(pg.mkBrush(COLOR_CANDLE_UP))
            else:
                brushes.append(pg.mkBrush(COLOR_CANDLE_DOWN))
            prev_close = c
        scatter = pg.ScatterPlotItem(
            x=xs, y=ys, size=3, pen=pg.mkPen(None), brush=brushes,
        )
        self._price_plot.addItem(scatter)

    # ---------- Crosshair / mouse ---------- #

    def leaveEvent(self, event):
        """Hide crosshair when the mouse exits the widget bounds.

        Fixes the fast-mouse trail bug on the IndustryPanel mini grid:
        sigMouseMoved doesn't deliver a final out-of-bounds event when
        the cursor crosses directly into a neighbor ChartWidget, so
        without this hook the prior chart's crosshair lines and floating
        price/date labels stay frozen at the last on-plot position.
        Qt's widget-level leaveEvent fires reliably in that case.
        """
        self._hide_crosshair()
        if self._mode == ChartMode.MAIN:
            self.cursor_left.emit()
        super().leaveEvent(event)

    def _on_mouse_moved(self, pos) -> None:
        if not self._last_payload or not self._bars_by_index:
            return
        if not self._price_plot.sceneBoundingRect().contains(pos):
            self._hide_crosshair()
            if self._mode == ChartMode.MAIN:
                self.cursor_left.emit()
            return
        mouse_point = self._price_plot.vb.mapSceneToView(pos)
        x_idx = int(round(mouse_point.x()))
        if x_idx < 0 or x_idx >= len(self._bars_by_index):
            self._hide_crosshair()
            if self._mode == ChartMode.MAIN:
                self.cursor_left.emit()
            return

        bar = self._bars_by_index[x_idx]
        dt = self._bar_index_to_dt[x_idx]

        self._vline_price.setPos(x_idx)
        self._hline_price.setPos(mouse_point.y())
        self._vline_price.show()
        self._hline_price.show()
        if self._vline_vol is not None:
            self._vline_vol.setPos(x_idx)
            self._vline_vol.show()

        if self._mode == ChartMode.MAIN:
            # Axis labels
            price_x_range = self._price_plot.vb.viewRange()[0]
            vol_y_range = self._volume_plot.vb.viewRange()[1]
            self._price_axis_label.setPos(
                price_x_range[1], mouse_point.y(),
            )
            self._price_axis_label.setText(
                format_price_label(mouse_point.y())
            )
            self._price_axis_label.show()
            self._date_axis_label.setPos(x_idx, vol_y_range[0])
            self._date_axis_label.setText(format_date_label(dt))
            self._date_axis_label.show()
            self.bar_under_cursor_changed.emit(bar)
        else:
            # MINI / INDUSTRY: same floating labels, positioned on
            # the price plot (no volume sub-plot in these modes).
            price_x_range = self._price_plot.vb.viewRange()[0]
            price_y_range = self._price_plot.vb.viewRange()[1]
            self._price_axis_label.setPos(
                price_x_range[1], mouse_point.y(),
            )
            self._price_axis_label.setText(
                format_price_label(mouse_point.y())
            )
            self._price_axis_label.show()
            self._date_axis_label.setPos(x_idx, price_y_range[0])
            self._date_axis_label.setText(format_date_label(dt))
            self._date_axis_label.show()

    def _hide_crosshair(self) -> None:
        self._vline_price.hide()
        self._hline_price.hide()
        if self._vline_vol is not None:
            self._vline_vol.hide()
        self._price_axis_label.hide()
        self._date_axis_label.hide()