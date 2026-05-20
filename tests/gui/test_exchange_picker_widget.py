"""Phase 15c GUI tests: extracted ExchangePicker widget.

These exercise the picker in isolation — no EtlStatusView, no
SignalsView, just the widget by itself. Verifies the populate /
restore-pref / selection-signal contract that consuming views
rely on.

The complementary 14.6c tests in
test_etl_status_view_exchange_picker.py exercise the picker
THROUGH EtlStatusView (which now delegates to this widget); those
remain unchanged and provide the integration coverage. This file
is the unit-level coverage so the widget can evolve without
necessarily breaking the integration suite.

Conventions inherited from 14.6c:
  - `isHidden()` rather than `isVisible()` (see the gotchas docstring
    in test_etl_status_view_exchange_picker.py).
  - Workers from `submit(...)` get drained before each test exits.
  - QSettings cleaned between tests via the `_clean_qsettings`
    autouse fixture.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from PySide6.QtCore import QThreadPool

from trip_ema.gui.views.widgets.exchange_picker import (
    EXCHANGE_LOADING_SENTINEL,
    ExchangePicker,
)


# ---------- Shared helpers ---------- #


def _add(qtbot, widget):
    qtbot.addWidget(widget)
    return widget


def _teardown_picker(picker) -> None:
    """Drain any in-flight worker, then close the widget. Mirrors
    `_teardown_view` from the 14.6c test file."""
    try:
        picker.close()
    except Exception:
        pass
    # Wait for any list_exchanges worker still in flight to land.
    # Without this, the next test inherits a worker pump that
    # delivers to a destroyed widget at the wrong moment.
    QThreadPool.globalInstance().waitForDone(1500)


def _make_http(*, exchanges=("NSE", "NYSE"), should_fail=False):
    """Mock HTTP client with list_exchanges configured."""
    http = MagicMock()
    if should_fail:
        # The worker's `failed` signal carries the exception's
        # string repr. We model failure by having the call raise.
        http.list_exchanges.side_effect = RuntimeError("simulated fetch failure")
    else:
        http.list_exchanges.return_value = {
            "exchanges": list(exchanges),
            "total": len(exchanges),
            "snapshot_taken_at": "2026-05-21T10:00:00Z",
        }
    return http


@pytest.fixture(autouse=True)
def _clean_qsettings():
    """Same fixture as the 14.6c picker tests: wipe the persisted
    selection between tests."""
    from PySide6.QtCore import QSettings
    from trip_ema.gui.views.settings import (
        SETTINGS_GROUP, KEY_LAST_SELECTED_EXCHANGE,
    )

    def _wipe():
        s = QSettings()
        s.beginGroup(SETTINGS_GROUP)
        s.remove(KEY_LAST_SELECTED_EXCHANGE)
        s.endGroup()
        s.sync()

    _wipe()
    yield
    _wipe()


# ============================================================
# Construction state
# ============================================================


class TestPickerInitialState:

    def test_initial_state_is_loading_sentinel_disabled(self, qtbot):
        """Pre-populate, the combo shows the loading sentinel and is
        disabled. current_exchange() is None, is_loaded() is False."""
        # auto_fetch=False so we can inspect the construction state
        # before any worker delivers a populate.
        http = _make_http()
        picker = ExchangePicker(http, auto_fetch=False)
        _add(qtbot, picker)
        try:
            assert picker.combo.currentText() == EXCHANGE_LOADING_SENTINEL
            assert picker.combo.isEnabled() is False
            assert picker.current_exchange() is None
            assert picker.is_loaded() is False
        finally:
            _teardown_picker(picker)


# ============================================================
# Populate via direct API (no worker)
# ============================================================


class TestPickerDirectPopulate:

    def test_populate_picks_first_when_no_saved_pref(self, qtbot):
        """No persisted choice → first configured exchange wins."""
        http = _make_http()
        picker = ExchangePicker(http, auto_fetch=False)
        _add(qtbot, picker)
        try:
            picker.populate_with_exchanges(["NYSE", "NSE"])
            assert picker.combo.currentText() == "NYSE"
            assert picker.current_exchange() == "NYSE"
            assert picker.combo.isEnabled() is True
            assert picker.is_loaded() is True
        finally:
            _teardown_picker(picker)

    def test_populate_restores_saved_pref(self, qtbot):
        """A persisted choice in the configured list takes precedence."""
        from trip_ema.gui.views.settings import save_last_selected_exchange
        save_last_selected_exchange("NSE")
        http = _make_http()
        picker = ExchangePicker(http, auto_fetch=False)
        _add(qtbot, picker)
        try:
            picker.populate_with_exchanges(["NYSE", "NSE"])
            assert picker.current_exchange() == "NSE"
            assert picker.combo.currentText() == "NSE"
        finally:
            _teardown_picker(picker)

    def test_populate_falls_back_when_saved_pref_no_longer_configured(self, qtbot):
        """Operator had NYSE saved, then removed it from config.
        Picker should fall back to the first configured one rather
        than show a stale value."""
        from trip_ema.gui.views.settings import save_last_selected_exchange
        save_last_selected_exchange("NYSE")
        http = _make_http()
        picker = ExchangePicker(http, auto_fetch=False)
        _add(qtbot, picker)
        try:
            # NYSE no longer in the configured list
            picker.populate_with_exchanges(["NSE"])
            assert picker.current_exchange() == "NSE"
        finally:
            _teardown_picker(picker)

    def test_empty_exchanges_leaves_picker_disabled(self, qtbot):
        """No configured exchanges → hint surfaced, picker stays
        disabled, current_exchange remains None."""
        http = _make_http()
        picker = ExchangePicker(http, auto_fetch=False)
        _add(qtbot, picker)
        try:
            picker.populate_with_exchanges([])
            assert picker.current_exchange() is None
            assert picker.is_loaded() is False
            assert picker.combo.isEnabled() is False
            assert "no exchanges" in picker.hint_label.text().lower()
        finally:
            _teardown_picker(picker)


# ============================================================
# selection_changed signal
# ============================================================


class TestPickerSelectionSignal:

    def test_signal_fires_on_initial_populate(self, qtbot):
        """The first apply_selection (from populate_with_exchanges)
        fires selection_changed exactly once. Consumers rely on this
        to do their initial setup (refresh buttons, fetch data for
        the resolved exchange, etc.)."""
        http = _make_http()
        picker = ExchangePicker(http, auto_fetch=False)
        _add(qtbot, picker)
        received: list[str] = []
        picker.selection_changed.connect(received.append)
        try:
            picker.populate_with_exchanges(["NSE", "NYSE"])
            assert received == ["NSE"]
        finally:
            _teardown_picker(picker)

    def test_signal_does_not_fire_for_sentinel(self, qtbot):
        """The combo briefly holds the loading sentinel between
        construction and populate. The widget must NOT emit
        selection_changed for that — consumers would see a synthetic
        '(loading…)' value and reject it as invalid."""
        http = _make_http()
        picker = ExchangePicker(http, auto_fetch=False)
        _add(qtbot, picker)
        received: list[str] = []
        picker.selection_changed.connect(received.append)
        try:
            # No populate. Receive nothing.
            assert received == []
        finally:
            _teardown_picker(picker)

    def test_signal_fires_on_user_change(self, qtbot):
        """Programmatic setCurrentIndex (mimicking the user clicking
        another entry) fires selection_changed again with the new
        value."""
        http = _make_http()
        picker = ExchangePicker(http, auto_fetch=False)
        _add(qtbot, picker)
        try:
            picker.populate_with_exchanges(["NSE", "NYSE"])
            received: list[str] = []
            picker.selection_changed.connect(received.append)
            # Switch to NYSE (post-populate so we don't catch the
            # initial signal)
            idx = picker.combo.findText("NYSE")
            picker.combo.setCurrentIndex(idx)
            assert received == ["NYSE"]
            assert picker.current_exchange() == "NYSE"
        finally:
            _teardown_picker(picker)


# ============================================================
# QSettings persistence
# ============================================================


class TestPickerPersistence:

    def test_selection_change_persists_to_qsettings(self, qtbot):
        from trip_ema.gui.views.settings import load_last_selected_exchange
        http = _make_http()
        picker = ExchangePicker(http, auto_fetch=False)
        _add(qtbot, picker)
        try:
            picker.populate_with_exchanges(["NSE", "NYSE"])
            # populate's initial apply_selection persisted "NSE"
            assert load_last_selected_exchange() == "NSE"
            # Now switch — should persist "NYSE"
            idx = picker.combo.findText("NYSE")
            picker.combo.setCurrentIndex(idx)
            assert load_last_selected_exchange() == "NYSE"
        finally:
            _teardown_picker(picker)


# ============================================================
# Failure path
# ============================================================


class TestPickerFailurePath:

    def test_fetch_failure_leaves_picker_disabled_with_hint(self, qtbot):
        """If list_exchanges raises, the picker stays in its
        sentinel-only state and surfaces the error in the hint
        label. current_exchange remains None so consumers gate their
        buttons appropriately."""
        http = _make_http(should_fail=True)
        # auto_fetch=True so the worker actually runs
        picker = ExchangePicker(http, auto_fetch=True)
        _add(qtbot, picker)
        try:
            # Wait for the worker's `failed` signal to land
            qtbot.waitUntil(
                lambda: "offline" in picker.hint_label.text()
                or "failed" in picker.hint_label.text().lower(),
                timeout=2000,
            )
            assert picker.current_exchange() is None
            assert picker.combo.isEnabled() is False
        finally:
            _teardown_picker(picker)
