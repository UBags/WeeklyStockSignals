"""Notification log view (Slice 4).

Layout:
  ┌────────────────────────────────────────────────────────────────┐
  │ Last [168] hours  Subscriber: [All ▾]  [Export CSV] [Refresh]  │
  ├────────────────────────────────────────────────────────────────┤
  │ Notification log table                                         │
  │ When | Subscriber | Channel | Ticker | Signal | Count          │
  │ ...                                                            │
  └────────────────────────────────────────────────────────────────┘
  Showing N entries. has_more=True if hit limit.

Read-only view. Polls every 60s (notifications are slow-moving).
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from PySide6.QtCore import (
    QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt,
)
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QHBoxLayout, QHeaderView, QLabel,
    QPushButton, QSpinBox, QTableView, QVBoxLayout, QWidget,
)

from ..csv_export import export_rows_to_csv, timestamped_filename
from ..db import GuiDbSession
from ..http_client import TripEmaHttpClient
from ..workers import RefreshScheduler, submit

logger = logging.getLogger(__name__)


COL_TIME = 0
COL_SUBSCRIBER = 1
COL_TRANSPORT = 2
COL_TICKER = 3
COL_SIGNAL = 4
COL_COUNT = 5

HEADERS = [
    "When", "Subscriber", "Channel", "Ticker", "Signal", "Count",
]


# Color the Signal column by signal type — matches the chart's palette.
SIGNAL_COLORS = {
    "BUY":            "#1f9c5e",
    "SELL":           "#c84a4a",
    "EXIT_FROM_BUY":  "#b08000",
    "EXIT_FROM_SELL": "#b08000",
    "NO_CHANGE":      "#666",
}


class NotificationLogModel(QAbstractTableModel):

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows: list[dict] = []

    def set_rows(self, rows: list[dict]) -> None:
        self.beginResetModel()
        self._rows = rows
        self.endResetModel()

    def row_at(self, source_row: int) -> dict | None:
        if 0 <= source_row < len(self._rows):
            return self._rows[source_row]
        return None

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
            if col == COL_COUNT:
                return int(Qt.AlignRight | Qt.AlignVCenter)
        if role == Qt.ForegroundRole and col == COL_SIGNAL:
            color = SIGNAL_COLORS.get(row.get("event_signal") or "")
            if color:
                return QBrush(QColor(color))
        return None

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role != Qt.DisplayRole:
            return None
        if orientation == Qt.Horizontal:
            return HEADERS[section] if 0 <= section < len(HEADERS) else None
        return str(section + 1)

    def _display(self, row: dict, col: int) -> str:
        if col == COL_TIME:
            ts = row.get("last_sent_at", "")
            return ts[:19].replace("T", " ") if ts else "—"
        if col == COL_SUBSCRIBER:
            sid = row.get("subscriber_id")
            addr = row.get("subscriber_address", "")
            return f"#{sid}  {addr[:40]}" if sid else addr[:40]
        if col == COL_TRANSPORT:
            return row.get("subscriber_transport", "")
        if col == COL_TICKER:
            sym = row.get("ticker_symbol")
            ex = row.get("ticker_exchange")
            if sym and ex:
                return f"{ex}:{sym}"
            return "—"
        if col == COL_SIGNAL:
            return row.get("event_signal") or ""
        if col == COL_COUNT:
            return str(row.get("notification_count", 1))
        return ""


class NotificationLogView(QWidget):

    REFRESH_CADENCE_SECONDS = 60.0

    def __init__(
        self,
        db: GuiDbSession,
        http: TripEmaHttpClient,
        *,
        parent: QWidget | None = None,
        start_refresh_on_init: bool = True,
    ):
        super().__init__(parent)
        self._db = db
        self._http = http

        self._model = NotificationLogModel()
        self._proxy = QSortFilterProxyModel()
        self._proxy.setSourceModel(self._model)
        self._proxy.setSortRole(Qt.DisplayRole)

        self._subscribers_for_filter: list[dict] = []
        """Populated on startup; drives the Subscriber filter dropdown."""

        self._build_ui()
        self._scheduler = RefreshScheduler(
            self.REFRESH_CADENCE_SECONDS, run_on_start=True,
        )
        self._scheduler.tick.connect(self._on_refresh_tick)
        if start_refresh_on_init:
            self._scheduler.start()

    # ---------- UI ---------- #

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)
        outer.setSpacing(10)

        outer.addLayout(self._build_filter_row())
        outer.addWidget(self._build_table(), 1)
        outer.addWidget(self._build_status_row())

    def _build_filter_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.addWidget(QLabel("Last"))
        self._hours_spin = QSpinBox()
        self._hours_spin.setRange(1, 24 * 90)
        self._hours_spin.setValue(168)  # 1 week
        self._hours_spin.setSuffix(" hours")
        self._hours_spin.valueChanged.connect(self._on_filter_changed)
        row.addWidget(self._hours_spin)

        row.addSpacing(20)
        row.addWidget(QLabel("Subscriber:"))
        self._subscriber_combo = QComboBox()
        self._subscriber_combo.addItem("All", None)  # userData=None
        self._subscriber_combo.currentIndexChanged.connect(self._on_filter_changed)
        row.addWidget(self._subscriber_combo, 0)

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
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.sortByColumn(COL_TIME, Qt.DescendingOrder)
        return self._table

    def _build_status_row(self) -> QWidget:
        widget = QWidget()
        row = QHBoxLayout(widget)
        row.setContentsMargins(4, 4, 4, 4)
        self._summary_label = QLabel("Loading...")
        self._summary_label.setStyleSheet("color: #666;")
        row.addWidget(self._summary_label)
        row.addStretch(1)
        self._last_refreshed_label = QLabel("Never refreshed")
        self._last_refreshed_label.setStyleSheet(
            "color: #888; font-style: italic;"
        )
        row.addWidget(self._last_refreshed_label)
        return widget

    # ---------- refresh ---------- #

    def _on_refresh_button_clicked(self) -> None:
        self._scheduler.trigger_now()

    def _on_filter_changed(self, *_args) -> None:
        # Filter changes refetch — server-side filtering is cheap and
        # avoids client-side off-by-one bugs.
        self._scheduler.trigger_now()

    def _on_refresh_tick(self) -> None:
        # Populate subscribers dropdown lazily on first tick.
        if self._subscriber_combo.count() <= 1:
            self._kick_subscribers_fetch()

        hours = self._hours_spin.value()
        sub_id = self._subscriber_combo.currentData()
        http = self._http

        def fetch():
            return http.list_recent_notifications(
                limit=500, since_hours=hours,
                subscriber_id=sub_id,
            )
        submit(
            fetch,
            on_finished=self._on_log_received,
            on_failed=self._on_log_failed,
        )

    def _kick_subscribers_fetch(self) -> None:
        """Populate the subscriber filter dropdown."""
        http = self._http
        submit(
            lambda: http.list_subscribers(),
            on_finished=self._on_subscribers_for_filter,
            on_failed=lambda msg: logger.warning("Subscribers dropdown fetch failed: %s", msg),
        )

    def _on_subscribers_for_filter(self, payload: dict) -> None:
        subs = payload.get("subscribers", [])
        self._subscribers_for_filter = subs
        # Block signals while populating so we don't trigger another refresh
        self._subscriber_combo.blockSignals(True)
        # Preserve the current selection
        current_data = self._subscriber_combo.currentData()
        self._subscriber_combo.clear()
        self._subscriber_combo.addItem("All", None)
        for s in subs:
            label = f"#{s['subscriber_id']} {s.get('address', '')[:40]}"
            self._subscriber_combo.addItem(label, s["subscriber_id"])
        # Re-select if possible
        if current_data is not None:
            for i in range(self._subscriber_combo.count()):
                if self._subscriber_combo.itemData(i) == current_data:
                    self._subscriber_combo.setCurrentIndex(i)
                    break
        self._subscriber_combo.blockSignals(False)

    def _on_log_received(self, payload: dict) -> None:
        entries = payload.get("entries", [])
        self._model.set_rows(entries)
        has_more = payload.get("has_more", False)
        suffix = " (limit reached — increase 'hours' to see more)" if has_more else ""
        self._summary_label.setText(
            f"Showing {len(entries)} notifications{suffix}"
        )
        self._last_refreshed_label.setText(
            f"Refreshed at {datetime.now().strftime('%H:%M:%S')}"
        )

    def _on_log_failed(self, message: str) -> None:
        self._last_refreshed_label.setText(f"Refresh failed: {message}")
        logger.warning("Notification log fetch failed: %s", message)

    # ---------- CSV export ---------- #

    def _on_export_csv_clicked(self) -> None:
        rows = []
        for r in range(self._model.rowCount()):
            entry = self._model.row_at(r)
            if entry is None:
                continue
            rows.append([
                entry.get("last_sent_at", ""),
                entry.get("subscriber_id", ""),
                entry.get("subscriber_address", ""),
                entry.get("subscriber_transport", ""),
                f"{entry.get('ticker_exchange', '')}:{entry.get('ticker_symbol', '')}"
                    if entry.get("ticker_symbol") else "",
                entry.get("event_signal", ""),
                entry.get("event_id", ""),
                entry.get("event_bar_timestamp", ""),
                entry.get("notification_count", 1),
            ])
        export_rows_to_csv(
            self,
            default_filename=timestamped_filename("notifications"),
            columns=[
                "last_sent_at", "subscriber_id", "subscriber_address",
                "subscriber_transport", "ticker", "signal",
                "event_id", "event_bar_timestamp", "notification_count",
            ],
            rows=rows,
        )

    # ---------- lifecycle ---------- #

    def on_close(self) -> None:
        try:
            if self._scheduler.is_running:
                self._scheduler.stop()
        except RuntimeError:
            pass
