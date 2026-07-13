"""Modal dialog for the 'Backfill historical shares' button.

Collects a start_date (with a sensible default of 10 years back) and
a 'force' flag, then closes Accepted or Rejected. The caller
(EtlStatusView) reads `values()` and POSTs to the admin endpoint.

Mirrors widgets/backfill_dialog.py's shape so the look-and-feel is
identical to the existing 'Backfill bars' dialog the operator
already knows.
"""

from __future__ import annotations

from datetime import date, timedelta

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import (
    QCheckBox, QDateEdit, QDialog, QDialogButtonBox, QFormLayout,
    QLabel, QVBoxLayout, QWidget,
)
from ....domain.market_session import market_today


# Default to 10 years ago — matches the typical operator intent for
# the industry-index backfill (10 years of history). yfinance
# typically only has ~16 months of shares data per ticker for Indian
# names; older dates return whatever's available without error.
_DEFAULT_YEARS_BACK = 10


class BackfillSharesDialog(QDialog):
    """Modal: pick start_date + force, return them via values()."""

    def __init__(
        self,
        *,
        exchange: str,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.setWindowTitle(f"Backfill historical shares — {exchange}")
        self.setModal(True)
        self.setMinimumWidth(460)
        self._exchange = exchange
        self._build_ui()

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(10)

        intro = QLabel(
            f"Fetch historical shares-outstanding snapshots from "
            f"yfinance for every active ticker on <b>{self._exchange}</b>. "
            f"One <code>instrument_shares</code> row is inserted per "
            f"filing date.\n\n"
            f"Once populated, <code>compute-market-cap</code> will "
            f"produce COMPUTED rows over the full backfill range "
            f"(no APPROX_TODAYS_SHARES gaps).\n\n"
            f"<b>Long-running</b> \u2014 about 90 minutes for a full "
            f"NSE pass (one yfinance call per ticker with a 2s delay)."
        )
        intro.setWordWrap(True)
        intro.setTextFormat(Qt.RichText)
        outer.addWidget(intro)

        form = QFormLayout()
        form.setSpacing(6)

        # Start date — default 10 years back.
        default = market_today(self._exchange) - timedelta(
            days=365 * _DEFAULT_YEARS_BACK,
        )
        self._date_edit = QDateEdit()
        self._date_edit.setCalendarPopup(True)
        self._date_edit.setDisplayFormat("yyyy-MM-dd")
        self._date_edit.setDate(QDate(default.year, default.month, default.day))
        self._date_edit.setMinimumDate(QDate(2000, 1, 1))
        self._date_edit.setMaximumDate(QDate.currentDate())
        form.addRow("Start date:", self._date_edit)

        # Force checkbox
        self._force_check = QCheckBox(
            "Force re-run even if a backfill ran in the last 80 days"
        )
        form.addRow("", self._force_check)
        outer.addLayout(form)

        # OK / Cancel
        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel,
        )
        button_box.accepted.connect(self.accept)
        button_box.rejected.connect(self.reject)
        outer.addWidget(button_box)

    def values(self) -> tuple[date, bool]:
        """Return (start_date, force). Caller invokes this only
        after exec() returned Accepted."""
        qd = self._date_edit.date()
        return date(qd.year(), qd.month(), qd.day()), self._force_check.isChecked()