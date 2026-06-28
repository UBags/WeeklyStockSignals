"""Tests for ETL Status view 13d-3 wiring:
  - Refresh Listings button → confirmation modal → QProcess
  - Recompute Tiers button → confirmation modal → QProcess
  - 4th See Trend button (signals)
  - Colored HTML footer labels
  - OperationStatusBanner integration
  - Mutex extended to 4 kinds
"""
from __future__ import annotations

from datetime import date, datetime, UTC
from unittest.mock import MagicMock, patch

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog


# Use the shared fixture from existing 13c/13d-2 test infrastructure.
# If the project's conftest doesn't provide `view`, this skips the
# whole module rather than crashing collection.
pytest.importorskip("trip_ema.gui.views.etl_status")


# === STEP 13D3-FIXUP: view fixture ===
@pytest.fixture
def view(qtbot, monkeypatch):
    """Build an EtlStatusView with stubbed db + http.

    EtlStatusView.__init__ signature:
        (self, db, http, *, parent=None, start_refresh_on_init=True)
    """
    from trip_ema.gui.views.etl_status import EtlStatusView

    db = MagicMock()
    http = MagicMock()
    # http calls used at startup return benign defaults
    http.unpark_all.return_value = {"notes": "submitted"}

    v = EtlStatusView(
        db=db, http=http, start_refresh_on_init=False,
    )
    qtbot.addWidget(v)
    return v


# ============================================================
# Mutex extended to 4 kinds
# ============================================================

class TestMutex:

    def test_inflight_kind_starts_none(self, view):
        assert view._inflight_kind is None
        assert view._local_running is False

    def test_apply_mutex_disables_all_4_buttons(self, view):
        view._apply_mutex_state(True, "backfill")
        assert not view._refresh_listings_btn.isEnabled()
        assert not view._recompute_tiers_btn.isEnabled()
        assert not view._backfill_btn.isEnabled()
        assert not view._run_intraday_btn.isEnabled()

    def test_apply_mutex_re_enables_all_4(self, view):
        view._apply_mutex_state(True, "intraday")
        view._apply_mutex_state(False, None)
        assert view._refresh_listings_btn.isEnabled()
        assert view._recompute_tiers_btn.isEnabled()
        assert view._backfill_btn.isEnabled()
        assert view._run_intraday_btn.isEnabled()


# ============================================================
# Refresh Listings click flow
# ============================================================

class TestRefreshListingsClick:

    def test_no_exchange_selected_short_circuits(self, view):
        view._picker.current_exchange = MagicMock(return_value=None)
        view._on_refresh_listings_clicked()
        # No process started
        assert view._singlestage_process is None

    def test_already_running_short_circuits(self, view):
        view._picker.current_exchange = MagicMock(return_value="NSE")
        view._local_running = True
        view._on_refresh_listings_clicked()
        assert view._singlestage_process is None

    def test_modal_cancelled_no_process(self, view, monkeypatch):
        view._picker.current_exchange = MagicMock(return_value="NSE")

        # Stub the modal to immediately reject
        from trip_ema.gui.views.widgets import (
            refresh_listings_confirmation_modal as rl_mod,
        )
        FakeModal = MagicMock()
        FakeModal.DialogCode = QDialog.DialogCode
        FakeModal.return_value.exec.return_value = (
            QDialog.DialogCode.Rejected
        )
        monkeypatch.setattr(
            rl_mod, "RefreshListingsConfirmationModal", FakeModal,
        )

        view._on_refresh_listings_clicked()
        assert view._singlestage_process is None
        assert view._local_running is False

    def test_modal_accepted_acquires_mutex(self, view, monkeypatch):
        view._picker.current_exchange = MagicMock(return_value="NSE")

        from trip_ema.gui.views.widgets import (
            refresh_listings_confirmation_modal as rl_mod,
        )

        class FakeModal:
            DialogCode = QDialog.DialogCode

            def __init__(self, *a, **k):
                pass

            def exec(self):
                return QDialog.DialogCode.Accepted

        monkeypatch.setattr(
            rl_mod, "RefreshListingsConfirmationModal", FakeModal,
        )

        # Stub QProcess.start so we don't actually spawn
        with patch(
            "PySide6.QtCore.QProcess.start",
        ) as mock_start:
            view._on_refresh_listings_clicked()

        assert view._local_running is True
        assert view._inflight_kind == "refresh_listings"
        assert view._singlestage_process is not None
        mock_start.assert_called_once()


# ============================================================
# Recompute Tiers click flow (mirrors refresh_listings)
# ============================================================

class TestRecomputeTiersClick:

    def test_no_exchange_short_circuits(self, view):
        view._picker.current_exchange = MagicMock(return_value=None)
        view._on_recompute_tiers_clicked()
        assert view._singlestage_process is None

    def test_modal_accepted_acquires_mutex(self, view, monkeypatch):
        view._picker.current_exchange = MagicMock(return_value="NSE")

        from trip_ema.gui.views.widgets import (
            recompute_tiers_confirmation_modal as rt_mod,
        )

        class FakeModal:
            DialogCode = QDialog.DialogCode

            def __init__(self, *a, **k):
                pass

            def exec(self):
                return QDialog.DialogCode.Accepted

        monkeypatch.setattr(
            rt_mod, "RecomputeTiersConfirmationModal", FakeModal,
        )

        with patch(
            "PySide6.QtCore.QProcess.start",
        ) as mock_start:
            view._on_recompute_tiers_clicked()

        assert view._local_running is True
        assert view._inflight_kind == "recompute_tiers"
        mock_start.assert_called_once()


# ============================================================
# Single-stage stdout/finished slots
# ============================================================

class TestSinglestageSlots:

    def test_stdout_accumulates_buffer(self, view):
        proc = MagicMock()
        proc.readAllStandardOutput = (
            lambda: b"hello world\n"
        )
        view._singlestage_stdout_buffer = ""
        view._on_singlestage_stdout(proc)
        assert "hello world" in view._singlestage_stdout_buffer

    def test_stderr_accumulates_buffer(self, view):
        proc = MagicMock()
        proc.readAllStandardError = (
            lambda: b"error: something failed\n"
        )
        view._singlestage_stderr_buffer = ""
        view._on_singlestage_stderr(proc)
        assert "error" in view._singlestage_stderr_buffer

    def test_finished_success_releases_mutex(self, view):
        view._local_running = True
        view._inflight_kind = "refresh_listings"
        view._singlestage_process = MagicMock()
        view._singlestage_stdout_buffer = ""
        view._singlestage_stderr_buffer = ""

        # Stub _refresh_autocomplete + scheduler triggers so they
        # don't try to do real work
        view._refresh_autocomplete = MagicMock()
        view._activity_scheduler.trigger_now = MagicMock()
        view._mutex_scheduler.trigger_now = MagicMock()

        view._on_singlestage_finished(
            proc=None, code=0, status=None,
            kind="refresh_listings", exchange="NSE",
        )

        assert view._local_running is False
        assert view._inflight_kind is None
        assert view._singlestage_process is None

    def test_finished_failure_shows_banner_failure(self, view):
        view._local_running = True
        view._inflight_kind = "refresh_listings"
        view._singlestage_process = MagicMock()
        view._singlestage_stdout_buffer = ""
        view._singlestage_stderr_buffer = "yfinance dead\n"

        view._refresh_autocomplete = MagicMock()
        view._activity_scheduler.trigger_now = MagicMock()
        view._mutex_scheduler.trigger_now = MagicMock()
        view._operation_banner.finish_failure = MagicMock()

        view._on_singlestage_finished(
            proc=None, code=1, status=None,
            kind="refresh_listings", exchange="NSE",
        )

        view._operation_banner.finish_failure.assert_called_once()
        call_arg = (
            view._operation_banner.finish_failure.call_args[0][0]
        )
        assert "yfinance dead" in call_arg

    def test_finished_refresh_listings_triggers_autocomplete(
        self, view,
    ):
        """F5 lock: any exit code triggers autocomplete refresh."""
        view._local_running = True
        view._inflight_kind = "refresh_listings"
        view._singlestage_process = MagicMock()
        view._singlestage_stdout_buffer = ""
        view._singlestage_stderr_buffer = ""

        view._refresh_autocomplete = MagicMock()
        view._activity_scheduler.trigger_now = MagicMock()
        view._mutex_scheduler.trigger_now = MagicMock()

        view._on_singlestage_finished(
            proc=None, code=0, status=None,
            kind="refresh_listings", exchange="NSE",
        )
        view._refresh_autocomplete.assert_called_once()

    def test_finished_refresh_listings_autocomplete_on_failure(
        self, view,
    ):
        """Even on failure, listings may be partially mutated → refresh."""
        view._local_running = True
        view._inflight_kind = "refresh_listings"
        view._singlestage_process = MagicMock()
        view._singlestage_stdout_buffer = ""
        view._singlestage_stderr_buffer = "boom\n"

        view._refresh_autocomplete = MagicMock()
        view._activity_scheduler.trigger_now = MagicMock()
        view._mutex_scheduler.trigger_now = MagicMock()
        view._operation_banner.finish_failure = MagicMock()

        view._on_singlestage_finished(
            proc=None, code=1, status=None,
            kind="refresh_listings", exchange="NSE",
        )
        view._refresh_autocomplete.assert_called_once()

    def test_finished_recompute_tiers_does_not_refresh_autocomplete(
        self, view,
    ):
        """Recompute Tiers doesn't change tickers identity — no
        autocomplete refresh needed."""
        view._local_running = True
        view._inflight_kind = "recompute_tiers"
        view._singlestage_process = MagicMock()
        view._singlestage_stdout_buffer = ""
        view._singlestage_stderr_buffer = ""

        view._refresh_autocomplete = MagicMock()
        view._activity_scheduler.trigger_now = MagicMock()
        view._mutex_scheduler.trigger_now = MagicMock()

        view._on_singlestage_finished(
            proc=None, code=0, status=None,
            kind="recompute_tiers", exchange="NSE",
        )
        view._refresh_autocomplete.assert_not_called()


# ============================================================
# 4th See Trend button (signals)
# ============================================================

class TestSignalsTrendButton:

    def test_signals_trend_btn_exists(self, view):
        assert hasattr(view, "_signals_trend_btn")
        assert view._signals_trend_btn.text() == "See Trend"

    def test_signals_trend_btn_opens_dialog_with_signals_kind(
        self, view,
    ):
        """Clicking the signals See Trend button should pass
        TrendKind.SIGNALS to _open_trend_dialog."""
        from trip_ema.gui.views.market_trend_dialog import TrendKind

        with patch.object(
            view, "_open_trend_dialog",
        ) as mock_open:
            view._signals_trend_btn.click()
            mock_open.assert_called_once_with(TrendKind.SIGNALS)


# ============================================================
# Colored HTML footer labels
# ============================================================

class TestFooterColorScheme:

    def test_color_span_helper(self, view):
        s = view._color_span("# Buys = 10", "#66bb6a")
        assert "<span" in s
        assert "color: #66bb6a" in s
        assert "# Buys = 10" in s

    def test_paint_market_overview_signals_colors(self, view):
        row = MagicMock(
            snapshot_date=date(2026, 6, 28),
            n_buys=10, n_sells=5, n_exit_buys=2, n_exit_sells=1,
            n_green_regime=100, n_red_regime=50, n_neutral_regime=200,
            n_above_highest_ema=80, n_below_lowest_ema=40,
            n_between_emas=230,
        )
        view._paint_market_overview(row)
        sig_html = view._signals_label.text()
        assert "#66bb6a" in sig_html  # Buys green
        assert "#ef5350" in sig_html  # Sells red
        assert "#1976d2" in sig_html  # Exit Buys dark blue
        assert "#9e9e9e" in sig_html  # Exit Sells gray

    def test_paint_market_overview_regimes_colors(self, view):
        row = MagicMock(
            snapshot_date=date(2026, 6, 28),
            n_buys=10, n_sells=5, n_exit_buys=2, n_exit_sells=1,
            n_green_regime=100, n_red_regime=50, n_neutral_regime=200,
            n_above_highest_ema=80, n_below_lowest_ema=40,
            n_between_emas=230,
        )
        view._paint_market_overview(row)
        reg_html = view._regimes_label.text()
        assert "#66bb6a" in reg_html  # Green Regimes
        assert "#ef5350" in reg_html  # Red Regimes
        assert "#42a5f5" in reg_html  # Neutral Regimes blue

    def test_paint_market_overview_ema_colors(self, view):
        row = MagicMock(
            snapshot_date=date(2026, 6, 28),
            n_buys=10, n_sells=5, n_exit_buys=2, n_exit_sells=1,
            n_green_regime=100, n_red_regime=50, n_neutral_regime=200,
            n_above_highest_ema=80, n_below_lowest_ema=40,
            n_between_emas=230,
        )
        view._paint_market_overview(row)
        ema_html = view._ema_label.text()
        assert "#66bb6a" in ema_html  # >highest_ema green
        assert "#ef5350" in ema_html  # <lowest_ema red
        assert "#42a5f5" in ema_html  # in between blue

    def test_paint_market_overview_empty_state_no_colors(self, view):
        view._paint_market_overview(None)
        sig = view._signals_label.text()
        # Empty state uses plain dashes, no color spans
        assert "<span" not in sig

    def test_labels_use_rich_text_format(self, view):
        assert view._signals_label.textFormat() == Qt.RichText
        assert view._regimes_label.textFormat() == Qt.RichText
        assert view._ema_label.textFormat() == Qt.RichText


# ============================================================
# OperationStatusBanner integration
# ============================================================

class TestOperationBannerIntegration:

    def test_banner_exists(self, view):
        assert hasattr(view, "_operation_banner")
        assert view._operation_banner.state() == "hidden"

    def test_start_singlestage_starts_banner(self, view):
        with patch(
            "PySide6.QtCore.QProcess.start",
        ):
            view._start_singlestage_process(
                kind="refresh_listings",
                cli_subcommand="refresh-listings",
                exchange="NSE",
                banner_label="Refresh Listings (NSE) running…",
            )
        assert view._operation_banner.state() == "running"
