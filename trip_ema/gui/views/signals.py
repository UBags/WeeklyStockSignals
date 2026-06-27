"""Signal viewer / chart view (Phase C layout).

Top bar:
  Ticker | Layers | Range buttons | Exchange | Refresh

Body — horizontal QSplitter:
  LEFT  (55% default): IndustryPanel
    - Industry chart on top
    - 4x3 grid of top-12-by-mcap mini charts below
  RIGHT (45% default): vertical:
    - Main ChartWidget (in MAIN mode — layer toggles apply here only)
    - Alt timeframe row (1W enabled; 1D disabled with tooltip)
    - Backtest row (range + run, was in Phase B — moves here)
    - Last 5 signals list
    - Bar detail readout (defaults to latest bar; live-updates on
      crosshair; reverts to latest bar when cursor leaves chart)

Cross-tab nav: load_ticker(ex, sym, inst) is unchanged externally
  — Tickers tab's "View chart" still works.

Splitter position persisted via QSettings under
"trip_ema/signals/splitter_state".
"""

from __future__ import annotations

import logging
from datetime import datetime

from PySide6.QtCore import Qt, QSettings
from PySide6.QtGui import (
    QBrush, QColor, QStandardItem, QStandardItemModel,
)
from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QComboBox, QGroupBox, QHBoxLayout,
    QLabel, QListWidget, QMessageBox, QPushButton, QSizePolicy,
    QSplitter, QVBoxLayout, QWidget,
)
from sqlalchemy import text  # noqa: F401 — retained for future inline SQL

from ..backtest_engine import (
    filter_bars_to_range,
    run_long_only_backtest,
)
from ..chart_widget import ChartMode, ChartWidget
from ..db import GuiDbSession
from ..http_client import TripEmaHttpClient
from ..queries import ticker_universe_with_recent_signals
from ..queries import latest_mcap_for_ticker
from .._format_mcap import format_mcap_cr
from ..benchmark_rerank_client import fetch_rank_scores
from ..signals_helpers import (
    industry_rank_alpha_sort_key,
    partition_ticker_rows,
)
from ..workers import submit
from .backtest_dialog import BacktestDialog
from .industry_panel import IndustryPanel
from .signal_generation_panel import SignalGenerationPanel

logger = logging.getLogger(__name__)


_DROPDOWN_GREY_BG = "#f0f0f0"
_DROPDOWN_BUY_BG = "#c8e6c9"       # light green
_DROPDOWN_SELL_BG = "#ffcdd2"      # light red
_DROPDOWN_EXIT_BG = "#bbdefb"      # light blue

_QSETTINGS_ORG = "trip_ema"
_QSETTINGS_APP = "gui"
_QSETTINGS_SPLITTER_KEY = "signals/splitter_state"


class SignalsView(QWidget):

    def __init__(
        self,
        db: GuiDbSession,
        http: TripEmaHttpClient,
        *,
        parent: QWidget | None = None,
        populate_combo_on_init: bool = True,
    ):
        super().__init__(parent)
        self._db = db
        # Latest market cap of the currently-loaded ticker.
        # Populated by load_ticker(); consumed by
        # _update_bar_detail() to label the Regime line.
        self._current_mcap = None
        self._http = http
        self._populate_combo_on_init = populate_combo_on_init

        # Selected-ticker state
        self._current_exchange: str | None = None
        self._current_symbol: str | None = None
        self._current_instrument: str | None = None
        self._current_years: int = 3
        self._last_payload: dict | None = None

        # Backtest dialog cache (Phase B)
        self._open_backtest_dialogs: list[BacktestDialog] = []

        self._settings = QSettings(_QSETTINGS_ORG, _QSETTINGS_APP)

        self._build_ui()
        self._restore_splitter()

        if populate_combo_on_init:
            self._populate_ticker_combo()

    # ====================  UI construction  ==================== #

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(6)

        outer.addLayout(self._build_top_bar())

        # Horizontal splitter: left panel (industry) | right panel (main)
        self._main_splitter = QSplitter(Qt.Horizontal)

        self._industry_panel = IndustryPanel(self._db, self._http, parent=self)
        self._main_splitter.addWidget(self._industry_panel)

        right_widget = QWidget()
        right_layout = QVBoxLayout(right_widget)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(6)

        # Main chart
        self._main_chart = ChartWidget(ChartMode.MAIN, parent=self)
        self._main_chart.bar_under_cursor_changed.connect(self._on_bar_under_cursor)
        self._main_chart.cursor_left.connect(self._on_cursor_left_chart)
        right_layout.addWidget(self._main_chart, 1)

        # Alt timeframe row (Phase C: 1D disabled)
        right_layout.addLayout(self._build_alt_tf_row())

        # Backtest controls (moved here from Phase B's position)
        right_layout.addLayout(self._build_backtest_row())

        # Last 5 signals
        right_layout.addWidget(self._build_last5_box())

        # Bar detail readout
        right_layout.addWidget(self._build_bar_detail_box())

        self._main_splitter.addWidget(right_widget)

        # 55/45 split default
        self._main_splitter.setStretchFactor(0, 55)
        self._main_splitter.setStretchFactor(1, 45)
        self._main_splitter.setSizes([550, 450])
        # Persist on every drag
        self._main_splitter.splitterMoved.connect(self._save_splitter)

        outer.addWidget(self._main_splitter, 1)

        # Existing signal-generation panel still belongs below the
        # main body (it controls the persisted backfill, separate
        # from the Phase B transient backtest).
        self._signal_generation_panel = SignalGenerationPanel(
            self._http, parent=self,
            start_polling_on_init=self._populate_combo_on_init,
        )
        self._signal_generation_panel.setMaximumHeight(130)
        outer.addWidget(self._signal_generation_panel)

    # ----- top bar ----- #

    def _build_top_bar(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)

        # Ticker dropdown
        row.addWidget(QLabel("Ticker:"))
        self._ticker_combo = QComboBox()
        self._ticker_combo.setEditable(True)
        self._ticker_combo.setInsertPolicy(QComboBox.NoInsert)
        self._ticker_combo.setMinimumWidth(240)
        self._ticker_combo.setMaxVisibleItems(20)
        self._ticker_combo.activated.connect(self._on_ticker_combo_activated)
        row.addWidget(self._ticker_combo)

        row.addSpacing(12)

        # Layer toggles (apply to right-panel main chart only)
        row.addWidget(QLabel("Layers:"))
        self._chk_emas = self._make_layer_checkbox("EMAs", "emas", True)
        self._chk_atr_bands = self._make_layer_checkbox("ATR", "atr", True)
        self._chk_regime = self._make_layer_checkbox("Regime", "regime", True)
        self._chk_signals = self._make_layer_checkbox("Signals", "signals", True)
        for chk in (self._chk_emas, self._chk_atr_bands,
                    self._chk_regime, self._chk_signals):
            row.addWidget(chk)

        row.addSpacing(12)

        # Range buttons
        self._range_buttons = QButtonGroup(self)
        self._range_buttons.setExclusive(True)
        for years, label in [(1, "1Y"), (3, "3Y"), (5, "5Y"), (10, "10Y"), (20, "All")]:
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setProperty("years", years)
            if years == 3:
                btn.setChecked(True)
            self._range_buttons.addButton(btn)
            row.addWidget(btn)
        self._range_buttons.buttonClicked.connect(self._on_range_button_clicked)

        row.addSpacing(12)

        # Exchange — read-only label for now (single-exchange deployments)
        row.addWidget(QLabel("Exchange:"))
        self._exchange_label = QLabel("NSE")
        self._exchange_label.setStyleSheet("color: #cccccc; font-weight: bold;")
        row.addWidget(self._exchange_label)

        row.addStretch(1)

        self._refresh_button = QPushButton("Refresh")
        self._refresh_button.clicked.connect(self._on_refresh_clicked)
        row.addWidget(self._refresh_button)

        # Status label for chart load state (was in old layer row)
        self._chart_status_label = QLabel("")
        self._chart_status_label.setStyleSheet("color: #888;")
        row.addWidget(self._chart_status_label)
        return row

    def _make_layer_checkbox(
        self, text_label: str, key: str, default_checked: bool,
    ) -> QCheckBox:
        cb = QCheckBox(text_label)
        cb.setChecked(default_checked)
        cb.setProperty("layer_key", key)
        cb.stateChanged.connect(self._on_layer_toggled)
        return cb

    # ----- right panel rows ----- #

    def _build_alt_tf_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(QLabel("Alt timeframe:"))
        self._chk_1w = QCheckBox("1W")
        self._chk_1w.setChecked(True)
        self._chk_1w.setEnabled(False)  # Always on; placeholder for future
        self._chk_1w.setToolTip("Weekly bars — currently the only supported timeframe.")
        row.addWidget(self._chk_1w)

        self._chk_1d = QCheckBox("1D")
        self._chk_1d.setEnabled(False)
        self._chk_1d.setToolTip(
            "Daily timeframe — coming in a later phase. "
            "Weekly indicators don't map cleanly to daily bars yet."
        )
        row.addWidget(self._chk_1d)

        row.addStretch(1)
        return row

    def _build_backtest_row(self) -> QHBoxLayout:
        """Phase B's transient backtest controls, moved into the
        right panel per the Phase C layout."""
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(QLabel("Backtest:"))

        self._backtest_range_combo = QComboBox()
        for years, label in [
            (3, "3 years"), (5, "5 years"),
            (10, "10 years"), (None, "All time"),
        ]:
            self._backtest_range_combo.addItem(label, years)
        self._backtest_range_combo.setCurrentIndex(1)
        row.addWidget(self._backtest_range_combo)

        self._run_backtest_button = QPushButton("Run")
        self._run_backtest_button.clicked.connect(self._on_run_backtest_clicked)
        row.addWidget(self._run_backtest_button)

        self._backtest_hint_label = QLabel("")
        self._backtest_hint_label.setStyleSheet("color: #888;")
        row.addWidget(self._backtest_hint_label)

        row.addStretch(1)
        return row

    def _build_last5_box(self) -> QGroupBox:
        box = QGroupBox("Last 5 signals")
        layout = QVBoxLayout(box)
        layout.setContentsMargins(6, 4, 6, 4)
        self._last5_list = QListWidget()
        self._last5_list.setMaximumHeight(110)
        self._last5_list.setStyleSheet(
            "QListWidget { font-family: monospace; font-size: 11px; }"
        )
        layout.addWidget(self._last5_list)
        return box

    def _build_bar_detail_box(self) -> QGroupBox:
        box = QGroupBox("Bar detail")
        box.setMaximumHeight(180)
        layout = QVBoxLayout(box)
        layout.setContentsMargins(8, 4, 8, 4)
        self._readout_label = QLabel("(load a ticker)")
        self._readout_label.setStyleSheet(
            "font-family: monospace; font-size: 11px;"
        )
        self._readout_label.setAlignment(Qt.AlignTop)
        self._readout_label.setSizePolicy(
            QSizePolicy.Expanding, QSizePolicy.Expanding,
        )
        self._readout_label.setTextFormat(Qt.RichText)
        layout.addWidget(self._readout_label)
        return box

    # ====================  Splitter persistence  ==================== #

    def _restore_splitter(self) -> None:
        state = self._settings.value(_QSETTINGS_SPLITTER_KEY)
        if state is not None:
            try:
                self._main_splitter.restoreState(state)
            except Exception:
                pass  # corrupt state — fall back to default sizes

    def _save_splitter(self, *_args) -> None:
        self._settings.setValue(
            _QSETTINGS_SPLITTER_KEY,
            self._main_splitter.saveState(),
        )

    # ====================  Ticker combo  ==================== #

    def _populate_ticker_combo(self) -> None:
        """Fetch the recent-signal-aware ticker universe AND each
        ticker's composite rank_score so the recent-signal section can
        be sorted by momentum DESC. Phase 6-3."""
        def fetch():
            with self._db.connect() as conn:
                rows = ticker_universe_with_recent_signals(conn)
                ticker_ids = [
                    r.ticker_id for r in rows
                    if getattr(r, "ticker_id", None) is not None
                ]
                rank_scores = fetch_rank_scores(conn, ticker_ids)
            return rows, rank_scores
        submit(
            fetch,
            on_finished=self._on_combo_rows_received,
            on_failed=self._on_combo_rows_failed,
        )

    def _on_combo_rows_received(self, payload) -> None:
        # Phase 6-3: payload is (rows, rank_scores). Recent-signal
        # section is sorted by rank_score DESC; the 'other' section
        # stays alphabetical so operators browsing by name aren't
        # surprised.
        rows, rank_scores = payload
        recent, other = partition_ticker_rows(rows)

        # Step 12b: industry-grouped + rank dropdown sort.
        # Both recent and "other" partitions are sorted by
        # industry_rank_alpha_sort_key — industry primary, signaled
        # rows before unsignaled within each industry, signaled
        # rows by rank_score DESC, unsignaled by symbol ASC. The
        # Tickers tab proxy uses the identical sort, so the two
        # views are aligned.
        recent = sorted(
            recent,
            key=lambda r: industry_rank_alpha_sort_key(
                r,
                getattr(r, 'latest_signal', None),
                rank_scores.get(r.ticker_id),
            ),
        )
        other = sorted(
            other,
            key=lambda r: industry_rank_alpha_sort_key(
                r, None,
                rank_scores.get(r.ticker_id),
            ),
        )
        model = QStandardItemModel(self)
        grey_brush = QBrush(QColor(_DROPDOWN_GREY_BG))
        buy_brush = QBrush(QColor(_DROPDOWN_BUY_BG))
        sell_brush = QBrush(QColor(_DROPDOWN_SELL_BG))
        exit_brush = QBrush(QColor(_DROPDOWN_EXIT_BG))

        def _signal_brush(sig: str | None) -> QBrush | None:
            if sig == "BUY":
                return buy_brush
            if sig == "SELL":
                return sell_brush
            if sig in ("EXIT_FROM_BUY", "EXIT_FROM_SELL"):
                return exit_brush
            return None

        def _make_item(row, *, grey: bool) -> QStandardItem:
            display = row.symbol
            if row.full_name:
                display = f"{row.symbol}  —  {row.full_name[:60]}"
            item = QStandardItem(display)
            item.setEditable(False)
            item.setData(
                (row.exchange, row.symbol, row.instrument_type),
                Qt.UserRole,
            )
            # Phase 6-3: hover-tooltip exposes the rank_score so the
            # operator can see WHY the row sits where it does in the
            # ranked recent-signal section.
            tid = getattr(row, "ticker_id", None)
            if tid is not None and tid in rank_scores:
                item.setToolTip(
                    f"{row.symbol}  rank_score = {rank_scores[tid]:+.4f}"
                )
            if grey:
                item.setData(grey_brush, Qt.BackgroundRole)
            else:
                sig_brush = _signal_brush(getattr(row, 'latest_signal', None))
                if sig_brush is not None:
                    item.setData(sig_brush, Qt.BackgroundRole)
            return item

        for r in recent:
            model.appendRow(_make_item(r, grey=False))
        for r in other:
            model.appendRow(_make_item(r, grey=True))
        self._ticker_combo.setModel(model)
        if recent and other:
            self._ticker_combo.insertSeparator(len(recent))
        self._ticker_combo.setCurrentIndex(-1)

    def _on_combo_rows_failed(self, message: str) -> None:
        self._chart_status_label.setText(f"Ticker list fetch failed: {message}")

    # ====================  Event handlers  ==================== #

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
        # The industry chart follows the same range; minis are fixed at 5Y.
        self._industry_panel.set_industry_chart_years(years)
        if self._current_exchange and self._current_symbol:
            self._fetch_history()

    def _on_refresh_clicked(self) -> None:
        if self._current_exchange and self._current_symbol:
            self._fetch_history()
            # Also re-trigger the left panel so a fresh industry
            # snapshot lands too.
            self._industry_panel.load_for_ticker(
                self._current_exchange,
                self._current_symbol,
                self._current_instrument or "EQ",
            )

    def _on_layer_toggled(self, _state) -> None:
        sender = self.sender()
        if sender is None:
            return
        key = sender.property("layer_key")
        if not key:
            return
        self._main_chart.set_layer_visible(key, sender.isChecked())

    def _on_bar_under_cursor(self, bar: dict) -> None:
        self._update_bar_detail(bar)

    def _on_cursor_left_chart(self) -> None:
        # Spec: revert to the latest available bar when cursor leaves.
        latest = self._main_chart.latest_bar()
        if latest is not None:
            self._update_bar_detail(latest)

    # ====================  Cross-tab nav target  ==================== #

    def load_ticker(
        self, exchange: str, symbol: str, instrument: str = "EQ",
    ) -> None:
        self._current_exchange = exchange
        self._current_symbol = symbol
        self._current_instrument = instrument
        # Refresh cached market cap for the Bar-detail readout. Best-effort:
        # if the lookup fails (DB hiccup, ticker not found), fall through
        # with mcap=None so the readout omits the label rather than failing.
        try:
            with self._db.connect() as conn:
                tid_row = conn.execute(
                    text(
                        "SELECT ticker_id FROM tickers "
                        "WHERE exchange = :ex AND symbol = :sym "
                        "AND instrument_type = :inst LIMIT 1"
                    ),
                    {"ex": exchange, "sym": symbol, "inst": instrument},
                ).first()
                self._current_mcap = (
                    latest_mcap_for_ticker(conn, tid_row.ticker_id)
                    if tid_row is not None else None
                )
        except Exception:
            self._current_mcap = None
        # Select in combo if present
        for i in range(self._ticker_combo.count()):
            data = self._ticker_combo.itemData(i)
            if data and data[0] == exchange and data[1] == symbol:
                self._ticker_combo.blockSignals(True)
                self._ticker_combo.setCurrentIndex(i)
                self._ticker_combo.blockSignals(False)
                break
        self._fetch_history()
        # Left panel: refresh industry chart + minis for the new ticker.
        self._industry_panel.load_for_ticker(exchange, symbol, instrument)

    # ====================  History fetch / render  ==================== #

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
        self._main_chart.render(payload)
        n = len(payload.get("bars", []))
        warmup = payload.get("warmup_bars_count", 0)
        self._chart_status_label.setText(
            f"{payload['symbol']}: {n} bars, {warmup} in warmup."
        )
        # Default the bar detail to the latest bar (spec).
        latest = self._main_chart.latest_bar()
        if latest is not None:
            self._update_bar_detail(latest)
        # Populate the last-5 signals list.
        self._update_last5_signals(payload)

    def _on_history_failed(self, message: str) -> None:
        self._chart_status_label.setText(f"Chart fetch failed: {message}")
        self._main_chart.render(None)
        QMessageBox.warning(
            self, "Chart fetch failed",
            f"Could not load chart data:\n\n{message}",
        )

    # ====================  Bar detail + last 5 ==================== #

    def _update_bar_detail(self, bar: dict) -> None:
        dt_raw = bar.get("bar_timestamp")
        if isinstance(dt_raw, str):
            dt = datetime.fromisoformat(dt_raw)
        else:
            dt = dt_raw
        warmed = "yes" if bar.get("is_warmed_up") else "warming up"
        med = _fmt(bar.get("med_ema"))
        long_ = _fmt(bar.get("long_ema"))
        longest = _fmt(bar.get("longest_ema"))
        atr = _fmt(bar.get("atr"))
        self._readout_label.setText(
            f"<b>{dt.strftime('%d-%b-%y') if dt else '—'}</b><br>"
            f"O: {bar.get('open', 0):.2f}  "
            f"H: {bar.get('high', 0):.2f}<br>"
            f"L: {bar.get('low', 0):.2f}  "
            f"C: {bar.get('close', 0):.2f}<br>"
            f"Vol: {int(bar.get('volume', 0)):,}<br>"
            f"Med EMA: {med}  Long EMA: {long_}<br>"
            f"Longest: {longest}  ATR: {atr}<br>"
            f"Regime: {bar.get('display_bg', '—')} ({warmed})"
            + (
                f"  ;  Mkt Cap: {format_mcap_cr(self._current_mcap)}"
                if self._current_mcap is not None else ""
            )
        )

    def _update_last5_signals(self, payload: dict) -> None:
        self._last5_list.clear()
        events = payload.get("signal_events", []) or []
        # signal_events come oldest→newest from the endpoint; take
        # the last 5 and reverse so newest is at the top.
        recent = list(events[-5:])
        recent.reverse()
        # Two timestamps shown side-by-side per row:
        #   "Week" = bar_timestamp (the Monday-IST-anchored weekly bar
        #            this signal fired on)
        #   "at"   = computed_at (when the scheduler/runner emitted
        #            the SignalEvent row). Both rendered in IST so
        #            the operator sees local-clock times.
        # Hoisted out of the per-row loop so the closure doesn't
        # capture a rebound loop variable (ruff B023).
        from zoneinfo import ZoneInfo as _ZI
        ist = _ZI("Asia/Kolkata")

        def _to_ist(ts):
            if not ts:
                return None
            if isinstance(ts, str):
                return datetime.fromisoformat(
                    ts.replace("Z", "+00:00"),
                ).astimezone(ist)
            return ts.astimezone(ist)

        for e in recent:
            bar_dt = _to_ist(e.get("bar_timestamp"))
            computed_dt = _to_ist(e.get("computed_at"))
            bar_str = bar_dt.strftime("%d-%m-%y") if bar_dt else "—"
            computed_str = (
                computed_dt.strftime("%d-%m-%y %H:%M") if computed_dt else "—"
            )

            self._last5_list.addItem(
                f"Week {bar_str} at {computed_str}   "
                f"{e.get('signal', '—'):>14} @ "
                f"₹{e.get('price_at_signal', 0):,.2f}"
            )

    # ====================  Phase B: backtest  ==================== #

    def _on_run_backtest_clicked(self) -> None:
        if not self._last_payload:
            self._backtest_hint_label.setText("Load a ticker first.")
            return
        bars = self._last_payload.get("bars", [])
        if not bars:
            self._backtest_hint_label.setText("No bars to backtest.")
            return

        years = self._backtest_range_combo.currentData()
        sliced = filter_bars_to_range(bars, years=years)
        if not sliced:
            self._backtest_hint_label.setText("Selected range has no bars.")
            return
        if years is not None and len(sliced) < years * 50:
            self._backtest_hint_label.setText(
                f"Note: chart has only {len(sliced)} bars in the "
                f"last {years}y; bump the chart range for more."
            )
        else:
            self._backtest_hint_label.setText("")

        result = run_long_only_backtest(sliced)
        ticker_label = (
            f"{self._current_exchange}:{self._current_symbol}"
            if self._current_exchange and self._current_symbol else "?"
        )
        range_label = "All time" if years is None else f"{years} years"
        dlg = BacktestDialog(
            result,
            ticker_label=ticker_label,
            range_label=range_label,
            parent=self,
        )
        dlg.show()
        # Prune dead dialogs from prior runs (Qt.WA_DeleteOnClose
        # leaves the wrapper pointing at a freed C++ object).
        self._open_backtest_dialogs = [
            d for d in self._open_backtest_dialogs if _is_dialog_alive(d)
        ]
        self._open_backtest_dialogs.append(dlg)

        s = result.summary
        if s.n_trades == 0:
            self._backtest_hint_label.setText(
                "Backtest complete: no trades fired."
            )
        else:
            self._backtest_hint_label.setText(
                f"Backtest complete: {s.n_trades} trades, "
                f"{s.win_rate_pct:.1f}% win rate, "
                f"total return {s.total_return_pct:+.1f}%."
            )

    # ====================  Lifecycle  ==================== #

    def on_close(self) -> None:
        """Called by MainWindow.closeEvent. Stop embedded panels
        and persist final splitter state."""
        try:
            self._save_splitter()
        except Exception:
            pass
        try:
            if hasattr(self, "_signal_generation_panel"):
                self._signal_generation_panel.on_close()
        except Exception:
            pass


def _is_dialog_alive(dlg) -> bool:
    """Return True if the Qt wrapper still maps to a live C++ object."""
    try:
        dlg.isVisible()
    except RuntimeError:
        return False
    return True


def _fmt(v) -> str:
    if v is None:
        return "—"
    return f"{v:.2f}"