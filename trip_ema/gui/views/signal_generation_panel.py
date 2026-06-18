"""Signal-generation panel for the Signals tab (Phase 15c).

A `QGroupBox` widget embedded BELOW the existing chart area on the
Signals tab. Lets the operator fire a backtest signal-generation
run for one configured exchange and see the "last generated file"
metadata for every configured exchange.

Compact layout (all controls on one row, metadata on a second):

    +- Generate backtest signal history -----------------------------------------+
    | Exchange: [NSE v]  Timeframes: [x] 1W [ ] 1D  Mode: (*) Incr ( ) Full  [Generate signals] |
    | -- Last generation per exchange --  BSE: no file yet  NSE: 13.7 MB ...     |
    +----------------------------------------------------------------------------+
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


_METADATA_POLL_INTERVAL_MS = 30_000
_POST_POST_REFRESH_DELAY_MS = 5_000
_MAX_METADATA_ROWS = 20


def _format_size(n_bytes: Optional[int]) -> str:
    if n_bytes is None:
        return "\u2014"
    if n_bytes < 1024:
        return f"{n_bytes} B"
    if n_bytes < 1024 ** 2:
        return f"{n_bytes / 1024:.1f} KB"
    if n_bytes < 1024 ** 3:
        return f"{n_bytes / (1024 ** 2):.1f} MB"
    return f"{n_bytes / (1024 ** 3):.2f} GB"


def _format_mtime(mtime_iso: Optional[str]) -> str:
    if not mtime_iso:
        return "\u2014"
    try:
        dt = datetime.fromisoformat(mtime_iso.replace("Z", "+00:00"))
        return dt.astimezone().strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return mtime_iso[:16]


def _format_metadata_line(payload: dict) -> str:
    if not payload.get("exists"):
        return "no file yet"
    size = _format_size(payload.get("size_bytes"))
    mtime = _format_mtime(payload.get("mtime"))
    counts = payload.get("counts") or {}
    total = counts.get("signals_total")
    total_str = f"{total:,} signals" if total is not None else "(no counts)"
    return f"{size}  \u00b7  {mtime}  \u00b7  {total_str}"


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
        self._metadata_labels: dict[str, QLabel] = {}

        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(_METADATA_POLL_INTERVAL_MS)
        self._poll_timer.timeout.connect(self._refresh_metadata_rows)

        self._post_refresh_timer = QTimer(self)
        self._post_refresh_timer.setSingleShot(True)
        self._post_refresh_timer.setInterval(_POST_POST_REFRESH_DELAY_MS)
        self._post_refresh_timer.timeout.connect(self._refresh_metadata_rows)

        self._build_ui()

        self._picker.selection_changed.connect(self._on_picker_selection_changed)

        if start_polling_on_init:
            self._poll_timer.start()

    # ---------- UI construction ---------- #

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 6, 8, 6)
        outer.setSpacing(4)

        # Single row: Exchange | Timeframes | Mode | Generate button
        controls_row = QHBoxLayout()
        controls_row.setSpacing(12)

        self._picker = ExchangePicker(self._http, label_text="Exchange:")
        controls_row.addWidget(self._picker)

        controls_row.addWidget(QLabel("Timeframes:"))
        self._chk_1w = QCheckBox("1W")
        self._chk_1w.setChecked(True)
        self._chk_1d = QCheckBox("1D")
        self._chk_1w.toggled.connect(self._refresh_button_enabled)
        self._chk_1d.toggled.connect(self._refresh_button_enabled)
        controls_row.addWidget(self._chk_1w)
        controls_row.addWidget(self._chk_1d)

        controls_row.addSpacing(8)
        controls_row.addWidget(QLabel("Mode:"))
        self._rb_incremental = QRadioButton("Incremental")
        self._rb_incremental.setChecked(True)
        self._rb_force_full = QRadioButton("Force full")
        self._mode_group = QButtonGroup(self)
        self._mode_group.addButton(self._rb_incremental)
        self._mode_group.addButton(self._rb_force_full)
        controls_row.addWidget(self._rb_incremental)
        controls_row.addWidget(self._rb_force_full)

        controls_row.addSpacing(8)
        self._generate_btn = QPushButton("Generate signals")
        self._generate_btn.setEnabled(False)
        self._generate_btn.clicked.connect(self._on_generate_clicked)
        controls_row.addWidget(self._generate_btn)

        controls_row.addStretch(1)
        outer.addLayout(controls_row)

        # Second row: metadata (horizontal)
        meta_row = QHBoxLayout()
        meta_row.setSpacing(16)
        sep_label = QLabel("\u2014 Last generation per exchange \u2014")
        sep_label.setStyleSheet("color: #666; font-style: italic;")
        meta_row.addWidget(sep_label)

        self._metadata_grid = QGridLayout()
        self._metadata_grid.setColumnStretch(1, 1)
        meta_row.addLayout(self._metadata_grid)
        meta_row.addStretch(1)
        outer.addLayout(meta_row)

    # ---------- Picker-driven state ---------- #

    def _on_picker_selection_changed(self, exchange: str) -> None:
        self._maybe_build_metadata_rows()
        self._refresh_button_enabled()

    def _maybe_build_metadata_rows(self) -> None:
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
            value_label = QLabel("(loading\u2026)")
            value_label.setStyleSheet("color: #666;")
            self._metadata_grid.addWidget(ex_label, row_idx, 0)
            self._metadata_grid.addWidget(value_label, row_idx, 1)
            self._metadata_labels[ex] = value_label

        self._refresh_metadata_rows()

    def _refresh_button_enabled(self, *_) -> None:
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
            return
        timeframes = self._collect_timeframes()
        if not timeframes:
            return
        force_full = self._rb_force_full.isChecked()

        if force_full:
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

        self._generate_btn.setEnabled(False)

        http = self._http

        def do_post():
            return http.generate_signals(
                exchange=exchange, timeframes=timeframes,
                force_full=force_full,
            )

        submit(
            do_post,
            on_finished=lambda payload: self._on_post_succeeded(payload, exchange),
            on_failed=lambda msg: self._on_post_failed(msg, exchange),
        )

    def _on_post_succeeded(self, payload: dict, exchange: str) -> None:
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
        self._post_refresh_timer.start()
        self._refresh_button_enabled()

    def _on_post_failed(self, message: str, exchange: str) -> None:
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
        if not self._metadata_labels:
            return
        for exchange in list(self._metadata_labels.keys()):
            self._fetch_one_metadata(exchange)

    def _fetch_one_metadata(self, exchange: str) -> None:
        http = self._http

        def do_get():
            return http.get_signal_generation_metadata(exchange)

        submit(
            do_get,
            on_finished=lambda payload: self._on_metadata_received(exchange, payload),
            on_failed=lambda msg: self._on_metadata_failed(exchange, msg),
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
        truncated = message[:80]
        label.setText(f"(fetch failed: {truncated})")
        label.setStyleSheet("color: #a02020; font-style: italic;")

    # ---------- Lifecycle ---------- #

    def on_close(self) -> None:
        try:
            self._poll_timer.stop()
        except Exception:
            pass
        try:
            self._post_refresh_timer.stop()
        except Exception:
            pass
