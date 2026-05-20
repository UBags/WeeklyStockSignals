"""Signal-generation panel for the Signals tab (Phase 15c).

A `QGroupBox` widget embedded BELOW the existing chart area on the
Signals tab. Lets the operator fire a backtest signal-generation
run for one configured exchange and see the "last generated file"
metadata for every configured exchange.

Layout:

    ┌─ Generate backtest signal history ─────────────────────┐
    │ Exchange:   [NSE ▼]                                    │
    │ Timeframes: [✓] 1W  [ ] 1D                             │
    │ Mode:       (•) Incremental ( ) Force full             │
    │           [ Generate signals ]                         │
    │                                                        │
    │ ─ Last generation per exchange ─────────────────────── │
    │ NSE:  14.0 MB  ·  2026-05-21 02:02  ·  105684 signals  │
    │ NYSE: no file yet                                      │
    └────────────────────────────────────────────────────────┘

Action flow (per design §4.5.2):
  1. User picks exchange, ticks timeframes, picks mode.
  2. Clicks [Generate signals].
  3. If force_full=True, a confirmation QMessageBox.question
     blocks the action ("This may take 30+ minutes. Continue?").
  4. POSTs /v1/admin/signals/generate with the right params.
  5. On 202: non-modal toast (information dialog) "Backtest started
     for NSE (run_id=17). Watch progress in the ETL Status tab."
     Schedule a refresh of the "last generation per exchange" grid
     for 5s later.
  6. On 409: modal warning "A backtest is already running for NSE
     (run_id=15). Wait for completion."
  7. On other ApiError: modal warning with the server's detail text.

Polling (per design §4.5.3):
  The "last generation per exchange" grid polls
  GET /v1/admin/signals/generated for each configured exchange every
  30s. Each row updates independently. When a generation completes,
  the panel picks up the new file on its next poll — no special
  notification wiring.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QGridLayout, QGroupBox, QHBoxLayout,
    QLabel, QMessageBox, QPushButton, QRadioButton, QVBoxLayout, QWidget,
)

from ..http_client import ApiError, TripEmaHttpClient
from ..workers import submit
from .widgets.exchange_picker import ExchangePicker

logger = logging.getLogger(__name__)


# How often (ms) the "last generation per exchange" grid polls the
# metadata endpoint per exchange. 30s matches design §4.5.3.
_METADATA_POLL_INTERVAL_MS = 30_000

# After a successful POST, schedule one extra refresh after this
# many ms so the operator sees the "in flight" → "completed" flip
# without having to wait a full poll cycle.
_POST_POST_REFRESH_DELAY_MS = 5_000

# Cap how many rows the metadata grid shows. With 3-4 exchanges this
# is comfortably unlimited; if the system grows to dozens, the grid
# can paginate or scroll later.
_MAX_METADATA_ROWS = 20


def _format_size(n_bytes: Optional[int]) -> str:
    """Pretty-print a file size. Returns "—" for None."""
    if n_bytes is None:
        return "—"
    if n_bytes < 1024:
        return f"{n_bytes} B"
    if n_bytes < 1024 ** 2:
        return f"{n_bytes / 1024:.1f} KB"
    if n_bytes < 1024 ** 3:
        return f"{n_bytes / (1024 ** 2):.1f} MB"
    return f"{n_bytes / (1024 ** 3):.2f} GB"


def _format_mtime(mtime_iso: Optional[str]) -> str:
    """Render an ISO timestamp as the operator-facing 'YYYY-MM-DD HH:MM'.
    Returns '—' on parse failure or None input.
    """
    if not mtime_iso:
        return "—"
    try:
        dt = datetime.fromisoformat(mtime_iso.replace("Z", "+00:00"))
        # Display in local time, dropping seconds for compactness.
        return dt.astimezone().strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return mtime_iso[:16]   # fallback: first 16 chars of ISO string


def _format_metadata_line(payload: dict) -> str:
    """Build the single-line description for one exchange's row.
    Payload is the SignalGeneratedFilePayload shape."""
    if not payload.get("exists"):
        return "no file yet"
    size = _format_size(payload.get("size_bytes"))
    mtime = _format_mtime(payload.get("mtime"))
    counts = payload.get("counts") or {}
    total = counts.get("signals_total")
    total_str = f"{total:,} signals" if total is not None else "(no counts)"
    return f"{size}  ·  {mtime}  ·  {total_str}"


class SignalGenerationPanel(QGroupBox):
    """The Phase 15c panel embedded on the Signals tab."""

    def __init__(
        self,
        http: TripEmaHttpClient,
        *,
        parent: Optional[QWidget] = None,
        start_polling_on_init: bool = True,
    ):
        super().__init__("Generate backtest signal history", parent)
        self._http = http

        # Map exchange → QLabel that shows the metadata line. Updated
        # by _refresh_metadata_rows. Initialized by the picker once
        # the exchange list is known.
        self._metadata_labels: dict[str, QLabel] = {}

        # The poll timer. Started by start_polling() / start_polling_on_init.
        # Test seam: tests pass start_polling_on_init=False and drive
        # _refresh_now() manually.
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(_METADATA_POLL_INTERVAL_MS)
        self._poll_timer.timeout.connect(self._refresh_metadata_rows)

        # One-shot timer used after a successful POST for the
        # near-immediate refresh. Single-shot, restarted as needed.
        self._post_refresh_timer = QTimer(self)
        self._post_refresh_timer.setSingleShot(True)
        self._post_refresh_timer.setInterval(_POST_POST_REFRESH_DELAY_MS)
        self._post_refresh_timer.timeout.connect(self._refresh_metadata_rows)

        self._build_ui()

        # The picker fires its list_exchanges fetch automatically.
        # We listen for selection_changed to drive both:
        #   (a) the "Generate" button's enable state, and
        #   (b) the one-shot build of the metadata grid rows.
        # The picker blocks signals during its repopulate, so the
        # combo's `currentIndexChanged` is NOT a reliable trigger
        # for (b) — `selection_changed` is what actually fires
        # after the combo settles. _maybe_build_metadata_rows is
        # idempotent so subsequent selection changes are no-ops.
        self._picker.selection_changed.connect(self._on_picker_selection_changed)

        if start_polling_on_init:
            self._poll_timer.start()

    # ---------- UI construction ---------- #

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)
        outer.setSpacing(8)

        # Row 1: exchange picker
        self._picker = ExchangePicker(self._http, label_text="Exchange:")
        outer.addWidget(self._picker)

        # Row 2: timeframe checkboxes
        tf_row = QHBoxLayout()
        tf_row.addWidget(QLabel("Timeframes:"))
        self._chk_1w = QCheckBox("1W")
        self._chk_1w.setChecked(True)   # 1W default, per design §4.5.1
        self._chk_1d = QCheckBox("1D")
        self._chk_1w.toggled.connect(self._refresh_button_enabled)
        self._chk_1d.toggled.connect(self._refresh_button_enabled)
        tf_row.addWidget(self._chk_1w)
        tf_row.addWidget(self._chk_1d)
        tf_row.addStretch(1)
        outer.addLayout(tf_row)

        # Row 3: incremental/force-full radio buttons
        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("Mode:"))
        self._rb_incremental = QRadioButton("Incremental")
        self._rb_incremental.setChecked(True)
        self._rb_force_full = QRadioButton("Force full")
        self._mode_group = QButtonGroup(self)
        self._mode_group.addButton(self._rb_incremental)
        self._mode_group.addButton(self._rb_force_full)
        mode_row.addWidget(self._rb_incremental)
        mode_row.addWidget(self._rb_force_full)
        mode_row.addStretch(1)
        outer.addLayout(mode_row)

        # Row 4: Generate button
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        self._generate_btn = QPushButton("Generate signals")
        self._generate_btn.setEnabled(False)   # picker not resolved yet
        self._generate_btn.clicked.connect(self._on_generate_clicked)
        btn_row.addWidget(self._generate_btn)
        btn_row.addStretch(1)
        outer.addLayout(btn_row)

        # Separator label for the metadata grid below
        sep_label = QLabel("— Last generation per exchange —")
        sep_label.setStyleSheet("color: #666; font-style: italic;")
        sep_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        outer.addWidget(sep_label)

        # Metadata grid: filled after the picker populates
        self._metadata_grid = QGridLayout()
        self._metadata_grid.setColumnStretch(1, 1)
        outer.addLayout(self._metadata_grid)

    # ---------- Picker-driven state ---------- #

    def _on_picker_selection_changed(self, exchange: str) -> None:
        """Slot for ExchangePicker.selection_changed. Two jobs:

        1. Build the metadata grid rows the first time we see a real
           exchange land. After that, _maybe_build_metadata_rows is
           a no-op (idempotent on _metadata_labels being non-empty),
           so subsequent picker changes only trigger (2).
        2. Refresh the Generate button's enable state — the button
           was disabled until the picker resolved, and stays gated
           by token + timeframe.
        """
        self._maybe_build_metadata_rows()
        self._refresh_button_enabled()

    def _maybe_build_metadata_rows(self) -> None:
        """When the picker first populates, iterate its combo items
        and build one row per exchange in the metadata grid.
        Idempotent — re-running this is a no-op once rows exist.

        We only build once the picker reports is_loaded()=True; that
        flips True after populate_with_exchanges() runs, which
        guarantees the combo no longer contains the loading sentinel.
        """
        if self._metadata_labels:
            return
        if not self._picker.is_loaded():
            return

        exchanges: list[str] = []
        for i in range(self._picker.combo.count()):
            text = self._picker.combo.itemText(i)
            if text:
                exchanges.append(text)

        if not exchanges:
            return

        for row_idx, ex in enumerate(exchanges[:_MAX_METADATA_ROWS]):
            ex_label = QLabel(f"{ex}:")
            ex_label.setStyleSheet("font-weight: 600;")
            value_label = QLabel("(loading…)")
            value_label.setStyleSheet("color: #666;")
            self._metadata_grid.addWidget(ex_label, row_idx, 0)
            self._metadata_grid.addWidget(value_label, row_idx, 1)
            self._metadata_labels[ex] = value_label

        # Kick off the first refresh now that rows exist.
        self._refresh_metadata_rows()

    def _refresh_button_enabled(self, *_) -> None:
        """The Generate button is enabled when ALL of:
          - picker has resolved to a real exchange,
          - at least one timeframe is checked,
          - http client has a token configured.
        """
        has_exchange = self._picker.current_exchange() is not None
        has_timeframe = self._chk_1w.isChecked() or self._chk_1d.isChecked()
        has_token = self._http.token_configured
        self._generate_btn.setEnabled(
            has_exchange and has_timeframe and has_token
        )

    # ---------- Generate button action ---------- #

    def _collect_timeframes(self) -> list[str]:
        out: list[str] = []
        if self._chk_1d.isChecked():
            out.append("1D")
        if self._chk_1w.isChecked():
            out.append("1W")
        return out

    def _on_generate_clicked(self) -> None:
        exchange = self._picker.current_exchange()
        if exchange is None:
            return   # button shouldn't have been enabled
        timeframes = self._collect_timeframes()
        if not timeframes:
            return
        force_full = self._rb_force_full.isChecked()

        if force_full:
            # Per design §4.5.2 step 1: confirm before doing
            # something destructive.
            filename = f"generated_signals/{exchange}.json"
            reply = QMessageBox.question(
                self,
                "Confirm force-full regeneration",
                (
                    f"Force-full will recompute all historical signals "
                    f"for {exchange} and overwrite existing entries in "
                    f"{filename}.\n\nThis may take 30+ minutes. "
                    f"Continue?"
                ),
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        # Disable the button while the POST is in flight so the
        # operator can't double-fire.
        self._generate_btn.setEnabled(False)

        # Capture state into a closure for the worker, then dispatch.
        http = self._http

        def do_post():
            return http.generate_signals(
                exchange=exchange, timeframes=timeframes,
                force_full=force_full,
            )

        worker = submit(do_post)
        worker.signals.finished.connect(
            lambda payload: self._on_post_succeeded(payload, exchange)
        )
        worker.signals.failed.connect(
            lambda msg: self._on_post_failed(msg, exchange)
        )

    def _on_post_succeeded(self, payload: dict, exchange: str) -> None:
        """The 202 path. Show a non-modal-ish info dialog, schedule
        the near-immediate metadata refresh, re-enable the button."""
        run_id = payload.get("run_id", "?")
        QMessageBox.information(
            self,
            "Backtest started",
            (
                f"Backtest started for {exchange} (run_id={run_id}).\n\n"
                f"Watch progress in the ETL Status tab. The 'last "
                f"generation per exchange' panel below will refresh "
                f"when the file appears."
            ),
        )
        # Refresh the metadata grid in 5s so the operator sees the
        # "no file yet" → file-appearing flip without waiting a full
        # poll cycle. (For incremental runs that finish in seconds,
        # this gives near-instant feedback. For force-full runs that
        # take 30+ minutes, it's a no-op but doesn't hurt.)
        self._post_refresh_timer.start()
        self._refresh_button_enabled()

    def _on_post_failed(self, message: str, exchange: str) -> None:
        """A 4xx / 5xx / network error. Show modal, then re-enable
        the button.

        409 (concurrent-run guard) gets a specifically friendly
        message: parse the run_id out of the server's detail string
        if we can, otherwise fall back to the raw message.
        """
        # `message` here is whatever ApiError's string repr produced —
        # the workers layer converts our exception to a string before
        # firing `failed`. Check for the 409 hallmark in the text.
        if "already in flight" in message or "run_id=" in message:
            QMessageBox.warning(
                self,
                "Backtest already running",
                (
                    f"A backtest is already running for {exchange}.\n\n"
                    f"Wait for it to finish before starting another, "
                    f"or check the ETL Status tab for progress.\n\n"
                    f"Server detail: {message}"
                ),
            )
        else:
            QMessageBox.warning(
                self,
                "Backtest request failed",
                f"Could not start backtest for {exchange}:\n\n{message}",
            )
        self._refresh_button_enabled()

    # ---------- Metadata grid polling ---------- #

    def _refresh_metadata_rows(self) -> None:
        """Fire one worker per row to fetch the latest metadata.
        Each row updates independently — a slow/failed fetch for one
        exchange doesn't block the others."""
        if not self._metadata_labels:
            return
        for exchange in list(self._metadata_labels.keys()):
            self._fetch_one_metadata(exchange)

    def _fetch_one_metadata(self, exchange: str) -> None:
        http = self._http

        def do_get():
            return http.get_signal_generation_metadata(exchange)

        worker = submit(do_get)
        worker.signals.finished.connect(
            lambda payload: self._on_metadata_received(exchange, payload)
        )
        worker.signals.failed.connect(
            lambda msg: self._on_metadata_failed(exchange, msg)
        )

    def _on_metadata_received(self, exchange: str, payload: dict) -> None:
        label = self._metadata_labels.get(exchange)
        if label is None:
            return
        label.setText(_format_metadata_line(payload))
        if payload.get("exists"):
            label.setStyleSheet("color: #1a1a1a;")
        else:
            label.setStyleSheet("color: #666; font-style: italic;")

    def _on_metadata_failed(self, exchange: str, message: str) -> None:
        label = self._metadata_labels.get(exchange)
        if label is None:
            return
        # Show the error inline rather than popping a modal — these
        # are polled in the background; a modal storm would be
        # annoying.
        truncated = message[:80]
        label.setText(f"(fetch failed: {truncated})")
        label.setStyleSheet("color: #a02020; font-style: italic;")

    # ---------- Lifecycle ---------- #

    def on_close(self) -> None:
        """Called by the parent view's on_close so the polling timers
        don't keep firing after the widget is torn down. Match
        EtlStatusView's pattern."""
        try:
            self._poll_timer.stop()
        except Exception:
            pass
        try:
            self._post_refresh_timer.stop()
        except Exception:
            pass
