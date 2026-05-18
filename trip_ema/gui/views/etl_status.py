"""ETL Status & Control view (Tab A).

Layout:
  ┌──────────────────────────────────────────────────────────┐
  │ Action bar                                  [Refresh ◯]  │
  │ [Refresh Listings] [Run EoD] [Run Intraday] [Recompute   │
  │  Tiers] [Unpark All]                                     │
  ├──────────────────────────────────┬───────────────────────┤
  │ Current run / recent runs table  │ Parked tickers panel  │
  │ (sortable, color-coded by        │ Symbol / parked_at /  │
  │  outcome)                        │ last_error            │
  │                                  │                       │
  │                                  │                       │
  └──────────────────────────────────┴───────────────────────┘
  │ Tier counts: T1=200  T2=500  T3=1600   Parked: 7         │
  └──────────────────────────────────────────────────────────┘

QTimer polls /v1/admin/etl/status every 5s (configurable). Each
poll spawns a BackgroundWorker so the UI never blocks. The
[Refresh] button at top-right calls `RefreshScheduler.trigger_now()`.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QGroupBox, QHBoxLayout, QHeaderView, QLabel, QListWidget,
    QListWidgetItem, QMessageBox, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from ..db import GuiDbSession
from ..http_client import ApiError, TripEmaHttpClient
from ..workers import RefreshScheduler, submit

logger = logging.getLogger(__name__)


# Color codes for run outcomes — same palette as the circuit-breaker
# states elsewhere in the system.
OUTCOME_COLORS = {
    "success": "#1f7a1f",   # green
    "partial": "#b08000",   # amber
    "failed":  "#a02020",   # red
}


class EtlStatusView(QWidget):

    REFRESH_CADENCE_SECONDS = 5.0

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

        outer.addLayout(self._build_action_bar())
        outer.addLayout(self._build_main_body(), 1)
        outer.addWidget(self._build_counts_row())

    def _build_action_bar(self) -> QHBoxLayout:
        bar = QHBoxLayout()

        # Action buttons — disabled if no admin token.
        self._action_buttons: list[QPushButton] = []

        def make_action_button(text: str, slot) -> QPushButton:
            btn = QPushButton(text)
            btn.clicked.connect(slot)
            self._action_buttons.append(btn)
            return btn

        bar.addWidget(make_action_button(
            "Refresh Listings", self._on_refresh_listings_clicked,
        ))
        bar.addWidget(make_action_button(
            "Run EoD", self._on_run_eod_clicked,
        ))
        bar.addWidget(make_action_button(
            "Run Intraday", self._on_run_intraday_clicked,
        ))
        bar.addWidget(make_action_button(
            "Recompute Tiers", self._on_recompute_tiers_clicked,
        ))
        bar.addWidget(make_action_button(
            "Unpark All", self._on_unpark_all_clicked,
        ))

        if not self._http.token_configured:
            for btn in self._action_buttons:
                btn.setEnabled(False)
                btn.setToolTip(
                    "Set TRIP_EMA_ADMIN_TOKEN env var to enable action buttons."
                )

        bar.addStretch(1)

        self._refresh_button = QPushButton("Refresh")
        self._refresh_button.clicked.connect(self._on_refresh_button_clicked)
        bar.addWidget(self._refresh_button)

        return bar

    def _build_main_body(self) -> QHBoxLayout:
        body = QHBoxLayout()
        body.setSpacing(10)

        # Left: recent runs table
        runs_box = QGroupBox("Recent ETL runs")
        runs_layout = QVBoxLayout(runs_box)
        self._runs_table = QTableWidget()
        self._runs_table.setColumnCount(7)
        self._runs_table.setHorizontalHeaderLabels([
            "Started", "Kind", "Outcome", "Duration",
            "Tickers (ok/fail)", "Bars", "Notes",
        ])
        self._runs_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeToContents,
        )
        self._runs_table.horizontalHeader().setStretchLastSection(True)
        self._runs_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._runs_table.setSelectionBehavior(QTableWidget.SelectRows)
        self._runs_table.verticalHeader().setVisible(False)
        runs_layout.addWidget(self._runs_table)
        body.addWidget(runs_box, 3)

        # Right: parked tickers panel
        parked_box = QGroupBox("Parked tickers")
        parked_layout = QVBoxLayout(parked_box)
        self._parked_count_label = QLabel("0 tickers parked")
        self._parked_count_label.setStyleSheet("font-weight: 600;")
        parked_layout.addWidget(self._parked_count_label)
        self._parked_list = QListWidget()
        self._parked_list.setAlternatingRowColors(True)
        parked_layout.addWidget(self._parked_list, 1)
        body.addWidget(parked_box, 1)

        return body

    def _build_counts_row(self) -> QWidget:
        row_widget = QWidget()
        row = QHBoxLayout(row_widget)
        row.setContentsMargins(4, 4, 4, 4)
        self._counts_label = QLabel("Tier counts: —")
        self._counts_label.setStyleSheet("color: #666;")
        row.addWidget(self._counts_label)
        row.addStretch(1)
        self._last_refreshed_label = QLabel("Never refreshed")
        self._last_refreshed_label.setStyleSheet("color: #888; font-style: italic;")
        row.addWidget(self._last_refreshed_label)
        return row_widget

    # ---------- refresh handling ---------- #

    def _on_refresh_button_clicked(self) -> None:
        self._scheduler.trigger_now()

    def _on_refresh_tick(self) -> None:
        """Periodic poll. Spawn one worker per fetch."""
        http = self._http

        # Status (recent runs + tier counts + parked count)
        w1 = submit(lambda: http.get_etl_status(limit=20))
        w1.signals.finished.connect(self._on_status_received)
        w1.signals.failed.connect(self._on_status_failed)

        # Parked detail list
        w2 = submit(lambda: http.get_etl_parked())
        w2.signals.finished.connect(self._on_parked_received)
        w2.signals.failed.connect(self._on_parked_failed)

    def _on_status_received(self, payload: dict) -> None:
        self._populate_runs_table(payload.get("recent_runs", []))
        tc = payload.get("tier_counts", {})
        parked_count = payload.get("parked_count", 0)
        self._counts_label.setText(
            f"Tier counts: T1={tc.get('tier_1', 0)} "
            f"T2={tc.get('tier_2', 0)} T3={tc.get('tier_3', 0)} "
            f"Parked: {parked_count}"
        )
        self._parked_count_label.setText(
            f"{parked_count} ticker{'s' if parked_count != 1 else ''} parked"
        )
        self._last_refreshed_label.setText(
            f"Refreshed at {datetime.now().strftime('%H:%M:%S')}"
        )

    def _on_status_failed(self, message: str) -> None:
        self._last_refreshed_label.setText(f"Refresh failed: {message}")
        logger.warning("ETL status fetch failed: %s", message)

    def _on_parked_received(self, payload: dict) -> None:
        self._parked_list.clear()
        for p in payload.get("parked", []):
            text = f"{p['symbol']}"
            if p.get("parked_at"):
                text += f"  ({p['parked_at'][:10]})"
            if p.get("last_error"):
                text += f"\n  └─ {p['last_error'][:80]}"
            item = QListWidgetItem(text)
            item.setData(Qt.UserRole, p)
            self._parked_list.addItem(item)

    def _on_parked_failed(self, message: str) -> None:
        # Don't pop up a dialog — failures during automatic refresh are
        # noisy. The status-bar already reflects it via _on_status_failed.
        logger.warning("ETL parked fetch failed: %s", message)

    def _populate_runs_table(self, runs: list[dict]) -> None:
        self._runs_table.setRowCount(len(runs))
        for row_idx, r in enumerate(runs):
            started = _parse_iso(r.get("started_at"))
            finished = _parse_iso(r.get("finished_at"))
            duration = ""
            if started and finished:
                d = (finished - started).total_seconds()
                if d < 60:
                    duration = f"{d:.1f}s"
                elif d < 3600:
                    duration = f"{d / 60:.1f}m"
                else:
                    duration = f"{d / 3600:.1f}h"
            elif started and not finished:
                duration = "in-flight"

            cells = [
                started.strftime("%Y-%m-%d %H:%M:%S") if started else "?",
                r.get("kind", ""),
                r.get("outcome") or "in-flight",
                duration,
                f"{r.get('tickers_succeeded', 0)}/{r.get('tickers_failed', 0)}",
                str(r.get("bars_written", 0)),
                (r.get("notes") or "")[:120],
            ]
            for col, val in enumerate(cells):
                item = QTableWidgetItem(val)
                if col == 2:  # outcome column
                    color = OUTCOME_COLORS.get(r.get("outcome") or "")
                    if color:
                        item.setForeground(QBrush(QColor(color)))
                self._runs_table.setItem(row_idx, col, item)

    # ---------- action button handlers ---------- #

    def _on_refresh_listings_clicked(self) -> None:
        self._fire_action(
            "Refresh listings",
            lambda: self._http.refresh_listings(),
        )

    def _on_run_eod_clicked(self) -> None:
        self._fire_action(
            "Run EoD",
            lambda: self._http.run_eod(lookback_days=7),
        )

    def _on_run_intraday_clicked(self) -> None:
        self._fire_action(
            "Run intraday",
            lambda: self._http.run_intraday(),
        )

    def _on_recompute_tiers_clicked(self) -> None:
        self._fire_action(
            "Recompute tiers",
            lambda: self._http.recompute_tiers(),
        )

    def _on_unpark_all_clicked(self) -> None:
        result = QMessageBox.question(
            self, "Unpark all?",
            "Clear the parked flag for every parked ticker?\n"
            "They will retry on the next EoD run.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if result != QMessageBox.Yes:
            return
        self._fire_action(
            "Unpark all",
            lambda: self._http.unpark_all(),
        )

    def _fire_action(self, label: str, fn) -> None:
        """Disable all action buttons until the response arrives;
        spawn a worker; show the outcome via QMessageBox + status bar."""
        for btn in self._action_buttons:
            btn.setEnabled(False)
        worker = submit(fn)
        def on_done(payload: dict):
            for btn in self._action_buttons:
                btn.setEnabled(True)
            notes = payload.get("notes", "Submitted.")
            QMessageBox.information(self, f"{label} accepted", notes)
            # Trigger an immediate refresh so the user sees the new
            # in-flight row appear in the table.
            self._scheduler.trigger_now()
        def on_fail(message: str):
            for btn in self._action_buttons:
                btn.setEnabled(True)
            QMessageBox.warning(
                self, f"{label} failed",
                f"The action could not be submitted:\n\n{message}",
            )
        worker.signals.finished.connect(on_done)
        worker.signals.failed.connect(on_fail)

    # ---------- lifecycle ---------- #

    def on_close(self) -> None:
        """Called by MainWindow.closeEvent. Stop the refresh timer
        cleanly so the worker pool can drain."""
        try:
            if self._scheduler.is_running:
                self._scheduler.stop()
        except RuntimeError:
            # Qt object may have been destroyed already during test
            # teardown; ignore.
            pass


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        # FastAPI emits with timezone; fromisoformat handles it from 3.11+.
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None
