"""ExchangeStatePanel — dedicated state panel for one exchange (Phase 14.6c).

Sits between the action bar and the recent-runs table in EtlStatusView.
Displays every field from `exchange_state` for the currently-selected
exchange in a compact grid:

  ┌─ Exchange: NSE ─────────────────────────────────────────────┐
  │ Initial load:    started 2026-05-18 22:52  completed 2026-… │
  │ Last EoD:        2026-05-19 ✓ success    (data 2026-05-19)  │
  │ Last intraday:   2026-05-19 09:42                           │
  │ Last listings:   2026-05-18 18:44                           │
  │ Last tiering:    —                                          │
  └─────────────────────────────────────────────────────────────┘

Empty states:
  - "No state recorded yet" when the exchange has no exchange_state
    row (e.g. just-configured BSE before any ETL). The picker can
    still target it; this panel just shows the empty-state banner.
  - Individual NULL fields render as "—".

Refresh:
  - This widget is a pure display. Refresh is driven by its parent
    (EtlStatusView), which calls `set_state(payload)` or
    `set_not_yet_active()` on every poll.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QGridLayout, QGroupBox, QLabel, QVBoxLayout, QWidget,
)


# Color codes shared with EtlStatusView's runs table.
OUTCOME_COLORS = {
    "success": "#1f7a1f",
    "partial": "#b08000",
    "failed":  "#a02020",
}


class ExchangeStatePanel(QWidget):
    """A compact, read-only panel showing one exchange's ETL state.

    Construction is cheap and does no I/O. The parent owns refresh
    timing and calls `set_state(payload)` with the raw dict from
    `/v1/admin/exchange-state/{exchange}`. For exchanges with no
    row, parent calls `set_not_yet_active(exchange)`.
    """

    def __init__(self, *, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._current_exchange: Optional[str] = None
        self._build_ui()

    # ---------- public ---------- #

    def set_state(self, payload: dict) -> None:
        """Render the panel from an ExchangeStatePayload dict.

        Expects the keys the server emits: `exchange`,
        `initial_load_started_at`, `initial_load_completed_at`,
        `last_eod_run_at`, `last_eod_outcome`, `last_eod_data_date`,
        `last_intraday_run_at`, `last_listings_sync_at`,
        `last_tier_recompute_at`, `updated_at`. NULL fields render
        as the em-dash placeholder.
        """
        exchange = payload.get("exchange") or "?"
        self._current_exchange = exchange
        self._box.setTitle(f"Exchange: {exchange}")
        self._empty_label.setVisible(False)
        self._grid_widget.setVisible(True)

        # Initial load
        started = _fmt_dt(payload.get("initial_load_started_at"))
        completed = _fmt_dt(payload.get("initial_load_completed_at"))
        completed_html = (
            f"<span style='color:#1f7a1f'>{completed}</span>"
            if completed != "—"
            else "<span style='color:#a02020'>not yet</span>"
        )
        self._initial_load_value.setText(
            f"started {started}   •   completed {completed_html}"
        )

        # Last EoD
        eod_at = _fmt_dt(payload.get("last_eod_run_at"))
        outcome = payload.get("last_eod_outcome")
        data_date = _fmt_date(payload.get("last_eod_data_date"))
        outcome_html = _outcome_html(outcome) if outcome else "—"
        if eod_at == "—":
            self._last_eod_value.setText("—")
        else:
            self._last_eod_value.setText(
                f"{eod_at}   {outcome_html}   "
                f"<span style='color:#888'>(data {data_date})</span>"
            )

        # Last intraday / listings / tier-recompute
        self._last_intraday_value.setText(
            _fmt_dt(payload.get("last_intraday_run_at"))
        )
        self._last_listings_value.setText(
            _fmt_dt(payload.get("last_listings_sync_at"))
        )
        self._last_tier_value.setText(
            _fmt_dt(payload.get("last_tier_recompute_at"))
        )

    def set_not_yet_active(self, exchange: str) -> None:
        """Display the empty state for an exchange that has no
        exchange_state row yet (server returned 404)."""
        self._current_exchange = exchange
        self._box.setTitle(f"Exchange: {exchange}")
        self._grid_widget.setVisible(False)
        self._empty_label.setText(
            f"No ETL activity recorded for {exchange} yet. "
            f"Trigger a backfill or EoD pass to populate state."
        )
        self._empty_label.setVisible(True)

    def set_load_failed(self, exchange: str, message: str) -> None:
        """Display a fetch-error state. Distinct from 'no activity'
        — this is a transport/server issue, not an empty result."""
        self._current_exchange = exchange
        self._box.setTitle(f"Exchange: {exchange}")
        self._grid_widget.setVisible(False)
        self._empty_label.setText(
            f"State fetch failed: {message}"
        )
        self._empty_label.setVisible(True)

    @property
    def current_exchange(self) -> Optional[str]:
        return self._current_exchange

    # ---------- UI construction ---------- #

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self._box = QGroupBox("Exchange: —")
        outer.addWidget(self._box)

        box_layout = QVBoxLayout(self._box)
        box_layout.setContentsMargins(10, 10, 10, 10)
        box_layout.setSpacing(6)

        # Empty-state banner (shown when no row exists OR fetch failed).
        self._empty_label = QLabel("Select an exchange to view state.")
        self._empty_label.setStyleSheet("color: #888; font-style: italic;")
        self._empty_label.setWordWrap(True)
        box_layout.addWidget(self._empty_label)

        # The grid (visible only when state is populated).
        self._grid_widget = QWidget()
        grid = QGridLayout(self._grid_widget)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(4)

        def add_row(row: int, label: str) -> QLabel:
            lbl = QLabel(label)
            lbl.setStyleSheet("color: #666; font-weight: 600;")
            lbl.setMinimumWidth(110)
            lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            value = QLabel("—")
            value.setTextFormat(Qt.RichText)
            value.setTextInteractionFlags(Qt.TextSelectableByMouse)
            grid.addWidget(lbl, row, 0)
            grid.addWidget(value, row, 1)
            return value

        self._initial_load_value = add_row(0, "Initial load:")
        self._last_eod_value = add_row(1, "Last EoD:")
        self._last_intraday_value = add_row(2, "Last intraday:")
        self._last_listings_value = add_row(3, "Last listings:")
        self._last_tier_value = add_row(4, "Last tiering:")

        grid.setColumnStretch(1, 1)

        box_layout.addWidget(self._grid_widget)
        self._grid_widget.setVisible(False)


# ---------- helpers ---------- #


def _fmt_dt(s) -> str:
    """ISO-8601 → 'YYYY-MM-DD HH:MM' in the user's local time.

    Returns the em-dash placeholder for NULL/empty inputs. The server
    emits aware UTC; we keep it simple by trimming to date+time without
    timezone display — the operator usually cares about chronology, not
    TZ math, and the row's `updated_at` (also UTC) gives them a reference.
    Bad inputs (non-string, malformed) render as em-dash rather than
    raising, so the panel never breaks on unexpected payloads.
    """
    if not s:
        return "—"
    try:
        # FastAPI emits with timezone info; fromisoformat handles it from 3.11+.
        dt = datetime.fromisoformat(s) if isinstance(s, str) else s
    except (ValueError, TypeError):
        return str(s)
    if not isinstance(dt, datetime):
        return "—"
    return dt.strftime("%Y-%m-%d %H:%M")


def _fmt_date(s) -> str:
    """ISO date string → 'YYYY-MM-DD'. Em-dash for NULL/empty."""
    if not s:
        return "—"
    if isinstance(s, str):
        # Could be a date or a datetime; we want just the date portion.
        return s[:10]
    try:
        return s.isoformat()
    except AttributeError:
        return str(s)


def _outcome_html(outcome: str) -> str:
    """Render the outcome string in its semantic color. Unknown
    outcomes render in default color rather than raising; the server's
    CHECK constraint already enforces the set."""
    color = OUTCOME_COLORS.get(outcome)
    label = outcome.upper() if outcome else "—"
    if outcome == "success":
        label = f"✓ {label}"
    elif outcome == "partial":
        label = f"⚠ {label}"
    elif outcome == "failed":
        label = f"✗ {label}"
    if color:
        return f"<span style='color:{color};font-weight:600'>{label}</span>"
    return label
