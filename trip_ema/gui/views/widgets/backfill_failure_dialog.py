"""BackfillFailureDialog — shown when a backfill QProcess exits
with a non-zero code.

Per Q5 (option a): exit code + failed_stage line from stdout
summary + last 5 lines of stderr in an expandable details
section.

`failed_stage` is parsed from the CLI's stdout summary line:
    "backfill FAILED in <stage>: <error_message>"
The view's stdout buffer is searched for that pattern before
opening this dialog; if found, the parsed pieces are passed to
the dialog; if not, we just show "Exit code N — see stderr."

The dialog also surfaces an actionable next step: if the failure
looks like a stale backfill_runs marker (IntegrityError mentioned
in the CLI's own error path), it suggests
    `trip-ema-etl backfill --release-lock`
as a one-line operator action.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFrame, QLabel, QPlainTextEdit,
    QPushButton, QVBoxLayout,
)


_RELEASE_LOCK_HINT = (
    "another backfill appears to be active"
)


class BackfillFailureDialog(QDialog):

    def __init__(
        self,
        *,
        exit_code: int,
        failed_stage: str | None,
        error_message: str | None,
        stderr_tail: str,
        full_stderr: str,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Backfill Failed")
        self.setModal(True)
        self.setMinimumWidth(640)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 14, 16, 12)
        outer.setSpacing(10)

        # Header
        header = QLabel("✗ Backfill Failed")
        header.setStyleSheet(
            "font-size: 14px; font-weight: 700; color: #c84a4a;"
        )
        outer.addWidget(header)

        # Summary block
        summary_lines = [f"<b>Exit code:</b> {exit_code}"]
        if failed_stage:
            summary_lines.append(
                f"<b>Failed stage:</b> <code>{failed_stage}</code>"
            )
        if error_message:
            # Clip long messages; the full text appears in details
            clipped = error_message
            if len(clipped) > 200:
                clipped = clipped[:200] + "…"
            summary_lines.append(
                f"<b>Error:</b> {_escape_html(clipped)}"
            )
        summary = QLabel("<br>".join(summary_lines))
        summary.setTextFormat(Qt.RichText)
        summary.setWordWrap(True)
        outer.addWidget(summary)

        # Stale-lock hint?
        suggestion_text = _maybe_release_lock_hint(
            error_message, full_stderr,
        )
        if suggestion_text:
            sep = QFrame()
            sep.setFrameShape(QFrame.HLine)
            outer.addWidget(sep)
            hint = QLabel(suggestion_text)
            hint.setTextFormat(Qt.RichText)
            hint.setWordWrap(True)
            hint.setStyleSheet(
                "color: #b08000; padding: 2px;"
            )
            outer.addWidget(hint)

        # Stderr tail header
        tail_label = QLabel("<b>Recent stderr:</b>")
        tail_label.setTextFormat(Qt.RichText)
        outer.addWidget(tail_label)

        # Stderr tail (preformatted)
        tail_widget = QPlainTextEdit()
        tail_widget.setReadOnly(True)
        tail_widget.setPlainText(stderr_tail or "(empty)")
        monofont = QFont("Monospace")
        monofont.setStyleHint(QFont.TypeWriter)
        monofont.setPointSize(9)
        tail_widget.setFont(monofont)
        tail_widget.setMaximumHeight(140)
        tail_widget.setStyleSheet(
            "background-color: #1a1a1a; color: #d8d8d8;"
        )
        outer.addWidget(tail_widget)

        # Expandable full-stderr details (hidden by default)
        self._full_stderr_widget = QPlainTextEdit()
        self._full_stderr_widget.setReadOnly(True)
        self._full_stderr_widget.setPlainText(
            full_stderr or "(no stderr)"
        )
        self._full_stderr_widget.setFont(monofont)
        self._full_stderr_widget.setStyleSheet(
            "background-color: #1a1a1a; color: #d8d8d8;"
        )
        self._full_stderr_widget.hide()
        outer.addWidget(self._full_stderr_widget, 1)

        self._show_full_btn = QPushButton(
            "▸ Show full stderr"
        )
        self._show_full_btn.clicked.connect(
            self._toggle_full_stderr,
        )
        self._show_full_btn.setStyleSheet(
            "text-align: left; padding: 2px; border: none; "
            "color: #888;"
        )
        outer.addWidget(self._show_full_btn)

        # Close button
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        outer.addWidget(buttons)

    def _toggle_full_stderr(self) -> None:
        if self._full_stderr_widget.isHidden():
            self._full_stderr_widget.show()
            self._show_full_btn.setText("▾ Hide full stderr")
            self.resize(self.width(), self.height() + 200)
        else:
            self._full_stderr_widget.hide()
            self._show_full_btn.setText("▸ Show full stderr")


def parse_failure_summary(
    stdout: str,
) -> tuple[str | None, str | None]:
    """Parse the CLI's stdout for the failure summary line:
        backfill FAILED in <stage>: <error_message>

    Returns (failed_stage, error_message), with None for missing
    components."""
    import re
    m = re.search(
        r"backfill FAILED in (\S+): (.+)",
        stdout,
    )
    if not m:
        return None, None
    return m.group(1), m.group(2).rstrip()


def _maybe_release_lock_hint(
    error_message: str | None, full_stderr: str,
) -> str | None:
    """If the error indicates a stale backfill_runs marker, return
    a hint suggesting --release-lock. Else None."""
    combined = (error_message or "") + " " + (full_stderr or "")
    if _RELEASE_LOCK_HINT in combined.lower():
        return (
            "<b>Hint:</b> Looks like a stale "
            "<code>backfill_runs</code> marker (a previous run may "
            "have been Ctrl-C'd or crashed). Clear it with:<br>"
            "&nbsp;&nbsp;&nbsp;&nbsp;"
            "<code>trip-ema-etl backfill --release-lock</code>"
            "<br>...then retry the backfill from the GUI."
        )
    return None


def _escape_html(s: str) -> str:
    return (
        s.replace("&", "&amp;")
         .replace("<", "&lt;")
         .replace(">", "&gt;")
    )
