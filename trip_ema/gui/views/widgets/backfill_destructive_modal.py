"""BackfillDestructiveModal — the pre-flight confirmation dialog
shown before a backfill kicks off.

Modal shows:
  - Date range
  - Stocks scope (count + first-few-symbols preview, or "ALL active")
  - Pre-flight COUNT for historical_signals (will be DELETED)
  - Pre-flight COUNT for market_overview_daily (will become STALE)
  - Refetch-bars flag
  - Rough ETA (from CLI's same estimator)
  - [Cancel] [Confirm] buttons (Cancel is default — destructive ops
    should be one extra click away)

Caller constructs with the pre-computed counts (the queries live
in trip_ema.persistence.backfill_queries; the view runs them
before opening this dialog). exec() returns QDialog.Accepted iff
the operator clicked Confirm.
"""
from __future__ import annotations

from datetime import date

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFrame, QLabel, QVBoxLayout,
)


class BackfillDestructiveModal(QDialog):
    """Returns Accepted on Confirm, Rejected on Cancel/Esc."""

    def __init__(
        self,
        *,
        exchange: str,
        date_from: date,
        date_to: date | None,
        n_stocks: int,
        stocks_preview: list[str] | None,
        n_historical_to_delete: int,
        n_overview_to_stale: int,
        refetch_bars: bool,
        runtime_estimate_minutes: int,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Confirm Backfill")
        self.setModal(True)
        self.setMinimumWidth(520)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 14, 16, 12)
        outer.setSpacing(10)

        # Warning header
        header = QLabel("⚠ Destructive Operation")
        header.setStyleSheet(
            "font-size: 14px; font-weight: 700; color: #c84a4a;"
        )
        outer.addWidget(header)

        # Plan summary block
        plan_lines = [
            f"<b>Exchange:</b> {exchange}",
            f"<b>From:</b> {date_from.isoformat()}"
            + (
                f"  <b>To:</b> {date_to.isoformat()}"
                if date_to else "  <b>To:</b> today"
            ),
        ]
        if n_stocks == 0:
            plan_lines.append(
                "<b>Stocks:</b> ALL active tickers on this exchange"
            )
        else:
            preview = (
                ", ".join(stocks_preview[:5]) if stocks_preview
                else ""
            )
            if stocks_preview and len(stocks_preview) > 5:
                preview += ", …"
            plan_lines.append(
                f"<b>Stocks:</b> {n_stocks} selected ({preview})"
            )
        plan_lines.append(
            f"<b>Refetch bars:</b> "
            f"{'Yes (yfinance pull)' if refetch_bars else 'No'}"
        )
        plan_label = QLabel("<br>".join(plan_lines))
        plan_label.setTextFormat(Qt.RichText)
        outer.addWidget(plan_label)

        # Separator
        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet("color: #555;")
        outer.addWidget(sep)

        # Impact block
        impact_label = QLabel(
            "<b>This will:</b><br>"
            f"&bull; <span style='color:#c84a4a;'>"
            f"DELETE {n_historical_to_delete:,}</span> rows from "
            f"<code>historical_signals</code> "
            f"(computed_at &gt;= {date_from.isoformat()}, "
            f"scoped to selected tickers)<br>"
            f"&bull; "
            f"<span style='color:#b08000;'>"
            f"MARK {n_overview_to_stale:,}</span> rows in "
            f"<code>market_overview_daily</code> as stale "
            f"(snapshot_date &gt;= {date_from.isoformat()})<br>"
            "&nbsp;&nbsp;&nbsp;&nbsp;"
            "<i>(not auto-rebuilt; re-run via "
            "<code>trip-ema-etl snapshot</code> if needed)</i><br>"
            "&bull; Re-fetch OHLCV bars (if Refetch is on)<br>"
            "&bull; Re-compute the V1 pipeline forward from "
            f"<code>{date_from.isoformat()}</code> to today, "
            "writing fresh <code>historical_signals</code>, "
            "<code>current_signals</code>, indicator state, and "
            "industry tables"
        )
        impact_label.setTextFormat(Qt.RichText)
        impact_label.setWordWrap(True)
        outer.addWidget(impact_label)

        # ETA
        eta_label = QLabel(
            f"<i>Estimated runtime: ~{runtime_estimate_minutes} "
            f"min</i>"
        )
        eta_label.setTextFormat(Qt.RichText)
        eta_label.setStyleSheet("color: #888; padding-top: 4px;")
        outer.addWidget(eta_label)

        # Buttons — Cancel is DEFAULT (destructive op safety)
        buttons = QDialogButtonBox()
        confirm_btn = buttons.addButton(
            "Confirm", QDialogButtonBox.AcceptRole,
        )
        cancel_btn = buttons.addButton(
            "Cancel", QDialogButtonBox.RejectRole,
        )
        cancel_btn.setDefault(True)
        cancel_btn.setAutoDefault(True)
        confirm_btn.setStyleSheet(
            "background-color: #6a1f1f; color: white; "
            "padding: 5px 14px;"
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)


def estimate_runtime_minutes(
    *, n_tickers: int, refetch_bars: bool,
) -> int:
    """Mirrors `cli._estimate_runtime_seconds`. Kept here so the
    GUI's modal stays in sync if the CLI estimator changes.

    Numbers are approximate; actual runtime depends on network,
    CPU, DB load.
    """
    yfinance_per_ticker = 0.4 if refetch_bars else 0.0
    stock_state_per_ticker = 0.1
    stock_enrichment_per_ticker = 0.1
    industry_stages_total = 30.0
    per_ticker = (
        yfinance_per_ticker
        + stock_state_per_ticker
        + stock_enrichment_per_ticker
    )
    seconds = n_tickers * per_ticker + industry_stages_total
    return max(1, round(seconds / 60))
