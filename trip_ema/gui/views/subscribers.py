"""Subscribers admin view (Slice 4).

Layout:
  ┌──────────────────────────────────────────────────────────────┐
  │ [Add subscriber] [Export CSV] [Refresh]    Filter: [text...] │
  ├─────────────────────────────┬────────────────────────────────┤
  │ Subscribers table           │ Edit form                      │
  │ id | transport | address    │ Transport: [email ▾]           │
  │  | cadence | active         │ Address:   [...............]   │
  │ ...                         │ Cadence:   [60 ▾] minutes      │
  │                             │ ☑ Active                       │
  │                             │ ────────────────────────────── │
  │                             │ Filters:                       │
  │                             │ • All tickers / All signals    │
  │                             │ • RELIANCE / BUY,SELL          │
  │                             │ [Add filter] [Remove]          │
  │                             │ ────────────────────────────── │
  │                             │ [Save] [Test notify] [Delete]  │
  └─────────────────────────────┴────────────────────────────────┘

Behavior:
  - Table: QAbstractTableModel, one row per subscriber.
  - Selecting a row populates the edit form (which holds the LOCAL
    draft). Editing form fields does NOT mutate the server; [Save]
    PATCHes the changed scalars then PUTs the canonical filter set.
  - [Add subscriber] clears the form to defaults and disables the
    table; Save creates a new row. A catch-all filter is pre-seeded
    on the new draft so saving without further filter edits still
    produces a working subscriber (without this, the distributor's
    find_pending_for_subscriber short-circuits to [] on zero filter
    rows and no notifications ever go out — see CATCH_ALL_FILTER
    below).
  - [Test notify] fires the server-side endpoint; result via dialog.
  - [Delete] confirms then DELETEs.
  - Token-less mode: all writes disabled; reads work.

Refresh cadence: 30s. Same pattern as Tickers tab.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from PySide6.QtCore import (
    QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt, Signal,
)
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QFormLayout,
    QGroupBox, QHBoxLayout, QHeaderView, QInputDialog, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMessageBox,
    QPushButton, QSpinBox, QTableView, QVBoxLayout, QWidget,
)

from ..csv_export import export_rows_to_csv, timestamped_filename
from ..db import GuiDbSession
from ..http_client import ApiError, TripEmaHttpClient
from ..workers import RefreshScheduler, submit

logger = logging.getLogger(__name__)


# Column indices for the subscribers table.
COL_SUB_ID = 0
COL_TRANSPORT = 1
COL_ADDRESS = 2
COL_CADENCE = 3
COL_ACTIVE = 4
COL_LAST_NOTIFIED = 5
COL_FILTERS_COUNT = 6

HEADERS = [
    "ID", "Transport", "Address", "Cadence (min)",
    "Active", "Last notified", "Filters",
]

TRANSPORT_CHOICES = ["email", "webhook", "slack", "sms"]
SMS_DISABLED_TOOLTIP = (
    "SMS transport is not yet implemented server-side; "
    "test notifications will return 501."
)

# Cadence dropdown values — server CHECK constraint is 15-1440 step 15.
CADENCE_CHOICES = [15, 30, 60, 120, 240, 480, 1440]


def _catch_all_filter() -> dict:
    """A single 'all tickers, all signals' filter row, in the same shape
    the form's filter list and the HTTP create payload expect.

    Pre-seeded on new-subscriber drafts (see _on_add_clicked) and used
    as the auto-add option when a Save is attempted with zero filters
    (see _on_save_clicked). Keep this in sync with the server-side
    SubscriptionFilterPayload shape — both fields NULL means 'match
    every event'.
    """
    return {
        "subscription_id": None,
        "ticker_id": None,
        "ticker_symbol": None,
        "ticker_exchange": None,
        "signal_filter": None,
    }


# ---------- table model ---------- #


class SubscriberTableModel(QAbstractTableModel):
    """One row per subscriber. Filters count is shown but the full
    filter list lives in the edit form."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows: list[dict] = []

    def set_rows(self, rows: list[dict]) -> None:
        self.beginResetModel()
        self._rows = rows
        self.endResetModel()

    def row_at(self, source_row: int) -> Optional[dict]:
        if 0 <= source_row < len(self._rows):
            return self._rows[source_row]
        return None

    # QAbstractTableModel API

    def rowCount(self, parent=QModelIndex()) -> int:
        if parent.isValid():
            return 0
        return len(self._rows)

    def columnCount(self, parent=QModelIndex()) -> int:
        return len(HEADERS)

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):
        if not index.isValid():
            return None
        row = self._rows[index.row()]
        col = index.column()
        if role == Qt.DisplayRole:
            return self._display(row, col)
        if role == Qt.TextAlignmentRole:
            if col in (COL_SUB_ID, COL_CADENCE, COL_FILTERS_COUNT):
                return int(Qt.AlignRight | Qt.AlignVCenter)
        if role == Qt.ForegroundRole:
            if not row.get("is_active", True):
                return QBrush(QColor("#888888"))
        if role == Qt.UserRole:
            return row
        return None

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role != Qt.DisplayRole:
            return None
        if orientation == Qt.Horizontal:
            return HEADERS[section] if 0 <= section < len(HEADERS) else None
        return str(section + 1)

    def _display(self, row: dict, col: int) -> str:
        if col == COL_SUB_ID:
            return str(row.get("subscriber_id", ""))
        if col == COL_TRANSPORT:
            return row.get("transport", "")
        if col == COL_ADDRESS:
            return (row.get("address") or "")[:80]
        if col == COL_CADENCE:
            return str(row.get("notify_cadence_minutes", ""))
        if col == COL_ACTIVE:
            return "yes" if row.get("is_active") else "no"
        if col == COL_LAST_NOTIFIED:
            ln = row.get("last_notified_at")
            if not ln:
                return "—"
            return ln[:19].replace("T", " ")
        if col == COL_FILTERS_COUNT:
            return str(len(row.get("filters", []) or []))
        return ""


# ---------- view ---------- #


class SubscribersView(QWidget):

    REFRESH_CADENCE_SECONDS = 30.0

    def __init__(
        self,
        db: GuiDbSession,
        http: TripEmaHttpClient,
        *,
        parent: Optional[QWidget] = None,
        start_refresh_on_init: bool = True,
        populate_ticker_lookup_on_init: bool = True,
    ):
        super().__init__(parent)
        self._db = db
        self._http = http

        self._model = SubscriberTableModel()
        self._proxy = QSortFilterProxyModel()
        self._proxy.setSourceModel(self._model)
        self._proxy.setSortRole(Qt.DisplayRole)
        self._proxy.setFilterKeyColumn(COL_ADDRESS)
        self._proxy.setFilterCaseSensitivity(Qt.CaseInsensitive)

        # In-progress form state — separate from server state until Save.
        self._editing_subscriber_id: Optional[int] = None
        """None when adding a NEW subscriber; otherwise the id being edited."""
        self._form_filters: list[dict] = []
        """Local draft of the filter list. Each dict is a
        SubscriptionFilterPayload-shaped row."""
        self._form_dirty: bool = False
        """True after any form field changes. Save button reflects this."""

        # Cache of (exchange, symbol, ticker_id) for filter editor
        self._ticker_lookup: list[dict] = []

        self._build_ui()
        if populate_ticker_lookup_on_init:
            self._populate_ticker_lookup()
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

        outer.addLayout(self._build_action_row())

        body = QHBoxLayout()
        body.setSpacing(10)
        body.addWidget(self._build_table_panel(), 3)
        body.addWidget(self._build_form_panel(), 2)
        outer.addLayout(body, 1)

        outer.addWidget(self._build_status_row())

    def _build_action_row(self) -> QHBoxLayout:
        row = QHBoxLayout()

        self._add_button = QPushButton("Add subscriber")
        self._add_button.clicked.connect(self._on_add_clicked)
        if not self._http.token_configured:
            self._add_button.setEnabled(False)
            self._add_button.setToolTip(
                "Set TRIP_EMA_ADMIN_TOKEN to enable adding subscribers."
            )
        row.addWidget(self._add_button)

        self._export_button = QPushButton("Export CSV")
        self._export_button.clicked.connect(self._on_export_csv_clicked)
        row.addWidget(self._export_button)

        row.addStretch(1)

        row.addWidget(QLabel("Filter:"))
        self._search_input = QLineEdit()
        self._search_input.setPlaceholderText("address substring")
        self._search_input.textChanged.connect(self._proxy.setFilterFixedString)
        row.addWidget(self._search_input, 0)

        self._refresh_button = QPushButton("Refresh")
        self._refresh_button.clicked.connect(self._on_refresh_button_clicked)
        row.addWidget(self._refresh_button)
        return row

    def _build_table_panel(self) -> QWidget:
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
        self._table.selectionModel().selectionChanged.connect(
            self._on_selection_changed,
        )
        self._table.sortByColumn(COL_SUB_ID, Qt.AscendingOrder)
        return self._table

    def _build_form_panel(self) -> QWidget:
        box = QGroupBox("Subscriber detail")
        outer = QVBoxLayout(box)

        form = QFormLayout()
        self._form_transport = QComboBox()
        for t in TRANSPORT_CHOICES:
            self._form_transport.addItem(t)
        # Mark SMS as "not yet implemented"
        sms_idx = TRANSPORT_CHOICES.index("sms")
        item_model = self._form_transport.model()
        sms_item = item_model.item(sms_idx)
        if sms_item is not None:
            sms_item.setEnabled(False)
            sms_item.setToolTip(SMS_DISABLED_TOOLTIP)
        self._form_transport.currentTextChanged.connect(self._mark_dirty)
        form.addRow("Transport:", self._form_transport)

        self._form_address = QLineEdit()
        self._form_address.setPlaceholderText("email / URL / channel id")
        self._form_address.textChanged.connect(self._mark_dirty)
        form.addRow("Address:", self._form_address)

        self._form_cadence = QComboBox()
        for c in CADENCE_CHOICES:
            self._form_cadence.addItem(f"{c} min", c)
        self._form_cadence.setCurrentText("60 min")
        self._form_cadence.currentTextChanged.connect(self._mark_dirty)
        form.addRow("Cadence:", self._form_cadence)

        self._form_active = QCheckBox("Active")
        self._form_active.setChecked(True)
        self._form_active.stateChanged.connect(self._mark_dirty)
        form.addRow("", self._form_active)

        outer.addLayout(form)

        # Filters section
        outer.addWidget(QLabel("Filters:"))
        self._filters_list = QListWidget()
        self._filters_list.setAlternatingRowColors(True)
        self._filters_list.setMaximumHeight(150)
        outer.addWidget(self._filters_list)

        filter_buttons = QHBoxLayout()
        self._add_filter_button = QPushButton("Add filter")
        self._add_filter_button.clicked.connect(self._on_add_filter_clicked)
        filter_buttons.addWidget(self._add_filter_button)
        self._remove_filter_button = QPushButton("Remove")
        self._remove_filter_button.clicked.connect(self._on_remove_filter_clicked)
        filter_buttons.addWidget(self._remove_filter_button)
        filter_buttons.addStretch(1)
        outer.addLayout(filter_buttons)

        # Action buttons at bottom
        action_row = QHBoxLayout()
        self._save_button = QPushButton("Save")
        self._save_button.clicked.connect(self._on_save_clicked)
        self._save_button.setEnabled(False)
        action_row.addWidget(self._save_button)

        self._test_button = QPushButton("Test notify")
        self._test_button.clicked.connect(self._on_test_clicked)
        self._test_button.setEnabled(False)
        action_row.addWidget(self._test_button)

        self._delete_button = QPushButton("Delete")
        self._delete_button.clicked.connect(self._on_delete_clicked)
        self._delete_button.setEnabled(False)
        action_row.addWidget(self._delete_button)

        action_row.addStretch(1)
        outer.addLayout(action_row)

        # Disable everything when no token
        if not self._http.token_configured:
            for btn in (
                self._add_filter_button, self._remove_filter_button,
                self._save_button, self._test_button, self._delete_button,
            ):
                btn.setEnabled(False)
                btn.setToolTip(
                    "Set TRIP_EMA_ADMIN_TOKEN to enable edits."
                )

        # Begin in cleared/disabled state
        self._clear_form()
        return box

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

    def _on_refresh_tick(self) -> None:
        http = self._http
        submit(
            lambda: http.list_subscribers(),
            on_finished=self._on_subscribers_received,
            on_failed=self._on_subscribers_failed,
        )

    def _on_subscribers_received(self, payload: dict) -> None:
        subs = payload.get("subscribers", [])
        self._model.set_rows(subs)
        active_count = sum(1 for s in subs if s.get("is_active"))
        self._summary_label.setText(
            f"{len(subs)} subscribers ({active_count} active)"
        )
        self._last_refreshed_label.setText(
            f"Refreshed at {datetime.now().strftime('%H:%M:%S')}"
        )
        # Re-select the row currently being edited so the user doesn't
        # lose their place across a refresh.
        if self._editing_subscriber_id is not None:
            self._reselect_subscriber(self._editing_subscriber_id)

    def _on_subscribers_failed(self, message: str) -> None:
        self._last_refreshed_label.setText(f"Refresh failed: {message}")
        logger.warning("Subscribers fetch failed: %s", message)

    def _reselect_subscriber(self, subscriber_id: int) -> None:
        for proxy_row in range(self._proxy.rowCount()):
            src_idx = self._proxy.mapToSource(self._proxy.index(proxy_row, 0))
            row = self._model.row_at(src_idx.row())
            if row and row.get("subscriber_id") == subscriber_id:
                self._table.selectRow(proxy_row)
                return

    # ---------- selection ---------- #

    def _on_selection_changed(self, selected, _deselected) -> None:
        idxs = selected.indexes()
        if not idxs:
            return
        src_row = self._proxy.mapToSource(idxs[0]).row()
        row = self._model.row_at(src_row)
        if row is None:
            return
        self._load_row_into_form(row)

    def _load_row_into_form(self, row: dict) -> None:
        """Populate the form from a server-side subscriber dict.
        Disables dirty-tracking while populating, then re-enables."""
        self._editing_subscriber_id = row.get("subscriber_id")
        # block signals so dirty flag doesn't get set by our own population
        self._with_dirty_suppressed(lambda: self._populate_form(row))
        self._form_filters = list(row.get("filters", []) or [])
        self._refresh_filters_list()
        self._form_dirty = False
        self._update_button_states()

    def _populate_form(self, row: dict) -> None:
        transport = row.get("transport", "email")
        if transport in TRANSPORT_CHOICES:
            self._form_transport.setCurrentText(transport)
        self._form_address.setText(row.get("address", ""))
        cadence = row.get("notify_cadence_minutes", 60)
        # Find the closest match in CADENCE_CHOICES
        idx = 2  # default 60
        for i, c in enumerate(CADENCE_CHOICES):
            if c == cadence:
                idx = i
                break
        self._form_cadence.setCurrentIndex(idx)
        self._form_active.setChecked(bool(row.get("is_active", True)))

    def _refresh_filters_list(self) -> None:
        self._filters_list.clear()
        for f in self._form_filters:
            self._filters_list.addItem(_format_filter(f))

    # ---------- form actions ---------- #

    def _on_add_clicked(self) -> None:
        """Begin a new-subscriber draft. Table selection cleared so we
        don't accidentally save against an existing row.

        CATCH_ALL_FILTER: A single 'all tickers / all signals' filter
        row is pre-seeded into the draft so that a subscriber created
        without further filter edits is still functional. Without
        this seed, `SignalsRepository.find_pending_for_subscriber`
        short-circuits to [] on any subscriber with zero `subscriptions`
        rows, and the SubscriberDistributor silently drops the
        subscriber on every cycle — `notification_log` stays empty,
        `subscribers.last_notified_at` never advances, and the operator
        gets no signal that anything is wrong. The user can still
        remove this filter and add specific ones; the empty-filters
        guard in `_on_save_clicked` is the second line of defense.
        """
        if self._form_dirty:
            confirm = QMessageBox.question(
                self, "Discard unsaved changes?",
                "You have unsaved changes. Discard them?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if confirm != QMessageBox.Yes:
                return
        self._table.clearSelection()
        self._editing_subscriber_id = None
        self._with_dirty_suppressed(self._reset_form_to_defaults)
        self._form_filters = [_catch_all_filter()]
        self._refresh_filters_list()
        self._form_dirty = True
        self._update_button_states()
        self._form_address.setFocus()

    def _reset_form_to_defaults(self) -> None:
        self._form_transport.setCurrentText("email")
        self._form_address.setText("")
        # Default cadence: 60 min
        for i, c in enumerate(CADENCE_CHOICES):
            if c == 60:
                self._form_cadence.setCurrentIndex(i)
                break
        self._form_active.setChecked(True)

    def _on_save_clicked(self) -> None:
        if not self._http.token_configured:
            return
        # Validate
        address = self._form_address.text().strip()
        if not address:
            QMessageBox.warning(
                self, "Address required",
                "Please enter an address for this subscriber.",
            )
            return
        transport = self._form_transport.currentText()
        if transport == "sms":
            QMessageBox.warning(
                self, "SMS not supported",
                SMS_DISABLED_TOOLTIP,
            )
            return
        cadence = self._form_cadence.currentData()
        active = self._form_active.isChecked()

        # Empty-filters guard: a subscriber with zero subscription rows
        # is never matched by the distributor. The Add-subscriber flow
        # pre-seeds a catch-all, but the user can edit-then-remove all
        # filters; this Yes/No catches that path. Confirming auto-adds
        # the catch-all back so the Save proceeds with a working state.
        if not self._form_filters:
            confirm = QMessageBox.question(
                self, "No filters set",
                "This subscriber has no filters, so the distributor "
                "will never match any events to them and no "
                "notifications will be sent.\n\n"
                "Add an 'all tickers / all signals' catch-all and "
                "continue?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes,
            )
            if confirm != QMessageBox.Yes:
                return
            self._form_filters = [_catch_all_filter()]
            self._refresh_filters_list()

        if self._editing_subscriber_id is None:
            # Create
            self._fire_save_as_create(
                transport=transport, address=address,
                cadence=cadence, active=active,
                filters=self._form_filters,
            )
        else:
            # Update existing
            self._fire_save_as_update(
                subscriber_id=self._editing_subscriber_id,
                transport=transport, address=address,
                cadence=cadence, active=active,
                filters=self._form_filters,
            )

    def _fire_save_as_create(
        self, *, transport: str, address: str,
        cadence: int, active: bool, filters: list,
    ) -> None:
        http = self._http
        self._set_save_in_progress(True)

        def do_create():
            return http.create_subscriber(
                transport=transport, address=address,
                notify_cadence_minutes=cadence, is_active=active,
                filters=filters,
            )

        submit(
            do_create,
            on_finished=self._on_create_succeeded,
            on_failed=self._on_save_failed,
        )

    def _on_create_succeeded(self, new_row: dict) -> None:
        self._set_save_in_progress(False)
        self._form_dirty = False
        self._editing_subscriber_id = new_row.get("subscriber_id")
        QMessageBox.information(
            self, "Subscriber created",
            f"Created subscriber {new_row.get('subscriber_id')} "
            f"({new_row.get('transport')} → {new_row.get('address')})."
        )
        self._scheduler.trigger_now()

    def _fire_save_as_update(
        self, *, subscriber_id: int, transport: str,
        address: str, cadence: int, active: bool, filters: list,
    ) -> None:
        http = self._http
        self._set_save_in_progress(True)
        # Two HTTP calls: PATCH scalars, then PUT filters. If the first
        # fails we don't fire the second. We do both serially in one
        # worker fn so the GUI only sees one finished/failed signal.

        def do_update():
            http.update_subscriber(
                subscriber_id,
                transport=transport, address=address,
                notify_cadence_minutes=cadence, is_active=active,
            )
            return http.replace_filters(subscriber_id, filters)

        submit(
            do_update,
            on_finished=self._on_update_succeeded,
            on_failed=self._on_save_failed,
        )

    def _on_update_succeeded(self, _payload: dict) -> None:
        self._set_save_in_progress(False)
        self._form_dirty = False
        QMessageBox.information(self, "Saved", "Subscriber updated.")
        self._scheduler.trigger_now()

    def _on_save_failed(self, message: str) -> None:
        self._set_save_in_progress(False)
        QMessageBox.warning(self, "Save failed", message)

    def _set_save_in_progress(self, in_progress: bool) -> None:
        self._save_button.setEnabled(not in_progress)
        self._add_button.setEnabled(not in_progress)
        self._delete_button.setEnabled(
            not in_progress and self._editing_subscriber_id is not None,
        )

    def _on_test_clicked(self) -> None:
        if self._editing_subscriber_id is None:
            QMessageBox.information(
                self, "Save first",
                "Save the subscriber before sending a test notification.",
            )
            return
        sid = self._editing_subscriber_id
        http = self._http
        self._test_button.setEnabled(False)

        def on_done(payload: dict):
            self._test_button.setEnabled(self._http.token_configured)
            ok = payload.get("succeeded", False)
            QMessageBox.information(
                self,
                "Test notification result",
                f"{'Success' if ok else 'Failed'}\n\n"
                f"Transport: {payload.get('transport')}\n"
                f"Address:   {payload.get('address')}\n"
                f"Notes:     {payload.get('notes', '—')}",
            )

        def on_fail(message: str):
            self._test_button.setEnabled(self._http.token_configured)
            QMessageBox.warning(
                self, "Test notification request failed",
                message,
            )
        submit(
            lambda: http.test_notification(sid),
            on_finished=on_done,
            on_failed=on_fail,
        )

    def _on_delete_clicked(self) -> None:
        if self._editing_subscriber_id is None:
            return
        sid = self._editing_subscriber_id
        addr = self._form_address.text()
        confirm = QMessageBox.question(
            self, "Delete subscriber?",
            f"Permanently delete subscriber {sid} ({addr})?\n"
            f"This also deletes their filter rows and notification log "
            f"entries (FK cascade).",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return

        http = self._http
        self._delete_button.setEnabled(False)
        def on_done(_payload):
            self._editing_subscriber_id = None
            self._clear_form()
            QMessageBox.information(self, "Deleted", f"Subscriber {sid} deleted.")
            self._scheduler.trigger_now()
        def on_fail(message: str):
            self._delete_button.setEnabled(True)
            QMessageBox.warning(self, "Delete failed", message)
        submit(
            lambda: http.delete_subscriber(sid),
            on_finished=on_done,
            on_failed=on_fail,
        )

    # ---------- filters editor ---------- #

    def _on_add_filter_clicked(self) -> None:
        """Open a tiny picker dialog: choose ticker (or 'All') + signals."""
        # Step 1: ticker
        ticker_choices = ["(All tickers)"] + [
            f"{t['exchange']}:{t['symbol']}" for t in self._ticker_lookup
        ]
        ticker_label, ok = QInputDialog.getItem(
            self, "Add filter — choose ticker",
            "Ticker:", ticker_choices, 0, False,
        )
        if not ok:
            return
        if ticker_label == "(All tickers)":
            ticker_id = None
            ticker_symbol = None
            ticker_exchange = None
        else:
            # Match back to the lookup
            ex, sym = ticker_label.split(":", 1)
            picked = next(
                (t for t in self._ticker_lookup
                 if t["exchange"] == ex and t["symbol"] == sym),
                None,
            )
            if picked is None:
                QMessageBox.warning(self, "Unknown ticker", ticker_label)
                return
            ticker_id = picked["ticker_id"]
            ticker_symbol = picked["symbol"]
            ticker_exchange = picked["exchange"]

        # Step 2: signal_filter
        signal_label, ok = QInputDialog.getItem(
            self, "Add filter — choose signals",
            "Signals (comma-separated, blank = all):",
            ["(All signals)", "BUY", "SELL", "BUY,SELL"],
            0, True,  # editable so the user can enter custom like 'BUY,EXIT_FROM_BUY'
        )
        if not ok:
            return
        signal_filter = None if signal_label == "(All signals)" else signal_label

        self._form_filters.append({
            "subscription_id": None,
            "ticker_id": ticker_id,
            "ticker_symbol": ticker_symbol,
            "ticker_exchange": ticker_exchange,
            "signal_filter": signal_filter,
        })
        self._refresh_filters_list()
        self._mark_dirty()

    def _on_remove_filter_clicked(self) -> None:
        row = self._filters_list.currentRow()
        if row < 0 or row >= len(self._form_filters):
            return
        del self._form_filters[row]
        self._refresh_filters_list()
        self._mark_dirty()

    # ---------- helpers ---------- #

    def _populate_ticker_lookup(self) -> None:
        """For the Add Filter dialog. Reads NSE tickers from DB."""
        from sqlalchemy import text
        def fetch():
            with self._db.connect() as conn:
                rows = conn.execute(text("""
                    SELECT ticker_id, exchange, symbol
                    FROM tickers
                    WHERE is_active = TRUE AND delisted_at IS NULL
                    ORDER BY symbol ASC
                """)).fetchall()
                return [
                    {"ticker_id": r.ticker_id,
                     "exchange": r.exchange, "symbol": r.symbol}
                    for r in rows
                ]
        submit(
            fetch,
            on_finished=self._on_ticker_lookup_ready,
            on_failed=self._on_ticker_lookup_failed,
        )

    def _on_ticker_lookup_ready(self, rows: list) -> None:
        self._ticker_lookup = rows

    def _on_ticker_lookup_failed(self, message: str) -> None:
        logger.warning("Ticker lookup for filters failed: %s", message)

    def _mark_dirty(self, *_args) -> None:
        if self._suppress_dirty:
            return
        self._form_dirty = True
        self._update_button_states()

    _suppress_dirty: bool = False

    def _with_dirty_suppressed(self, fn) -> None:
        self._suppress_dirty = True
        try:
            fn()
        finally:
            self._suppress_dirty = False

    def _update_button_states(self) -> None:
        if not self._http.token_configured:
            return
        editing = self._editing_subscriber_id is not None
        self._save_button.setEnabled(self._form_dirty)
        self._delete_button.setEnabled(editing)
        self._test_button.setEnabled(editing)

    def _clear_form(self) -> None:
        self._editing_subscriber_id = None
        self._form_dirty = False
        self._with_dirty_suppressed(self._reset_form_to_defaults)
        self._form_filters = []
        self._refresh_filters_list()
        self._update_button_states()
        self._save_button.setEnabled(False)
        self._delete_button.setEnabled(False)
        self._test_button.setEnabled(False)

    # ---------- CSV export ---------- #

    def _on_export_csv_clicked(self) -> None:
        rows = []
        for r in range(self._model.rowCount()):
            sub = self._model.row_at(r)
            if sub is None:
                continue
            rows.append([
                sub.get("subscriber_id", ""),
                sub.get("transport", ""),
                sub.get("address", ""),
                sub.get("notify_cadence_minutes", ""),
                "yes" if sub.get("is_active") else "no",
                sub.get("last_notified_at") or "",
                len(sub.get("filters", []) or []),
                sub.get("created_at") or "",
            ])
        export_rows_to_csv(
            self,
            default_filename=timestamped_filename("subscribers"),
            columns=[
                "subscriber_id", "transport", "address",
                "notify_cadence_minutes", "is_active",
                "last_notified_at", "filter_count", "created_at",
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


def _format_filter(f: dict) -> str:
    """Render a single filter for the list widget. e.g.
    'RELIANCE / BUY,SELL'  or  'All tickers / All signals'."""
    if f.get("ticker_symbol"):
        ticker_part = f"{f['ticker_exchange']}:{f['ticker_symbol']}"
    elif f.get("ticker_id"):
        ticker_part = f"ticker_id={f['ticker_id']}"
    else:
        ticker_part = "All tickers"
    sig = f.get("signal_filter") or "All signals"
    return f"{ticker_part}  /  {sig}"