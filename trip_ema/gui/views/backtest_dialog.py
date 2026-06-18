"""Non-modal dialog showing the result of an in-GUI backtest.

Pops up from SignalsView when 'Run Backtest' is clicked. Stays open
until the user closes it (multiple dialogs can coexist for
side-by-side comparison across tickers or ranges).

Layout:
  ┌─────────────────────────────────────────────────────────────┐
  │ Parameters             Summary                              │
  │ ─────────────────────  ───────────────────────              │
  │ Ticker:    NSE:RELIANCE  Trades:        N (W wins, L losses)│
  │ Range:     5 years       Win rate:      X.X%                │
  │ Capital:   ₹100,000      Final capital: ₹X (from ₹100k)     │
  │ Strategy:  ...           Total return:  X.X% (compounded)   │
  │ Entry:     max(EMAs)…    Worst MAE:     -X.X%               │
  │ Exit:      close…        CAGR:          X.X%                │
  │ Bars:      X (Y warmup)  Duration:      DD-Mon-YY → DD-Mon-YY│
  ├─────────────────────────────────────────────────────────────┤
  │ Closed trades (N)                                            │
  │ ┌─# ─ Entry ─ Entry$ ─ Exit ─ Exit$ ─ #Shares ─ P/L ─ Ret%─ MAE%─┐│
  │ │ 1  15-Jan-25 1234.5  03-Feb-25 1287.1 81.012 ₹4,304 +4.3% -2.1%││
  │ │ 2  17-Mar-25 ...                                                ││
  │ └─────────────────────────────────────────────────────────────────┘│
  ├─────────────────────────────────────────────────────────────┤
  │ Open trade (if any):                                        │
  │   Entered 02-Jun-26 at ₹X. Mark-to-market: ₹Y (+Z.Z%).     │
  ├─────────────────────────────────────────────────────────────┤
  │                                                  [ Close ] │
  └─────────────────────────────────────────────────────────────┘

Notes:
  - QDialog with setModal(False) so the user can still pan/zoom the
    main chart while the dialog is up. The signals view holds a
    list of open dialogs so they don't get GC'd; each one calls
    Qt.WA_DeleteOnClose so closing fully releases it.
  - Color coding: green return%/PL on wins, red on losses. Open
    trade panel uses a neutral border so 'still in progress' reads
    differently from 'closed at a profit'.
  - The dialog is read-only — nothing here writes to disk. Per
    Phase B spec the backtest is transient; signal_backtest_runs
    is NOT touched.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor, QFont
from PySide6.QtWidgets import (
    QDialog, QFormLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel,
    QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from ..backtest_engine import BacktestResult, OpenTrade, Trade


# Match the SignalsView chart palette so the dialog reads as the
# same product. These are read straight from gui/views/signals.py.
_COLOR_WIN = "#1f9c5e"
_COLOR_LOSS = "#c84a4a"
_COLOR_OPEN_TRADE_BORDER = "#5cb6e8"
_COLOR_MUTED = "#888888"

# Subtle row tint — barely visible, just enough to scan-read the
# table at a glance.
_COLOR_WIN_BG = "#e7f5ec"
_COLOR_LOSS_BG = "#fbeaea"


class BacktestDialog(QDialog):
    """Show one backtest result. Non-modal."""

    def __init__(
        self,
        result: BacktestResult,
        *,
        ticker_label: str,
        range_label: str,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self.setWindowTitle(f"Backtest — {ticker_label} ({range_label})")
        self.setModal(False)
        # Qt.WA_DeleteOnClose so closing the window also releases the
        # C++ object — otherwise the signals view's open_dialogs list
        # would keep a stale wrapper around.
        self.setAttribute(Qt.WA_DeleteOnClose, True)
        self.resize(1100, 650)

        self._result = result
        self._ticker_label = ticker_label
        self._range_label = range_label
        self._build_ui()

    # ---------- layout ---------- #

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)
        outer.setSpacing(10)

        # Header row: parameters + summary, side by side
        header = QHBoxLayout()
        header.setSpacing(10)
        header.addWidget(self._build_parameters_box(), 1)
        header.addWidget(self._build_summary_box(), 1)
        outer.addLayout(header)

        # Closed trades table
        trades_label = QLabel(
            f"<b>Closed trades</b> ({len(self._result.trades)})"
        )
        outer.addWidget(trades_label)
        outer.addWidget(self._build_trades_table(), 1)

        # Open trade footer (if any)
        if self._result.open_trade is not None:
            outer.addWidget(self._build_open_trade_panel(
                self._result.open_trade,
            ))

        # Close button
        button_row = QHBoxLayout()
        button_row.addStretch(1)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.close)
        close_btn.setDefault(True)
        button_row.addWidget(close_btn)
        outer.addLayout(button_row)

    def _build_parameters_box(self) -> QGroupBox:
        """Left panel: ticker / range / strategy parameters."""
        box = QGroupBox("Parameters")
        form = QFormLayout(box)
        form.setContentsMargins(10, 10, 10, 10)
        form.setSpacing(4)

        bars_evaluated = self._result.bars_evaluated
        bars_warmup = self._result.bars_skipped_warmup
        bars_total = bars_evaluated + bars_warmup

        rows = [
            ("Ticker:",   self._ticker_label),
            ("Range:",    self._range_label),
            ("Capital:",  self._result.parameters.get("capital_per_trade", "—")),
            ("Strategy:", self._result.parameters.get("strategy", "—")),
            ("Entry:",    self._result.parameters.get("entry", "—")),
            ("Exit:",     self._result.parameters.get("exit", "—")),
            ("Bars:",     f"{bars_total} total ({bars_evaluated} evaluated, "
                          f"{bars_warmup} warmup)"),
        ]
        for label, value in rows:
            form.addRow(label, _wrap(value))
        return box

    def _build_summary_box(self) -> QGroupBox:
        """Right panel: headline metrics."""
        box = QGroupBox("Summary")
        form = QFormLayout(box)
        form.setContentsMargins(10, 10, 10, 10)
        form.setSpacing(4)

        s = self._result.summary

        n_trades_text = (
            f"{s.n_trades} "
            f"({s.n_wins} wins, {s.n_losses} losses)"
            if s.n_trades > 0 else "0"
        )

        if s.n_trades == 0:
            duration_text = "—"
        else:
            assert s.first_entry_date and s.last_exit_date  # noqa: S101
            years = s.duration_days / 365.25
            duration_text = (
                f"{s.first_entry_date.strftime('%d-%b-%y')} → "
                f"{s.last_exit_date.strftime('%d-%b-%y')} "
                f"({years:.2f} years)"
            )

        rows = [
            ("Trades:",         n_trades_text),
            ("Win rate:",       f"{s.win_rate_pct:.1f}%"),
            ("Starting:",       f"₹{s.starting_capital_inr:,.0f}"),
            ("Final capital:",  f"₹{s.final_capital_inr:,.0f}"),
            ("Total return:",   _signed_pct_text(s.total_return_pct,
                                                 suffix=" (compounded)")),
            ("Total P/L (₹):",  _signed_inr_text(s.total_pnl_inr)),
            ("Worst MAE:",      f"{s.worst_mae_pct:.1f}%"),
            ("CAGR:",           _signed_pct_text(s.cagr_pct)),
            ("Duration:",       duration_text),
        ]
        for label, value_widget_or_str in rows:
            if isinstance(value_widget_or_str, QWidget):
                form.addRow(label, value_widget_or_str)
            else:
                form.addRow(label, _wrap(value_widget_or_str))
        return box

    def _build_trades_table(self) -> QTableWidget:
        """The middle section: one row per closed trade."""
        cols = [
            "#", "Entry date", "Entry ₹", "Exit date", "Exit ₹",
            "Shares", "P/L (₹)", "Return %", "MAE %",
        ]
        tbl = QTableWidget(len(self._result.trades), len(cols))
        tbl.setHorizontalHeaderLabels(cols)
        tbl.verticalHeader().setVisible(False)
        tbl.setAlternatingRowColors(True)
        tbl.setEditTriggers(QTableWidget.NoEditTriggers)
        tbl.setSelectionBehavior(QTableWidget.SelectRows)
        tbl.setSelectionMode(QTableWidget.SingleSelection)
        tbl.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeToContents,
        )
        tbl.horizontalHeader().setStretchLastSection(True)

        for row_idx, trade in enumerate(self._result.trades):
            self._populate_trade_row(tbl, row_idx, trade)

        return tbl

    def _populate_trade_row(
        self, tbl: QTableWidget, row: int, trade: Trade,
    ) -> None:
        """One row in the trades table."""
        is_win = trade.return_pct > 0
        is_loss = trade.return_pct < 0
        row_bg: Optional[QBrush] = None
        if is_win:
            row_bg = QBrush(QColor(_COLOR_WIN_BG))
        elif is_loss:
            row_bg = QBrush(QColor(_COLOR_LOSS_BG))

        cells = [
            str(row + 1),
            trade.entry_date.strftime("%d-%b-%y"),
            f"{trade.entry_price:,.2f}",
            trade.exit_date.strftime("%d-%b-%y"),
            f"{trade.exit_price:,.2f}",
            f"{trade.n_shares:,.3f}",
            _signed_inr_text(trade.pnl_inr, plain=True),
            f"{trade.return_pct:+.2f}%",
            f"{trade.max_adverse_excursion_pct:.2f}%",
        ]
        for col_idx, text in enumerate(cells):
            item = QTableWidgetItem(text)
            # Right-align numeric columns (everything except dates and #)
            if col_idx in (2, 4, 5, 6, 7, 8):
                item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            # Color the return% cell text (green/red) for fast scanning
            if col_idx == 7:
                if is_win:
                    item.setForeground(QBrush(QColor(_COLOR_WIN)))
                elif is_loss:
                    item.setForeground(QBrush(QColor(_COLOR_LOSS)))
                f = QFont()
                f.setBold(True)
                item.setFont(f)
            if row_bg is not None:
                item.setBackground(row_bg)
            tbl.setItem(row, col_idx, item)

    def _build_open_trade_panel(self, ot: OpenTrade) -> QGroupBox:
        """Footer panel for a still-open trade. Mark-to-market only —
        not counted in summary stats above."""
        box = QGroupBox("Open trade — not counted in summary above")
        box.setStyleSheet(
            f"QGroupBox {{ border: 1px solid {_COLOR_OPEN_TRADE_BORDER}; "
            f"border-radius: 3px; margin-top: 6px; }} "
            f"QGroupBox::title {{ subcontrol-origin: margin; "
            f"left: 8px; padding: 0 4px; color: {_COLOR_OPEN_TRADE_BORDER}; }}"
        )
        layout = QVBoxLayout(box)
        layout.setContentsMargins(10, 10, 10, 10)

        # Direction-aware tint
        ret_sign = "+" if ot.unrealized_return_pct >= 0 else ""
        text = (
            f"Entered {ot.entry_date.strftime('%d-%b-%y')} at "
            f"<b>₹{ot.entry_price:,.2f}</b>, held {ot.bars_held} bar(s). "
            f"Last close: <b>₹{ot.last_bar_close:,.2f}</b>. "
            f"Mark-to-market: <b>{_inr_html(ot.unrealized_pnl_inr)}</b> "
            f"({ret_sign}{ot.unrealized_return_pct:.2f}%). "
            f"Current MAE: {ot.current_mae_pct:.2f}%."
        )
        label = QLabel(text)
        label.setWordWrap(True)
        label.setTextFormat(Qt.RichText)
        layout.addWidget(label)
        return box


# ---------- formatting helpers ---------- #


def _signed_pct_text(value: float, suffix: str = "") -> str:
    """e.g. -3.42 -> '-3.42%' ; 12.0 -> '+12.00%'."""
    return f"{value:+.2f}%{suffix}"


def _signed_inr_text(value: float, *, plain: bool = False) -> str:
    """₹-aware signed money. `plain=True` strips the sign on negative
    values that already render with a minus inside the format spec
    (used for table cells where a leading '+' on positives is the
    visual cue but '-' on negatives already comes from the number)."""
    if plain:
        # Format with leading sign for positives, leave negatives alone
        if value > 0:
            return f"+₹{value:,.2f}"
        return f"₹{value:,.2f}"
    return f"{'+' if value > 0 else ''}₹{value:,.2f}"


def _inr_html(value: float) -> str:
    """Rich-text version of _signed_inr_text with color coding for
    the open-trade footer."""
    color = _COLOR_WIN if value > 0 else _COLOR_LOSS if value < 0 else _COLOR_MUTED
    sign = "+" if value > 0 else ""
    return f'<span style="color:{color}">{sign}₹{value:,.2f}</span>'


def _wrap(text: str) -> QLabel:
    """Wrap a string in a QLabel with word-wrap on, so long parameter
    descriptions don't push the dialog wider than needed."""
    label = QLabel(text)
    label.setWordWrap(True)
    return label