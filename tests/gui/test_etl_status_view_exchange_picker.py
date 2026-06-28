"""Phase 14.6c GUI tests: exchange picker and state panel.

Two surfaces under test:

  - `ExchangeStatePanel`: pure display widget. Render the empty state,
    populated state, and load-failed state from synthetic payloads.

  - `EtlStatusView` picker integration: the picker populates from the
    list_exchanges fetch, persists the operator's selection via
    QSettings, gates action buttons until populated, and dispatches
    action calls with the right exchange.

All tests use the GUI conftest's offscreen Qt setup. The HTTP client
is a MagicMock; no real network.

# Three gotchas worth understanding before reading the tests

## (1) `QWidget.isVisible()` is not "setVisible state"

`isVisible()` answers "is on screen right now", which depends on
ancestors having a window AND the paint pipeline having flushed.
On the offscreen platform with no real window, and immediately after
mutating widget state, it returns confusingly stale values.

`isHidden()` is the right call: it returns True iff the widget was
explicitly hidden via `hide()` or `setVisible(False)`, regardless of
ancestors or paint state. Its negation reads cleanly as "this widget
believes itself visible." Tests below use `not isHidden()` everywhere
they want to assert intent rather than actual screen state.

## (2) Worker threads outlive test functions if not drained

`EtlStatusView.__init__` kicks off `_fetch_exchanges_list` which
submits a worker to QThreadPool. If the test exits while that worker
is still in flight, the worker's `finished` signal lands on a
freshly-destroyed view — pure-virtual-method-called / SIGSEGV.

The `_teardown_view` helper closes the view properly and drains the
global thread pool. Every test that constructs a real EtlStatusView
calls it before returning.

## (3) Modal dialogs wedge pytest-qt's event loop

`EtlStatusView._fire_action` pops a `QMessageBox.information(...)`
in its success callback. `QMessageBox.information` opens a MODAL
dialog that spins its own event loop until dismissed. With no user
to click OK, the dialog stays open; pytest-qt's `waitUntil` and the
modal's event loop conflict; SIGSEGV inside the wait_signal machinery.

Action-dispatch tests monkeypatch QMessageBox.information/warning
to no-op. The test cares about the HTTP call, not the dialog.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from PySide6.QtCore import QThreadPool
from PySide6.QtWidgets import QMessageBox

from trip_ema.gui.views.etl_status import EtlStatusView
from trip_ema.gui.views.exchange_state_panel import ExchangeStatePanel


# ---------- Shared helpers ---------- #


def _add(qtbot, widget):
    """qtbot.addWidget shorthand. We don't bother calling show() —
    every test asserts setVisible state via isHidden() rather than
    isVisible(), so we don't need a real on-screen window."""
    qtbot.addWidget(widget)
    return widget


def _teardown_view(view) -> None:
    """Stop the view's scheduler and drain any in-flight workers.

    Required because EtlStatusView.__init__ submits the
    list_exchanges worker unconditionally. If we let the test exit
    while that worker is still pumping, the worker's finished/failed
    signal lands on a deleted view → segfault.
    """
    try:
        view.on_close()
    except Exception:
        pass
    # Give the global thread pool a generous window to drain. 1500ms
    # is overkill for a MagicMock fetch but cheap insurance.
    QThreadPool.globalInstance().waitForDone(1500)


@pytest.fixture
def silent_modals(monkeypatch):
    """Replace QMessageBox.information / .warning / .question with
    no-op stubs so they don't wedge pytest-qt's event loop.

    .question normally returns a button enum; stub returns Yes so
    flows that branch on the user's answer take the affirmative path.
    """
    monkeypatch.setattr(
        QMessageBox, "information",
        lambda *a, **kw: QMessageBox.Ok,
    )
    monkeypatch.setattr(
        QMessageBox, "warning",
        lambda *a, **kw: QMessageBox.Ok,
    )
    monkeypatch.setattr(
        QMessageBox, "question",
        lambda *a, **kw: QMessageBox.Yes,
    )


@pytest.fixture(autouse=True)
def _clean_qsettings():
    """Wipe the last_selected_exchange QSettings key between tests so
    persistence tests don't bleed state. Uses the same group/key the
    real code uses."""
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


# ---------- ExchangeStatePanel: render states ---------- #


class TestExchangeStatePanelRenderStates:

    def test_initial_state_is_empty_banner(self, qtbot):
        panel = ExchangeStatePanel()
        _add(qtbot, panel)
        # Before any set_*: grid hidden, banner visible.
        assert panel._grid_widget.isHidden() is True
        assert panel._empty_label.isHidden() is False
        assert panel.current_exchange is None

    def test_set_state_populates_fields(self, qtbot):
        panel = ExchangeStatePanel()
        _add(qtbot, panel)
        panel.set_state({
            "exchange": "NSE",
            "initial_load_started_at":   "2026-05-18T17:22:37+00:00",
            "initial_load_completed_at": "2026-05-18T20:39:23+00:00",
            "last_eod_run_at":           "2026-05-19T19:31:21+00:00",
            "last_eod_outcome":          "success",
            "last_eod_data_date":        "2026-05-19",
            "last_intraday_run_at":      None,
            "last_listings_sync_at":     "2026-05-18T13:14:25+00:00",
            "last_tier_recompute_at":    None,
            "updated_at":                "2026-05-21T10:00:00+00:00",
        })
        assert panel._grid_widget.isHidden() is False
        assert panel._empty_label.isHidden() is True
        assert panel.current_exchange == "NSE"
        # Box title carries the exchange code
        assert "NSE" in panel._box.title()
        # Spot-check rendered values
        assert "2026-05-18" in panel._initial_load_value.text()
        # success outcome should render colorized HTML
        assert "1f7a1f" in panel._last_eod_value.text().lower()
        # NULL fields render as em-dash placeholder
        assert panel._last_intraday_value.text() == "—"
        assert panel._last_tier_value.text() == "—"

    def test_set_state_with_failed_outcome_uses_red(self, qtbot):
        panel = ExchangeStatePanel()
        _add(qtbot, panel)
        panel.set_state({
            "exchange": "BSE",
            "initial_load_started_at": None,
            "initial_load_completed_at": None,
            "last_eod_run_at": "2026-05-20T01:00:00+00:00",
            "last_eod_outcome": "failed",
            "last_eod_data_date": None,
            "last_intraday_run_at": None,
            "last_listings_sync_at": None,
            "last_tier_recompute_at": None,
            "updated_at": "2026-05-20T01:00:01+00:00",
        })
        assert "a02020" in panel._last_eod_value.text().lower()

    def test_set_not_yet_active(self, qtbot):
        panel = ExchangeStatePanel()
        _add(qtbot, panel)
        panel.set_not_yet_active("BSE")
        assert panel.current_exchange == "BSE"
        assert panel._grid_widget.isHidden() is True
        assert panel._empty_label.isHidden() is False
        assert "BSE" in panel._empty_label.text()
        assert "no etl activity" in panel._empty_label.text().lower()

    def test_set_load_failed_shows_distinct_message(self, qtbot):
        panel = ExchangeStatePanel()
        _add(qtbot, panel)
        panel.set_load_failed("NSE", "network error: connection refused")
        assert panel.current_exchange == "NSE"
        assert panel._grid_widget.isHidden() is True
        assert "State fetch failed" in panel._empty_label.text()
        assert "connection refused" in panel._empty_label.text()

    def test_set_state_after_not_yet_active_shows_grid(self, qtbot):
        """An exchange that had no activity, then gets a backfill,
        should transition from the empty-state banner to the populated
        grid without restarting the widget."""
        panel = ExchangeStatePanel()
        _add(qtbot, panel)
        panel.set_not_yet_active("BSE")
        assert panel._grid_widget.isHidden() is True
        # Now activity arrives:
        panel.set_state({
            "exchange": "BSE",
            "initial_load_started_at": "2026-05-21T10:00:00+00:00",
            "initial_load_completed_at": None,
            "last_eod_run_at": None,
            "last_eod_outcome": None,
            "last_eod_data_date": None,
            "last_intraday_run_at": None,
            "last_listings_sync_at": None,
            "last_tier_recompute_at": None,
            "updated_at": "2026-05-21T10:00:00+00:00",
        })
        assert panel._grid_widget.isHidden() is False
        assert panel._empty_label.isHidden() is True


# ---------- EtlStatusView picker behaviour ---------- #


def _build_view(*, token_configured=True, exchanges=("NSE", "BSE")):
    """Construct an EtlStatusView with the HTTP client fully mocked.
    Returns (view, http). Tests must call _teardown_view(view)
    before returning to drain workers."""
    db = MagicMock()
    http = MagicMock()
    http.token_configured = token_configured
    http.list_exchanges.return_value = {
        "exchanges": list(exchanges),
        "total": len(exchanges),
        "snapshot_taken_at": "2026-05-21T10:00:00Z",
    }
    http.get_etl_status.return_value = {
        "recent_runs": [], "parked_count": 0,
        "tier_counts": {"tier_1": 0, "tier_2": 0, "tier_3": 0},
    }
    http.get_etl_parked.return_value = {"parked": [], "total": 0}
    http.get_exchange_state.side_effect = lambda ex: {
        "exchange": ex,
        "initial_load_started_at": None,
        "initial_load_completed_at": None,
        "last_eod_run_at": None,
        "last_eod_outcome": None,
        "last_eod_data_date": None,
        "last_eod_run_id": None,
        "last_intraday_run_at": None,
        "last_listings_sync_at": None,
        "last_listings_run_id": None,
        "last_tier_recompute_at": None,
        "updated_at": "2026-05-21T10:00:00+00:00",
    }
    # Make action-button calls return a benign payload so on_done gets
    # something with .get("notes") — even though we monkey-patch the
    # modal away, the slot still iterates over the payload.
    http.run_eod.return_value = {"notes": "submitted"}
    http.refresh_listings.return_value = {"notes": "submitted"}
    http.run_intraday.return_value = {"notes": "submitted"}
    http.recompute_tiers.return_value = {"notes": "submitted"}
    http.unpark_all.return_value = {"notes": "submitted"}
    return EtlStatusView(db=db, http=http, start_refresh_on_init=False), http


def _seed_picker(view, exchanges):
    """Bypass the async populate worker and drive the picker directly."""
    view._populate_exchange_picker(list(exchanges))


def _drain_initial_workers(qtbot):
    """Let the construction-time _fetch_exchanges_list worker complete
    so it doesn't deliver its signal to a destroyed view at teardown."""
    QThreadPool.globalInstance().waitForDone(500)
    qtbot.wait(50)


