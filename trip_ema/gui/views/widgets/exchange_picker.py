"""Reusable exchange-picker widget (Phase 15c).

A QComboBox + status hint that:
  - Starts in a "loading..." sentinel state, disabled.
  - Asynchronously fetches the configured exchange list via the
    injected HTTP client and populates itself.
  - Restores the operator's last selection from QSettings if it's
    still in the configured list; otherwise selects the first
    configured exchange.
  - Persists selection changes to QSettings.
  - Emits `selection_changed(str)` whenever the resolved exchange
    changes. Consumers wire their own handlers (refresh data,
    re-enable buttons, etc.) since each tab has different
    follow-up actions.

Phase 14.6c put this same logic inline in `EtlStatusView`. Phase 15c
extracts it so SignalGenerationPanel (and any future
exchange-aware view) can reuse it without copy-pasting ~80 lines
of picker plumbing.

Design notes:
  - The widget OWNS the QSettings key. Consumers don't manage the
    persisted value — they're a key-value caller of this widget.
  - The widget OWNS the loading sentinel. Consumers see a single
    public `current_exchange` property (None until resolved).
  - The widget does NOT decide what "enabled" means for the
    consuming view's action buttons — that's a per-tab policy
    (e.g. EtlStatusView additionally requires `http.token_configured`).
    Consumers gate their own buttons against
    `picker.current_exchange is not None` AND their other
    conditions.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLabel, QWidget

from ...http_client import TripEmaHttpClient
from ...workers import submit
from ..settings import (
    load_last_selected_exchange,
    save_last_selected_exchange,
)

logger = logging.getLogger(__name__)


# The single source of truth for the loading-sentinel string. Tests
# can import this rather than hard-coding "(loading…)".
EXCHANGE_LOADING_SENTINEL = "(loading…)"


class ExchangePicker(QWidget):
    """Combobox + hint that resolves to one configured exchange.

    Emits:
        selection_changed(exchange: str)
            Fired EVERY time a real (non-sentinel) exchange becomes
            the active selection. Includes the first populate
            (where the picker resolves from the persisted-or-default
            value) and every subsequent user-driven change. Does NOT
            fire for the loading-sentinel state.

    Public attributes consumers may read:
        combo: the underlying QComboBox (exposed so existing tests
            and the 14.6c EtlStatusView code can reach it during
            the refactor).
        hint_label: the small label that shows "(picker offline:
            …)" or other status messages.

    Public methods:
        current_exchange() -> Optional[str]
            The currently selected exchange, or None if the picker
            hasn't resolved yet (still loading, or load failed).
        is_loaded() -> bool
            True once `_populate` has run with a non-empty exchange
            list. Distinct from `current_exchange is not None`
            because an empty configured list also returns None for
            current_exchange but never reaches loaded=True.
    """

    selection_changed = Signal(str)

    def __init__(
        self,
        http: TripEmaHttpClient,
        *,
        label_text: str = "Exchange:",
        parent: QWidget | None = None,
        auto_fetch: bool = True,
    ):
        """
        Args:
            http: the shared HTTP client. The picker uses it to
                fetch `/v1/admin/exchanges`.
            label_text: text for the leading label. Set to ""
                to suppress the label (e.g. when the consumer
                already provides one).
            auto_fetch: when True (default), the picker fires off
                the list_exchanges worker at construction time.
                Set False in tests that want to drive
                populate_with_exchanges() directly without
                touching the network.
        """
        super().__init__(parent)
        self._http = http
        self._selected_exchange: str | None = None
        self._loaded: bool = False

        self.combo = QComboBox()
        self.combo.setMinimumWidth(120)
        self.combo.addItem(EXCHANGE_LOADING_SENTINEL)
        self.combo.setEnabled(False)
        self.combo.currentTextChanged.connect(self._on_combo_changed)

        self.hint_label = QLabel("")
        self.hint_label.setStyleSheet("color: gray; font-style: italic;")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        if label_text:
            layout.addWidget(QLabel(label_text))
        layout.addWidget(self.combo)
        layout.addWidget(self.hint_label)

        if auto_fetch:
            self._fetch_exchanges_list()

    # ---------- public API ---------- #

    def current_exchange(self) -> str | None:
        return self._selected_exchange

    def is_loaded(self) -> bool:
        return self._loaded

    def populate_with_exchanges(self, exchanges: list[str]) -> None:
        """Test seam: skip the network and populate directly. Also
        used internally by _on_exchanges_received."""
        if not exchanges:
            self.hint_label.setText(
                "(no exchanges configured in config.yaml)"
            )
            return

        self._loaded = True

        # Decide initial selection: persisted choice → first available.
        last = load_last_selected_exchange()
        target = last if last in exchanges else exchanges[0]

        # Repopulate with signals blocked, so the in-flight rebuild
        # doesn't emit currentTextChanged events for the transient
        # intermediate states.
        self.combo.blockSignals(True)
        try:
            self.combo.clear()
            for ex in exchanges:
                self.combo.addItem(ex)
            idx = self.combo.findText(target)
            if idx >= 0:
                self.combo.setCurrentIndex(idx)
        finally:
            self.combo.blockSignals(False)

        self.combo.setEnabled(True)
        # Apply the initial selection now that the combo is wired up.
        # This is the first time selection_changed fires for the
        # consumer's connected slot.
        self._apply_selection(target)

    # ---------- internal: worker plumbing ---------- #

    def _fetch_exchanges_list(self) -> None:
        """One-shot async fetch of /v1/admin/exchanges."""
        http = self._http
        submit(
            lambda: http.list_exchanges(),
            on_finished=self._on_exchanges_received,
            on_failed=self._on_exchanges_failed,
        )

    def _on_exchanges_received(self, payload: dict) -> None:
        exchanges = list(payload.get("exchanges", []))
        self.populate_with_exchanges(exchanges)

    def _on_exchanges_failed(self, message: str) -> None:
        """Picker fetch failed. Leave the picker disabled with the
        loading sentinel; surface the error in the hint."""
        logger.warning("Exchange list fetch failed: %s", message)
        self.hint_label.setText(f"(picker offline: {message[:60]})")
        # Combo stays in sentinel-only state. Consumers see
        # current_exchange() returning None and gate their buttons
        # accordingly.

    # ---------- internal: selection state ---------- #

    def _on_combo_changed(self, value: str) -> None:
        """Fires when the user (or programmatic setCurrentIndex)
        changes the combo's current value. Ignore the sentinel."""
        if value == EXCHANGE_LOADING_SENTINEL or not value:
            return
        self._apply_selection(value)

    def _apply_selection(self, exchange: str) -> None:
        """Resolve `exchange` as the active selection: update internal
        state, persist to QSettings, emit selection_changed.

        Idempotent w.r.t. the combo's current index — the caller is
        responsible for having set that. This method just propagates
        the value to internal state + the signal.
        """
        self._selected_exchange = exchange
        try:
            save_last_selected_exchange(exchange)
        except Exception:
            # QSettings write failures shouldn't break the UI; log it.
            logger.exception("Could not persist last_selected_exchange")
        self.selection_changed.emit(exchange)
