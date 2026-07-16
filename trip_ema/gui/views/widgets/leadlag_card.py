"""Signal-detail lead/lag card (design §6.4).

Reuses the pure renderer (leadlag_render.render_leadlag_lines) to turn a
LeadLagContext into display lines, shown in a QGroupBox on the signals view.
No DB access — SignalsView fetches the context (via load_leadlag_context) off
the UI thread and calls set_context(). An absent, thin, or decoupled context
shows a muted placeholder rather than a fabricated relationship.
"""
from __future__ import annotations

from types import SimpleNamespace

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QGroupBox, QLabel, QVBoxLayout

from ....notifications.leadlag_render import (
    LeadLagContext,
    render_leadlag_lines,
)

_PLACEHOLDER = "(no lead/lag data)"


def _esc(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


class LeadLagCard(QGroupBox):
    """Read-only card showing the stock's coupling, its industry's regime, and
    the corroborating industries for the currently loaded signal."""

    def __init__(self, parent=None) -> None:
        super().__init__("Lead / Lag", parent)
        self.setMaximumHeight(140)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        self._label = QLabel(_PLACEHOLDER)
        self._label.setAlignment(Qt.AlignTop)
        self._label.setTextFormat(Qt.RichText)
        self._label.setWordWrap(True)
        layout.addWidget(self._label)
        self._set_placeholder()

    def clear(self) -> None:
        self._set_placeholder()

    def set_context(
        self, ctx: LeadLagContext | None, industry: str | None,
    ) -> None:
        if ctx is None or industry is None:
            self._set_placeholder()
            return
        lines = render_leadlag_lines(SimpleNamespace(industry=industry), ctx)
        if not lines:
            self._set_placeholder()
            return
        self._label.setStyleSheet("font-size: 11px; color: #222;")
        self._label.setText("<br>".join(f"\u2022 {_esc(x)}" for x in lines))

    def _set_placeholder(self) -> None:
        self._label.setStyleSheet("font-size: 11px; color: #888;")
        self._label.setText(_PLACEHOLDER)
