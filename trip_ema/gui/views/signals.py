"""Signal viewer / chart view (Tab C — Slice 3).

Layout:
  ┌─────────────────────────────────────────────────────────────┐
  │ Ticker: [ RELIANCE        ▾]   [1Y] [3Y] [5Y] [10Y] [All]   │
  │ Layers: ☑EMAs  ☑ATR bands  ☑Regime fill  ☑Signals  [Refresh]│
  ├─────────────────────────────────────────────────────────────┤
  │                                          ┌─OHLC readout──┐  │
  │  ┌─candles + overlays──────────────┐    │ Date  W of...  │  │
  │  │                                 │    │ O ...          │  │
  │  │     [chart, 75% height]         │    │ H ...          │  │
  │  │                                 │    │ L ...          │  │
  │  │                                 │    │ C ...          │  │
  │  │                                 │    │ Vol ...        │  │
  │  ├─volume───────────────────────────┤    │ Regime ...     │  │
  │  │   [volume bars, 25% height]     │    └────────────────┘  │
  │  └─────────────────────────────────┘                        │
  └─────────────────────────────────────────────────────────────┘

Built on pyqtgraph with a custom CandlestickItem (pyqtgraph has no
built-in OHLC). Everything else (EMA lines, ATR bands, volume bars)
uses pyqtgraph's stock primitives.

The chart re-fetches `/v1/signals/{exchange}/{symbol}/history` when:
  - the ticker combo changes
  - any time-range button is clicked
  - the [Refresh] button is clicked
  - load_ticker() is called externally (cross-tab nav from Tickers)

The chart does NOT poll automatically — historical data doesn't
change minute-to-minute.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

import pyqtgraph as pg
from PySide6.QtCore import Qt, QPointF, Signal
from PySide6.QtGui import (
    QBrush, QColor, QPainter, QPen, QPicture, QStandardItem,
    QStandardItemModel,
)
from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QComboBox, QGroupBox, QHBoxLayout,
    QLabel, QMessageBox, QPushButton, QVBoxLayout, QWidget,
)
from sqlalchemy import text  # noqa: F401 — retained for future inline SQL

from ..db import GuiDbSession
from ..http_client import ApiError, TripEmaHttpClient
from ..queries import ticker_universe_with_recent_signals
from ..signals_helpers import (
    format_date_label,
    format_price_label,
    partition_ticker_rows,
)
from ..workers import submit
from .signal_generation_panel import SignalGenerationPanel

logger = logging.getLogger(__name__)


# Dark-theme palette. Matches the sidebar styling so the chart reads
# as part of the same UI.
COLOR_BG = "#1e1e1e"
COLOR_AXIS = "#666"
COLOR_GRID = "#333"

COLOR_CANDLE_UP = "#1f9c5e"     # green
COLOR_CANDLE_DOWN = "#c84a4a"   # red

COLOR_EMA_MED = "#5cb6e8"       # light blue
COLOR_EMA_LONG = "#ed8a3a"      # orange
COLOR_EMA_LONGEST = "#a26adb"   # purple

COLOR_BAND_INNER = "#777"
COLOR_BAND_MID = "#c8aa28"
COLOR_BAND_OUTER = "#c64242"

COLOR_REGIME_BULL = "#1f9c5e"
COLOR_REGIME_BEAR = "#c84a4a"
COLOR_REGIME_NEUTRAL = "#555"

COLOR_SIGNAL_BUY = "#1f9c5e"
COLOR_SIGNAL_SELL = "#c84a4a"

# Phase A:
# Grey background for the 'no recent signal' section of the ticker
# combo dropdown. Light enough to be readable as a quiet differentiator
# without overpowering the row text.
_DROPDOWN_GREY_BG = "#f0f0f0"
# TradingView-style pinned axis label tab. Same blue as the med EMA
# so the chart palette stays cohesive.
COLOR_AXIS_LABEL_BG = "#5cb6e8"
COLOR_AXIS_LABEL_FG = "#1e1e1e"   # dark text on the blue tab


# Configure pyqtgraph globally — we want dark background and antialiasing.
pg.setConfigOption("background", COLOR_BG)
pg.setConfigOption("foreground", "#cccccc")
pg.setConfigOption("antialias", True)


class CandlestickItem(pg.GraphicsObject):
    """Custom OHLC candle plot.

    pyqtgraph doesn't ship one (its examples have a half-finished
    sample). We do the minimum: rectangle body from open to close,
    line from high to low, color by direction.

    Bars are indexed numerically along the X axis (0, 1, 2, ...) so
    weekend/holiday gaps don't widen the chart. The DateAxisItem
    above the X axis translates indices back to dates via a lookup.
    """

    def __init__(self, ohlc_points: list[tuple[int, float, float, float, float]]):
        """ohlc_points: list of (x_index, open, high, low, close)."""
        super().__init__()
        self._points = ohlc_points
        self._picture = QPicture()
        self._generate_picture()

    def _generate_picture(self) -> None:
        p = QPainter(self._picture)
        try:
            width = 0.6  # candle body half-width, in x-units (one bar = 1)
            for x, o, h, l, c in self._points:
                color = COLOR_CANDLE_UP if c >= o else COLOR_CANDLE_DOWN
                pen = QPen(QColor(color))
                pen.setWidthF(0.0)  # cosmetic
                p.setPen(pen)
                p.setBrush(QBrush(QColor(color)))
                # The wick (high-low)
                p.drawLine(QPointF(x, l), QPointF(x, h))
                # The body (open-close)
                top = max(o, c)
                bot = min(o, c)
                if abs(top - bot) < 1e-9:
                    # Doji: no body, draw a thin horizontal line
                    p.drawLine(QPointF(x - width, o), QPointF(x + width, o))
                else:
                    rect_h = top - bot
                    p.drawRect(pg.QtCore.QRectF(x - width, bot, 2 * width, rect_h))
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


class SignalsView(QWidget):
    """Slice 3's chart tab. See module docstring for layout."""

    def __init__(
        self,
        db: GuiDbSession,
        http: TripEmaHttpClient,
        *,
        parent: Optional[QWidget] = None,
        populate_combo_on_init: bool = True,
    ):
        super().__init__(parent)
        self._db = db
        self._http = http
        # Captured for _build_ui — the signal-generation panel uses
        # this same flag to decide whether to start its metadata
        # polling timer.
        self._populate_combo_on_init = populate_combo_on_init

        # State carried across refetches
        self._current_exchange: Optional[str] = None
        self._current_symbol: Optional[str] = None
        self._current_instrument: Optional[str] = None
        self._current_years: int = 3
        self._last_payload: Optional[dict] = None

        self._build_ui()
        if populate_combo_on_init:
            self._populate_ticker_combo()

    # ---------- UI ---------- #

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)
        outer.setSpacing(8)

        outer.addLayout(self._build_selector_row())
        outer.addLayout(self._build_layer_row())
        outer.addLayout(self._build_chart_row(), 1)

        # Phase 15c: the signal-generation panel sits below the chart
        # area. Embedded as a child widget so the same SignalsView
        # tab now hosts both "look at the chart" and "regenerate the
        # backtest file" responsibilities. Constructed lazily on
        # populate_combo_on_init so tests that build the view in
        # headless minimal mode don't end up polling the network.
        self._signal_generation_panel = SignalGenerationPanel(
            self._http, parent=self,
            start_polling_on_init=self._populate_combo_on_init,
        )
        outer.addWidget(self._signal_generation_panel)

    def _build_selector_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.addWidget(QLabel("Ticker:"))

        self._ticker_combo = QComboBox()
        self._ticker_combo.setEditable(True)
        self._ticker_combo.setInsertPolicy(QComboBox.NoInsert)
        self._ticker_combo.setMinimumWidth(220)
        self._ticker_combo.setMaxVisibleItems(20)
        # currentIndexChanged fires when the user picks from the list;
        # we don't react to free-text edits (search-as-you-type filtering
        # is via the combo's built-in completer).
        self._ticker_combo.activated.connect(self._on_ticker_combo_activated)
        row.addWidget(self._ticker_combo)

        row.addSpacing(20)

        # Time-range button group
        self._range_buttons = QButtonGroup(self)
        self._range_buttons.setExclusive(True)
        for years, label in [(1, "1Y"), (3, "3Y"), (5, "5Y"), (10, "10Y"), (20, "All")]:
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setProperty("years", years)
            if years == 3:
                btn.setChecked(True)  # default
            self._range_buttons.addButton(btn)
            row.addWidget(btn)
        self._range_buttons.buttonClicked.connect(self._on_range_button_clicked)

        row.addStretch(1)

        self._refresh_button = QPushButton("Refresh")
        self._refresh_button.clicked.connect(self._on_refresh_clicked)
        row.addWidget(self._refresh_button)
        return row

    def _build_layer_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(QLabel("Layers:"))
        self._chk_emas = self._make_layer_checkbox("EMAs", True)
        self._chk_atr_bands = self._make_layer_checkbox("ATR bands", True)
        self._chk_regime = self._make_layer_checkbox("Regime fill", True)
        self._chk_signals = self._make_layer_checkbox("Signal markers", True)
        row.addWidget(self._chk_emas)
        row.addWidget(self._chk_atr_bands)
        row.addWidget(self._chk_regime)
        row.addWidget(self._chk_signals)
        row.addStretch(1)
        # Status label on the right for "loaded N bars / warmed up M"
        self._chart_status_label = QLabel("")
        self._chart_status_label.setStyleSheet("color: #888;")
        row.addWidget(self._chart_status_label)
        return row

    def _make_layer_checkbox(self, label: str, default_checked: bool) -> QCheckBox:
        cb = QCheckBox(label)
        cb.setChecked(default_checked)
        cb.stateChanged.connect(self._on_layer_toggled)
        return cb

    def _build_chart_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)

        # Left: chart area (price + volume in a vertical layout)
        chart_widget = self._build_chart_widget()
        row.addWidget(chart_widget, 5)

        # Right: OHLC readout panel (crosshair-driven)
        readout_box = QGroupBox("Bar detail")
        readout_box.setMaximumWidth(220)
        readout_layout = QVBoxLayout(readout_box)
        self._readout_label = QLabel("Hover over a bar to inspect.")
        self._readout_label.setStyleSheet(
            "font-family: monospace; font-size: 12px; padding: 6px;"
        )
        self._readout_label.setWordWrap(True)
        self._readout_label.setAlignment(Qt.AlignTop)
        readout_layout.addWidget(self._readout_label)
        readout_layout.addStretch(1)
        row.addWidget(readout_box, 0)

        return row

    def _build_chart_widget(self) -> QWidget:
        """Build the GraphicsLayoutWidget with a price plot above and
        a volume plot below, sharing the X axis."""
        gl = pg.GraphicsLayoutWidget()
        gl.setBackground(COLOR_BG)

        # Price plot — takes the top ~75% via stretch factor
        self._price_plot = gl.addPlot(row=0, col=0)
        self._price_plot.showGrid(x=True, y=True, alpha=0.15)
        self._price_plot.getAxis("left").setTextPen(COLOR_AXIS)
        self._price_plot.getAxis("bottom").setTextPen(COLOR_AXIS)
        # Crosshair lines (added to price plot only — volume sub-plot
        # gets a vertical line connected to the same x)
        self._vline_price = pg.InfiniteLine(angle=90, movable=False,
                                            pen=pg.mkPen("#888", width=1, style=Qt.DashLine))
        self._hline_price = pg.InfiniteLine(angle=0, movable=False,
                                            pen=pg.mkPen("#888", width=1, style=Qt.DashLine))
        self._price_plot.addItem(self._vline_price, ignoreBounds=True)
        self._price_plot.addItem(self._hline_price, ignoreBounds=True)
        self._vline_price.hide()
        self._hline_price.hide()

        # Volume plot — bottom ~25%
        self._volume_plot = gl.addPlot(row=1, col=0)
        self._volume_plot.showGrid(x=True, y=True, alpha=0.1)
        self._volume_plot.getAxis("left").setTextPen(COLOR_AXIS)
        self._volume_plot.getAxis("bottom").setTextPen(COLOR_AXIS)
        self._volume_plot.setMaximumHeight(150)
        self._volume_plot.setXLink(self._price_plot)  # share X axis

        self._vline_vol = pg.InfiniteLine(angle=90, movable=False,
                                          pen=pg.mkPen("#888", width=1, style=Qt.DashLine))
        self._volume_plot.addItem(self._vline_vol, ignoreBounds=True)
        self._vline_vol.hide()

        # Phase A: TradingView-style pinned axis label tabs.
        # The price tab hugs the right edge of the price plot at the
        # cursor's y; the date tab hugs the bottom edge of the volume
        # plot at the cursor's x. Both are hidden until the cursor
        # enters the chart area (handled in _on_mouse_moved).
        self._price_axis_label = pg.TextItem(
            text="", anchor=(1.0, 0.5),  # right-middle of tab at position
            color=COLOR_AXIS_LABEL_FG,
            fill=pg.mkBrush(COLOR_AXIS_LABEL_BG),
            border=pg.mkPen(COLOR_AXIS_LABEL_BG),
        )
        self._price_plot.addItem(self._price_axis_label, ignoreBounds=True)
        self._price_axis_label.hide()

        self._date_axis_label = pg.TextItem(
            text="", anchor=(0.5, 1.0),  # middle-bottom of tab at position
            color=COLOR_AXIS_LABEL_FG,
            fill=pg.mkBrush(COLOR_AXIS_LABEL_BG),
            border=pg.mkPen(COLOR_AXIS_LABEL_BG),
        )
        self._volume_plot.addItem(self._date_axis_label, ignoreBounds=True)
        self._date_axis_label.hide()

        # Hide the bottom X axis label on the price plot (visible on
        # the volume plot since it's the lower one)
        self._price_plot.hideAxis("bottom")

        # Mouse-move handler for crosshair
        self._price_plot.scene().sigMouseMoved.connect(self._on_mouse_moved)

        # Stretch row heights: price gets most space
        gl.ci.layout.setRowStretchFactor(0, 4)
        gl.ci.layout.setRowStretchFactor(1, 1)

        return gl

    # ---------- ticker combo population ---------- #

    def _populate_ticker_combo(self) -> None:
        """Populate the search-as-you-type ticker combo with the
        active universe, partitioned into two sections: tickers
        with a signal event in the rolling 'this week + last week'
        window (top, normal background), and the rest (below, grey
        background, separator between).

        ~2300 entries — Qt's QCompleter handles the flat model fine
        without virtualization. The sectioning is applied in
        _on_combo_rows_received once the rows arrive; this fetcher
        just gets them ordered by (has_recent_signal DESC, symbol ASC)
        from the SQL layer.
        """
        def fetch():
            with self._db.connect() as conn:
                return ticker_universe_with_recent_signals(conn)

        submit(
            fetch,
            on_finished=self._on_combo_rows_received,
            on_failed=self._on_combo_rows_failed,
        )

    def _on_combo_rows_received(self, rows: list) -> None:
        """Build the combo's underlying model with two sections:
        recent-signal tickers on top with normal background, then a
        non-selectable separator, then the rest with a grey row
        background. Search-as-you-type filtering preserves the
        section ordering because rows arrive (recent DESC, alpha
        ASC) and the completer iterates the model in order.

        Edge cases:
          - All tickers have a recent signal: no separator, no grey
            rows. Combo is one alphabetical list.
          - No tickers have a recent signal (quiet week, or initial
            deploy): no separator, every row grey. Visually subtle
            but the operator still sees the universe.
        """
        recent, other = partition_ticker_rows(rows)

        model = QStandardItemModel(self)
        grey_brush = QBrush(QColor(_DROPDOWN_GREY_BG))

        def _make_item(row, *, grey: bool) -> QStandardItem:
            display = row.symbol
            if row.full_name:
                display = f"{row.symbol}  —  {row.full_name[:60]}"
            item = QStandardItem(display)
            item.setEditable(False)
            # itemData(idx) reads Qt.UserRole by default, so the
            # existing _on_ticker_combo_activated handler keeps
            # working unchanged.
            item.setData(
                (row.exchange, row.symbol, row.instrument_type),
                Qt.UserRole,
            )
            if grey:
                item.setData(grey_brush, Qt.BackgroundRole)
            return item

        for r in recent:
            model.appendRow(_make_item(r, grey=False))
        for r in other:
            model.appendRow(_make_item(r, grey=True))

        self._ticker_combo.setModel(model)
        # Insert separator between the two sections. Only meaningful
        # when both sections are non-empty.
        if recent and other:
            self._ticker_combo.insertSeparator(len(recent))
        # No initial selection — wait for the user to pick or for
        # cross-tab nav (load_ticker) to set one explicitly.
        self._ticker_combo.setCurrentIndex(-1)

    def _on_combo_rows_failed(self, message: str) -> None:
        self._chart_status_label.setText(f"Ticker list fetch failed: {message}")
        logger.warning("Combo population failed: %s", message)

    # ---------- event handlers ---------- #

    def _on_ticker_combo_activated(self, idx: int) -> None:
        if idx < 0:
            return
        data = self._ticker_combo.itemData(idx)
        if data is None:
            return
        exchange, symbol, instrument = data
        self.load_ticker(exchange, symbol, instrument)

    def _on_range_button_clicked(self, btn) -> None:
        years = btn.property("years")
        self._current_years = years
        if self._current_exchange and self._current_symbol:
            self._fetch_history()

    def _on_refresh_clicked(self) -> None:
        if self._current_exchange and self._current_symbol:
            self._fetch_history()

    def _on_layer_toggled(self, _state) -> None:
        # Re-render from cached payload; no refetch needed.
        if self._last_payload is not None:
            self._render_chart(self._last_payload)

    # ---------- public API (cross-tab nav target) ---------- #

    def load_ticker(self, exchange: str, symbol: str, instrument: str = "EQ") -> None:
        """Called by MainWindow when the user clicks 'View chart' on
        the Tickers tab. Sets the combo + fetches history."""
        self._current_exchange = exchange
        self._current_symbol = symbol
        self._current_instrument = instrument
        # Try to find and select the matching combo entry
        for i in range(self._ticker_combo.count()):
            data = self._ticker_combo.itemData(i)
            if data and data[0] == exchange and data[1] == symbol:
                # blockSignals so we don't trigger activated and re-fetch
                self._ticker_combo.blockSignals(True)
                self._ticker_combo.setCurrentIndex(i)
                self._ticker_combo.blockSignals(False)
                break
        self._fetch_history()

    # ---------- history fetch + render ---------- #

    def _fetch_history(self) -> None:
        if not (self._current_exchange and self._current_symbol):
            return
        ex, sym = self._current_exchange, self._current_symbol
        inst = self._current_instrument or "EQ"
        years = self._current_years
        self._chart_status_label.setText(f"Loading {sym}...")
        submit(
            lambda: self._http.get_signal_history(
                ex, sym, instrument=inst, years=years,
            ),
            on_finished=self._on_history_received,
            on_failed=self._on_history_failed,
        )

    def _on_history_received(self, payload: dict) -> None:
        self._last_payload = payload
        self._render_chart(payload)
        n = len(payload.get("bars", []))
        warmup = payload.get("warmup_bars_count", 0)
        self._chart_status_label.setText(
            f"{payload['symbol']}: {n} weekly bars, {warmup} in warmup."
        )

    def _on_history_failed(self, message: str) -> None:
        self._chart_status_label.setText(f"Chart fetch failed: {message}")
        self._clear_chart()
        QMessageBox.warning(
            self, "Chart fetch failed",
            f"Could not load chart data:\n\n{message}",
        )

    # ---------- chart rendering ---------- #

    def _clear_chart(self) -> None:
        self._price_plot.clear()
        self._volume_plot.clear()
        # Re-add crosshair lines (clear removes them)
        self._price_plot.addItem(self._vline_price, ignoreBounds=True)
        self._price_plot.addItem(self._hline_price, ignoreBounds=True)
        self._volume_plot.addItem(self._vline_vol, ignoreBounds=True)
        self._vline_price.hide()
        self._hline_price.hide()
        self._vline_vol.hide()

    def _render_chart(self, payload: dict) -> None:
        """Render everything from the latest payload according to
        layer checkbox state."""
        self._clear_chart()
        bars = payload.get("bars", [])
        if not bars:
            return

        # Store bar-index → datetime for the date-axis labels and the
        # crosshair readout.
        self._bar_index_to_dt: list[datetime] = []
        for b in bars:
            self._bar_index_to_dt.append(
                datetime.fromisoformat(b["bar_timestamp"])
            )
        # Also keep bars indexed for the readout
        self._bars_by_index: dict[int, dict] = {i: b for i, b in enumerate(bars)}

        # Configure the bottom axis to show dates instead of indices
        date_axis = _DateAxisItem(self._bar_index_to_dt, orientation="bottom")
        self._volume_plot.setAxisItems({"bottom": date_axis})

        # 1. Regime fill (drawn first so candles sit on top)
        if self._chk_regime.isChecked():
            self._render_regime_fill(bars)

        # 2. Candles
        candles_data = [
            (i, b["open"], b["high"], b["low"], b["close"])
            for i, b in enumerate(bars)
        ]
        self._price_plot.addItem(CandlestickItem(candles_data))

        # 3. EMAs
        if self._chk_emas.isChecked():
            self._render_emas(bars)

        # 4. ATR bands
        if self._chk_atr_bands.isChecked():
            self._render_atr_bands(bars)

        # 5. Signal markers
        if self._chk_signals.isChecked():
            self._render_signal_markers(payload, bars)

        # 6. Volume bars
        self._render_volume(bars)

        # Make sure the crosshair lines are still on the plots after clear()
        # (we re-added them in _clear_chart, but they got removed when we
        # called `clear()` on the plot; addItem above for candles/etc reset
        # is fine — only need to ensure they're present)
        if self._vline_price not in self._price_plot.items:
            self._price_plot.addItem(self._vline_price, ignoreBounds=True)
            self._price_plot.addItem(self._hline_price, ignoreBounds=True)
        if self._vline_vol not in self._volume_plot.items:
            self._volume_plot.addItem(self._vline_vol, ignoreBounds=True)

        # Auto-range
        self._price_plot.enableAutoRange()
        self._volume_plot.enableAutoRange()

    def _render_emas(self, bars: list[dict]) -> None:
        xs = list(range(len(bars)))
        # Separate (x, y) lists per EMA, skipping None values
        def line_data(field: str):
            out_x, out_y = [], []
            for i, b in enumerate(bars):
                v = b.get(field)
                if v is not None:
                    out_x.append(i)
                    out_y.append(v)
            return out_x, out_y

        for field, color, name in [
            ("med_ema", COLOR_EMA_MED, "med"),
            ("long_ema", COLOR_EMA_LONG, "long"),
            ("longest_ema", COLOR_EMA_LONGEST, "longest"),
        ]:
            x, y = line_data(field)
            if x:
                pen = pg.mkPen(color, width=1.5)
                self._price_plot.plot(x, y, pen=pen, name=name)

    def _render_atr_bands(self, bars: list[dict]) -> None:
        def line_data(field: str):
            out_x, out_y = [], []
            for i, b in enumerate(bars):
                v = b.get(field)
                if v is not None:
                    out_x.append(i)
                    out_y.append(v)
            return out_x, out_y

        # Thin, low-alpha lines for the bands so they don't dominate
        for field, color in [
            ("upper_inner", COLOR_BAND_INNER),
            ("lower_inner", COLOR_BAND_INNER),
            ("upper_mid", COLOR_BAND_MID),
            ("lower_mid", COLOR_BAND_MID),
            ("upper_outer", COLOR_BAND_OUTER),
            ("lower_outer", COLOR_BAND_OUTER),
        ]:
            x, y = line_data(field)
            if x:
                pen = pg.mkPen(QColor(color), width=1.0,
                               style=Qt.DashLine if "outer" in field else Qt.SolidLine)
                self._price_plot.plot(x, y, pen=pen)

    def _render_regime_fill(self, bars: list[dict]) -> None:
        """Light-alpha vertical bands at bar positions, colored by
        display_bg regime."""
        # Build runs of consecutive same-regime bars for efficiency.
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

        for x0, x1, regime in runs:
            color = QColor(regime_color(regime))
            color.setAlpha(28)  # ~10%
            region = pg.LinearRegionItem(
                values=[x0 - 0.5, x1 - 0.5],
                brush=pg.mkBrush(color),
                movable=False, pen=pg.mkPen(None),
            )
            region.setZValue(-10)  # behind everything else
            self._price_plot.addItem(region)

    def _render_signal_markers(self, payload: dict, bars: list[dict]) -> None:
        """BUY = green ▲ below bar; SELL = red ▼ above bar. Positioned
        slightly outside the bar's range so they don't overlap candles."""
        events = payload.get("signal_events", [])
        if not events:
            return

        # Build index lookup by timestamp for efficient placement
        ts_to_idx: dict[str, int] = {
            b["bar_timestamp"]: i for i, b in enumerate(bars)
        }

        for e in events:
            ts = e["bar_timestamp"]
            i = ts_to_idx.get(ts)
            if i is None:
                # The event may have been logged on a sub-day bar that
                # got aggregated into a different weekly bucket; skip.
                continue
            bar = bars[i]
            signal = e["signal"].upper()
            if signal == "BUY":
                # Place arrow below the low
                offset = (bar["high"] - bar["low"]) * 0.5
                y = bar["low"] - offset
                arrow = pg.ArrowItem(
                    pos=(i, y), angle=90,  # pointing UP
                    headLen=12, tipAngle=40,
                    brush=COLOR_SIGNAL_BUY, pen=pg.mkPen(COLOR_SIGNAL_BUY),
                )
                self._price_plot.addItem(arrow)
            elif signal == "SELL":
                offset = (bar["high"] - bar["low"]) * 0.5
                y = bar["high"] + offset
                arrow = pg.ArrowItem(
                    pos=(i, y), angle=-90,  # pointing DOWN
                    headLen=12, tipAngle=40,
                    brush=COLOR_SIGNAL_SELL, pen=pg.mkPen(COLOR_SIGNAL_SELL),
                )
                self._price_plot.addItem(arrow)

    def _render_volume(self, bars: list[dict]) -> None:
        """Vertical volume bars, colored by candle direction."""
        x = []
        y = []
        brushes = []
        for i, b in enumerate(bars):
            x.append(i)
            y.append(b.get("volume", 0))
            up = b["close"] >= b["open"]
            color = QColor(COLOR_CANDLE_UP if up else COLOR_CANDLE_DOWN)
            color.setAlpha(180)
            brushes.append(color)
        bar_item = pg.BarGraphItem(
            x=x, height=y, width=0.7,
            brushes=brushes, pen=pg.mkPen(None),
        )
        self._volume_plot.addItem(bar_item)

    # ---------- crosshair ---------- #

    def _on_mouse_moved(self, pos) -> None:
        """Update the readout panel + crosshair lines + axis label
        tabs from the cursor position. Hides everything when the
        cursor leaves the chart area or falls outside the bar
        range. Cheap on every move — no fetches or heavy work."""
        if not self._last_payload:
            return
        if not self._price_plot.sceneBoundingRect().contains(pos):
            self._hide_crosshair_items()
            return
        mouse_point = self._price_plot.vb.mapSceneToView(pos)
        x_idx = int(round(mouse_point.x()))
        if x_idx < 0 or x_idx >= len(self._bars_by_index):
            self._hide_crosshair_items()
            return

        bar = self._bars_by_index[x_idx]
        dt = self._bar_index_to_dt[x_idx]

        # Position the dashed crosshair lines
        self._vline_price.setPos(x_idx)
        self._hline_price.setPos(mouse_point.y())
        self._vline_vol.setPos(x_idx)
        self._vline_price.show()
        self._hline_price.show()
        self._vline_vol.show()

        # Phase A: position the TradingView-style axis label tabs.
        # The price tab sits inside the right edge of the price plot
        # at the cursor's y; the date tab sits just above the bottom
        # edge of the volume plot at the bar's x-index. The view
        # ranges can shift on pan/zoom, so we re-read them every
        # mouse move.
        price_view_x_range = self._price_plot.vb.viewRange()[0]
        vol_view_y_range = self._volume_plot.vb.viewRange()[1]
        self._price_axis_label.setPos(
            price_view_x_range[1], mouse_point.y(),
        )
        self._price_axis_label.setText(
            format_price_label(mouse_point.y()),
        )
        self._price_axis_label.show()
        self._date_axis_label.setPos(x_idx, vol_view_y_range[0])
        self._date_axis_label.setText(format_date_label(dt))
        self._date_axis_label.show()

        # Populate the side-panel OHLC readout
        warmed = "yes" if bar.get("is_warmed_up") else "warming up"
        med = _fmt(bar.get("med_ema"))
        long_ = _fmt(bar.get("long_ema"))
        longest = _fmt(bar.get("longest_ema"))
        atr = _fmt(bar.get("atr"))
        self._readout_label.setText(
            f"<b>{dt.strftime('%Y-%m-%d')}</b><br>"
            f"O: {bar['open']:.2f}<br>"
            f"H: {bar['high']:.2f}<br>"
            f"L: {bar['low']:.2f}<br>"
            f"C: {bar['close']:.2f}<br>"
            f"Vol: {int(bar.get('volume', 0)):,}<br>"
            f"<br>"
            f"Med EMA: {med}<br>"
            f"Long EMA: {long_}<br>"
            f"Longest: {longest}<br>"
            f"ATR: {atr}<br>"
            f"<br>"
            f"Regime: {bar.get('display_bg', '—')}<br>"
            f"Warmup: {warmed}"
        )

    def _hide_crosshair_items(self) -> None:
        """Hide all crosshair-driven overlays. Used when the cursor
        leaves the chart area or falls outside the bar range."""
        self._vline_price.hide()
        self._hline_price.hide()
        self._vline_vol.hide()
        self._price_axis_label.hide()
        self._date_axis_label.hide()

    # ---------- lifecycle ---------- #

    def on_close(self) -> None:
        """Called by MainWindow.closeEvent. Stop the embedded
        SignalGenerationPanel's polling timers so they don't keep
        firing after the view (and the QApplication) tears down."""
        try:
            if hasattr(self, "_signal_generation_panel"):
                self._signal_generation_panel.on_close()
        except Exception:
            pass


def _fmt(v) -> str:
    if v is None:
        return "—"
    return f"{v:.2f}"


class _DateAxisItem(pg.AxisItem):
    """Translates the integer bar-index X axis into date strings for
    display. The chart uses 0,1,2,... so weekend/holiday gaps don't
    widen the chart; this axis maps those indices back to readable
    dates for tick labels."""

    def __init__(self, index_to_dt: list[datetime], **kwargs):
        super().__init__(**kwargs)
        self._index_to_dt = index_to_dt

    def tickStrings(self, values, scale, spacing):
        out = []
        for v in values:
            i = int(round(v))
            if 0 <= i < len(self._index_to_dt):
                out.append(self._index_to_dt[i].strftime("%Y-%m"))
            else:
                out.append("")
        return out
