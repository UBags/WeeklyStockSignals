"""Backfill dialog (Phase 15d).

A small modal dialog driven from EtlStatusView's "Backfill…" button.
Collects:
  - start_date (required) — QDateEdit, defaults to today minus 30 days
  - tier (optional, 1-3) — QSpinBox gated by a "Restrict to one tier"
    checkbox; when the checkbox is off, tier is None (server uses
    all tiers).

The exchange is NOT collected here — the EtlStatusView's picker is
the canonical exchange selector for the tab, and asking again here
would invite mismatches.

Returns via the standard QDialog `exec()` pattern:
  dialog = BackfillDialog(parent=view)
  if dialog.exec() == QDialog.DialogCode.Accepted:
      start, tier = dialog.values()
      # ... drive the http.backfill call

Inputs are validated at the widget level (QDateEdit limits range,
QSpinBox limits tier to 1-3). The OK button stays enabled — if the
operator picks today as the start date, that's a 1-day backfill, and
that's a legitimate (if usually pointless) operation. We don't
second-guess.
"""

from __future__ import annotations

from datetime import date, timedelta

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import (
    QCheckBox, QDateEdit, QDialog, QDialogButtonBox, QFormLayout,
    QLabel, QSpinBox, QVBoxLayout, QWidget,
)


# Default start date: 30 days back from today. Operators typically
# backfill recent gaps (vendor data hiccups, a delayed run); a
# multi-year backfill is rarer and deserves a deliberate adjustment.
_DEFAULT_START_LOOKBACK_DAYS = 30

# Earliest plausible date. Yahoo Finance has data well before this,
# but a 1900 start would produce nonsense given the system's actual
# coverage. 1990 is a comfortable floor.
_EARLIEST_START = date(1990, 1, 1)


class BackfillDialog(QDialog):
    """Modal dialog collecting start_date and optional tier for a
    backfill request."""

    def __init__(
        self,
        *,
        exchange: str,
        parent: QWidget | None = None,
        default_start: date | None = None,
    ):
        super().__init__(parent)
        self._exchange = exchange
        self.setWindowTitle(f"Backfill — {exchange}")
        self.setModal(True)

        if default_start is None:
            default_start = date.today() - timedelta(days=_DEFAULT_START_LOOKBACK_DAYS)

        self._build_ui(default_start)

    # ---------- UI ---------- #

    def _build_ui(self, default_start: date) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(12)

        header = QLabel(
            f"Trigger historical backfill for <b>{self._exchange}</b>.<br>"
            "Pick a start date; backfill runs forward through today."
        )
        header.setTextFormat(Qt.TextFormat.RichText)
        header.setWordWrap(True)
        outer.addWidget(header)

        form = QFormLayout()
        form.setSpacing(8)

        # Start date
        self._date_edit = QDateEdit()
        self._date_edit.setCalendarPopup(True)
        self._date_edit.setDisplayFormat("yyyy-MM-dd")
        self._date_edit.setMinimumDate(QDate(
            _EARLIEST_START.year,
            _EARLIEST_START.month,
            _EARLIEST_START.day,
        ))
        # Max date: today. Backfilling from a future date is nonsense.
        today = date.today()
        self._date_edit.setMaximumDate(QDate(today.year, today.month, today.day))
        self._date_edit.setDate(QDate(
            default_start.year,
            default_start.month,
            default_start.day,
        ))
        form.addRow("Start date:", self._date_edit)

        # Tier — gated by a "restrict" checkbox.
        # The checkbox is the user's primary control; the spinbox
        # only takes effect when the checkbox is on.
        self._tier_checkbox = QCheckBox("Restrict to one tier")
        self._tier_checkbox.toggled.connect(self._on_tier_checkbox_toggled)
        form.addRow("", self._tier_checkbox)

        self._tier_spinbox = QSpinBox()
        self._tier_spinbox.setMinimum(1)
        self._tier_spinbox.setMaximum(3)
        self._tier_spinbox.setValue(1)
        self._tier_spinbox.setEnabled(False)  # off until checkbox checked
        form.addRow("Tier (1-3):", self._tier_spinbox)

        outer.addLayout(form)

        # OK / Cancel buttons (standard QDialogButtonBox handles the
        # OS-specific button ordering automatically).
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel,
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)
        self._button_box = buttons

    # ---------- event handlers ---------- #

    def _on_tier_checkbox_toggled(self, checked: bool) -> None:
        """Enable/disable the spinbox to mirror the checkbox state.
        When off, the spinbox is greyed out and its value is ignored
        in `values()` regardless of what's displayed."""
        self._tier_spinbox.setEnabled(checked)

    # ---------- public API ---------- #

    def values(self) -> tuple[date, int | None]:
        """Return the (start_date, tier) the operator selected.

        `tier` is None when the "Restrict to one tier" checkbox is
        off, regardless of what number the spinbox is displaying.
        This matches the server contract: omit `tier` to backfill
        all tiers.

        Safe to call after either accept() or reject() — the
        widget values persist until the dialog is destroyed.
        Caller is expected to gate on `exec() == Accepted`.
        """
        qd = self._date_edit.date()
        start = date(qd.year(), qd.month(), qd.day())
        tier = self._tier_spinbox.value() if self._tier_checkbox.isChecked() else None
        return start, tier
