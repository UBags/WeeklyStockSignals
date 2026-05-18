"""Tickers view (Tab B).

Layout:
  ┌─────────────────────────────────────────────────────────────┐
  │ [ Search... ]  Tier: [All ▾]  ☐ Include delisted   [↻]      │
  ├──────────────────────────────────────┬──────────────────────┤
  │ Ticker table (≈2300 rows)            │ Detail panel         │
  │ Sym | Type | Tier | Last bar | ...   │ (selected row info)  │
  │                                      │ Symbol history       │
  │                                      │ Recent signal events │
  │                                      │ [Reprime button]     │
  └──────────────────────────────────────┴──────────────────────┘
  │ Active: 2298  Delisted: 47  T1: 200  T2: 500  T3: 1598  Parked: 7 │

Uses QAbstractTableModel (not QTableWidget) for performance — Qt
only renders visible rows, so 2300 rows scroll smoothly without
per-row widget construction.

Refresh cadence: 30s (the tickers table changes infrequently).
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any, Optional

from PySide6.QtCore import (
    QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt, Signal,
)
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QGroupBox, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QMessageBox, QPushButton,
    QTableView, QTextEdit, QVBoxLayout, QWidget,
)

from ..db import GuiDbSession
from ..http_client import ApiError, TripEmaHttpClient
from ..queries import (
    SignalEventRow, SymbolHistoryRow, TickerRow,
    list_tickers, recent_signal_events,
    ticker_symbol_history, ticker_universe_summary,
)
from ..workers import RefreshScheduler, submit

logger = logging.getLogger(__name__)


# Column indices — single source of truth used by header labels,
# data accessors, and filter logic.
COL_TICKER_ID = 0
COL_SYMBOL = 1
COL_INSTRUMENT = 2
COL_TIER = 3
COL_LAST_BAR = 4
COL_FAILURES = 5
COL_PARKED = 6
COL_FULL_NAME = 7

HEADERS = [
    "ID", "Symbol", "Type", "Tier", "Last bar", "Fails", "Parked", "Name",
]


class TickerTableModel(QAbstractTableModel):
    """Model wrapping a list of TickerRow. Implements only what
    QTableView needs (rowCount, columnCount, data, headerData)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows: list[TickerRow] = []

    # ---------- mutators ---------- #

    def set_rows(self, rows: list[TickerRow]) -> None:
        """Replace the entire row set. Bulk update is cheaper than
        per-row delete-and-insert for our refresh cadence."""
        self.beginResetModel()
        self._rows = rows
        self.endResetModel()

    def row_at(self, source_row: int) -> Optional[TickerRow]:
        if 0 <= source_row < len(self._rows):
            return self._rows[source_row]
        return None

    # ---------- QAbstractTableModel API ---------- #

    def rowCount(self, parent=QModelIndex()) -> int:
        if parent.isValid():
            return 0
        return len(self._rows)

    def columnCount(self, parent=QModelIndex()) -> int:
        return len(HEADERS)

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole) -> Any:
        if not index.isValid():
            return None
        row = self._rows[index.row()]
        col = index.column()

        if role == Qt.DisplayRole:
            return self._display(row, col)
        if role == Qt.TextAlignmentRole:
            if col in (COL_TICKER_ID, COL_TIER, COL_FAILURES):
                return int(Qt.AlignRight | Qt.AlignVCenter)
        if role == Qt.ForegroundRole:
            # Parked tickers stand out in red text; delisted in gray.
            if row.parked:
                return QBrush(QColor("#a02020"))
            if row.delisted_at is not None:
                return QBrush(QColor("#888888"))
        if role == Qt.UserRole:
            return row
        return None

    def headerData(self, section, orientation, role=Qt.DisplayRole) -> Any:
        if role != Qt.DisplayRole:
            return None
        if orientation == Qt.Horizontal:
            return HEADERS[section] if 0 <= section < len(HEADERS) else None
        return str(section + 1)

    # ---------- internal ---------- #

    def _display(self, row: TickerRow, col: int) -> str:
        if col == COL_TICKER_ID:
            return str(row.ticker_id)
        if col == COL_SYMBOL:
            return row.symbol
        if col == COL_INSTRUMENT:
            return row.instrument_type
        if col == COL_TIER:
            return str(row.tier)
        if col == COL_LAST_BAR:
            return row.last_successful_bar_date.isoformat() if row.last_successful_bar_date else "—"
        if col == COL_FAILURES:
            return str(row.consecutive_failures) if row.consecutive_failures else ""
        if col == COL_PARKED:
            return "yes" if row.parked else ""
        if col == COL_FULL_NAME:
            return row.full_name or ""
        return ""


class TickersView(QWidget):
    """The Tickers tab. Table on the left, detail panel on the right."""

    REFRESH_CADENCE_SECONDS = 30.0

    # Cross-tab nav: emitted when the user clicks "View chart" in the
    # detail panel. MainWindow listens; switches to the Signals tab
    # and loads the requested ticker.
    request_chart = Signal(str, str, str)  # exchange, symbol, instrument

    def __init__(
        self,
        db: GuiDbSession,
        http: TripEmaHttpClient,
        *,
        parent: Optional[QWidget] = None,
        start_refresh_on_init: bool = True,
    ):
        super().__init__(parent)
        self._db = db
        self._http = http

        self._model = TickerTableModel()
        self._proxy = QSortFilterProxyModel()
        self._proxy.setSourceModel(self._model)
        self._proxy.setSortRole(Qt.DisplayRole)

        self._selected_ticker_id: Optional[int] = None

        self._build_ui()
        self._scheduler = RefreshScheduler(
            self.REFRESH_CADENCE_SECONDS, run_on_start=True,
        )
        self._scheduler.tick.connect(self._on_refresh_tick)
        if start_refresh_on_init:
            self._scheduler.start()

    # ---------- UI construction ---------- #

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)
        outer.setSpacing(10)

        outer.addLayout(self._build_filter_row())

        body = QHBoxLayout()
        body.setSpacing(10)
        body.addWidget(self._build_table(), 3)
        body.addWidget(self._build_detail_panel(), 1)
        outer.addLayout(body, 1)

        outer.addWidget(self._build_summary_row())

    def _build_filter_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.addWidget(QLabel("Search:"))
        self._search_input = QLineEdit()
        self._search_input.setPlaceholderText("symbol substring (e.g. RELI)")
        self._search_input.textChanged.connect(self._on_filter_changed)
        row.addWidget(self._search_input, 1)

        row.addSpacing(20)
        row.addWidget(QLabel("Tier:"))
        self._tier_combo = QComboBox()
        self._tier_combo.addItems(["All", "1", "2", "3"])
        self._tier_combo.currentTextChanged.connect(self._on_filter_changed)
        row.addWidget(self._tier_combo)

        row.addSpacing(20)
        self._show_delisted_checkbox = QCheckBox("Include delisted")
        self._show_delisted_checkbox.stateChanged.connect(self._on_filter_changed)
        row.addWidget(self._show_delisted_checkbox)

        row.addStretch(1)

        self._export_button = QPushButton("Export CSV")
        self._export_button.clicked.connect(self._on_export_csv_clicked)
        row.addWidget(self._export_button)

        self._refresh_button = QPushButton("Refresh")
        self._refresh_button.clicked.connect(self._on_refresh_button_clicked)
        row.addWidget(self._refresh_button)
        return row

    def _build_table(self) -> QWidget:
        self._table = QTableView()
        self._table.setModel(self._proxy)
        self._table.setSortingEnabled(True)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SingleSelection)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.setAlternatingRowColors(True)
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeToContents,
        )
        # Stretch the last column (full name) to fill horizontal space.
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.selectionModel().selectionChanged.connect(
            self._on_selection_changed
        )
        # Default sort: tier ASC, symbol ASC
        self._table.sortByColumn(COL_TIER, Qt.AscendingOrder)
        return self._table

    def _build_detail_panel(self) -> QWidget:
        box = QGroupBox("Selected ticker")
        layout = QVBoxLayout(box)

        self._detail_summary = QLabel("Select a ticker to see detail.")
        self._detail_summary.setWordWrap(True)
        self._detail_summary.setStyleSheet("padding: 6px;")
        layout.addWidget(self._detail_summary)

        self._reprime_button = QPushButton("Reprime ticker")
        self._reprime_button.setEnabled(False)
        self._reprime_button.clicked.connect(self._on_reprime_clicked)
        if not self._http.token_configured:
            self._reprime_button.setToolTip(
                "Set TRIP_EMA_ADMIN_TOKEN to enable reprime."
            )
        layout.addWidget(self._reprime_button)

        self._view_chart_button = QPushButton("View chart")
        self._view_chart_button.setEnabled(False)
        self._view_chart_button.clicked.connect(self._on_view_chart_clicked)
        layout.addWidget(self._view_chart_button)

        layout.addWidget(QLabel("Recent signal events:"))
        self._events_box = QTextEdit()
        self._events_box.setReadOnly(True)
        self._events_box.setStyleSheet("font-family: monospace; font-size: 12px;")
        layout.addWidget(self._events_box, 1)

        layout.addWidget(QLabel("Symbol history:"))
        self._history_box = QTextEdit()
        self._history_box.setReadOnly(True)
        self._history_box.setStyleSheet("font-family: monospace; font-size: 12px;")
        self._history_box.setMaximumHeight(120)
        layout.addWidget(self._history_box)

        return box

    def _build_summary_row(self) -> QWidget:
        widget = QWidget()
        row = QHBoxLayout(widget)
        row.setContentsMargins(4, 4, 4, 4)
        self._summary_label = QLabel("Loading...")
        self._summary_label.setStyleSheet("color: #666;")
        row.addWidget(self._summary_label)
        row.addStretch(1)
        self._last_refreshed_label = QLabel("Never refreshed")
        self._last_refreshed_label.setStyleSheet("color: #888; font-style: italic;")
        row.addWidget(self._last_refreshed_label)
        return widget

    # ---------- refresh ---------- #

    def _on_refresh_button_clicked(self) -> None:
        self._scheduler.trigger_now()

    def _on_filter_changed(self) -> None:
        # Filtering is a server-side concern (we requery the DB rather
        # than client-filtering a 2300-row model). Just trigger a refresh.
        self._scheduler.trigger_now()

    def _on_refresh_tick(self) -> None:
        # Capture filters at tick time so the async worker uses the
        # right snapshot.
        substr = self._search_input.text().strip()
        tier_text = self._tier_combo.currentText()
        tier_filter: Optional[int] = None
        if tier_text.isdigit():
            tier_filter = int(tier_text)
        include_delisted = self._show_delisted_checkbox.isChecked()

        db = self._db

        def fetch_rows():
            with db.connect() as conn:
                rows = list_tickers(
                    conn,
                    symbol_substr=substr or None,
                    tier_filter=tier_filter,
                    include_delisted=include_delisted,
                )
                summary = ticker_universe_summary(conn)
            return rows, summary

        worker = submit(fetch_rows)
        worker.signals.finished.connect(self._on_rows_received)
        worker.signals.failed.connect(self._on_rows_failed)

    def _on_rows_received(self, payload) -> None:
        rows, summary = payload
        self._model.set_rows(rows)
        self._summary_label.setText(
            f"Active: {summary['active']}  Delisted: {summary['delisted']}  "
            f"T1: {summary['tier_1']}  T2: {summary['tier_2']}  "
            f"T3: {summary['tier_3']}  Parked: {summary['parked']}"
        )
        self._last_refreshed_label.setText(
            f"Refreshed at {datetime.now().strftime('%H:%M:%S')}"
        )
        # Restore the previously-selected ticker if it's still present.
        if self._selected_ticker_id is not None:
            self._reselect_ticker_id(self._selected_ticker_id)

    def _on_rows_failed(self, message: str) -> None:
        self._last_refreshed_label.setText(f"Refresh failed: {message}")
        logger.warning("Tickers fetch failed: %s", message)

    def _reselect_ticker_id(self, ticker_id: int) -> None:
        for proxy_row in range(self._proxy.rowCount()):
            src_idx = self._proxy.mapToSource(self._proxy.index(proxy_row, 0))
            row = self._model.row_at(src_idx.row())
            if row and row.ticker_id == ticker_id:
                self._table.selectRow(proxy_row)
                return

    # ---------- selection & detail ---------- #

    def _on_selection_changed(self, selected, deselected) -> None:
        indexes = selected.indexes()
        if not indexes:
            return
        # Map back through the proxy to the source model row
        src_row = self._proxy.mapToSource(indexes[0]).row()
        row = self._model.row_at(src_row)
        if row is None:
            return
        self._selected_ticker_id = row.ticker_id
        self._render_detail(row)
        self._fetch_signal_events(row.ticker_id)
        self._fetch_symbol_history(row.ticker_id)
        self._reprime_button.setEnabled(self._http.token_configured)
        self._view_chart_button.setEnabled(True)

    def _render_detail(self, row: TickerRow) -> None:
        delisted = (
            f"\n  ⚠ Delisted: {row.delisted_at.strftime('%Y-%m-%d')}"
            if row.delisted_at else ""
        )
        parked = "\n  🚧 Parked (ETL failures)" if row.parked else ""
        self._detail_summary.setText(
            f"<b>{row.symbol}</b> ({row.instrument_type}) — "
            f"Tier {row.tier}<br>"
            f"<small>"
            f"ID: {row.ticker_id}<br>"
            f"ISIN: {row.isin or '—'}<br>"
            f"Name: {row.full_name or '—'}<br>"
            f"Last bar: {row.last_successful_bar_date or '—'}<br>"
            f"Consecutive failures: {row.consecutive_failures}"
            f"{delisted}{parked}"
            f"</small>"
        )

    def _fetch_signal_events(self, ticker_id: int) -> None:
        db = self._db
        def fetch():
            with db.connect() as conn:
                return recent_signal_events(conn, ticker_id, limit=10)
        worker = submit(fetch)
        worker.signals.finished.connect(self._on_events_received)
        worker.signals.failed.connect(self._on_events_failed)

    def _on_events_received(self, events: list[SignalEventRow]) -> None:
        if not events:
            self._events_box.setPlainText("(no signal events for this ticker)")
            return
        lines = []
        for e in events:
            ts = e.bar_timestamp.strftime("%Y-%m-%d %H:%M")
            lines.append(
                f"{ts}  {e.timeframe:>3}  {e.signal:6}  @ {e.price_at_signal:.2f}"
            )
        self._events_box.setPlainText("\n".join(lines))

    def _on_events_failed(self, message: str) -> None:
        self._events_box.setPlainText(f"(error: {message})")

    def _fetch_symbol_history(self, ticker_id: int) -> None:
        db = self._db
        def fetch():
            with db.connect() as conn:
                return ticker_symbol_history(conn, ticker_id)
        worker = submit(fetch)
        worker.signals.finished.connect(self._on_history_received)
        worker.signals.failed.connect(self._on_history_failed)

    def _on_history_received(self, history: list[SymbolHistoryRow]) -> None:
        if not history:
            self._history_box.setPlainText("(no renames recorded)")
            return
        lines = []
        for h in history:
            ts = h.changed_at.strftime("%Y-%m-%d")
            lines.append(f"{ts}  {h.old_symbol} → {h.new_symbol}  ({h.detected_via})")
        self._history_box.setPlainText("\n".join(lines))

    def _on_history_failed(self, message: str) -> None:
        self._history_box.setPlainText(f"(error: {message})")

    # ---------- reprime action ---------- #

    # ---------- CSV export ---------- #

    def _on_export_csv_clicked(self) -> None:
        """Dump the currently-visible (post-filter) rows to CSV."""
        from ..csv_export import export_rows_to_csv, timestamped_filename
        # Iterate the model directly so we get all loaded rows; the
        # proxy's filtering is already reflected because we requery on
        # filter changes.
        rows = []
        for r in range(self._model.rowCount()):
            tr = self._model.row_at(r)
            if tr is None:
                continue
            rows.append([
                tr.ticker_id, tr.exchange, tr.symbol, tr.instrument_type,
                tr.full_name or "", tr.isin or "", tr.tier,
                "yes" if tr.is_active else "no",
                tr.delisted_at.isoformat() if tr.delisted_at else "",
                tr.last_successful_bar_date.isoformat() if tr.last_successful_bar_date else "",
                tr.consecutive_failures,
                "yes" if tr.parked else "no",
            ])
        export_rows_to_csv(
            self,
            default_filename=timestamped_filename("tickers"),
            columns=[
                "ticker_id", "exchange", "symbol", "instrument_type",
                "full_name", "isin", "tier", "is_active", "delisted_at",
                "last_successful_bar_date", "consecutive_failures", "parked",
            ],
            rows=rows,
        )

    def _on_view_chart_clicked(self) -> None:
        """Emit cross-tab navigation signal to load this ticker in
        the chart view."""
        if self._selected_ticker_id is None:
            return
        row = None
        for r in range(self._model.rowCount()):
            tr = self._model.row_at(r)
            if tr and tr.ticker_id == self._selected_ticker_id:
                row = tr
                break
        if row is None:
            return
        # instrument_type ("EQ" / "ETF") doubles as the `instrument`
        # query-param value the history endpoint expects.
        self.request_chart.emit(row.exchange, row.symbol, row.instrument_type)

    def _on_reprime_clicked(self) -> None:
        if self._selected_ticker_id is None:
            return
        # Need to look up the ticker's (exchange, symbol) from the model.
        # The reprime endpoint takes those, not ticker_id.
        row = None
        for r in range(self._model.rowCount()):
            tr = self._model.row_at(r)
            if tr and tr.ticker_id == self._selected_ticker_id:
                row = tr
                break
        if row is None:
            return

        confirm = QMessageBox.question(
            self, f"Reprime {row.symbol}?",
            f"Force-reprime ticker {row.symbol} from DB?\n"
            f"This refreshes its metadata (tz, source_timeframe) and "
            f"rebuilds its engine from current bars.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return

        self._reprime_button.setEnabled(False)
        worker = submit(lambda: self._http.reprime_ticker(row.exchange, row.symbol))
        def on_done(payload: dict):
            self._reprime_button.setEnabled(self._http.token_configured)
            msg = f"Reprime: succeeded={payload.get('succeeded')}, " \
                  f"bars_loaded={payload.get('bars_loaded')}\n" \
                  f"{payload.get('notes', '')}"
            QMessageBox.information(self, f"Reprime {row.symbol}", msg)
            self._scheduler.trigger_now()
        def on_fail(message: str):
            self._reprime_button.setEnabled(self._http.token_configured)
            QMessageBox.warning(
                self, f"Reprime {row.symbol} failed", message,
            )
        worker.signals.finished.connect(on_done)
        worker.signals.failed.connect(on_fail)

    # ---------- lifecycle ---------- #

    def on_close(self) -> None:
        try:
            if self._scheduler.is_running:
                self._scheduler.stop()
        except RuntimeError:
            pass
