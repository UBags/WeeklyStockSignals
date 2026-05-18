"""Qt-level tests for the GUI. Runs with pytest-qt on the offscreen
platform — no display required.

Coverage:
  - TickerTableModel: row count, headers, display strings, parked
    foreground color.
  - BackgroundWorker: synchronous wait pattern with QSignalSpy.
  - RefreshScheduler: start fires immediately, manual trigger emits,
    stop halts.
  - MainWindow: constructs, sidebar has expected entries, page
    switching works, status bar reflects auth state.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from unittest.mock import MagicMock

import pytest
from PySide6.QtCore import Qt, QThreadPool
from PySide6.QtTest import QSignalSpy

from trip_ema.gui.queries import TickerRow
from trip_ema.gui.views.tickers import (
    COL_FAILURES, COL_FULL_NAME, COL_PARKED, COL_SYMBOL, COL_TIER,
    TickerTableModel,
)
from trip_ema.gui.workers import BackgroundWorker, RefreshScheduler


def _ticker(
    tid=1, symbol="RELIANCE", tier=1, parked=False,
    delisted_at=None, full_name="Reliance Industries", failures=0,
) -> TickerRow:
    return TickerRow(
        ticker_id=tid, exchange="NSE", symbol=symbol,
        instrument_type="EQ", full_name=full_name,
        isin="INE002A01018", tier=tier, is_active=True,
        delisted_at=delisted_at,
        last_successful_bar_date=date(2026, 5, 16),
        consecutive_failures=failures, parked=parked,
    )


# ============================================================
# TickerTableModel
# ============================================================


class TestTickerTableModel:

    def test_empty_model_has_zero_rows(self, qtbot):
        model = TickerTableModel()
        assert model.rowCount() == 0

    def test_set_rows_updates_count(self, qtbot):
        model = TickerTableModel()
        model.set_rows([_ticker(1), _ticker(2, "TCS")])
        assert model.rowCount() == 2

    def test_column_count_matches_headers(self, qtbot):
        model = TickerTableModel()
        # We have 8 columns based on HEADERS in tickers.py
        assert model.columnCount() == 8

    def test_display_symbol_column(self, qtbot):
        model = TickerTableModel()
        model.set_rows([_ticker(1, "RELIANCE")])
        idx = model.index(0, COL_SYMBOL)
        assert model.data(idx, Qt.DisplayRole) == "RELIANCE"

    def test_display_tier_column(self, qtbot):
        model = TickerTableModel()
        model.set_rows([_ticker(1, tier=2)])
        idx = model.index(0, COL_TIER)
        assert model.data(idx, Qt.DisplayRole) == "2"

    def test_failures_column_blank_when_zero(self, qtbot):
        """Blank rather than '0' to reduce visual noise on the common case."""
        model = TickerTableModel()
        model.set_rows([_ticker(failures=0)])
        idx = model.index(0, COL_FAILURES)
        assert model.data(idx, Qt.DisplayRole) == ""

    def test_failures_column_shows_count_when_nonzero(self, qtbot):
        model = TickerTableModel()
        model.set_rows([_ticker(failures=3)])
        idx = model.index(0, COL_FAILURES)
        assert model.data(idx, Qt.DisplayRole) == "3"

    def test_parked_ticker_renders_red(self, qtbot):
        model = TickerTableModel()
        model.set_rows([_ticker(parked=True)])
        idx = model.index(0, COL_SYMBOL)
        brush = model.data(idx, Qt.ForegroundRole)
        assert brush is not None
        # Red color from the model — use approximate match
        assert brush.color().red() > 100
        assert brush.color().green() < 100

    def test_delisted_ticker_renders_gray(self, qtbot):
        model = TickerTableModel()
        model.set_rows([
            _ticker(delisted_at=datetime(2025, 1, 1, tzinfo=timezone.utc)),
        ])
        idx = model.index(0, COL_SYMBOL)
        brush = model.data(idx, Qt.ForegroundRole)
        assert brush is not None
        # All three RGB components should be roughly equal (gray)
        c = brush.color()
        assert abs(c.red() - c.green()) < 20
        assert abs(c.green() - c.blue()) < 20

    def test_set_rows_resets_model(self, qtbot):
        """The reset signal must fire so views re-render."""
        model = TickerTableModel()
        model.set_rows([_ticker(1)])
        spy = QSignalSpy(model.modelReset)
        model.set_rows([_ticker(2), _ticker(3)])
        assert spy.count() == 1
        assert model.rowCount() == 2

    def test_row_at_returns_correct_row(self, qtbot):
        model = TickerTableModel()
        rows = [_ticker(1, "A"), _ticker(2, "B"), _ticker(3, "C")]
        model.set_rows(rows)
        assert model.row_at(0).symbol == "A"
        assert model.row_at(2).symbol == "C"
        assert model.row_at(99) is None  # out of range

    def test_user_role_returns_full_row(self, qtbot):
        """The view fetches the full TickerRow via UserRole."""
        model = TickerTableModel()
        model.set_rows([_ticker(42, "RELIANCE")])
        idx = model.index(0, 0)
        row = model.data(idx, Qt.UserRole)
        assert row is not None
        assert row.ticker_id == 42

    def test_headers(self, qtbot):
        model = TickerTableModel()
        assert model.headerData(COL_SYMBOL, Qt.Horizontal) == "Symbol"
        assert model.headerData(COL_TIER, Qt.Horizontal) == "Tier"
        assert model.headerData(COL_PARKED, Qt.Horizontal) == "Parked"


# ============================================================
# BackgroundWorker
# ============================================================


class TestBackgroundWorker:

    def test_finished_signal_emitted_with_result(self, qtbot):
        worker = BackgroundWorker(lambda: 42)
        with qtbot.waitSignal(worker.signals.finished, timeout=2000) as blocker:
            QThreadPool.globalInstance().start(worker)
        assert blocker.args[0] == 42

    def test_failed_signal_emitted_on_exception(self, qtbot):
        def raiser():
            raise RuntimeError("simulated failure")
        worker = BackgroundWorker(raiser)
        with qtbot.waitSignal(worker.signals.failed, timeout=2000) as blocker:
            QThreadPool.globalInstance().start(worker)
        assert "simulated failure" in blocker.args[0]

    def test_args_kwargs_forwarded(self, qtbot):
        def fn(a, b, multiplier=1):
            return (a + b) * multiplier
        worker = BackgroundWorker(fn, 3, 5, multiplier=2)
        with qtbot.waitSignal(worker.signals.finished, timeout=2000) as blocker:
            QThreadPool.globalInstance().start(worker)
        assert blocker.args[0] == 16


# ============================================================
# RefreshScheduler
# ============================================================


class TestRefreshScheduler:

    def test_start_fires_tick_immediately_when_run_on_start_true(self, qtbot):
        sched = RefreshScheduler(cadence_seconds=10.0, run_on_start=True)
        spy = QSignalSpy(sched.tick)
        sched.start()
        # First tick should be immediate (within ms, not the 10s cadence)
        assert spy.count() == 1
        sched.stop()

    def test_start_does_not_fire_immediately_when_run_on_start_false(self, qtbot):
        sched = RefreshScheduler(cadence_seconds=10.0, run_on_start=False)
        spy = QSignalSpy(sched.tick)
        sched.start()
        assert spy.count() == 0
        sched.stop()

    def test_trigger_now_fires_tick(self, qtbot):
        sched = RefreshScheduler(cadence_seconds=10.0, run_on_start=False)
        spy = QSignalSpy(sched.tick)
        sched.start()
        sched.trigger_now()
        assert spy.count() == 1
        sched.stop()

    def test_is_running_reflects_timer_state(self, qtbot):
        sched = RefreshScheduler(cadence_seconds=10.0, run_on_start=False)
        assert not sched.is_running
        sched.start()
        assert sched.is_running
        sched.stop()
        assert not sched.is_running

    def test_set_cadence_updates_interval(self, qtbot):
        sched = RefreshScheduler(cadence_seconds=10.0, run_on_start=False)
        assert sched.cadence_seconds == 10.0
        sched.set_cadence(2.5)
        assert sched.cadence_seconds == 2.5

    def test_timer_fires_after_cadence(self, qtbot):
        """Use a very short cadence (50ms) and verify a tick fires."""
        sched = RefreshScheduler(cadence_seconds=0.05, run_on_start=False)
        spy = QSignalSpy(sched.tick)
        sched.start()
        # Wait at least one cadence period + a margin
        assert spy.wait(500)
        sched.stop()


# ============================================================
# MainWindow construction
# ============================================================


class TestMainWindow:
    """Sanity that the window assembles correctly. We use a mocked
    GuiDbSession and TripEmaHttpClient — no real DB or HTTP."""

    def _make_window(self, token_configured: bool):
        from trip_ema.gui.main_window import MainWindow
        db = MagicMock()
        http = MagicMock()
        http.token_configured = token_configured
        # Return a sensible-shaped JSON for any HTTP call the views fire.
        http.get_etl_status.return_value = {
            "recent_runs": [], "parked_count": 0,
            "tier_counts": {"tier_1": 0, "tier_2": 0, "tier_3": 0},
            "snapshot_taken_at": "2026-05-17T10:00:00Z",
        }
        http.get_etl_parked.return_value = {
            "parked": [], "total": 0,
            "snapshot_taken_at": "2026-05-17T10:00:00Z",
        }
        # Slice 4: suppress all background workers in tests so the
        # event loop is quiescent at teardown.
        return MainWindow(
            db=db, http=http,
            populate_signals_combo=False,
            populate_subscribers_lookup=False,
            start_refresh_on_init=False,
        )

    def test_window_constructs_and_shows(self, qtbot):
        window = self._make_window(token_configured=True)
        qtbot.addWidget(window)
        window.show()
        assert window.windowTitle() == "trip_ema — operator console"

    def test_sidebar_has_expected_entries(self, qtbot):
        window = self._make_window(token_configured=True)
        qtbot.addWidget(window)
        sidebar = window._sidebar
        # Slice 4 final: 7 working views, no placeholders.
        assert sidebar.count() == 7
        labels = [sidebar.item(i).text() for i in range(sidebar.count())]
        joined = " ".join(labels).lower()
        assert "etl status" in joined
        assert "tickers" in joined
        assert "signals" in joined
        assert "subscribers" in joined
        assert "notifications" in joined
        assert "system health" in joined
        assert "settings" in joined

    def test_all_sidebar_items_enabled(self, qtbot):
        """Slice 4 final: no more placeholders. All 7 sidebar rows
        are enabled."""
        window = self._make_window(token_configured=True)
        qtbot.addWidget(window)
        sidebar = window._sidebar
        for i in range(sidebar.count()):
            assert (sidebar.item(i).flags() & Qt.ItemIsEnabled) == Qt.ItemIsEnabled, \
                f"sidebar row {i} ({sidebar.item(i).text()}) is disabled"

    def test_selecting_sidebar_switches_stack(self, qtbot):
        window = self._make_window(token_configured=True)
        qtbot.addWidget(window)
        # Start at row 0 (ETL Status). Select row 1 (Tickers).
        window._sidebar.setCurrentRow(1)
        # The stack widget index should follow
        assert window._stack.currentIndex() == 1

    def test_status_bar_when_token_missing(self, qtbot):
        window = self._make_window(token_configured=False)
        qtbot.addWidget(window)
        msg = window.statusBar().currentMessage()
        assert "disabled" in msg.lower() or "read-only" in msg.lower()

    def test_status_bar_when_token_present(self, qtbot):
        window = self._make_window(token_configured=True)
        qtbot.addWidget(window)
        msg = window.statusBar().currentMessage()
        assert "ready" in msg.lower()


# ============================================================
# View action-button enabling
# ============================================================


class TestActionButtonGating:
    """The action buttons in the ETL Status view must be enabled iff
    the HTTP token is configured."""

    def _build_etl_view(self, token_configured: bool):
        from trip_ema.gui.views.etl_status import EtlStatusView
        db = MagicMock()
        http = MagicMock()
        http.token_configured = token_configured
        http.get_etl_status.return_value = {
            "recent_runs": [], "parked_count": 0,
            "tier_counts": {"tier_1": 0, "tier_2": 0, "tier_3": 0},
        }
        http.get_etl_parked.return_value = {"parked": [], "total": 0}
        return EtlStatusView(db=db, http=http, start_refresh_on_init=False)

    def test_action_buttons_disabled_without_token(self, qtbot):
        view = self._build_etl_view(token_configured=False)
        qtbot.addWidget(view)
        for btn in view._action_buttons:
            assert not btn.isEnabled()

    def test_action_buttons_enabled_with_token(self, qtbot):
        view = self._build_etl_view(token_configured=True)
        qtbot.addWidget(view)
        for btn in view._action_buttons:
            assert btn.isEnabled()

    def test_refresh_button_always_enabled(self, qtbot):
        """The [Refresh] button is just a read; doesn't need the token."""
        view = self._build_etl_view(token_configured=False)
        qtbot.addWidget(view)
        assert view._refresh_button.isEnabled()
