"""RefreshListingsConfirmationModal (Step 13d-3).

Light confirmation dialog shown before `trip-ema-etl refresh-listings
--exchange X` is invoked from the ETL Status view.

Refresh Listings is a low-risk operation (pulls NSE CSVs, syncs
the tickers table — adds new listings, marks delisted), but it
DOES mutate tickers state, so we surface it behind a confirmation
to prevent accidental clicks.

Returns QDialog.Accepted on Proceed, QDialog.Rejected on Cancel.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QLabel, QVBoxLayout,
)


class RefreshListingsConfirmationModal(QDialog):
    """Modal confirmation for Refresh Listings."""

    def __init__(
        self,
        *,
        exchange: str,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._exchange = exchange
        self.setWindowTitle("Refresh Listings — Confirm")
        self.setModal(True)
        self.resize(520, 220)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 16, 20, 16)
        outer.setSpacing(12)

        self._title_label = QLabel(
            f"<b>Refresh listings for {exchange}?</b>"
        )
        self._title_label.setTextFormat(Qt.RichText)
        outer.addWidget(self._title_label)

        self._detail_label = QLabel(
            f"This will download the latest "
            f"<b>{exchange}</b> equities + ETFs CSVs, then sync "
            f"the tickers table:<br>"
            f"&nbsp;&nbsp;• New listings will be inserted.<br>"
            f"&nbsp;&nbsp;• Delisted entries will be marked "
            f"inactive.<br>"
            f"&nbsp;&nbsp;• Existing rows with unchanged "
            f"identity stay untouched."
        )
        self._detail_label.setTextFormat(Qt.RichText)
        self._detail_label.setWordWrap(True)
        outer.addWidget(self._detail_label)

        self._duration_label = QLabel(
            "Expected duration: 10–30 seconds (network + parse + "
            "DB sync)."
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
