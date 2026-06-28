"""TickerChipArea — flow layout of removable ticker chips.

Used in the ETL Status view's Backfill panel. Each chip shows
"SYMBOL ✕" — clicking the ✕ removes it. The chip area exposes
add_chip / chips / clear / remove_chip.

In 13b, chips are added via the operator typing in the Stocks
input and clicking ADD. 13c adds autocomplete + DB validation.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QPushButton, QSizePolicy, QVBoxLayout,
    QWidget,
)


# Soft pill background — neutral so it doesn't compete with the
# colored backgrounds used elsewhere in the GUI
_CHIP_STYLE = """
    QFrame#TickerChip {
        background-color: #2d4a5c;
        border-radius: 9px;
        padding: 2px 6px;
    }
    QLabel#TickerChipLabel {
        color: #d0e0e8;
        font-size: 11px;
    }
    QPushButton#TickerChipRemove {
        background: transparent;
        color: #d0e0e8;
        border: none;
        padding: 0 2px;
        font-size: 11px;
        font-weight: bold;
    }
    QPushButton#TickerChipRemove:hover {
        color: #ff6b6b;
    }
"""


class TickerChip(QWidget):
    """One pill. Emits remove_requested(symbol) when ✕ clicked."""

    remove_requested = Signal(str)

    def __init__(
        self, symbol: str, parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._symbol = symbol
        self.setObjectName("TickerChip")
        self.setStyleSheet(_CHIP_STYLE)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 2, 4, 2)
        layout.setSpacing(4)

        label = QLabel(symbol)
        label.setObjectName("TickerChipLabel")
        layout.addWidget(label)

        remove_btn = QPushButton("✕")
        remove_btn.setObjectName("TickerChipRemove")
        remove_btn.setFixedSize(14, 14)
        remove_btn.setCursor(Qt.PointingHandCursor)
        remove_btn.clicked.connect(
            lambda: self.remove_requested.emit(self._symbol),
        )
        layout.addWidget(remove_btn)

        self.setSizePolicy(
            QSizePolicy.Fixed, QSizePolicy.Fixed,
        )

    def symbol(self) -> str:
        return self._symbol


class TickerChipArea(QWidget):
    """Container that holds zero-or-more TickerChip widgets and
    wraps them across multiple rows.

    For 13b we use a simple QHBoxLayout — Qt will let chips
    overflow rightward. In 13c we may revisit with a proper
    FlowLayout if we get a lot of chips (the autocomplete will
    make multi-chip backfills realistic).
    """

    chips_changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._chips: list[TickerChip] = []
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(4)
        self._layout.addStretch(1)
        self.setSizePolicy(
            QSizePolicy.Expanding, QSizePolicy.Fixed,
        )

    def add_chip(self, symbol: str) -> bool:
        """Add a chip for the given symbol. Returns True if added,
        False if it was already present (no-op)."""
        symbol = symbol.strip().upper()
        if not symbol:
            return False
        if any(c.symbol() == symbol for c in self._chips):
            return False
        chip = TickerChip(symbol)
        chip.remove_requested.connect(self.remove_chip)
        # Insert before the stretch so chips stay left-aligned
        self._layout.insertWidget(
            self._layout.count() - 1, chip,
        )
        self._chips.append(chip)
        self.chips_changed.emit()
        return True

    def remove_chip(self, symbol: str) -> None:
        for chip in list(self._chips):
            if chip.symbol() == symbol:
                self._layout.removeWidget(chip)
                chip.setParent(None)
                chip.deleteLater()
                self._chips.remove(chip)
                self.chips_changed.emit()
                return

    def clear(self) -> None:
        for chip in list(self._chips):
            self._layout.removeWidget(chip)
            chip.setParent(None)
            chip.deleteLater()
        self._chips.clear()
        self.chips_changed.emit()

    def chips(self) -> list[str]:
        return [c.symbol() for c in self._chips]

    def is_empty(self) -> bool:
        return len(self._chips) == 0
