"""OperationStatusBanner — single-stage progress indicator (Step 13d-3).

Spinner + status text for operations that don't fit the full
LIVE_CYCLE_STAGES list (Refresh Listings, Recompute Tiers).

States: HIDDEN (default), RUNNING (spinner + label), SUCCESS
(brief 2-sec green flash), FAILED (red border + error label).

Public API:
  - start(label_text: str) — show spinner + label, set RUNNING
  - finish_success(message: str | None = None) — show 2s SUCCESS
    flash then auto-hide
  - finish_failure(error_text: str) — show FAILED state (sticky)
  - reset() — hide + clear

Auto-hide on SUCCESS uses a QTimer; the banner does not block.
"""
from __future__ import annotations

from enum import Enum

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QProgressBar,
)


class _State(Enum):
    HIDDEN = "hidden"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"


# Auto-hide delay after a successful operation.
SUCCESS_AUTOHIDE_MS = 2500


class OperationStatusBanner(QFrame):
    """Single-row banner with marquee progress bar + status label."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFrameShape(QFrame.StyledPanel)
        self.setFrameShadow(QFrame.Plain)
        self._state: _State = _State.HIDDEN

        h = QHBoxLayout(self)
        h.setContentsMargins(10, 6, 10, 6)
        h.setSpacing(10)

        self._spinner = QProgressBar()
        # Indeterminate mode: range 0..0 produces marquee animation.
        self._spinner.setRange(0, 0)
        self._spinner.setTextVisible(False)
        self._spinner.setFixedWidth(140)
        self._spinner.setFixedHeight(14)
        h.addWidget(self._spinner)

        self._status_label = QLabel("")
        self._status_label.setTextFormat(Qt.PlainText)
        h.addWidget(self._status_label, 1)

        self._autohide_timer = QTimer(self)
        self._autohide_timer.setSingleShot(True)
        self._autohide_timer.timeout.connect(self.reset)

        self.reset()

    # ============================================================
    # Public API
    # ============================================================

    def start(self, label_text: str) -> None:
        """Begin RUNNING state with the given label."""
        self._autohide_timer.stop()
        self._state = _State.RUNNING
        self._spinner.show()
        self._status_label.setText(label_text)
        self._apply_style()
        self.show()

    def finish_success(self, message: str | None = None) -> None:
        """Transition to SUCCESS — brief flash then auto-hide."""
        self._autohide_timer.stop()
        self._state = _State.SUCCESS
        self._spinner.hide()
        self._status_label.setText(message or "Done.")
        self._apply_style()
        self.show()
        self._autohide_timer.start(SUCCESS_AUTOHIDE_MS)

    def finish_failure(self, error_text: str) -> None:
        """Transition to FAILED — sticky; user dismisses via reset()
        or a successor start()."""
        self._autohide_timer.stop()
        self._state = _State.FAILED
        self._spinner.hide()
        self._status_label.setText(f"FAILED: {error_text}")
        self._apply_style()
        self.show()

    def reset(self) -> None:
        """Hide + clear state."""
        self._autohide_timer.stop()
        self._state = _State.HIDDEN
        self._spinner.hide()
        self._status_label.clear()
        self.hide()

    def state(self) -> str:
        """Current state name (for tests)."""
        return self._state.value

    # ============================================================
    # Styling
    # ============================================================

    def _apply_style(self) -> None:
        if self._state == _State.RUNNING:
            self.setStyleSheet(
                "QFrame { background-color: #2a3a4a; "
                "border: 1px solid #3498db; border-radius: 4px; } "
                "QLabel { color: #ccc; }"
            )
        elif self._state == _State.SUCCESS:
            self.setStyleSheet(
                "QFrame { background-color: #2a4a30; "
                "border: 1px solid #2ecc71; border-radius: 4px; } "
                "QLabel { color: #d6f5d6; font-weight: 600; }"
            )
        elif self._state == _State.FAILED:
            self.setStyleSheet(
                "QFrame { background-color: #4a2a2a; "
                "border: 1px solid #e74c3c; border-radius: 4px; } "
                "QLabel { color: #f5d6d6; font-weight: 600; }"
            )
        else:
            self.setStyleSheet("")
