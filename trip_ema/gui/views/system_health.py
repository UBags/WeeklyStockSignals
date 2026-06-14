"""System Health view (Slice 4).

Layout:
  ┌────────────────────────────────────────────────────────────┐
  │ Overall: OK              [Refresh]                          │
  ├──────────────┬──────────────┬──────────────┬───────────────┤
  │ Ticker univ. │ ETL freshness│ Compute      │ Signal cache  │
  │ ✓ 2300 ...   │ ⚠ 36h ago    │ ✓ 5 min ago  │ ✓ 2300/2300   │
  │              │              │              │               │
  ├──────────────┼──────────────┼──────────────┼───────────────┤
  │ Breakers     │ Queue        │ Subscribers  │ Events (7d)   │
  │ ✓ 2 closed   │ ✓ legacy     │ ⚠ 0 active   │ ✓ 47 events   │
  └──────────────┴──────────────┴──────────────┴───────────────┘

Each card is a small widget rendering one HealthCardPayload. Background
color encodes status (green=ok, amber=warn, red=crit, gray=unknown).

Polls every 10s. The snapshot endpoint is read-only and side-effect-free,
so polling is cheap.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton,
    QSizePolicy, QVBoxLayout, QWidget,
)

from ..db import GuiDbSession
from ..http_client import ApiError, TripEmaHttpClient
from ..workers import RefreshScheduler, submit

logger = logging.getLogger(__name__)


# Status → background color mapping
STATUS_COLORS = {
    "ok":      ("#1f9c5e", "#0e4d2f"),  # bright + darker for the card body
    "warn":    ("#c88a14", "#5e3f08"),
    "crit":    ("#c84a4a", "#5e2020"),
    "unknown": ("#777777", "#3a3a3a"),
}

STATUS_GLYPHS = {
    "ok": "✓",
    "warn": "⚠",
    "crit": "✗",
    "unknown": "—",
}


class HealthCardWidget(QFrame):
    """One card on the dashboard. Reads from a HealthCardPayload-shaped
    dict and renders a colored panel with name, status glyph, headline,
    and optional detail."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.StyledPanel)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMinimumHeight(120)
        self._build_ui()
        # Set initial empty state
        self.set_card({
            "name": "—", "status": "unknown",
            "headline": "Loading…", "detail": None,
        })

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(4)

        # Header row: name (left) + status glyph (right)
        header = QHBoxLayout()
        self._name_label = QLabel()
        name_font = QFont()
        name_font.setBold(True)
        name_font.setPointSize(11)
        self._name_label.setFont(name_font)
        self._name_label.setStyleSheet("color: white;")
        header.addWidget(self._name_label)
        header.addStretch(1)
        self._glyph_label = QLabel()
        glyph_font = QFont()
        glyph_font.setBold(True)
        glyph_font.setPointSize(18)
        self._glyph_label.setFont(glyph_font)
        self._glyph_label.setStyleSheet("color: white;")
        header.addWidget(self._glyph_label)
        layout.addLayout(header)

        # Headline (one-liner)
        self._headline_label = QLabel()
        headline_font = QFont()
        headline_font.setPointSize(13)
        self._headline_label.setFont(headline_font)
        self._headline_label.setStyleSheet("color: white;")
        self._headline_label.setWordWrap(True)
        layout.addWidget(self._headline_label)

        # Detail (optional)
        self._detail_label = QLabel()
        self._detail_label.setStyleSheet(
            "color: rgba(255,255,255,0.7); font-size: 11px;"
        )
        self._detail_label.setWordWrap(True)
        layout.addWidget(self._detail_label)

        layout.addStretch(1)

    def set_card(self, card: dict) -> None:
        """Populate from a HealthCardPayload-shaped dict."""
        status = card.get("status", "unknown")
        accent, body = STATUS_COLORS.get(status, STATUS_COLORS["unknown"])
        self.setStyleSheet(f"""
            QFrame {{
                background-color: {body};
                border: 2px solid {accent};
                border-radius: 6px;
            }}
        """)
        self._name_label.setText(card.get("name", "—"))
        self._glyph_label.setText(STATUS_GLYPHS.get(status, "?"))
        self._headline_label.setText(card.get("headline", ""))
        detail = card.get("detail") or ""
        self._detail_label.setText(detail)
        self._detail_label.setVisible(bool(detail))


class SystemHealthView(QWidget):

    REFRESH_CADENCE_SECONDS = 10.0

    # Grid columns. Cards reflow if there are more or fewer than
    # 8 — we just keep adding rows.
    GRID_COLUMNS = 4

    def __init__(
        self,
        db: GuiDbSession,
        http: TripEmaHttpClient,
        *,
        parent: Optional[QWidget] = None,
        start_refresh_on_init: bool = True,
    ):
        super().__init__(parent)
        self._db = db
        self._http = http
        self._cards: list[HealthCardWidget] = []

        self._build_ui()
        self._scheduler = RefreshScheduler(
            self.REFRESH_CADENCE_SECONDS, run_on_start=True,
        )
        self._scheduler.tick.connect(self._on_refresh_tick)
        if start_refresh_on_init:
            self._scheduler.start()

    # ---------- UI ---------- #

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)
        outer.setSpacing(10)

        # Top bar: overall status banner + refresh button
        top_row = QHBoxLayout()
        self._overall_label = QLabel("Overall: …")
        overall_font = QFont()
        overall_font.setPointSize(14)
        overall_font.setBold(True)
        self._overall_label.setFont(overall_font)
        top_row.addWidget(self._overall_label)
        top_row.addStretch(1)
        self._last_refreshed_label = QLabel("Never refreshed")
        self._last_refreshed_label.setStyleSheet(
            "color: #888; font-style: italic;"
        )
        top_row.addWidget(self._last_refreshed_label)
        self._refresh_button = QPushButton("Refresh")
        self._refresh_button.clicked.connect(self._on_refresh_button_clicked)
        top_row.addWidget(self._refresh_button)
        outer.addLayout(top_row)

        # Grid of cards
        self._grid_widget = QWidget()
        self._grid = QGridLayout(self._grid_widget)
        self._grid.setSpacing(10)
        outer.addWidget(self._grid_widget, 1)

    def _on_refresh_button_clicked(self) -> None:
        self._scheduler.trigger_now()

    # ---------- refresh ---------- #

    def _on_refresh_tick(self) -> None:
        http = self._http
        submit(
            lambda: http.get_health_snapshot(),
            on_finished=self._on_snapshot_received,
            on_failed=self._on_snapshot_failed,
        )

    def _on_snapshot_received(self, payload: dict) -> None:
        overall = payload.get("overall_status", "unknown")
        glyph = STATUS_GLYPHS.get(overall, "?")
        color = STATUS_COLORS.get(overall, STATUS_COLORS["unknown"])[0]
        self._overall_label.setText(f"Overall: {glyph} {overall.upper()}")
        self._overall_label.setStyleSheet(f"color: {color};")

        cards = payload.get("cards", [])
        self._render_cards(cards)
        self._last_refreshed_label.setText(
            f"Refreshed at {datetime.now().strftime('%H:%M:%S')}"
        )

    def _on_snapshot_failed(self, message: str) -> None:
        self._overall_label.setText("Overall: — UNREACHABLE")
        self._overall_label.setStyleSheet("color: #c84a4a;")
        self._last_refreshed_label.setText(f"Refresh failed: {message}")
        logger.warning("Health snapshot fetch failed: %s", message)
        # Show a single 'unknown' card with the error
        self._render_cards([{
            "name": "Health endpoint",
            "status": "crit",
            "headline": "Snapshot fetch failed",
            "detail": message,
        }])

    def _render_cards(self, cards: list[dict]) -> None:
        """Update the grid. We reuse existing card widgets when possible
        to avoid flicker on every poll."""
        # Add card widgets to match `cards` length
        while len(self._cards) < len(cards):
            card_w = HealthCardWidget()
            idx = len(self._cards)
            row = idx // self.GRID_COLUMNS
            col = idx % self.GRID_COLUMNS
            self._grid.addWidget(card_w, row, col)
            self._cards.append(card_w)
        # Hide extras if we have too many existing widgets
        for i, card_w in enumerate(self._cards):
            if i < len(cards):
                card_w.set_card(cards[i])
                card_w.setVisible(True)
            else:
                card_w.setVisible(False)

    # ---------- lifecycle ---------- #

    def on_close(self) -> None:
        try:
            if self._scheduler.is_running:
                self._scheduler.stop()
        except RuntimeError:
            pass
