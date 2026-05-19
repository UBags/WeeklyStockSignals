"""ETL Status & Control view (Tab A).

Layout (Phase 14.6c):
  ┌──────────────────────────────────────────────────────────────────┐
  │ [Exchange: NSE ▼ — EoD 2026-05-19 ✓]   [Refresh Listings] [Run   │
  │  EoD] [Run Intraday] [Recompute Tiers] [Unpark All]   [Refresh]  │
  ├──────────────────────────────────────────────────────────────────┤
  │ Exchange: NSE                                                    │
  │ Initial load: started …  completed …                             │
  │ Last EoD: … ✓ success (data 2026-05-19)                          │
  │ Last intraday: …  Last listings: …  Last tiering: …              │
  ├──────────────────────────────────┬───────────────────────────────┤
  │ Current run / recent runs table  │ Parked tickers panel          │
  │ (sortable, color-coded by        │ Symbol / parked_at /          │
  │  outcome)                        │ last_error                    │
  ├──────────────────────────────────┴───────────────────────────────┤
  │ Tier counts: T1=200  T2=500  T3=1600   Parked: 7                 │
  └──────────────────────────────────────────────────────────────────┘

QTimer polls /v1/admin/etl/status every 5s (configurable). Each
poll spawns BackgroundWorkers so the UI never blocks.

Phase 14.6c additions:
  - Exchange picker (QComboBox + summary label) at the top-left of
    the action bar. Populated on first refresh from
    /v1/admin/exchanges; persisted across launches via QSettings
    (KEY_LAST_SELECTED_EXCHANGE). Action buttons target the picked
    exchange. Tier counts scope to the picked exchange.
  - Dedicated ExchangeStatePanel above the runs table showing the
    full exchange_state row for the selected exchange.

What stays GLOBAL (intentional, per design Q5 Option C):
  - The recent-runs table. Shows all runs across all exchanges so
    operators see system-wide ETL activity. Exchange is visible via
    the runner's notes field.
  - The parked tickers list. Same reasoning.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QComboBox, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QListWidget,
    QListWidgetItem, QMessageBox, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from ..db import GuiDbSession
from ..http_client import ApiError, TripEmaHttpClient
from ..workers import RefreshScheduler, submit
from .exchange_state_panel import ExchangeStatePanel
from .settings import (
    load_last_selected_exchange,
    save_last_selected_exchange,
)

logger = logging.getLogger(__name__)


# Color codes for run outcomes — same palette as the circuit-breaker
# states elsewhere in the system.
OUTCOME_COLORS = {
    "success": "#1f7a1f",   # green
    "partial": "#b08000",   # amber
    "failed":  "#a02020",   # red
}


# Sentinel for "no exchange chosen yet". Used as the picker's initial
# state before the first /v1/admin/exchanges response lands. Action
# buttons are disabled while this is the value; an early click would
# otherwise send empty-string and hit a 422.
_EXCHANGE_LOADING = "(loading…)"


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

        # Phase 14.6c: the picker drives every operator action. Until
        # /v1/admin/exchanges populates it we hold the sentinel and
        # gate action buttons.
        self._selected_exchange: Optional[str] = None
        self._exchanges_loaded: bool = False

        self._build_ui()
        self._scheduler = RefreshScheduler(
            self.REFRESH_CADENCE_SECONDS, run_on_start=True,
        )
        self._scheduler.tick.connect(self._on_refresh_tick)

        # Kick off the one-shot exchanges-list fetch so the picker
        # populates as soon as the network responds. Independent of
        # the periodic-refresh scheduler — we only need this once
        # per session.
        self._fetch_exchanges_list()

        if start_refresh_on_init:
            self._scheduler.start()

    # ---------- UI construction ---------- #

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)
        outer.setSpacing(10)

        outer.addLayout(self._build_action_bar())
        outer.addWidget(self._build_state_panel())
        outer.addLayout(self._build_main_body(), 1)
        outer.addWidget(self._build_counts_row())

    def _build_action_bar(self) -> QHBoxLayout:
        bar = QHBoxLayout()

        # ---- Picker (left) ---- #
        # Combo + status hint together act as the "annotated picker"
        # per design Q3 Option C. The hint label updates whenever
        # the panel refreshes — see _refresh_picker_hint().
        picker_label = QLabel("Exchange:")
        picker_label.setStyleSheet("font-weight: 600;")
        bar.addWidget(picker_label)

        self._exchange_combo = QComboBox()
        self._exchange_combo.setMinimumWidth(120)
        self._exchange_combo.addItem(_EXCHANGE_LOADING)
        self._exchange_combo.setEnabled(False)
        self._exchange_combo.currentTextChanged.connect(
            self._on_exchange_picker_changed
        )
        bar.addWidget(self._exchange_combo)

        self._picker_hint = QLabel("")
        self._picker_hint.setStyleSheet("color: #666; font-style: italic;")
        bar.addWidget(self._picker_hint)

        bar.addSpacing(20)

        # ---- Action buttons (middle) ---- #
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

        # Action buttons start disabled until BOTH conditions hold:
        #   1. token is configured
        #   2. picker has resolved to a real exchange
        # _refresh_action_buttons_enabled() centralises the rule.
        self._refresh_action_buttons_enabled()

        bar.addStretch(1)

        # ---- Refresh (right) ---- #
        self._refresh_button = QPushButton("Refresh")
        self._refresh_button.clicked.connect(self._on_refresh_button_clicked)
        bar.addWidget(self._refresh_button)

        return bar

    def _build_state_panel(self) -> QWidget:
        """The dedicated ExchangeStatePanel widget. Pure display;
        EtlStatusView drives its data via set_state / set_not_yet_active /
        set_load_failed."""
        self._state_panel = ExchangeStatePanel(parent=self)
        return self._state_panel

    def _build_main_body(self) -> QHBoxLayout:
        body = QHBoxLayout()
        body.setSpacing(10)

        # Left: recent runs table (kept global per design Q5).
        runs_box = QGroupBox("Recent ETL runs (all exchanges)")
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

        # Right: parked tickers panel (also global).
        parked_box = QGroupBox("Parked tickers (all exchanges)")
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

    # ---------- exchange picker plumbing ---------- #

    def _fetch_exchanges_list(self) -> None:
        """One-shot fetch of /v1/admin/exchanges to populate the
        picker. Runs once at construction; no polling — the configured
        exchange set is static for a session.

        Worker thread keeps UI responsive even if the server is slow.
        """
        http = self._http
        worker = submit(lambda: http.list_exchanges())
        worker.signals.finished.connect(self._on_exchanges_received)
        worker.signals.failed.connect(self._on_exchanges_failed)

    def _on_exchanges_received(self, payload: dict) -> None:
        exchanges = list(payload.get("exchanges", []))
        self._populate_exchange_picker(exchanges)

    def _on_exchanges_failed(self, message: str) -> None:
        """Picker fetch failed. Leave the picker disabled with a
        sentinel so action buttons stay gated; surface the error in
        the picker hint so the operator knows what went wrong."""
        logger.warning("Exchange list fetch failed: %s", message)
        self._picker_hint.setText(f"(picker offline: {message[:60]})")
        # Leave the combo in its sentinel-only state. Action buttons
        # remain disabled because _selected_exchange stays None.

    def _populate_exchange_picker(self, exchanges: list) -> None:
        """Fill the combo with the configured exchanges and select
        the operator's last choice (or the first one if none saved
        or the saved one is no longer configured).

        Blocks signals while repopulating so the in-flight rebuild
        doesn't fire spurious _on_exchange_picker_changed events.
        """
        if not exchanges:
            # No exchanges configured — operator has misconfigured.
            self._picker_hint.setText(
                "(no exchanges configured in config.yaml)"
            )
            return

        self._exchanges_loaded = True

        # Decide the initial selection: persisted choice → first available.
        last = load_last_selected_exchange()
        target = last if last in exchanges else exchanges[0]

        # Repopulate. Block signals so currentTextChanged doesn't fire
        # during the rebuild — we want one deliberate change, after.
        self._exchange_combo.blockSignals(True)
        try:
            self._exchange_combo.clear()
            for ex in exchanges:
                self._exchange_combo.addItem(ex)
            idx = self._exchange_combo.findText(target)
            if idx >= 0:
                self._exchange_combo.setCurrentIndex(idx)
        finally:
            self._exchange_combo.blockSignals(False)

        self._exchange_combo.setEnabled(True)
        # Apply the initial selection now that the combo is wired up.
        self._apply_exchange_selection(target)

    def _on_exchange_picker_changed(self, value: str) -> None:
        """Fires when the user picks a different exchange. Ignore the
        sentinel value (only present pre-populate)."""
        if value == _EXCHANGE_LOADING or not value:
            return
        self._apply_exchange_selection(value)

    def _apply_exchange_selection(self, exchange: str) -> None:
        """Make `exchange` the active target: update internal state,
        persist to QSettings, refresh the state panel + status, and
        re-enable action buttons.

        Called both on first populate (with the persisted-or-default
        value) and on every subsequent picker change. Idempotent
        relative to picker UI state — the caller is responsible for
        having already set the combo's current index if needed.
        """
        self._selected_exchange = exchange
        try:
            save_last_selected_exchange(exchange)
        except Exception:
            # QSettings write failures shouldn't break the UI; log it.
            logger.exception("Could not persist last_selected_exchange")
        self._refresh_action_buttons_enabled()
        # Trigger an immediate refresh so the panel and tier counts
        # reflect the new selection without waiting for the next tick.
        self._scheduler.trigger_now()

    def _refresh_action_buttons_enabled(self) -> None:
        """The single source of truth for action-button enable state.
        Two conditions BOTH required:
          1. The HTTP client has an admin token (otherwise the server
             rejects with 401).
          2. The picker has resolved to a real exchange (otherwise
             we'd send `exchange=(loading…)` and get a 422).
        """
        has_token = self._http.token_configured
        has_exchange = self._selected_exchange is not None
        enabled = has_token and has_exchange
        for btn in self._action_buttons:
            btn.setEnabled(enabled)

        # Tooltip explains *which* condition is blocking the click.
        if not has_token:
            tip = "Set TRIP_EMA_ADMIN_TOKEN env var to enable action buttons."
        elif not has_exchange:
            tip = "Waiting for the exchange list to load…"
        else:
            tip = ""
        for btn in self._action_buttons:
            btn.setToolTip(tip)

    # ---------- refresh handling ---------- #

    def _on_refresh_button_clicked(self) -> None:
        self._scheduler.trigger_now()

    def _on_refresh_tick(self) -> None:
        """Periodic poll. Spawn one worker per fetch.

        Phase 14.6c: tier counts are scoped to the currently-picked
        exchange (server-side), and a separate worker fetches the
        per-exchange state for the panel.
        """
        http = self._http
        exchange = self._selected_exchange  # snapshot for closure stability

        # Status — recent runs (global) + parked count (global) + tier
        # counts (scoped to picker, or server default if picker not loaded).
        w1 = submit(lambda: http.get_etl_status(limit=20, exchange=exchange))
        w1.signals.finished.connect(self._on_status_received)
        w1.signals.failed.connect(self._on_status_failed)

        # Parked detail list (global).
        w2 = submit(lambda: http.get_etl_parked())
        w2.signals.finished.connect(self._on_parked_received)
        w2.signals.failed.connect(self._on_parked_failed)

        # Per-exchange state for the panel. Only fetched when the
        # picker has a real value; pre-load we just leave the panel
        # in its empty state.
        if exchange is not None:
            w3 = submit(lambda: http.get_exchange_state(exchange))
            w3.signals.finished.connect(self._on_exchange_state_received)
            # We need the exchange code at failure time too, so wrap
            # the callback. The closure pins `exchange` to the value
            # that was active when the fetch was kicked off — the
            # operator may have switched by the time the response
            # lands, but the panel should still show coherent data
            # for whichever exchange the WORKER was fetching.
            w3.signals.failed.connect(
                lambda msg, ex=exchange: self._on_exchange_state_failed(ex, msg)
            )

    def _on_status_received(self, payload: dict) -> None:
        self._populate_runs_table(payload.get("recent_runs", []))
        tc = payload.get("tier_counts", {})
        parked_count = payload.get("parked_count", 0)
        ex_label = self._selected_exchange or "—"
        self._counts_label.setText(
            f"{ex_label} tier counts: T1={tc.get('tier_1', 0)} "
            f"T2={tc.get('tier_2', 0)} T3={tc.get('tier_3', 0)}   "
            f"|   Parked (all): {parked_count}"
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

    def _on_exchange_state_received(self, payload: dict) -> None:
        """Per-exchange state response: feed it to the panel and
        update the picker hint."""
        self._state_panel.set_state(payload)
        self._refresh_picker_hint_from_state(payload)

    def _on_exchange_state_failed(self, exchange: str, message: str) -> None:
        """The state fetch failed for `exchange`. Distinguish 404
        (no activity yet — normal for never-touched exchanges) from
        actual transport errors (server down, etc.)."""
        if "404" in message or "not found" in message.lower():
            self._state_panel.set_not_yet_active(exchange)
            self._picker_hint.setText("(no activity yet)")
        else:
            self._state_panel.set_load_failed(exchange, message)
            self._picker_hint.setText("(state offline)")
        logger.warning(
            "Exchange-state fetch failed for %s: %s", exchange, message,
        )

    def _refresh_picker_hint_from_state(self, payload: dict) -> None:
        """Build the "(EoD 2026-05-19 ✓)"-style hint next to the
        combo. Pulled from the same payload the state panel uses, so
        no extra fetch — and it stays in sync with the panel by
        construction."""
        data_date = payload.get("last_eod_data_date")
        outcome = payload.get("last_eod_outcome")
        if not data_date:
            self._picker_hint.setText("(no EoD yet)")
            return
        glyph = ""
        if outcome == "success":
            glyph = "✓"
        elif outcome == "partial":
            glyph = "⚠"
        elif outcome == "failed":
            glyph = "✗"
        self._picker_hint.setText(f"(EoD {data_date[:10]} {glyph})".rstrip())

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

    # Phase 14.6c: every action targets self._selected_exchange (set
    # by the picker). The buttons are disabled when that value is None,
    # so the helper below can assume a non-None exchange.

    def _require_exchange(self) -> Optional[str]:
        """Defensive: return the current picker selection or None.
        Action handlers call this first; None means "something has
        gone wrong with button gating" — we just return without
        firing rather than risk a 422."""
        if self._selected_exchange is None:
            logger.warning(
                "Action fired without a selected exchange — gating bug?",
            )
            return None
        return self._selected_exchange

    def _on_refresh_listings_clicked(self) -> None:
        exchange = self._require_exchange()
        if exchange is None:
            return
        self._fire_action(
            f"Refresh listings ({exchange})",
            lambda: self._http.refresh_listings(exchange=exchange),
        )

    def _on_run_eod_clicked(self) -> None:
        exchange = self._require_exchange()
        if exchange is None:
            return
        self._fire_action(
            f"Run EoD ({exchange})",
            lambda: self._http.run_eod(exchange=exchange, lookback_days=7),
        )

    def _on_run_intraday_clicked(self) -> None:
        exchange = self._require_exchange()
        if exchange is None:
            return
        self._fire_action(
            f"Run intraday ({exchange})",
            lambda: self._http.run_intraday(exchange=exchange),
        )

    def _on_recompute_tiers_clicked(self) -> None:
        exchange = self._require_exchange()
        if exchange is None:
            return
        self._fire_action(
            f"Recompute tiers ({exchange})",
            lambda: self._http.recompute_tiers(exchange=exchange),
        )

    def _on_unpark_all_clicked(self) -> None:
        exchange = self._require_exchange()
        if exchange is None:
            return
        result = QMessageBox.question(
            self, f"Unpark all {exchange} tickers?",
            f"Clear the parked flag for every parked {exchange} ticker?\n"
            f"They will retry on the next EoD run.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if result != QMessageBox.Yes:
            return
        self._fire_action(
            f"Unpark all ({exchange})",
            lambda: self._http.unpark_all(exchange=exchange),
        )

    def _fire_action(self, label: str, fn) -> None:
        """Disable all action buttons until the response arrives;
        spawn a worker; show the outcome via QMessageBox + status bar.

        Note: while an action is in flight we disable buttons globally
        rather than per-button — concurrent actions on the same
        exchange are usually undesirable (e.g. two EoDs fighting),
        and the user shouldn't be encouraged to fire-and-forget.
        Re-enable returns to whatever the gating rule
        (_refresh_action_buttons_enabled) says.
        """
        for btn in self._action_buttons:
            btn.setEnabled(False)
        worker = submit(fn)
        def on_done(payload: dict):
            self._refresh_action_buttons_enabled()
            notes = payload.get("notes", "Submitted.")
            QMessageBox.information(self, f"{label} accepted", notes)
            # Trigger an immediate refresh so the user sees the new
            # in-flight row appear in the table.
            self._scheduler.trigger_now()
        def on_fail(message: str):
            self._refresh_action_buttons_enabled()
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
