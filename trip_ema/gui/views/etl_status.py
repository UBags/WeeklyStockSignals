"""ETL Status & Control view — Step 13c: Backfill panel wired.

Builds on the 13b layout (top strip + Activity Log + Backfill
panel + Intraday panel + Market Overview footer). 13c fills in
the Backfill panel:

  - Stocks input: QCompleter against active tickers (refreshed
    on exchange change; 13d hook for refresh-after-RefreshListings)
  - ADD button: validates symbol against the active-ticker set
    BEFORE adding to the chip area (in-GUI error message; matches
    CLI behavior)
  - From-date picker: auto-fills with MIN(ohlcv_bars.bar_timestamp)
    on view init + exchange change; falls back to today-minus-
    15-years if no bars present (fresh install)
  - [Backfill] button: shows destructive-confirmation modal with
    pre-flight counts; on confirm, spawns QProcess invoking
    `sys.executable -m trip_ema.etl.cli backfill ...`; live stage
    progress streams to an in-panel label below the button
  - [Cancel] button: visible only while a backfill is running.
    SIGTERM → 5s grace → SIGKILL.
  - On exit: success toast (status_label) or failure dialog
    (exit code + failed_stage + stderr tail, with --release-lock
    hint when applicable). Form state is preserved (chips +
    radio + dates kept as-is per the Q3 lock).

Intraday panel + Refresh Listings + Recompute Tiers remain
no-ops; they land in 13d.
"""
from __future__ import annotations

# === STEP 13D2-FIXUP: stage_status enum ===

import logging
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from PySide6.QtCore import (
    Qt, QDate, QProcess, QStringListModel, QTimer,
)
from PySide6.QtGui import QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QButtonGroup, QCompleter, QDateEdit, QFrame, QGroupBox,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox,
    QPushButton, QRadioButton, QSizePolicy, QTableView,
    QVBoxLayout, QWidget,
)

from ..db import GuiDbSession
from ..http_client import TripEmaHttpClient
from ..workers import RefreshScheduler, submit
from .market_trend_dialog import MarketTrendDialog, TrendKind
from .widgets.backfill_destructive_modal import (
    BackfillDestructiveModal, estimate_runtime_minutes,
)
from .widgets.backfill_failure_dialog import (
    BackfillFailureDialog, parse_failure_summary,
)
from .widgets.exchange_picker import ExchangePicker
from .widgets.flashing_light import FlashingLight
from .widgets.stage_progress import (
    LIVE_CYCLE_STAGES, StageProgressList, StageStatus,
)
from .widgets.intraday_confirmation_modal import (
    IntradayConfirmationModal,
)
from .widgets.cycle_stdout_parser import (
    is_cycle_complete_line, labels_before,
    parse_cycle_failure_summary, parse_stage_line,
)
from .widgets.ticker_chip import TickerChipArea

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")


# ============================================================
# Polling cadences (unchanged from 13b)
# ============================================================
ACTIVITY_LOG_CADENCE_SECONDS = 5.0
MUTEX_CADENCE_SECONDS = 2.0
MARKET_OVERVIEW_CADENCE_SECONDS = 30.0
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


KIND_LABELS = {
    "intraday": "Intraday Run",
    "eod":      "EoD Run",
    "backfill": "Backfill",
}


# QProcess cancel grace period before SIGKILL (per Q2 lock)
CANCEL_GRACE_MS = 5000


def _format_started_at(started_at: datetime) -> str:
    started_ist = started_at.astimezone(IST)
    today_ist = datetime.now(IST).date()
    if started_ist.date() == today_ist:
        return started_ist.strftime("%H:%M:%S")
    return started_ist.strftime("%Y-%m-%d %H:%M")


def _format_duration(seconds: float | None) -> str:
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
    if row.failed_stage is not None or row.error_message is not None:
        stage = row.failed_stage or "?"
        msg = (row.error_message or "")[:60]
        return f"❌ {stage}: {msg}"
    return row.notes or ""


def build_backfill_argv(
    *,
    exchange: str,
    date_from,
    date_to,
    stocks: list[str],
    refetch_bars: bool,
) -> list[str]:
    """Build the argv (after sys.executable) for the backfill
    QProcess invocation. Extracted as a module-level helper so the
    argv-construction logic is testable without spinning up Qt.

    Output shape:
        ['-m', 'trip_ema.etl.cli', 'backfill',
         '--exchange', '<ex>',
         '--date_from', 'YYYY-MM-DD',
         '--date_to', 'YYYY-MM-DD',
         ('--no_fetch_bars',)?,
         ('--stocks', 'A,B,C')?]
    """
    args = [
        "-m", "trip_ema.etl.cli", "backfill",
        "--exchange", exchange,
        "--date_from", date_from.isoformat(),
        "--date_to", date_to.isoformat(),
    ]
    if not refetch_bars:
        args.append("--no_fetch_bars")
    if stocks:
        args.extend(["--stocks", ",".join(stocks)])
    return args


# ============================================================
# The view
# ============================================================

class EtlStatusView(QWidget):
    """Operator console's ETL Status tab. Step 13c."""

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

        # Mutex state
        self._local_running: bool = False
        self._inflight_kind: str | None = None

        # Backfill subprocess state
        self._backfill_process: QProcess | None = None
        self._backfill_stdout_buffer: str = ""
        self._backfill_stderr_buffer: str = ""

        # === STEP 13D2: intraday wiring ===
        # Intraday subprocess state (mirror of backfill)
        self._intraday_process: QProcess | None = None
        self._intraday_stdout_buffer: str = ""
        self._intraday_stderr_buffer: str = ""

        # Active-ticker universe for autocomplete + validation
        self._active_tickers: list[str] = []
        self._active_ticker_set: frozenset[str] = frozenset()
        self._completer_model = QStringListModel(self)

        self._build_ui()

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

        outer.addLayout(self._build_top_strip())
        outer.addWidget(self._build_activity_log(), 5)
        outer.addLayout(self._build_panels_row(), 10)
        outer.addWidget(self._build_market_overview_footer(), 4)

        self._status_label = QLabel("")
        self._status_label.setStyleSheet(
            "color: #666; font-style: italic;"
        )
        outer.addWidget(self._status_label)

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

    def _build_activity_log(self) -> QGroupBox:
        box = QGroupBox("Activity Log")
        v = QVBoxLayout(box)
        v.setContentsMargins(8, 12, 8, 8)

        self._activity_model = QStandardItemModel(self)
        self._activity_model.setHorizontalHeaderLabels(
            ACTIVITY_HEADERS,
        )

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

        # Header row with flashing light
        header_row = QHBoxLayout()
        header_row.addStretch(1)
        self._backfill_light = FlashingLight()
        header_row.addWidget(QLabel("In progress:"))
        header_row.addWidget(self._backfill_light)
        v.addLayout(header_row)

        # Radio group
        self._fetch_prices_radio = QRadioButton("Fetch Stock Prices")
        self._no_fetch_radio = QRadioButton(
            "Do not fetch stock prices",
        )
        self._no_fetch_radio.setChecked(True)
        radio_group = QButtonGroup(self)
        radio_group.addButton(self._fetch_prices_radio)
        radio_group.addButton(self._no_fetch_radio)
        v.addWidget(self._fetch_prices_radio)
        v.addWidget(self._no_fetch_radio)

        # Date pickers
        date_row = QHBoxLayout()
        date_row.addWidget(QLabel("From:"))
        self._from_date = QDateEdit(calendarPopup=True)
        self._from_date.setDisplayFormat("yyyy-MM-dd")
        self._from_date.setDate(
            QDate.currentDate().addYears(-15),
        )
        date_row.addWidget(self._from_date)
        date_row.addSpacing(12)
        date_row.addWidget(QLabel("To:"))
        self._to_date = QDateEdit(calendarPopup=True)
        self._to_date.setDisplayFormat("yyyy-MM-dd")
        self._to_date.setDate(QDate.currentDate())
        date_row.addWidget(self._to_date)
        date_row.addStretch(1)
        v.addLayout(date_row)

        # Stocks input + ADD (with autocomplete)
        stocks_row = QHBoxLayout()
        stocks_row.addWidget(QLabel("Stocks:"))
        self._stocks_input = QLineEdit()
        self._stocks_input.setPlaceholderText(
            "type a symbol (autocomplete)…",
        )
        # QCompleter: case-insensitive prefix match
        self._completer = QCompleter(self._completer_model, self)
        self._completer.setCaseSensitivity(Qt.CaseInsensitive)
        self._completer.setFilterMode(Qt.MatchStartsWith)
        self._completer.setCompletionMode(
            QCompleter.PopupCompletion,
        )
        self._stocks_input.setCompleter(self._completer)
        # Enter in the input triggers ADD (UX nicety)
        self._stocks_input.returnPressed.connect(
            self._on_add_stock_clicked,
        )
        stocks_row.addWidget(self._stocks_input, 1)
        self._add_stock_btn = QPushButton("ADD")
        self._add_stock_btn.clicked.connect(
            self._on_add_stock_clicked,
        )
        stocks_row.addWidget(self._add_stock_btn)
        v.addLayout(stocks_row)

        # Chip area
        self._chip_area = TickerChipArea()
        v.addWidget(self._chip_area)

        v.addStretch(1)

        # Progress label (in-panel stage feedback — Q1 lock)
        self._backfill_progress_label = QLabel("")
        self._backfill_progress_label.setWordWrap(True)
        self._backfill_progress_label.setStyleSheet(
            "color: #5cb6e8; font-family: Monospace; "
            "font-size: 11px; padding: 4px;"
        )
        v.addWidget(self._backfill_progress_label)

        # Backfill + Cancel buttons (Cancel hidden when idle)
        btn_row = QHBoxLayout()
        self._backfill_btn = QPushButton("Backfill")
        self._backfill_btn.setStyleSheet(
            "font-weight: 600; padding: 6px;"
        )
        self._backfill_btn.clicked.connect(self._on_backfill_clicked)
        btn_row.addWidget(self._backfill_btn, 1)

        self._cancel_backfill_btn = QPushButton("Cancel")
        self._cancel_backfill_btn.setStyleSheet(
            "font-weight: 600; padding: 6px; "
            "background-color: #6a1f1f; color: white;"
        )
        self._cancel_backfill_btn.clicked.connect(
            self._on_cancel_backfill_clicked,
        )
        self._cancel_backfill_btn.hide()
        btn_row.addWidget(self._cancel_backfill_btn)
        v.addLayout(btn_row)

        return box

    def _build_intraday_panel(self) -> QGroupBox:
        box = QGroupBox("Intraday / EoD")
        v = QVBoxLayout(box)
        v.setContentsMargins(10, 14, 10, 10)
        v.setSpacing(8)

        header_row = QHBoxLayout()
        header_row.addStretch(1)
        self._intraday_light = FlashingLight()
        header_row.addWidget(QLabel("In progress:"))
        header_row.addWidget(self._intraday_light)
        v.addLayout(header_row)

        self._stage_list = StageProgressList(LIVE_CYCLE_STAGES)
        v.addWidget(self._stage_list)

        v.addStretch(1)

        # === STEP 13D2: intraday wiring ===
        # Progress label: shows current stage marker text from
        # cmd_cycle's stdout (e.g. "  [iiv] running").
        # Detail-level mirror of the StageProgressList above.
        self._intraday_progress_label = QLabel("")
        self._intraday_progress_label.setStyleSheet(
            "color: #888; font-size: 11px; padding: 2px;"
        )
        self._intraday_progress_label.setWordWrap(True)
        v.addWidget(self._intraday_progress_label)

        # Run + Cancel button row
        btn_row = QHBoxLayout()
        self._run_intraday_btn = QPushButton("Run Intraday")
        self._run_intraday_btn.setStyleSheet(
            "font-weight: 600; padding: 6px;"
        )
        self._run_intraday_btn.clicked.connect(
            self._on_run_intraday_clicked,
        )
        btn_row.addWidget(self._run_intraday_btn, 1)

        self._cancel_intraday_btn = QPushButton("Cancel")
        self._cancel_intraday_btn.setStyleSheet(
            "padding: 6px; color: #d33;"
        )
        self._cancel_intraday_btn.clicked.connect(
            self._on_cancel_intraday_clicked,
        )
        self._cancel_intraday_btn.setVisible(False)
        btn_row.addWidget(self._cancel_intraday_btn)
        v.addLayout(btn_row)

        return box

    def _build_market_overview_footer(self) -> QGroupBox:
        box = QGroupBox("Overall Market Overview")
        v = QVBoxLayout(box)
        v.setContentsMargins(10, 14, 10, 10)
        v.setSpacing(6)

        self._snapshot_date_label = QLabel("(no snapshot)")
        self._snapshot_date_label.setStyleSheet(
            "color: #555; font-style: italic;"
        )
        v.addWidget(
            self._snapshot_date_label, alignment=Qt.AlignRight,
        )

        self._signals_label = QLabel(
            "# Buys = —    # Sells = —    "
            "# Exit Buys = —    # Exit Sells = —"
        )
        v.addWidget(self._signals_label)

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
    # Scheduler ticks
    # ============================================================

    def _on_exchange_changed(self, exchange: str) -> None:
        """Picker emitted selection_changed. Refresh everything for
        the new exchange: schedulers fire NOW, autocomplete + From-
        date auto-fill (13c additions)."""
        self._activity_scheduler.trigger_now()
        self._mutex_scheduler.trigger_now()
        self._overview_scheduler.trigger_now()
        self._refresh_active_tickers()
        self._autofill_from_date()

    def _on_activity_tick(self) -> None:
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

        kinds = {r.kind for r in in_flight}

        # OR in the local flag (Q8 lock — covers the post-click,
        # pre-DB-row window so the buttons disable immediately)
        is_running = bool(kinds) or self._local_running

        if "backfill" in kinds:
            inflight_kind = "backfill"
        elif "eod" in kinds:
            inflight_kind = "eod"
        elif "intraday" in kinds:
            inflight_kind = "intraday"
        else:
            # No DB row in flight. If local flag is on (just-clicked
            # window), keep the inflight_kind we set at click time.
            inflight_kind = (
                self._inflight_kind if self._local_running
                else None
            )

        self._apply_mutex_state(is_running, inflight_kind)

    def _on_overview_tick(self) -> None:
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
    # 13c — Active tickers + From-date auto-fill
    # ============================================================

    def _refresh_active_tickers(self) -> None:
        """Populate `_active_tickers` from the DB. Drives both the
        QCompleter and the ADD-button validation set."""
        exchange = self._picker.current_exchange()
        if exchange is None:
            self._active_tickers = []
            self._active_ticker_set = frozenset()
            self._completer_model.setStringList([])
            return
        try:
            from ...persistence.backfill_queries import active_symbols
            with self._db.connect() as conn:
                symbols = active_symbols(conn, exchange=exchange)
        except Exception:
            logger.warning(
                "active-ticker refresh failed", exc_info=True,
            )
            return
        self._active_tickers = symbols
        self._active_ticker_set = frozenset(symbols)
        self._completer_model.setStringList(symbols)
        logger.info(
            "Loaded %d active tickers for %s",
            len(symbols), exchange,
        )

    def _autofill_from_date(self) -> None:
        """Set the From-date picker to MIN(ohlcv_bars.bar_timestamp)
        for the selected exchange. Fall back to today minus 15
        years if no bars exist."""
        exchange = self._picker.current_exchange()
        if exchange is None:
            return
        try:
            from ...persistence.backfill_queries import min_bar_date
            with self._db.connect() as conn:
                min_d = min_bar_date(conn, exchange=exchange)
        except Exception:
            logger.warning(
                "min bar date query failed", exc_info=True,
            )
            min_d = None

        if min_d is None:
            target = QDate.currentDate().addYears(-15)
            logger.info(
                "No daily bars for %s — defaulting From-date to "
                "%s (today minus 15 years)",
                exchange, target.toString("yyyy-MM-dd"),
            )
        else:
            target = QDate(min_d.year, min_d.month, min_d.day)
            logger.info(
                "From-date auto-filled with MIN(ohlcv_bars) = %s",
                target.toString("yyyy-MM-dd"),
            )
        self._from_date.setDate(target)

    # ============================================================
    # Painters
    # ============================================================

    def _populate_activity_table(self, rows) -> None:
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
            for col in (
                COL_TICKERS_ATTEMPTED, COL_TICKERS_FETCHED,
            ):
                items[col].setTextAlignment(
                    Qt.AlignRight | Qt.AlignVCenter,
                )
            if row.is_in_flight:
                for item in items:
                    f = item.font()
                    f.setItalic(True)
                    item.setFont(f)
            model.appendRow(items)

    def _apply_mutex_state(
        self, is_running: bool, inflight_kind: str | None,
    ) -> None:
        self._refresh_listings_btn.setEnabled(not is_running)
        self._recompute_tiers_btn.setEnabled(not is_running)
        self._backfill_btn.setEnabled(not is_running)
        self._run_intraday_btn.setEnabled(not is_running)

        # Show/hide Cancel based on backfill running and locally
        # owned. We only show Cancel for backfills WE started — if
        # another GUI instance started one, our QProcess is None
        # and Cancel wouldn't work.
        backfill_owned = (
            self._backfill_process is not None
            and self._backfill_process.state() != QProcess.NotRunning
        )
        self._cancel_backfill_btn.setVisible(backfill_owned)

        # === STEP 13D2: intraday wiring ===
        intraday_owned = (
            self._intraday_process is not None
            and self._intraday_process.state() != QProcess.NotRunning
        )
        self._cancel_intraday_btn.setVisible(intraday_owned)

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
    # Action button handlers
    # ============================================================

    def _on_refresh_listings_clicked(self) -> None:
        self._set_status(
            "Refresh Listings — wiring lands in Step 13d."
        )

    def _on_recompute_tiers_clicked(self) -> None:
        self._set_status(
            "Recompute Tiers — wiring lands in Step 13d."
        )

    def _on_run_intraday_clicked(self) -> None:
        """Validate → modal → spawn QProcess invoking
        `trip-ema-etl cycle --exchange X`. The CLI determines
        intraday-vs-eod kind at runtime via MarketHoursService."""
        # === STEP 13D2: intraday wiring ===
        if self._intraday_process is not None:
            self._set_status("Intraday cycle already running.")
            return
        if self._local_running:
            self._set_status(
                f"Another operation ({self._inflight_kind}) is "
                f"running locally."
            )
            return

        exchange = self._picker.current_exchange()
        if exchange is None:
            QMessageBox.warning(
                self, "Run Intraday blocked",
                "Select an exchange first.",
            )
            return

        # ---- Count active subscribers for the modal ----
        try:
            from ...persistence.subscribers_repo import (
                SubscribersRepository,
            )
            with self._db.connect() as conn:
                n_active = SubscribersRepository(
                    conn,
                ).count_active()
        except Exception as e:
            logger.exception(
                "Could not count active subscribers; "
                "defaulting to 0 in modal"
            )
            n_active = 0

        # ---- Modal ----
        # The actual intraday-vs-eod determination happens in the
        # CLI's cmd_cycle (which has the full MarketHoursService).
        # The modal's kind_hint is advisory; we pass "intraday"
        # by default and market_open=False so the modal text
        # nudges toward "determined at runtime".
        modal = IntradayConfirmationModal(
            exchange=exchange,
            kind_hint="determined at runtime",
            market_open=False,
            n_active_subscribers=n_active,
            parent=self,
        )
        from PySide6.QtWidgets import QDialog
        result = modal.exec()
        if result != QDialog.Accepted:
            self._set_status("Intraday cycle cancelled.")
            return

        # ---- Reset stage progress before spawn ----
        for label in LIVE_CYCLE_STAGES:
            self._stage_list.set_stage_status(
                label, StageStatus.PENDING,
            )

        self._start_intraday_process(exchange=exchange)


    def _on_add_stock_clicked(self) -> None:
        """Add a symbol to the chip list. 13c additions: strict
        validation against the active-ticker set; rejection
        feedback in the status line."""
        text = self._stocks_input.text().strip().upper()
        if not text:
            return
        if not self._active_ticker_set:
            # Universe not loaded yet — refuse rather than allow
            # arbitrary text through
            QMessageBox.warning(
                self, "ADD blocked",
                "Active-ticker list isn't loaded yet. Select an "
                "exchange first.",
            )
            return
        if text not in self._active_ticker_set:
            QMessageBox.warning(
                self, "Unknown symbol",
                f"'{text}' is not in the active-ticker universe "
                f"for {self._picker.current_exchange()}. "
                f"Backfill would fail at the CLI's symbol "
                f"resolution step.",
            )
            return
        added = self._chip_area.add_chip(text)
        if added:
            self._stocks_input.clear()

    # ============================================================
    # Backfill subprocess flow (13c — the big one)
    # ============================================================

    def _on_backfill_clicked(self) -> None:
        """Validate form → preflight DB queries → destructive
        modal → if confirmed, spawn QProcess."""
        if self._backfill_process is not None:
            self._set_status(
                "Backfill already running."
            )
            return

        exchange = self._picker.current_exchange()
        if exchange is None:
            QMessageBox.warning(
                self, "Backfill blocked",
                "Select an exchange first.",
            )
            return

        # ---- 1. Form validation ----
        date_from = self._from_date.date().toPython()
        date_to = self._to_date.date().toPython()
        if date_from > date_to:
            QMessageBox.warning(
                self, "Invalid dates",
                f"From date ({date_from}) must be on or before "
                f"To date ({date_to}).",
            )
            return

        chips = self._chip_area.chips()
        # Empty chips → backfill ALL active tickers (matches CLI's
        # no-`--stocks` behavior)
        is_all_tickers = (len(chips) == 0)

        # ---- 2. Pre-flight DB queries ----
        try:
            from ...persistence.backfill_queries import (
                historical_signals_delete_count_for_range,
                market_overview_stale_count_for_range,
                resolve_ticker_ids_by_symbol,
            )
            with self._db.connect() as conn:
                if is_all_tickers:
                    ticker_ids: list[int] | None = None
                    n_tickers = sum(
                        1 for _ in self._active_tickers
                    )
                else:
                    resolved, unresolved = (
                        resolve_ticker_ids_by_symbol(
                            conn,
                            exchange=exchange, symbols=chips,
                        )
                    )
                    if unresolved:
                        QMessageBox.warning(
                            self, "Symbol resolution failed",
                            f"These chips no longer match active "
                            f"tickers (universe may have changed): "
                            f"{', '.join(unresolved)}",
                        )
                        return
                    ticker_ids = resolved
                    n_tickers = len(resolved)

                n_hist = historical_signals_delete_count_for_range(
                    conn,
                    exchange=exchange,
                    date_from=date_from,
                    ticker_ids=ticker_ids,
                )
                n_overview = market_overview_stale_count_for_range(
                    conn,
                    exchange=exchange,
                    date_from=date_from,
                )
        except Exception as e:
            QMessageBox.critical(
                self, "Pre-flight query failed",
                f"Could not compute deletion counts: {e}",
            )
            logger.exception("backfill preflight failed")
            return

        # ---- 3. Destructive confirmation modal ----
        refetch = self._fetch_prices_radio.isChecked()
        eta_min = estimate_runtime_minutes(
            n_tickers=n_tickers, refetch_bars=refetch,
        )
        modal = BackfillDestructiveModal(
            exchange=exchange,
            date_from=date_from,
            date_to=date_to,
            n_stocks=len(chips),
            stocks_preview=chips if chips else None,
            n_historical_to_delete=n_hist,
            n_overview_to_stale=n_overview,
            refetch_bars=refetch,
            runtime_estimate_minutes=eta_min,
            parent=self,
        )
        if modal.exec() != BackfillDestructiveModal.Accepted:
            self._set_status("Backfill cancelled.")
            return

        # ---- 4. Spawn QProcess ----
        self._start_backfill_process(
            exchange=exchange,
            date_from=date_from, date_to=date_to,
            stocks=chips, refetch_bars=refetch,
        )

    def _start_backfill_process(
        self,
        *,
        exchange: str,
        date_from,
        date_to,
        stocks: list[str],
        refetch_bars: bool,
    ) -> None:
        args = build_backfill_argv(
            exchange=exchange,
            date_from=date_from, date_to=date_to,
            stocks=stocks, refetch_bars=refetch_bars,
        )

        proc = QProcess(self)
        proc.setProcessChannelMode(QProcess.SeparateChannels)
        proc.readyReadStandardOutput.connect(
            self._on_backfill_stdout,
        )
        proc.readyReadStandardError.connect(
            self._on_backfill_stderr,
        )
        proc.finished.connect(self._on_backfill_finished)
        proc.errorOccurred.connect(self._on_backfill_proc_error)

        self._backfill_process = proc
        self._backfill_stdout_buffer = ""
        self._backfill_stderr_buffer = ""

        # Flip local mutex flags BEFORE start() so mutex tick sees
        # the running state immediately
        self._local_running = True
        self._inflight_kind = "backfill"
        self._apply_mutex_state(True, "backfill")
        self._backfill_progress_label.setText(
            f"Starting backfill ({exchange}, "
            f"{date_from.isoformat()} → {date_to.isoformat()}, "
            f"{len(stocks) if stocks else 'ALL'} tickers, "
            f"refetch={refetch_bars})…"
        )
        self._set_status(
            f"Backfill spawned: {sys.executable} -m "
            f"trip_ema.etl.cli backfill ..."
        )
        logger.info("Backfill QProcess starting: argv=%r", args)
        proc.start(sys.executable, args)

    def _on_backfill_stdout(self) -> None:
        if self._backfill_process is None:
            return
        data = bytes(
            self._backfill_process.readAllStandardOutput(),
        ).decode("utf-8", errors="replace")
        if not data:
            return
        self._backfill_stdout_buffer += data
        # Parse per-line for `  [stage] message` markers
        for line in data.splitlines():
            stripped = line.strip()
            if stripped.startswith("[") and "]" in stripped:
                # "[stage_name] message" → display verbatim
                self._backfill_progress_label.setText(stripped)
            elif stripped:
                logger.debug("backfill stdout: %s", stripped)

    def _on_backfill_stderr(self) -> None:
        if self._backfill_process is None:
            return
        data = bytes(
            self._backfill_process.readAllStandardError(),
        ).decode("utf-8", errors="replace")
        if not data:
            return
        self._backfill_stderr_buffer += data
        for line in data.splitlines():
            if line.strip():
                logger.info("backfill stderr: %s", line)

    def _on_backfill_finished(
        self, exit_code: int, exit_status,
    ) -> None:
        """QProcess.finished → clean up + show success/failure."""
        proc = self._backfill_process
        self._backfill_process = None
        self._local_running = False
        self._inflight_kind = None

        # Force activity log + mutex refresh so the row's
        # finished_at gets reflected without waiting 5s
        self._activity_scheduler.trigger_now()
        self._mutex_scheduler.trigger_now()

        self._backfill_progress_label.setText("")

        if exit_code == 0:
            self._set_status(
                "Backfill complete (exit 0). Activity log "
                "updated."
            )
            QMessageBox.information(
                self, "Backfill complete",
                "Backfill finished successfully.",
            )
        else:
            failed_stage, error_msg = parse_failure_summary(
                self._backfill_stdout_buffer,
            )
            stderr_tail = "\n".join(
                self._backfill_stderr_buffer.splitlines()[-5:]
            )
            self._set_status(
                f"Backfill FAILED (exit {exit_code})."
                + (f" Failed stage: {failed_stage}"
                   if failed_stage else "")
            )
            BackfillFailureDialog(
                exit_code=exit_code,
                failed_stage=failed_stage,
                error_message=error_msg,
                stderr_tail=stderr_tail,
                full_stderr=self._backfill_stderr_buffer,
                parent=self,
            ).exec()

    def _on_backfill_proc_error(self, error) -> None:
        """QProcess.errorOccurred — e.g. executable not found."""
        if self._backfill_process is None:
            return
        msg = (
            self._backfill_process.errorString()
            if self._backfill_process else str(error)
        )
        logger.error("Backfill QProcess errorOccurred: %s", msg)
        # Don't try to surface here — finished() will fire too,
        # and the failure dialog covers the same ground.

    # === STEP 13D2: intraday wiring ===
    def _start_intraday_process(
        self, *, exchange: str,
    ) -> None:
        """Spawn `trip-ema-etl cycle --exchange X` as a QProcess
        and wire its stdout/stderr/finished signals to the
        in-panel UI."""
        args = [
            "-m", "trip_ema.etl.cli",
            "cycle", "--exchange", exchange,
        ]

        proc = QProcess(self)
        proc.setProcessChannelMode(QProcess.SeparateChannels)
        proc.readyReadStandardOutput.connect(
            self._on_intraday_stdout,
        )
        proc.readyReadStandardError.connect(
            self._on_intraday_stderr,
        )
        proc.finished.connect(self._on_intraday_finished)
        proc.errorOccurred.connect(self._on_intraday_proc_error)

        self._intraday_process = proc
        self._intraday_stdout_buffer = ""
        self._intraday_stderr_buffer = ""

        # Flip mutex BEFORE start() so the next mutex tick sees
        # the running state immediately. inflight_kind starts as
        # "intraday" — if the CLI determines kind=eod, we keep
        # the UI hint as intraday since "Run Intraday" is the
        # operator button label.
        self._local_running = True
        self._inflight_kind = "intraday"
        self._apply_mutex_state(True, "intraday")

        self._intraday_progress_label.setText(
            f"Starting cycle ({exchange})…"
        )
        self._set_status(
            f"Cycle spawned: {sys.executable} -m "
            f"trip_ema.etl.cli cycle --exchange {exchange}"
        )
        logger.info("Intraday QProcess starting: argv=%r", args)
        proc.start(sys.executable, args)

    def _on_intraday_stdout(self) -> None:
        if self._intraday_process is None:
            return
        data = bytes(
            self._intraday_process.readAllStandardOutput(),
        ).decode("utf-8", errors="replace")
        if not data:
            return
        self._intraday_stdout_buffer += data

        for line in data.splitlines():
            parsed = parse_stage_line(line)
            if parsed is not None:
                label, status, raw_msg = parsed
                # Mark all earlier stages as done — protects
                # against a stage that emitted "running" but
                # never emitted "done" before the next stage's
                # "running" arrived (e.g. cache_rebuild skipped).
                for earlier in labels_before(label):
                    self._stage_list.set_stage_status(
                        earlier, StageStatus.DONE,
                    )
                if status == "running":
                    self._stage_list.set_stage_status(
                        label, StageStatus.IN_PROGRESS,
                    )
                elif status == "done":
                    self._stage_list.set_stage_status(
                        label, StageStatus.DONE,
                    )
                elif status == "failed":
                    self._stage_list.set_stage_status(
                        label, StageStatus.FAILED,
                    )
                # Always update the detail label
                self._intraday_progress_label.setText(
                    f"[{label}] {raw_msg}"
                )
            elif is_cycle_complete_line(line):
                # On success summary, sweep all stages to DONE
                # (covers the "Notify Subscribers" stage which
                # only has a single emit pair, and any other
                # stages whose "done" emit we may have missed).
                for label in LIVE_CYCLE_STAGES:
                    self._stage_list.set_stage_status(
                        label, StageStatus.DONE,
                    )
                self._intraday_progress_label.setText(
                    "Cycle complete."
                )
            # Other lines (e.g. "V1 cycle starting:",
            # "compute_runs row:") are informational; don't
            # surface them in the stage progress UI.

    def _on_intraday_stderr(self) -> None:
        if self._intraday_process is None:
            return
        data = bytes(
            self._intraday_process.readAllStandardError(),
        ).decode("utf-8", errors="replace")
        if not data:
            return
        self._intraday_stderr_buffer += data
        for line in data.splitlines():
            if line.strip():
                logger.info("intraday stderr: %s", line)

    def _on_intraday_finished(
        self, exit_code: int, exit_status,
    ) -> None:
        """QProcess.finished → clean up + show success/failure."""
        proc = self._intraday_process
        self._intraday_process = None
        self._local_running = False
        self._inflight_kind = None

        # Force activity log + mutex refresh so the compute_runs
        # row's finished_at gets reflected without waiting 5s
        self._activity_scheduler.trigger_now()
        self._mutex_scheduler.trigger_now()

        if exit_code == 0:
            # Sweep all stages to DONE (covers any missed "done"
            # emits — e.g. Notify Subscribers fires "done"
            # exactly once, no chance to oscillate)
            for label in LIVE_CYCLE_STAGES:
                self._stage_list.set_stage_status(
                    label, StageStatus.DONE,
                )
            self._intraday_progress_label.setText("Cycle complete.")
            self._set_status(
                "Intraday cycle complete (exit 0). Activity "
                "log updated."
            )
            QMessageBox.information(
                self, "Cycle complete",
                "V1 cycle finished successfully.",
            )
        else:
            # Parse failure summary from stderr (or stdout —
            # cmd_cycle writes the summary to stderr but
            # graceful handling either way)
            failed_internal, error_msg = parse_cycle_failure_summary(
                self._intraday_stderr_buffer,
            )
            stderr_tail = "\n".join(
                self._intraday_stderr_buffer
                .splitlines()[-10:]
            )
            self._intraday_progress_label.setText(
                f"FAILED in {failed_internal or 'unknown'}: "
                f"{error_msg or 'see stderr'}"
            )
            self._set_status(
                f"Intraday cycle FAILED (exit {exit_code})."
                + (f" Failed stage: {failed_internal}"
                   if failed_internal else "")
            )
            from .widgets.backfill_failure_dialog import (
                BackfillFailureDialog,
            )
            BackfillFailureDialog(
                exit_code=exit_code,
                failed_stage=failed_internal,
                error_message=error_msg,
                stderr_tail=stderr_tail,
                full_stderr=self._intraday_stderr_buffer,
                parent=self,
            ).exec()

    def _on_intraday_proc_error(self, error) -> None:
        """QProcess.errorOccurred — e.g. executable not found.
        finished() will fire too; the failure dialog covers
        the same ground."""
        if self._intraday_process is None:
            return
        msg = (
            self._intraday_process.errorString()
            if self._intraday_process else str(error)
        )
        logger.error(
            "Intraday QProcess errorOccurred: %s", msg,
        )

    def _on_cancel_intraday_clicked(self) -> None:
        """Terminate the QProcess: SIGTERM → 5s grace → SIGKILL."""
        if self._intraday_process is None:
            return
        if self._intraday_process.state() == QProcess.NotRunning:
            return

        reply = QMessageBox.question(
            self, "Cancel intraday cycle?",
            "This will interrupt the running cycle. The "
            "pipeline rolls back any in-progress transaction "
            "and the compute_runs row stays as-is (cmd_cycle's "
            "finally block updates it with failed_stage='unknown')."
            "\n\n"
            "Cancel anyway?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        logger.warning("Sending SIGTERM to intraday QProcess")
        self._set_status("Cancelling intraday cycle (SIGTERM)…")
        self._intraday_progress_label.setText(
            "Cancelling… SIGTERM sent."
        )
        self._intraday_process.terminate()

        def _kill_if_still_alive():
            if self._intraday_process is None:
                return
            if (
                self._intraday_process.state()
                != QProcess.NotRunning
            ):
                logger.warning(
                    "Intraday QProcess didn't exit after %dms; "
                    "sending SIGKILL", CANCEL_GRACE_MS,
                )
                self._intraday_progress_label.setText(
                    "Cancelling… SIGKILL sent."
                )
                self._intraday_process.kill()

        QTimer.singleShot(CANCEL_GRACE_MS, _kill_if_still_alive)

    def _on_cancel_backfill_clicked(self) -> None:
        """Terminate the QProcess: SIGTERM → 5s grace → SIGKILL."""
        if self._backfill_process is None:
            return
        if self._backfill_process.state() == QProcess.NotRunning:
            return

        reply = QMessageBox.question(
            self, "Cancel backfill?",
            "This will interrupt the running backfill. The "
            "pipeline rolls back its current transaction but "
            "leaves a stale backfill_runs marker. Use "
            "`trip-ema-etl backfill --release-lock` afterward to "
            "clear it before re-running.\n\n"
            "Cancel anyway?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        logger.warning("Sending SIGTERM to backfill QProcess")
        self._set_status("Cancelling backfill (SIGTERM)…")
        self._backfill_progress_label.setText(
            "Cancelling… SIGTERM sent."
        )
        self._backfill_process.terminate()

        # Grace period: if still running after CANCEL_GRACE_MS,
        # SIGKILL it.
        def _kill_if_still_alive():
            if self._backfill_process is None:
                return
            if self._backfill_process.state() != QProcess.NotRunning:
                logger.warning(
                    "Backfill QProcess didn't exit after %dms; "
                    "sending SIGKILL", CANCEL_GRACE_MS,
                )
                self._backfill_progress_label.setText(
                    "Cancelling… SIGKILL sent."
                )
                self._backfill_process.kill()

        QTimer.singleShot(CANCEL_GRACE_MS, _kill_if_still_alive)

    # ============================================================
    # Trend dialog
    # ============================================================

    def _open_trend_dialog(self, kind: TrendKind) -> None:
        exchange = self._picker.current_exchange()
        if exchange is None:
            QMessageBox.information(
                self, "Trend", "Select an exchange first.",
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
        """Stop schedulers + kill any in-flight QProcess before
        widget destruction."""
        if (self._backfill_process is not None
                and self._backfill_process.state()
                != QProcess.NotRunning):
            logger.warning(
                "GUI closing while backfill still running — "
                "SIGTERM"
            )
            self._backfill_process.terminate()
        for sch in (
            self._activity_scheduler,
            self._mutex_scheduler,
            self._overview_scheduler,
        ):
            try:
                sch.stop()
            except Exception:
                pass
