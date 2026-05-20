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
    QDialog, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QListWidget,
    QListWidgetItem, QMessageBox, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from ..db import GuiDbSession
from ..http_client import ApiError, TripEmaHttpClient
from ..workers import RefreshScheduler, submit
from .exchange_state_panel import ExchangeStatePanel
from .widgets.exchange_picker import ExchangePicker

logger = logging.getLogger(__name__)


# Color codes for run outcomes — same palette as the circuit-breaker
# states elsewhere in the system.
OUTCOME_COLORS = {
    "success": "#1f7a1f",   # green
    "partial": "#b08000",   # amber
    "failed":  "#a02020",   # red
}


# Backward-compat re-export: existing tests import _EXCHANGE_LOADING
# from etl_status. The sentinel now lives on the extracted picker
# widget; we re-export the name here so tests don't have to know.
from .widgets.exchange_picker import EXCHANGE_LOADING_SENTINEL as _EXCHANGE_LOADING  # noqa: E402


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

        # Phase 15c: picker logic moved into ExchangePicker widget.
        # `self._exchange_combo`, `self._picker_hint`,
        # `self._selected_exchange`, and the populate/apply methods
        # are kept as legacy attributes/methods that delegate to the
        # picker so the 14.6c test suite continues to pass unchanged.

        self._build_ui()
        self._scheduler = RefreshScheduler(
            self.REFRESH_CADENCE_SECONDS, run_on_start=True,
        )
        self._scheduler.tick.connect(self._on_refresh_tick)

        # Picker kicks off its own list_exchanges fetch automatically
        # (auto_fetch=True is the default). No need to call it here.

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
        # Phase 15c: extracted to ExchangePicker widget. We still
        # expose `_exchange_combo`, `_picker_hint`, and
        # `_selected_exchange` for back-compat with 14.6c tests that
        # reach into those attributes directly.
        self._picker = ExchangePicker(
            self._http, label_text="Exchange:", parent=self,
        )
        # Legacy alias attrs — keep existing tests passing.
        self._exchange_combo = self._picker.combo
        self._picker_hint = self._picker.hint_label

        # Bold the label by re-styling the picker's first child (the
        # internal QLabel "Exchange:"). The picker's internal layout
        # is non-public, but the visual style was load-bearing in
        # 14.6c — operators got used to the bold label. If this
        # styling poking breaks in a future PySide6 rev, the worst
        # case is a non-bold label, which is fine.
        try:
            internal_label = self._picker.findChild(QLabel)
            if internal_label is not None:
                internal_label.setStyleSheet("font-weight: 600;")
        except Exception:
            pass

        # Wire selection changes to our follow-up actions (button
        # gating + scheduler trigger). The picker has already
        # persisted to QSettings and updated its own state by the
        # time we get here.
        self._picker.selection_changed.connect(
            self._on_picker_selection_changed
        )

        bar.addWidget(self._picker)

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
        bar.addWidget(make_action_button(
            "Backfill…", self._on_backfill_clicked,
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

    # ---------- exchange picker plumbing (15c: delegated to widget) ---------- #

    @property
    def _selected_exchange(self) -> Optional[str]:
        """Backward-compat: pre-15c code stored this as a plain
        attribute. Now it's just whatever the picker reports."""
        return self._picker.current_exchange() if hasattr(self, "_picker") else None

    @_selected_exchange.setter
    def _selected_exchange(self, value: Optional[str]) -> None:
        """No-op setter. The picker owns this state in 15c; the
        property is read-only in practice. Kept as a setter to
        absorb any legacy code that might still try to assign it
        directly (e.g. tests that pre-set state before populate).
        """
        # If we don't yet have a picker (called during __init__ before
        # _build_ui), silently swallow.
        if not hasattr(self, "_picker"):
            return
        # Forward to the picker if it's a non-None real value. For
        # None / sentinel, leave the picker alone.
        if value is not None and value != _EXCHANGE_LOADING:
            self._picker._apply_selection(value)

    @property
    def _exchanges_loaded(self) -> bool:
        """Backward-compat: was a flag set after first populate."""
        return self._picker.is_loaded() if hasattr(self, "_picker") else False

    @_exchanges_loaded.setter
    def _exchanges_loaded(self, value: bool) -> None:
        """No-op — the picker owns its own loaded state."""
        pass

    def _fetch_exchanges_list(self) -> None:
        """Backward-compat shim — 14.6c tests may call this directly.
        Forwards to the picker; safe to call multiple times since
        the picker's fetch is idempotent (the worker just runs again)."""
        self._picker._fetch_exchanges_list()

    def _populate_exchange_picker(self, exchanges: list) -> None:
        """Backward-compat shim for the 14.6c test
        `_seed_picker(view, [...])` helper, which calls this on
        EtlStatusView to bypass the network. Forwards to the picker."""
        self._picker.populate_with_exchanges(exchanges)

    def _on_picker_selection_changed(self, exchange: str) -> None:
        """Slot for ExchangePicker.selection_changed. Picker has
        already updated its own state and persisted to QSettings;
        all we do here is refresh action buttons and trigger an
        immediate panel refresh so tier counts + the
        ExchangeStatePanel reflect the new selection.
        """
        self._refresh_action_buttons_enabled()
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

    def _on_backfill_clicked(self) -> None:
        """Open the modal BackfillDialog to collect start_date +
        optional tier; if the operator confirms, POST to /backfill
        through the shared `_fire_action` machinery (button gating,
        worker, outcome modal).

        Phase 15d: deliberately no extra confirmation modal here.
        The dialog itself is the gating step — the operator picks
        a start date and clicks OK. A double-confirm would feel
        bureaucratic for an operation that's bounded by start_date.
        """
        exchange = self._require_exchange()
        if exchange is None:
            return
        # Lazy import — avoid loading the dialog module at view
        # construction time so test imports don't have to pay for it.
        from .widgets.backfill_dialog import BackfillDialog
        dialog = BackfillDialog(exchange=exchange, parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        start_date, tier = dialog.values()
        label = (
            f"Backfill ({exchange}, from {start_date.isoformat()}"
            f"{f', tier={tier}' if tier is not None else ''})"
        )
        self._fire_action(
            label,
            lambda: self._http.backfill(
                exchange=exchange, start_date=start_date, tier=tier,
            ),
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
