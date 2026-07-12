"""StageProgressList — vertical list of pipeline stages with status.

Used in the ETL Status view's Intraday panel. Each row shows:

    [icon]  Stage name                           [status text]

Where icon and text reflect the StageStatus enum:
    QUEUED      ⏸    Queued
    IN_PROGRESS →    In Progress
    DONE        ✓    Done
    FAILED      ✗    Failed
    SKIPPED     —    Skipped

In 13b, all stages start QUEUED and stay that way (the Run
Intraday button is a no-op). 13d wires per-stage status updates
from the live cycle's progress callbacks.
"""
from __future__ import annotations

from enum import Enum

from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget,
)


# ============================================================
# Status enum + visual mapping
# ============================================================

class StageStatus(Enum):
    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


_STATUS_GLYPHS = {
    StageStatus.QUEUED:      ("⏸", "Queued",      "#888"),
    StageStatus.IN_PROGRESS: ("→", "In Progress", "#3498db"),
    StageStatus.DONE:        ("✓", "Done",        "#2ecc71"),
    StageStatus.FAILED:      ("✗", "Failed",      "#e74c3c"),
    StageStatus.SKIPPED:     ("—", "Skipped",     "#777"),
}


# ============================================================
# Live cycle stages — Step 13b spec
# ============================================================
# The 7 stages of the V1 live compute cycle, in the order they
# execute. Stage 1 is bar fetch (skipped on the EoD recompute
# path); stages 2-7 are the deterministic computes.
# === STEP 13D2: LIVE_CYCLE_STAGES updated ===
# 8-stage list ordered to match cmd_cycle's actual stage emission
# sequence. Three internal cmd_cycle stages (rotation, derive,
# cache_rebuild) collapse to the single "Compute Signals" label
# via the trip_ema.gui.views.widgets.cycle_stdout_parser mapping.
LIVE_CYCLE_STAGES = (
    "Fetch Stock OHLCV",
    "Compute Stock Indicators",
    "Compute Stock Enrichment",
    "Compute Signals",
    "Refresh Industry Index Values",
    "Compute Industry Indicators",
    "Compute Industry Enrichment",
    "Notify Subscribers",
)


# ============================================================
# Single-stage row widget
# ============================================================

class StageRow(QWidget):
    """One row in the stage list."""

    def __init__(
        self, name: str, parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._name = name
        self._status = StageStatus.QUEUED

        layout = QHBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(8)

        self._icon_label = QLabel()
        self._icon_label.setFixedWidth(16)
        font = QFont()
        font.setBold(True)
        self._icon_label.setFont(font)
        layout.addWidget(self._icon_label)

        self._name_label = QLabel(name)
        layout.addWidget(self._name_label, 1)

        self._status_label = QLabel()
        layout.addWidget(self._status_label)

        self.set_status(StageStatus.QUEUED)

    def set_status(self, status: StageStatus) -> None:
        self._status = status
        glyph, text, color = _STATUS_GLYPHS[status]
        self._icon_label.setText(glyph)
        self._icon_label.setStyleSheet(f"color: {color};")
        self._status_label.setText(text)
        self._status_label.setStyleSheet(f"color: {color};")
        # Dim the name on skipped/queued so the operator can quickly
        # spot what's actually happening
        if status in (StageStatus.QUEUED, StageStatus.SKIPPED):
            self._name_label.setStyleSheet("color: #666;")
        else:
            self._name_label.setStyleSheet("")

    def status(self) -> StageStatus:
        return self._status

    def name(self) -> str:
        return self._name


# ============================================================
# Full list widget
# ============================================================

class StageProgressList(QWidget):
    """Vertical list of StageRow widgets, one per stage name."""

    def __init__(
        self,
        stage_names,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._rows: list[StageRow] = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        for name in stage_names:
            row = StageRow(name)
            layout.addWidget(row)
            self._rows.append(row)
        self.setSizePolicy(
            QSizePolicy.Expanding, QSizePolicy.Preferred,
        )

    def set_stage_status(
        self, stage_name: str, status: StageStatus,
    ) -> bool:
        """Update one stage by name. Returns True if found."""
        for row in self._rows:
            if row.name() == stage_name:
                row.set_status(status)
                return True
        return False

    def reset_all(
        self, status: StageStatus = StageStatus.QUEUED,
    ) -> None:
        for row in self._rows:
            row.set_status(status)

    def rows(self) -> list[StageRow]:
        return list(self._rows)

    def all_done(self) -> bool:
        return all(
            r.status() == StageStatus.DONE for r in self._rows
        )
