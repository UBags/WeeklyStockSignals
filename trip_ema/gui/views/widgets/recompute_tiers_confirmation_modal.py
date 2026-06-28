"""RecomputeTiersConfirmationModal (Step 13d-3).

Light confirmation dialog shown before `trip-ema-etl recompute-tiers
--exchange X` is invoked from the ETL Status view.

Recompute Tiers reads 30-day ADV from existing bars and updates
`tickers.tier`. Idempotent, no irreversible writes, but does
re-rank ~2,667 NSE tickers so we surface it behind a confirmation.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QLabel, QVBoxLayout,
)


class RecomputeTiersConfirmationModal(QDialog):
    """Modal confirmation for Recompute Tiers."""

    def __init__(
        self,
        *,
        exchange: str,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._exchange = exchange
        self.setWindowTitle("Recompute Tiers — Confirm")
        self.setModal(True)
        self.resize(520, 220)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 16, 20, 16)
        outer.setSpacing(12)

        self._title_label = QLabel(
            f"<b>Recompute tiers for {exchange}?</b>"
        )
        self._title_label.setTextFormat(Qt.RichText)
        outer.addWidget(self._title_label)

        self._detail_label = QLabel(
            f"This will read 30-day ADV from existing bars for "
            f"all active <b>{exchange}</b> tickers and re-rank "
            f"them into <b>Tier 1 / Tier 2 / Tier 3</b> buckets. "
            f"The intraday Tier-1 subset that the scheduled "
            f"orchestrator polls will be updated accordingly."
        )
        self._detail_label.setTextFormat(Qt.RichText)
        self._detail_label.setWordWrap(True)
        outer.addWidget(self._detail_label)

        self._duration_label = QLabel(
            "Expected duration: 30–60 seconds (read + rank + "
            "DB update)."
        )
        self._duration_label.setStyleSheet(
            "color: #888; font-style: italic;"
        )
        outer.addWidget(self._duration_label)

        outer.addStretch(1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel,
        )
        ok_btn = buttons.button(QDialogButtonBox.Ok)
        ok_btn.setText("Proceed")
        cancel_btn = buttons.button(QDialogButtonBox.Cancel)
        cancel_btn.setDefault(True)
        cancel_btn.setFocus()

        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    @property
    def exchange(self) -> str:
        return self._exchange
