"""FlashingLight — a circular indicator that pulses when active.

Used in the ETL Status view's Backfill and Intraday panels to give
a strong visual cue that something is currently running.

Active = pulsing between full brightness and 40% brightness at
500ms intervals. Inactive = solid dark gray, no animation.
"""
from __future__ import annotations

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QBrush, QColor, QPainter
from PySide6.QtWidgets import QWidget


# Palette — feel free to tweak per design preference
_COLOR_ACTIVE_BRIGHT = QColor("#e74c3c")   # warm red, fully on
_COLOR_ACTIVE_DIM = QColor("#7a2920")      # darker red, dimmed
_COLOR_INACTIVE = QColor("#444444")        # neutral dark gray

# Pulse cadence (ms between brightness flips)
_PULSE_PERIOD_MS = 500
# Indicator size in px (square widget)
_INDICATOR_SIZE = 14


class FlashingLight(QWidget):
    """Small circular indicator. Start inactive; call set_active()
    to turn it on or off."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._active = False
        self._bright = True  # alternates while active

        self.setFixedSize(_INDICATOR_SIZE, _INDICATOR_SIZE)

        self._timer = QTimer(self)
        self._timer.setInterval(_PULSE_PERIOD_MS)
        self._timer.timeout.connect(self._on_pulse)

    # ------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------

    def set_active(self, active: bool) -> None:
        if active == self._active:
            return
        self._active = active
        if active:
            self._bright = True
            self._timer.start()
        else:
            self._timer.stop()
        self.update()

    def is_active(self) -> bool:
        return self._active

    # ------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------

    def _on_pulse(self) -> None:
        self._bright = not self._bright
        self.update()

    def paintEvent(self, _event) -> None:
        if self._active:
            color = (
                _COLOR_ACTIVE_BRIGHT if self._bright
                else _COLOR_ACTIVE_DIM
            )
        else:
            color = _COLOR_INACTIVE

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setBrush(QBrush(color))
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(
            1, 1, _INDICATOR_SIZE - 2, _INDICATOR_SIZE - 2,
        )

    def sizeHint(self) -> QSize:
        return QSize(_INDICATOR_SIZE, _INDICATOR_SIZE)
