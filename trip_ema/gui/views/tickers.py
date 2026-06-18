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

# Default sort order (Phase 16e)

The table uses a custom `TickerSortFilterProxy` that orders rows in
two layers:

1. **Signalled stocks first**, grouped by the signal fired in the
   most recent compute tick:
     - 🟢 BUY first, alphabetical within
     - 🔴 SELL next, alphabetical within
     - 🔵 EXIT_FROM_BUY and EXIT_FROM_SELL last, alphabetical within
       (they share the same blue background and the same group rank)
   These rows also carry their light-color background from Phase 16d,
   so the visual matches the sort.

2. **Non-signalled stocks below**, ordered by whatever column the
   user has clicked. The default at view-construction is **tier ASC,
   then symbol ASC**, which produces the "high-mkt-cap-looking" head
   you see on first load.

Why the second layer looks like "by market cap":

  - `tickers.tier` is computed weekly by `trip-ema-etl recompute-tiers`.
  - Tier 1 = top **200** tickers ranked by 30-trading-day **Average
    Daily Value** (close × volume). That's not market cap directly,
    but ADV correlates strongly with index inclusion at NSE, so the
    top of the list reads as "NIFTY 100/200-ish".
  - Tier 2 = next **500** by ADV.
  - Tier 3 = everything else (incl. zero-ADV new listings, which can
    never jump to Tier 1).
  - Both `tier_1_size` and `tier_2_size` are knobs in `config.yaml`
    under the `etl:` block; the defaults match the README.

When the user clicks a column header, the secondary sort changes to
that column but the signalled-first grouping stays intact — Phase 16e
intent is "never scroll to find a fresh signal".
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any, Optional
from zoneinfo import ZoneInfo

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
    last_run_signals, list_tickers, recent_signal_events,
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


# Background colors for rows whose ticker fired a signal in the most
# recent compute tick. Applied to every cell in the row via
# Qt.BackgroundRole — sits below the row's Qt.ForegroundRole (which is
# used for parked/delisted text color), so a parked ticker with a
# fresh BUY will show light-green background with red text.
SIGNAL_BACKGROUND_COLORS = {
    "BUY":            "#c8e6c9",   # light green
    "SELL":           "#ffcdd2",   # light red
    "EXIT_FROM_BUY":  "#bbdefb",   # light blue (change to neutral from BUY)
    "EXIT_FROM_SELL": "#bbdefb",   # light blue (change to neutral from SELL)
}


class TickerTableModel(QAbstractTableModel):
    """Model wrapping a list of TickerRow. Implements only what
    QTableView needs (rowCount, columnCount, data, headerData)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows: list[TickerRow] = []
        # Phase 16d — most-recent-tick signals, keyed by ticker_id.
        # Drives the row's Qt.BackgroundRole. Empty dict = no recent
        # signals, no coloring. Updated atomically via set_rows().
        self._last_signals: dict[int, str] = {}

    # ---------- mutators ---------- #

    def set_rows(
        self,
        rows: list[TickerRow],
        last_signals: Optional[dict[int, str]] = None,
    ) -> None:
        """Replace the entire row set. Bulk update is cheaper than
        per-row delete-and-insert for our refresh cadence.

        `last_signals` (Phase 16d): optional dict {ticker_id: signal_type}
        for tickers that fired in the most recent compute pass. When
        provided, rows whose ticker_id is in this dict are rendered with
        a light background color matching the signal type. When omitted
        (or None), the previous coloring is cleared — passing the
        previous dict through is the caller's responsibility. Existing
        single-argument callers (tests, legacy paths) keep working
        unchanged; they implicitly clear coloring.
        """
        self.beginResetModel()
        self._rows = rows
        self._last_signals = last_signals if last_signals is not None else {}
        self.endResetModel()

    def row_at(self, source_row: int) -> Optional[TickerRow]:
        if 0 <= source_row < len(self._rows):
            return self._rows[source_row]
        return None

    @property
    def last_signals(self) -> dict[int, str]:
        """Read-only view of the most-recent-tick signals dict, keyed
        by ticker_id. The custom sort proxy reads this to put signalled
        rows at the top of the table."""
        return self._last_signals

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
        if role == Qt.BackgroundRole:
            # Phase 16d — color the row based on the most recent tick's
            # signal for this ticker, if any. We return a brush for
            # every column so the entire row paints uniformly. Returning
            # None for tickers without a recent signal lets Qt use the
            # alternating-row color (set on the view), so non-signalled
            # rows look the same as before.
            sig = self._last_signals.get(row.ticker_id)
            if sig is not None:
                hex_color = SIGNAL_BACKGROUND_COLORS.get(sig)
                if hex_color is not None:
                    return QBrush(QColor(hex_color))
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


class TickerSortFilterProxy(QSortFilterProxyModel):
    """Phase 16e — signal-grouped sort.

    The Tickers tab's `QTableView` uses this proxy instead of the bare
    `QSortFilterProxyModel`. The override is in `lessThan`: rows with
    a fresh signal in the most recent compute tick float to the top,
    grouped by color, alphabetical within group. Rows without a fresh
    signal stay below and continue to obey whatever column the user
    has clicked.

    The primary sort key per row is:
      - 0 if the row's ticker is in BUY (green)
      - 1 if SELL (red)
      - 2 if EXIT_FROM_BUY or EXIT_FROM_SELL (blue, share rank)
      - 99 otherwise (the "below the signalled group" bucket)

    Within ranks 0/1/2, sort is symbol ASC (case-insensitive). Within
    rank 99, the proxy delegates to the parent class's `lessThan`, so
    the user's clicked column (default: tier ASC) keeps working
    exactly as before for the non-signalled portion.

    Why a custom proxy instead of a custom sort role on the model:
      - QSortFilterProxyModel calls `data(index, sortRole)` for both
        sides of each `lessThan` comparison; encoding a (group, symbol)
        tuple as a single string sort key works but loses the natural
        "ascending" intuition and breaks when the user clicks a
        different column. A proxy override is the cleaner separation
        of concerns: the model stays one column = one value, the
        proxy owns ordering policy.

    Thread safety: only called from the Qt main thread (sort is a
    GUI-thread operation). No locking required.
    """

    SIGNAL_GROUP_ORDER = {
        "BUY":            0,
        "SELL":           1,
        "EXIT_FROM_BUY":  2,
        "EXIT_FROM_SELL": 2,
    }
    UNSIGNALLED_GROUP = 99

    def lessThan(self, left: QModelIndex, right: QModelIndex) -> bool:
        # The proxy always works against the source model; access it
        # via sourceModel() so this proxy is reusable even if a future
        # refactor swaps the model class.
        src = self.sourceModel()
        if not isinstance(src, TickerTableModel):
            # Defensive — the view never wires a non-TickerTableModel,
            # but if someone does in a test, fall through to default.
            return super().lessThan(left, right)

        left_row = src.row_at(left.row())
        right_row = src.row_at(right.row())
        if left_row is None or right_row is None:
            return super().lessThan(left, right)

        signals = src.last_signals
        left_sig = signals.get(left_row.ticker_id)
        right_sig = signals.get(right_row.ticker_id)
        left_group = self._group_rank(left_sig)
        right_group = self._group_rank(right_sig)

        # Layer 1: cross-group comparison wins immediately. Signalled
        # groups (0/1/2) sort before unsignalled (99), and within
        # signalled, BUY < SELL < EXIT.
        if left_group != right_group:
            return left_group < right_group

        # Layer 2a: same signalled group → alphabetical by symbol.
        # case-insensitive so "abc" sorts with "ABC".
        if left_group != self.UNSIGNALLED_GROUP:
            return left_row.symbol.lower() < right_row.symbol.lower()

        # Layer 2b: both unsignalled → defer to whatever column the
        # user has clicked. The view's initial sortByColumn() call
        # makes this Tier ASC by default; subsequent column-header
        # clicks change it.
        return super().lessThan(left, right)

    def _group_rank(self, signal_type: Optional[str]) -> int:
        if signal_type is None:
            return self.UNSIGNALLED_GROUP
        return self.SIGNAL_GROUP_ORDER.get(signal_type, self.UNSIGNALLED_GROUP)



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
        self._proxy = TickerSortFilterProxy()
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
        # Phase 16d — keep each event on its own line. Without this,
        # QTextEdit's default soft-wrap would split one event line
        # across two visible lines when the panel is narrow, making
        # the descending-time grouping hard to read.
        self._events_box.setLineWrapMode(QTextEdit.NoWrap)
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
                # Phase 16d — pull the most recent tick's signals so
                # the model can color those rows. Empty dict on
                # initial deploys / a still-priming service; that's
                # fine, the model treats it as "no coloring".
                signals = last_run_signals(conn)
            return rows, summary, signals

        submit(
            fetch_rows,
            on_finished=self._on_rows_received,
            on_failed=self._on_rows_failed,
        )

    def _on_rows_received(self, payload) -> None:
        rows, summary, signals = payload
        self._model.set_rows(rows, last_signals=signals)
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
                # Phase 16d — show the last 5 events per spec (was 10).
                return recent_signal_events(conn, ticker_id, limit=5)
        submit(
            fetch,
            on_finished=self._on_events_received,
            on_failed=self._on_events_failed,
        )

    def _on_events_received(self, events: list[SignalEventRow]) -> None:
        if not events:
            self._events_box.setPlainText("(no signal events for this ticker)")
            return
        # Phase 16e — render computed_at (when the engine fired the
        # signal) in IST, not bar_timestamp (the bar's start). Two
        # signal_events on the same weekly bar will share their
        # bar_timestamp but have distinct computed_at values; the
        # IST rendering separates them visually.
        ist = ZoneInfo("Asia/Kolkata")
        lines = []
        for e in events:
            ts = e.computed_at.astimezone(ist).strftime("%Y-%m-%d %H:%M")
            lines.append(
                f"{ts} : {e.timeframe} {e.signal} @ {e.price_at_signal:.2f}"
            )
        self._events_box.setPlainText("\n".join(lines))

    def _on_events_failed(self, message: str) -> None:
        self._events_box.setPlainText(f"(error: {message})")

    def _fetch_symbol_history(self, ticker_id: int) -> None:
        db = self._db
        def fetch():
            with db.connect() as conn:
                return ticker_symbol_history(conn, ticker_id)
        submit(
            fetch,
            on_finished=self._on_history_received,
            on_failed=self._on_history_failed,
        )

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
            f"This refreshes its metadata (tz) and "
            f"rebuilds its engine from current bars.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return

        self._reprime_button.setEnabled(False)
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
        submit(
            lambda: self._http.reprime_ticker(row.exchange, row.symbol),
            on_finished=on_done,
            on_failed=on_fail,
        )

    # ---------- lifecycle ---------- #

    def on_close(self) -> None:
        try:
            if self._scheduler.is_running:
                self._scheduler.stop()
        except RuntimeError:
            pass