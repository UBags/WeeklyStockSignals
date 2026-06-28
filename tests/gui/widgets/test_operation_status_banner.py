"""Tests for OperationStatusBanner (Step 13d-3)."""
from __future__ import annotations

import pytest

from trip_ema.gui.views.widgets.operation_status_banner import (
    OperationStatusBanner, SUCCESS_AUTOHIDE_MS,
)


class TestInitialState:

    def test_starts_hidden(self, qtbot):
        banner = OperationStatusBanner()
        qtbot.addWidget(banner)
        assert banner.state() == "hidden"
        assert not banner.isVisible() or banner.isHidden()


class TestRunningState:

    def test_start_shows_banner_with_label(self, qtbot):
        banner = OperationStatusBanner()
        qtbot.addWidget(banner)
        banner.show()
        banner.start("Refresh Listings (NSE) running…")
        assert banner.state() == "running"
        assert banner._status_label.text() == (
            "Refresh Listings (NSE) running…"
        )

    def test_start_shows_spinner(self, qtbot):
        banner = OperationStatusBanner()
        qtbot.addWidget(banner)
        banner.start("test")
        assert banner._spinner.isVisible()

    def test_start_applies_blue_style(self, qtbot):
        banner = OperationStatusBanner()
        qtbot.addWidget(banner)
        banner.start("test")
        sheet = banner.styleSheet()
        assert "#3498db" in sheet  # blue border for running


class TestSuccessState:

    def test_finish_success_hides_spinner(self, qtbot):
        banner = OperationStatusBanner()
        qtbot.addWidget(banner)
        banner.start("test")
        banner.finish_success("Refresh Listings complete.")
        assert banner.state() == "success"
        assert not banner._spinner.isVisible() or banner._spinner.isHidden()

    def test_finish_success_applies_green_style(self, qtbot):
        banner = OperationStatusBanner()
        qtbot.addWidget(banner)
        banner.finish_success("done")
        sheet = banner.styleSheet()
        assert "#2ecc71" in sheet  # green border for success

    def test_finish_success_default_message(self, qtbot):
        banner = OperationStatusBanner()
        qtbot.addWidget(banner)
        banner.finish_success()
        assert banner._status_label.text() == "Done."


class TestFailureState:

    def test_finish_failure_applies_red_style(self, qtbot):
        banner = OperationStatusBanner()
        qtbot.addWidget(banner)
        banner.finish_failure("yfinance dead")
        sheet = banner.styleSheet()
        assert "#e74c3c" in sheet  # red border for failure

    def test_finish_failure_includes_error_text(self, qtbot):
        banner = OperationStatusBanner()
        qtbot.addWidget(banner)
        banner.finish_failure("connection timeout")
        text = banner._status_label.text()
        assert "FAILED" in text
        assert "connection timeout" in text

    def test_finish_failure_is_sticky(self, qtbot):
        """No auto-hide on failure — operator must see the error."""
        banner = OperationStatusBanner()
        qtbot.addWidget(banner)
        banner.finish_failure("e")
        assert not banner._autohide_timer.isActive()


class TestReset:

    def test_reset_hides_banner(self, qtbot):
        banner = OperationStatusBanner()
        qtbot.addWidget(banner)
        banner.start("test")
        banner.reset()
        assert banner.state() == "hidden"
        assert banner._status_label.text() == ""

    def test_reset_stops_autohide_timer(self, qtbot):
        banner = OperationStatusBanner()
        qtbot.addWidget(banner)
        banner.finish_success()
        assert banner._autohide_timer.isActive()
        banner.reset()
        assert not banner._autohide_timer.isActive()


class TestStateTransitions:

    def test_running_to_success_to_hidden_via_autohide(self, qtbot):
        banner = OperationStatusBanner()
        qtbot.addWidget(banner)
        banner.start("running...")
        assert banner.state() == "running"
        banner.finish_success("done")
        assert banner.state() == "success"
        # === STEP 13D3-FIXUP: autohide interval ===
        # Autohide timer is queued for SUCCESS_AUTOHIDE_MS.
        # singleShot mode just means "fire once then stop"; the
        # interval is whatever start(ms) was given, i.e.
        # SUCCESS_AUTOHIDE_MS.
        assert banner._autohide_timer.isActive()
        assert banner._autohide_timer.isSingleShot()
        assert (
            banner._autohide_timer.remainingTime()
            <= SUCCESS_AUTOHIDE_MS
        )
        # We don't actually wait for the timer to fire in unit
        # tests — the timer firing is Qt-internal. We just verify
        # it's queued.

    def test_success_followed_by_new_start_replaces(self, qtbot):
        banner = OperationStatusBanner()
        qtbot.addWidget(banner)
        banner.finish_success("op 1 done")
        banner.start("op 2 running...")
        assert banner.state() == "running"
        assert not banner._autohide_timer.isActive()

    def test_constants_exposed(self):
        assert SUCCESS_AUTOHIDE_MS > 0
        assert SUCCESS_AUTOHIDE_MS < 10_000  # under 10s
