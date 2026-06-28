"""ETL Status & Control view — Step 13b rewrite.

Replaces the Phase 14.6c/15c/15d action-bar layout with the Step 13
sketch:

  ┌─ Exchange [▼]  [Refresh Listings] [Recompute Tiers] ────────┐
  ├─ Activity Log Table  (scrollable, last 10-15 batch runs) ───┤
  ├─ Backfill panel ───────────┬─ Intraday panel ───────────────┤
  │  radio: Fetch / Don't      │  Stage list (✓/→/Queued)        │
  │  From [date]  To [date]    │                                 │
  │  Stocks [...]   [ADD]      │                                 │
  │  [chip area]               │                                 │
  │  [Backfill]    🔆          │  [Run Intraday]     🔆          │
  ├────────────────────────────┴─────────────────────────────────┤
  │ OVERALL MKT OVERVIEW                                         │
  │ #Buys=. #Sells=. #ExitBuys=. #ExitSells=.                    │
  │ #Green=. #Red=. #Neutral=.                       [See Trend] │
  │ #Stocks > highest_ema=. ... in between=.         [See Trend] │
  └──────────────────────────────────────────────────────────────┘

Scope by step:
  13b (this bundle): layout + Activity Log (real data from
    compute_runs) + Market Overview footer (real data from
    market_overview_daily) + [See Trend] dialogs (real pyqtgraph
    charts). Action buttons (Backfill, Run Intraday, Refresh
    Listings, Recompute Tiers) exist but are no-ops with a
    placeholder status label.
  13c: wires the Backfill panel — autocomplete, chips, QProcess,
    destructive-action modal.
  13d: wires the Intraday panel — live cycle invocation, stage
    progress updates. Plus Refresh Listings / Recompute Tiers
    button wiring.

Mutex rules:
  - "Something is running" iff EITHER compute_runs has a row
    with finished_at IS NULL for the selected exchange OR
    backfill_runs reports an active lock. Polled every 2s.
  - When running: all four action buttons disabled; the
    appropriate flashing-light indicator on. Backfill panel light
    on if kind='backfill'; Intraday panel light on if
    kind='intraday' or 'eod'.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from PySide6.QtCore import Qt, QSortFilterProxyModel
from PySide6.QtGui import QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QButtonGroup, QDateEdit, QFrame, QGroupBox, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QMessageBox, QPushButton,
    QRadioButton, QSizePolicy, QTableView, QVBoxLayout, QWidget,
)

from ..db import GuiDbSession
from ..http_client import TripEmaHttpClient
from ..workers import RefreshScheduler, submit
from .market_trend_dialog import MarketTrendDialog, TrendKind
from .widgets.exchange_picker import ExchangePicker
from .widgets.flashing_light import FlashingLight
from .widgets.stage_progress import (
    LIVE_CYCLE_STAGES, StageProgressList, StageStatus,
)
from .widgets.ticker_chip import TickerChipArea

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")


# ============================================================
# Polling cadences
# ============================================================
# Activity log: most-visible feedback. 5s strikes a balance between
# "noticeable freshness" and "DB load" for a small read.
ACTIVITY_LOG_CADENCE_SECONDS = 5.0
# Mutex driver: 2s gives quick UI reaction when a run starts or
# finishes. Two cheap queries (in_flight on compute_runs + active
# on backfill_runs).
MUTEX_CADENCE_SECONDS = 2.0
# Market overview footer: changes only on EoD ticks (once a day).
# 30s is plenty for the operator to see "today's snapshot computed
# at 16:30 IST" reflected within a minute.
MARKET_OVERVIEW_CADENCE_SECONDS = 30.0

# Activity log row limit. Sketch says "10-15 batch runs"; using 15.
ACTIVITY_LOG_ROW_LIMIT = 15


# ============================================================
# Column indices for the activity log table
# ============================================================
COL_ACTIVITY = 0
COL_STARTED = 1
COL_DURATION = 2
COL_TICKERS_ATTEMPTED = 3
COL_TICKERS_FETCHED = 4
COL_NOTES = 5

ACTIVITY_HEADERS = [
    "Activity", "Started", "Duration",
    "Tickers Attempted", "Tickers Fetched", "Notes",
]


# Operator-facing labels for compute_runs.kind values.
KIND_LABELS = {
    "intraday": "Intraday Run",
    "eod":      "EoD Run",
    "backfill": "Backfill",
}


def _format_started_at(started_at: datetime) -> str:
    """If today (IST), show HH:MM:SS; otherwise YYYY-MM-DD HH:MM."""
    started_ist = started_at.astimezone(IST)
    today_ist = datetime.now(IST).date()
    if started_ist.date() == today_ist:
        return started_ist.strftime("%H:%M:%S")
    return started_ist.strftime("%Y-%m-%d %H:%M")


def _format_duration(seconds: float | None) -> str:
    """Human-readable duration."""
    if seconds is None:
        return "(running)"
    if seconds < 60:
        return f"{seconds:.1f}s"
    if seconds < 3600:
        m, s = divmod(int(seconds), 60)
        return f"{m:02d}:{s:02d}m"
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}h"


def _format_notes_or_error(row) -> str:
    """If the run had a failure, show '❌ stage: error[:40]'.
    Otherwise show the notes field, or empty string."""
    if row.failed_stage is not None or row.error_message is not None:
        stage = row.failed_stage or "?"
        msg = (row.error_message or "")[:60]
        return f"❌ {stage}: {msg}"
    return row.notes or ""


# ============================================================
# The view
# ============================================================

class EtlStatusView(QWidget):
    """Operator console's ETL Status tab.

    Constructor signature preserved from the previous (Phase 15d)
    implementation so main_window.py doesn't need touching.
    `http` is currently unused; 13d will use it to trigger live
    ticks via /v1/admin/compute-now.
    """

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

        # Local mutex state — the GUI-side "just-clicked" flag,
        # complements the persistent compute_runs / backfill_runs
        # state for the immediate UI feedback window before the
        # row hits the DB.
        self._local_running: bool = False
        # Most-recent in-flight kind (None / 'intraday' / 'eod' /
        # 'backfill'). Drives which flashing light to turn on.
        self._inflight_kind: str | None = None

        # Build UI before wiring schedulers (handlers reference
        # widget attributes set in _build_ui).
        self._build_ui()

        # Three schedulers — different cadences for different data
        self._activity_scheduler = RefreshScheduler(
            ACTIVITY_LOG_CADENCE_SECONDS, run_on_start=True,
        )
        self._activity_scheduler.tick.connect(
            self._on_activity_tick,
        )

        self._mutex_scheduler = RefreshScheduler(
            MUTEX_CADENCE_SECONDS, run_on_start=True,
        )
        self._mutex_scheduler.tick.connect(self._on_mutex_tick)

        self._overview_scheduler = RefreshScheduler(
            MARKET_OVERVIEW_CADENCE_SECONDS, run_on_start=True,
        )
        self._overview_scheduler.tick.connect(self._on_overview_tick)

        # Picker drives all three — refresh when the exchange changes
        self._picker.selection_changed.connect(
            self._on_exchange_changed,
        )

        if start_refresh_on_init:
            self._activity_scheduler.start()
            self._mutex_scheduler.start()
            self._overview_scheduler.start()

    # ============================================================
    # UI construction
    # ============================================================

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)
        outer.setSpacing(8)

        # Top strip — no stretch (fixed height, content drives it)
        outer.addLayout(self._build_top_strip())
        # Activity log — stretch 5
        outer.addWidget(self._build_activity_log(), 5)
        # Backfill + Intraday side-by-side — stretch 10
        outer.addLayout(self._build_panels_row(), 10)
        # Market overview footer — stretch 4
        outer.addWidget(self._build_market_overview_footer(), 4)

        # Status line at the very bottom — placeholder for action
        # feedback messages until the buttons wire up in 13c/13d.
        self._status_label = QLabel("")
        self._status_label.setStyleSheet("color: #666; font-style: italic;")
        outer.addWidget(self._status_label)

    # --- Top strip ----------------------------------------------

    def _build_top_strip(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(12)

        self._picker = ExchangePicker(
            self._http, label_text="Exchange:", parent=self,
        )
        row.addWidget(self._picker)

        row.addSpacing(20)

        self._refresh_listings_btn = QPushButton("Refresh Listings")
        self._refresh_listings_btn.clicked.connect(
            self._on_refresh_listings_clicked,
        )
        row.addWidget(self._refresh_listings_btn)

        self._recompute_tiers_btn = QPushButton("Recompute Tiers")
        self._recompute_tiers_btn.clicked.connect(
            self._on_recompute_tiers_clicked,
        )
        row.addWidget(self._recompute_tiers_btn)

        row.addStretch(1)
        return row

    # --- Activity log -------------------------------------------

    def _build_activity_log(self) -> QGroupBox:
        box = QGroupBox("Activity Log")
        v = QVBoxLayout(box)
        v.setContentsMargins(8, 12, 8, 8)

        self._activity_model = QStandardItemModel(self)
        self._activity_model.setHorizontalHeaderLabels(ACTIVITY_HEADERS)

        self._activity_table = QTableView()
        self._activity_table.setModel(self._activity_model)
        self._activity_table.setEditTriggers(
            QTableView.NoEditTriggers,
        )
        self._activity_table.setSelectionBehavior(
            QTableView.SelectRows,
        )
        self._activity_table.setSelectionMode(
            QTableView.SingleSelection,
        )
        self._activity_table.setAlternatingRowColors(True)
        self._activity_table.verticalHeader().setVisible(False)
        # Notes column stretches; others auto-size to content
        header = self._activity_table.horizontalHeader()
        for col in (
            COL_ACTIVITY, COL_STARTED, COL_DURATION,
            COL_TICKERS_ATTEMPTED, COL_TICKERS_FETCHED,
        ):
            header.setSectionResizeMode(
                col, QHeaderView.ResizeToContents,
            )
        header.setSectionResizeMode(
            COL_NOTES, QHeaderView.Stretch,
        )

        v.addWidget(self._activity_table)
        return box

    # --- Side-by-side panels (Backfill | Intraday) --------------

    def _build_panels_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(12)
        row.addWidget(self._build_backfill_panel(), 1)
        row.addWidget(self._build_intraday_panel(), 1)
        return row

    def _build_backfill_panel(self) -> QGroupBox:
        box = QGroupBox("Backfill")
        v = QVBoxLayout(box)
        v.setContentsMargins(10, 14, 10, 10)
        v.setSpacing(8)

        # Header row with the flashing light
        header_row = QHBoxLayout()
        header_row.addStretch(1)
        self._backfill_light = FlashingLight()
        header_row.addWidget(QLabel("In progress:"))
        header_row.addWidget(self._backfill_light)
        v.addLayout(header_row)

        # Radio group — "Do not fetch stock prices" is default
        self._fetch_prices_radio = QRadioButton("Fetch Stock Prices")
        self._no_fetch_radio = QRadioButton("Do not fetch stock prices")
        self._no_fetch_radio.setChecked(True)
        radio_group = QButtonGroup(self)
        radio_group.addButton(self._fetch_prices_radio)
        radio_group.addButton(self._no_fetch_radio)
        v.addWidget(self._fetch_prices_radio)
        v.addWidget(self._no_fetch_radio)

        # From / To date pickers
        date_row = QHBoxLayout()
        date_row.addWidget(QLabel("From:"))
        self._from_date = QDateEdit(calendarPopup=True)
        self._from_date.setDisplayFormat("yyyy-MM-dd")
        date_row.addWidget(self._from_date)
        date_row.addSpacing(12)
        date_row.addWidget(QLabel("To:"))
        self._to_date = QDateEdit(calendarPopup=True)
        self._to_date.setDisplayFormat("yyyy-MM-dd")
        date_row.addWidget(self._to_date)
        date_row.addStretch(1)
        v.addLayout(date_row)

        # Stocks input + ADD button
        stocks_row = QHBoxLayout()
        stocks_row.addWidget(QLabel("Stocks:"))
        self._stocks_input = QLineEdit()
        self._stocks_input.setPlaceholderText(
            "type a symbol (autocomplete in 13c)",
        )
        stocks_row.addWidget(self._stocks_input, 1)
        self._add_stock_btn = QPushButton("ADD")
        self._add_stock_btn.clicked.connect(self._on_add_stock_clicked)
        stocks_row.addWidget(self._add_stock_btn)
        v.addLayout(stocks_row)

        # Chip area for selected stocks
        self._chip_area = TickerChipArea()
        v.addWidget(self._chip_area)

        v.addStretch(1)

        # Backfill button
        self._backfill_btn = QPushButton("Backfill")
        self._backfill_btn.setStyleSheet(
            "font-weight: 600; padding: 6px;"
        )
        self._backfill_btn.clicked.connect(self._on_backfill_clicked)
        v.addWidget(self._backfill_btn)

        return box

    def _build_intraday_panel(self) -> QGroupBox:
        box = QGroupBox("Intraday / EoD")
        v = QVBoxLayout(box)
        v.setContentsMargins(10, 14, 10, 10)
        v.setSpacing(8)

        # Header row with the flashing light
        header_row = QHBoxLayout()
        header_row.addStretch(1)
        self._intraday_light = FlashingLight()
        header_row.addWidget(QLabel("In progress:"))
        header_row.addWidget(self._intraday_light)
        v.addLayout(header_row)

        # Stage list — the 7 stages of the V1 live cycle
        self._stage_list = StageProgressList(LIVE_CYCLE_STAGES)
        v.addWidget(self._stage_list)

        v.addStretch(1)

        # Run Intraday button
        self._run_intraday_btn = QPushButton("Run Intraday")
        self._run_intraday_btn.setStyleSheet(
            "font-weight: 600; padding: 6px;"
        )
        self._run_intraday_btn.clicked.connect(
            self._on_run_intraday_clicked,
        )
        v.addWidget(self._run_intraday_btn)

        return box

    # --- Market overview footer ---------------------------------

    def _build_market_overview_footer(self) -> QGroupBox:
        box = QGroupBox("Overall Market Overview")
        v = QVBoxLayout(box)
        v.setContentsMargins(10, 14, 10, 10)
        v.setSpacing(6)

        # Snapshot date label (top-right of the box)
        self._snapshot_date_label = QLabel("(no snapshot)")
        self._snapshot_date_label.setStyleSheet(
            "color: #555; font-style: italic;"
        )
        v.addWidget(
            self._snapshot_date_label,
            alignment=Qt.AlignRight,
        )

        # Row 1: signal counts (no trend button — per sketch)
        self._signals_label = QLabel(
            "# Buys = —    # Sells = —    "
            "# Exit Buys = —    # Exit Sells = —"
        )
        v.addWidget(self._signals_label)

        # Row 2: regime counts + See Trend
        row2 = QHBoxLayout()
        self._regimes_label = QLabel(
            "# Green Regimes = —    # Red Regimes = —    "
            "# Neutral Regimes = —"
        )
        row2.addWidget(self._regimes_label, 1)
        self._regime_trend_btn = QPushButton("See Trend")
        self._regime_trend_btn.clicked.connect(
            lambda: self._open_trend_dialog(TrendKind.REGIMES),
        )
        row2.addWidget(self._regime_trend_btn)
        v.addLayout(row2)

        # Row 3: EMA-envelope counts + See Trend
        row3 = QHBoxLayout()
        self._ema_label = QLabel(
            "# Stocks > highest_ema = —    "
            "# Stocks < lowest_ema = —    "
            "# in between = —"
        )
        row3.addWidget(self._ema_label, 1)
        self._ema_trend_btn = QPushButton("See Trend")
        self._ema_trend_btn.clicked.connect(
            lambda: self._open_trend_dialog(TrendKind.EMA_ENVELOPE),
        )
        row3.addWidget(self._ema_trend_btn)
        v.addLayout(row3)

        return box

    # ============================================================
    # Scheduler ticks — refresh data
    # ============================================================

    def _on_exchange_changed(self, exchange: str) -> None:
        """Picker emitted selection_changed. Force-refresh all
        three data streams so the operator sees the new exchange's
        view immediately."""
        # Trigger ticks now instead of waiting for the next cadence
        self._activity_scheduler.trigger_now()
        self._mutex_scheduler.trigger_now()
        self._overview_scheduler.trigger_now()

    def _on_activity_tick(self) -> None:
        """Query compute_runs for the latest N rows. Runs on the
        GUI thread (cheap query, no need to background)."""
        exchange = self._picker.current_exchange()
        if exchange is None:
            return
        try:
            from ...persistence.compute_runs_repo import (
                ComputeRunsRepository,
            )
            with self._db.connect() as conn:
                rows = ComputeRunsRepository(conn).recent(
                    n=ACTIVITY_LOG_ROW_LIMIT, exchange=exchange,
                )
        except Exception:
            logger.warning(
                "activity log refresh failed", exc_info=True,
            )
            return
        self._populate_activity_table(rows)

    def _on_mutex_tick(self) -> None:
        """Update button-enabled state + flashing lights based on
        DB state (compute_runs in_flight + backfill_runs active).

        Cheap two-query check. Both queries return at most a tiny
        result set."""
        exchange = self._picker.current_exchange()
        if exchange is None:
            return
        try:
            from ...persistence.compute_runs_repo import (
                ComputeRunsRepository,
            )
            with self._db.connect() as conn:
                in_flight = ComputeRunsRepository(conn).in_flight(
                    exchange=exchange,
                )
        except Exception:
            logger.warning(
                "mutex check failed", exc_info=True,
            )
            return

        # Determine if anything is running + what kind
        kinds = {r.kind for r in in_flight}
        is_running = bool(kinds) or self._local_running

        if "backfill" in kinds:
            inflight_kind = "backfill"
        elif "eod" in kinds:
            inflight_kind = "eod"
        elif "intraday" in kinds:
            inflight_kind = "intraday"
        else:
            inflight_kind = None

        self._apply_mutex_state(is_running, inflight_kind)

    def _on_overview_tick(self) -> None:
        """Read the most-recent market_overview_daily row for the
        selected exchange + paint the footer labels."""
        exchange = self._picker.current_exchange()
        if exchange is None:
            return
        try:
            from ...persistence.market_overview_repo import (
                MarketOverviewRepository,
            )
            with self._db.connect() as conn:
                row = MarketOverviewRepository(conn).latest(
                    exchange=exchange,
                )
        except Exception:
            logger.warning(
                "market overview refresh failed", exc_info=True,
            )
            return
        self._paint_market_overview(row)

    # ============================================================
    # Painters
    # ============================================================

    def _populate_activity_table(self, rows) -> None:
        """Repaint the activity log table from a list of
        ComputeRunRow."""
        model = self._activity_model
        model.removeRows(0, model.rowCount())

        for row in rows:
            activity = KIND_LABELS.get(row.kind, row.kind)
            started = _format_started_at(row.started_at)
            duration = _format_duration(row.duration_seconds)
            attempted = (
                str(row.tickers_attempted)
                if row.tickers_attempted is not None else "—"
            )
            fetched = (
                str(row.tickers_fetched)
                if row.tickers_fetched is not None else "—"
            )
            notes = _format_notes_or_error(row)

            items = [
                QStandardItem(activity),
                QStandardItem(started),
                QStandardItem(duration),
                QStandardItem(attempted),
                QStandardItem(fetched),
                QStandardItem(notes),
            ]
            # Right-align numeric columns
            for col in (
                COL_TICKERS_ATTEMPTED, COL_TICKERS_FETCHED,
            ):
                items[col].setTextAlignment(
                    Qt.AlignRight | Qt.AlignVCenter,
                )
            # Italics on in-flight rows for visual hint
            if row.is_in_flight:
                for item in items:
                    f = item.font()
                    f.setItalic(True)
                    item.setFont(f)
            model.appendRow(items)

    def _apply_mutex_state(
        self, is_running: bool, inflight_kind: str | None,
    ) -> None:
        """Enable/disable the four action buttons + flip the
        flashing lights based on running state."""
        self._refresh_listings_btn.setEnabled(not is_running)
        self._recompute_tiers_btn.setEnabled(not is_running)
        self._backfill_btn.setEnabled(not is_running)
        self._run_intraday_btn.setEnabled(not is_running)

        # Flashing lights: backfill kind → backfill panel light;
        # intraday/eod kind → intraday panel light. Local-running
        # without a known kind: turn ON neither (we don't know
        # which panel the click was on at this layer; the click
        # handlers set their own light).
        self._backfill_light.set_active(inflight_kind == "backfill")
        self._intraday_light.set_active(
            inflight_kind in ("intraday", "eod"),
        )

    def _paint_market_overview(self, row) -> None:
        if row is None:
            self._snapshot_date_label.setText("(no snapshot yet)")
            self._signals_label.setText(
                "# Buys = —    # Sells = —    "
                "# Exit Buys = —    # Exit Sells = —"
            )
            self._regimes_label.setText(
                "# Green Regimes = —    # Red Regimes = —    "
                "# Neutral Regimes = —"
            )
            self._ema_label.setText(
                "# Stocks > highest_ema = —    "
                "# Stocks < lowest_ema = —    "
                "# in between = —"
            )
            return

        self._snapshot_date_label.setText(
            f"Snapshot date: {row.snapshot_date.isoformat()}"
        )
        self._signals_label.setText(
            f"# Buys = {row.n_buys}    "
            f"# Sells = {row.n_sells}    "
            f"# Exit Buys = {row.n_exit_buys}    "
            f"# Exit Sells = {row.n_exit_sells}"
        )
        self._regimes_label.setText(
            f"# Green Regimes = {row.n_green_regime}    "
            f"# Red Regimes = {row.n_red_regime}    "
            f"# Neutral Regimes = {row.n_neutral_regime}"
        )
        self._ema_label.setText(
            f"# Stocks > highest_ema = {row.n_above_highest_ema}    "
            f"# Stocks < lowest_ema = {row.n_below_lowest_ema}    "
            f"# in between = {row.n_between_emas}"
        )

    # ============================================================
    # Action button handlers — STUBS for 13b
    # ============================================================
    # Each one just shows a status message + (for the panel-based
    # actions) toggles the local flashing light for a few seconds.
    # 13c/13d will replace these with real wiring.

    def _on_refresh_listings_clicked(self) -> None:
        self._set_status(
            "Refresh Listings — wiring lands in Step 13d."
        )

    def _on_recompute_tiers_clicked(self) -> None:
        self._set_status(
            "Recompute Tiers — wiring lands in Step 13d."
        )

    def _on_add_stock_clicked(self) -> None:
        """Add typed text to the chip list. In 13b this works as a
        plain-text-to-chip; 13c adds autocomplete + validation."""
        text = self._stocks_input.text().strip().upper()
        if not text:
            return
        self._chip_area.add_chip(text)
        self._stocks_input.clear()

    def _on_backfill_clicked(self) -> None:
        # 13c will pull date_from, date_to, stocks list + spawn
        # the CLI subprocess. For now: placeholder + log to status.
        chips = self._chip_area.chips()
        date_from = self._from_date.date().toString("yyyy-MM-dd")
        date_to = self._to_date.date().toString("yyyy-MM-dd")
        refetch = self._fetch_prices_radio.isChecked()
        self._set_status(
            f"Backfill (placeholder) — from={date_from} "
            f"to={date_to} stocks={len(chips)} refetch={refetch}. "
            "Wiring lands in Step 13c."
        )

    def _on_run_intraday_clicked(self) -> None:
        self._set_status(
            "Run Intraday — wiring lands in Step 13d."
        )

    def _open_trend_dialog(self, kind: TrendKind) -> None:
        """Open the 90-day trend chart for the selected metric."""
        exchange = self._picker.current_exchange()
        if exchange is None:
            QMessageBox.information(
                self, "Trend",
                "Select an exchange first.",
            )
            return
        dlg = MarketTrendDialog(
            db=self._db, exchange=exchange, kind=kind, parent=self,
        )
        dlg.exec()

    def _set_status(self, message: str) -> None:
        self._status_label.setText(message)
        logger.info("EtlStatusView status: %s", message)

    # ============================================================
    # Lifecycle
    # ============================================================

    def on_close(self) -> None:
        """Called by MainWindow on app shutdown. Stop schedulers
        so QTimers don't fire post-destruction."""
        for sch in (
            self._activity_scheduler,
            self._mutex_scheduler,
            self._overview_scheduler,
        ):
            try:
                sch.stop()
            except Exception:
                pass
