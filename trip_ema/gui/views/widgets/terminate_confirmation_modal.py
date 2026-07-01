"""TerminateInProgressConfirmationModal (Phase 1b).

Confirmation dialog shown when the operator clicks Force Reset in
the ETL Status view. The Force Reset button becomes enabled only
when the currently-picked exchange has an in-progress row in the
`update_run_in_progress` table (fetched via the queue-snapshot
HTTP endpoint on each mutex tick).

Title format (per operator spec):
    Terminate In-process {kind} on Exchange : {exchange}

where kind is "Intraday" or "EoD" (display-cased).

Body text explains the destructive semantics: SIGTERM is sent to
the running subprocess, a 10-second grace period is allowed, then
SIGKILL. The `update_run_in_progress` marker row is cleared. The
run queue is NOT cleared — the next queued run will pick up as soon
as the dispatcher's next poll fires.

Returns:
    QDialog.Accepted on Terminate
    QDialog.Rejected on Cancel / Escape (Cancel is the default
    focus so the destructive action is never triggered by Enter).
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QLabel, QVBoxLayout,
)


def _display_kind(kind: str) -> str:
    """Map the DB-level kind string to the operator-facing form."""
    if kind == "eod":
        return "EoD"
    if kind == "intraday":
        return "Intraday"
    # Fallback — display the raw string so we don't silently swallow
    # a future kind value.
    return kind


class TerminateInProgressConfirmationModal(QDialog):
    """Modal confirmation for Force Reset of an in-progress run."""

    def __init__(
        self,
        *,
        exchange: str,
        kind: str,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._exchange = exchange
        self._kind = kind

        # Store display fields so tests can assert on them cleanly.
        self._display_kind = _display_kind(kind)
        self._title_text = (
            f"Terminate In-process {self._display_kind} "
            f"on Exchange : {exchange}"
        )

        self.setWindowTitle("Force Reset — Confirm")
        self.setModal(True)
        self.resize(520, 240)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 16, 20, 16)
        outer.setSpacing(12)

        self._title_label = QLabel(f"<b>{self._title_text}</b>")
        self._title_label.setTextFormat(Qt.RichText)
        outer.addWidget(self._title_label)

        self._detail_label = QLabel(
            "This will send <b>SIGTERM</b> to the running subprocess. "
            "If it doesn't exit within 10 seconds, <b>SIGKILL</b> "
            "follows. The in-progress marker is cleared.<br><br>"
            "The run queue is NOT cleared \u2014 any queued runs "
            "will resume on the dispatcher's next poll."
        )
        self._detail_label.setTextFormat(Qt.RichText)
        self._detail_label.setWordWrap(True)
        outer.addWidget(self._detail_label)

        self._warning_label = QLabel(
            "Use this only if a run has crashed or wedged. "
            "For normal cancellation, wait for the run to finish."
        )
        self._warning_label.setStyleSheet(
            "color: #a00; font-style: italic;"
        )
        self._warning_label.setWordWrap(True)
        outer.addWidget(self._warning_label)

        outer.addStretch(1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel,
        )
        ok_btn = buttons.button(QDialogButtonBox.Ok)
        ok_btn.setText("Terminate")
        # Style the destructive button red so it's visually distinct.
        ok_btn.setStyleSheet(
            "color: #a00; font-weight: 600;"
        )
        cancel_btn = buttons.button(QDialogButtonBox.Cancel)
        # Cancel is default focus — Enter key doesn't fire the
        # destructive action.
        cancel_btn.setDefault(True)
        cancel_btn.setFocus()

        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    @property
    def exchange(self) -> str:
        return self._exchange

    @property
    def kind(self) -> str:
        """DB-level kind value ('intraday' or 'eod')."""
        return self._kind

    @property
    def display_kind(self) -> str:
        """Operator-facing kind ('Intraday' or 'EoD')."""
        return self._display_kind

    @property
    def title_text(self) -> str:
        """The full title string shown at the top of the dialog.
        Exposed for tests to assert format without scraping widgets."""
        return self._title_text
