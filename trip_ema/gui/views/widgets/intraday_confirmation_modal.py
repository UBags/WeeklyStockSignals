"""IntradayConfirmationModal — lightweight pre-flight dialog for
the [Run Intraday] button.

Mirrors BackfillDestructiveModal's structure but is intentionally
LIGHTER (per F1 lock): intraday cycles are NOT destructive — they
fetch fresh bars + recompute incrementally + send emails. The
modal just exists to make accidental clicks one extra step away
and to surface the subscriber-count impact before emails go out.

Shows:
  - Exchange
  - Cycle kind hint (intraday during market hours, eod outside)
  - Rough runtime estimate (~3-8 min, depends on yfinance latency
    + ticker count for the recompute stages)
  - Subscriber impact (active subscriber count from
    SubscribersRepository.count_active() — caller supplies it)
  - [Cancel] [Run Cycle] buttons (Cancel is default)

exec() returns QDialog.Accepted iff the operator clicked Run Cycle.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFrame, QLabel, QVBoxLayout,
)


# Runtime estimate — coarse, since the live cycle's variance is
# dominated by yfinance latency (typically 1-5 min for Tier 1
# intraday, 3-8 min for all-tier EoD). This is meant to set
# operator expectations, not to be a precise SLA.
RUNTIME_ESTIMATE_TEXT = (
    "Expected duration: 3–8 minutes "
    "(yfinance fetch + 8 compute stages + email distribute)."
)


class IntradayConfirmationModal(QDialog):
    """Returns Accepted on Run Cycle, Rejected on Cancel/Esc."""

    def __init__(
        self,
        *,
        exchange: str,
        kind_hint: str,          # "intraday" or "eod"
        market_open: bool,
        n_active_subscribers: int,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Confirm Intraday Cycle")
        self.setModal(True)
        self.setMinimumWidth(480)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        # ---- Header ----
        header = QLabel(
            f"<b>Run V1 cycle for {exchange}?</b>"
        )
        header.setTextFormat(Qt.RichText)
        layout.addWidget(header)

        # ---- Kind / market state ----
        kind_label = QLabel(
            f"Kind: <b>{kind_hint}</b> "
            f"(market currently "
            f"{'open' if market_open else 'closed'})"
        )
        kind_label.setTextFormat(Qt.RichText)
        layout.addWidget(kind_label)

        # ---- Subscriber impact ----
        sub_color = "#ef5350" if n_active_subscribers > 0 else "#888"
        sub_label = QLabel(
            f"Email impact: "
            f"<span style='color: {sub_color}; font-weight: 600;'>"
            f"{n_active_subscribers} active subscriber"
            f"{'s' if n_active_subscribers != 1 else ''}"
            f"</span> will receive email(s) if any signals fire."
        )
        sub_label.setTextFormat(Qt.RichText)
        sub_label.setWordWrap(True)
        layout.addWidget(sub_label)

        # ---- Runtime estimate ----
        runtime_label = QLabel(RUNTIME_ESTIMATE_TEXT)
        runtime_label.setWordWrap(True)
        runtime_label.setStyleSheet("color: #888;")
        layout.addWidget(runtime_label)

        # ---- Divider ----
        divider = QFrame()
        divider.setFrameShape(QFrame.HLine)
        divider.setFrameShadow(QFrame.Sunken)
        layout.addWidget(divider)

        # ---- Pipeline outline ----
        outline = QLabel(
            "Pipeline stages:<br>"
            "&nbsp;&nbsp;1. Fetch Stock OHLCV<br>"
            "&nbsp;&nbsp;2. Compute Stock Indicators<br>"
            "&nbsp;&nbsp;3. Compute Stock Enrichment<br>"
            "&nbsp;&nbsp;4. Compute Signals<br>"
            "&nbsp;&nbsp;5. Refresh Industry Index Values<br>"
            "&nbsp;&nbsp;6. Compute Industry Indicators<br>"
            "&nbsp;&nbsp;7. Compute Industry Enrichment<br>"
            "&nbsp;&nbsp;8. Notify Subscribers"
        )
        outline.setTextFormat(Qt.RichText)
        outline.setStyleSheet("color: #aaa; font-size: 11px;")
        layout.addWidget(outline)

        # ---- Buttons ----
        button_box = QDialogButtonBox(
            QDialogButtonBox.Cancel,
        )
        # Add custom "Run Cycle" button (instead of Ok) for clarity
        run_btn = button_box.addButton(
            "Run Cycle", QDialogButtonBox.AcceptRole,
        )
        run_btn.setStyleSheet(
            "font-weight: 600; padding: 6px 12px;"
        )
        # Cancel is default (destructive-ish ops should be one extra
        # click away)
        cancel_btn = button_box.button(QDialogButtonBox.Cancel)
        cancel_btn.setDefault(True)
        cancel_btn.setAutoDefault(True)
        run_btn.setDefault(False)
        run_btn.setAutoDefault(False)

        button_box.accepted.connect(self.accept)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)
