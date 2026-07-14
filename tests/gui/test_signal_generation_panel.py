"""Phase 15c GUI tests: SignalGenerationPanel.

Coverage:
  - Construction state: button disabled, defaults (1W checked, 1D
    not checked, Incremental selected, Force full not selected)
  - Button enable logic: requires exchange resolved AND timeframe
    checked AND token configured
  - Action dispatch: clicking Generate POSTs the right query params
    (exchange, timeframes, force_full)
  - Force-full confirmation: cancel path does NOT POST; Yes path
    DOES POST
  - 409 conflict: friendly modal, button re-enables
  - Metadata grid: rows appear after picker populates; each row
    updates with what the server returns; 'no file yet' shown for
    exists=false
  - on_close stops the polling timers

Conventions inherited from 14.6c:
  - silent_modals fixture supplies a FakePrompts (no global stubbing)
  - _teardown picker drains QThreadPool
  - QSettings cleaned between tests
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from PySide6.QtCore import QThreadPool

from .fakes import FakePrompts

from trip_ema.gui.http_client import ApiError
from trip_ema.gui.views.signal_generation_panel import (
    SignalGenerationPanel,
    _format_size,
    _format_mtime,
    _format_metadata_line,
)


# ---------- Shared helpers ---------- #


def _add(qtbot, widget):
    qtbot.addWidget(widget)
    return widget


def _teardown_panel(panel) -> None:
    try:
        panel.on_close()
    except Exception:
        pass
    try:
        panel.close()
    except Exception:
        pass
    QThreadPool.globalInstance().waitForDone(1500)


def _make_http(*, token_configured=True, exchanges=("NSE", "NYSE")):
    http = MagicMock()
    http.token_configured = token_configured
    http.list_exchanges.return_value = {
        "exchanges": list(exchanges),
        "total": len(exchanges),
        "snapshot_taken_at": "2026-05-21T10:00:00Z",
    }
    http.generate_signals.return_value = {
        "run_id": 42, "exchange": "NSE", "timeframes": ["1W"],
        "force_full": False,
        "estimated_output_path": "generated_signals/NSE.json",
        "submitted_at": "2026-05-21T10:00:00+00:00",
    }
    http.get_signal_generation_metadata.side_effect = (
        lambda exchange: {"exchange": exchange, "exists": False}
    )
    return http


def _build_panel(*, http=None, start_polling_on_init=False, prompts=None):
    """Build a panel with the HTTP mock; polling off by default so
    tests drive _refresh_metadata_rows() manually when they care."""
    if http is None:
        http = _make_http()
    panel = SignalGenerationPanel(
        http,
        start_polling_on_init=start_polling_on_init,
        prompts=prompts if prompts is not None else FakePrompts(),
    )
    return panel, http


def _populate_picker(panel, exchanges):
    """Bypass the picker's async fetch by driving populate
    directly. After this, the panel's grid rows are built and the
    Generate button is enabled (subject to other conditions)."""
    panel._picker.populate_with_exchanges(list(exchanges))


def _seed_one_metadata_response(panel, exchange, payload):
    """Test seam: simulate a successful metadata fetch for one row.
    The panel's _on_metadata_received is the slot connected to
    workers' `finished` signals; calling it directly is equivalent
    to a worker delivering the payload."""
    panel._on_metadata_received(exchange, payload)


@pytest.fixture
def silent_modals():
    """Prompts that say yes and remember everything else. Hand it to
    _build_panel; nothing global is touched."""
    return FakePrompts(confirm_answer=True)


@pytest.fixture
def cancel_force_full_dialog():
    """Variant: the operator says No to the force-full confirmation."""
    return FakePrompts(confirm_answer=False)


@pytest.fixture(autouse=True)
def _clean_qsettings():
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
# Module-level helpers (pure functions, no Qt)
# ============================================================


class TestFormatSize:

    def test_none_renders_as_dash(self):
        assert _format_size(None) == "—"

    def test_bytes(self):
        assert _format_size(500) == "500 B"

    def test_kilobytes(self):
        assert "KB" in _format_size(2048)

    def test_megabytes(self):
        assert "MB" in _format_size(14 * 1024 * 1024)

    def test_gigabytes(self):
        assert "GB" in _format_size(3 * 1024 * 1024 * 1024)


class TestFormatMtime:

    def test_none_renders_as_dash(self):
        assert _format_mtime(None) == "—"

    def test_iso_with_z_suffix(self):
        # We don't assert exact timezone-converted format here
        # because tests run in different host TZs; just confirm the
        # date portion survives.
        result = _format_mtime("2026-05-21T10:00:00Z")
        assert "2026-05-21" in result or "2026-05-2" in result

    def test_garbage_input_falls_back_to_prefix(self):
        result = _format_mtime("not a valid timestamp at all")
        assert result.startswith("not a valid time")   # 16-char prefix


class TestFormatMetadataLine:

    def test_exists_false_says_no_file_yet(self):
        line = _format_metadata_line({"exists": False})
        assert "no file yet" in line.lower()

    def test_exists_true_shows_size_mtime_signals(self):
        payload = {
            "exists": True,
            "size_bytes": 14_680_064,
            "mtime": "2026-05-21T10:00:00+00:00",
            "counts": {"signals_total": 105684},
        }
        line = _format_metadata_line(payload)
        assert "MB" in line
        assert "105,684" in line   # thousands separator


# ============================================================
# Construction state
# ============================================================


class TestPanelConstruction:

    def test_constructs_cleanly(self, qtbot):
        panel, http = _build_panel()
        _add(qtbot, panel)
        try:
            assert panel.title().lower().startswith("generate")
        finally:
            _teardown_panel(panel)

    def test_generate_button_disabled_initially(self, qtbot):
        """Pre-populate, no exchange resolved → button disabled."""
        panel, http = _build_panel()
        _add(qtbot, panel)
        try:
            assert panel._generate_btn.isEnabled() is False
        finally:
            _teardown_panel(panel)

    def test_defaults_1W_checked_1D_unchecked_incremental(self, qtbot):
        """Per design §4.5.1: 1W is the default; 1D is opt-in;
        Incremental is the default mode."""
        panel, http = _build_panel()
        _add(qtbot, panel)
        try:
            assert panel._chk_1w.isChecked() is True
            assert panel._chk_1d.isChecked() is False
            assert panel._rb_incremental.isChecked() is True
            assert panel._rb_force_full.isChecked() is False
        finally:
            _teardown_panel(panel)


# ============================================================
# Generate-button enable logic
# ============================================================


class TestGenerateButtonEnableLogic:

    def test_enabled_when_exchange_resolved_and_timeframe_and_token(self, qtbot):
        panel, http = _build_panel(http=_make_http(token_configured=True))
        _add(qtbot, panel)
        try:
            _populate_picker(panel, ["NSE", "NYSE"])
            assert panel._generate_btn.isEnabled() is True
        finally:
            _teardown_panel(panel)

    def test_disabled_when_no_token(self, qtbot):
        """A populated picker + a checked timeframe is not enough;
        the http client must report token_configured=True. Without
        a token, the server returns 401 — we shouldn't even let the
        operator fire."""
        panel, http = _build_panel(http=_make_http(token_configured=False))
        _add(qtbot, panel)
        try:
            _populate_picker(panel, ["NSE"])
            assert panel._generate_btn.isEnabled() is False
        finally:
            _teardown_panel(panel)

    def test_disabled_when_no_timeframe_checked(self, qtbot):
        panel, http = _build_panel()
        _add(qtbot, panel)
        try:
            _populate_picker(panel, ["NSE"])
            # Uncheck 1W; 1D was already unchecked
            panel._chk_1w.setChecked(False)
            assert panel._generate_btn.isEnabled() is False
        finally:
            _teardown_panel(panel)

    def test_disabled_when_picker_not_populated(self, qtbot):
        """No populate → picker.current_exchange() is None → button
        stays disabled even though token + timeframe are fine."""
        panel, http = _build_panel()
        _add(qtbot, panel)
        try:
            # 1W is checked by default; token is configured. The
            # only missing piece is picker resolution.
            assert panel._generate_btn.isEnabled() is False
        finally:
            _teardown_panel(panel)


# ============================================================
# Action dispatch: POSTed query params reflect UI state
# ============================================================


class TestActionDispatchPostsCorrectQueryParams:

    def test_default_1W_only(self, qtbot, silent_modals):
        panel, http = _build_panel(prompts=silent_modals)
        _add(qtbot, panel)
        try:
            _populate_picker(panel, ["NSE"])
            # Click Generate
            panel._generate_btn.click()
            # Worker hasn't drained yet — wait for the POST to land
            qtbot.waitUntil(
                lambda: http.generate_signals.called, timeout=2000,
            )
            kwargs = http.generate_signals.call_args.kwargs
            assert kwargs["exchange"] == "NSE"
            assert kwargs["timeframes"] == ["1W"]
            assert kwargs["force_full"] is False
        finally:
            _teardown_panel(panel)

    def test_both_timeframes(self, qtbot, silent_modals):
        panel, http = _build_panel(prompts=silent_modals)
        _add(qtbot, panel)
        try:
            _populate_picker(panel, ["NSE"])
            panel._chk_1d.setChecked(True)   # both 1D and 1W now checked
            panel._generate_btn.click()
            qtbot.waitUntil(
                lambda: http.generate_signals.called, timeout=2000,
            )
            kwargs = http.generate_signals.call_args.kwargs
            assert kwargs["timeframes"] == ["1D", "1W"]
        finally:
            _teardown_panel(panel)

    def test_1D_only(self, qtbot, silent_modals):
        panel, http = _build_panel(prompts=silent_modals)
        _add(qtbot, panel)
        try:
            _populate_picker(panel, ["NSE"])
            panel._chk_1w.setChecked(False)
            panel._chk_1d.setChecked(True)
            panel._generate_btn.click()
            qtbot.waitUntil(
                lambda: http.generate_signals.called, timeout=2000,
            )
            kwargs = http.generate_signals.call_args.kwargs
            assert kwargs["timeframes"] == ["1D"]
        finally:
            _teardown_panel(panel)

    def test_force_full_propagates_with_confirmation(self, qtbot, silent_modals):
        """silent_modals' question returns Yes, so the force-full
        path goes through to POST with force_full=True."""
        panel, http = _build_panel(prompts=silent_modals)
        _add(qtbot, panel)
        try:
            _populate_picker(panel, ["NSE"])
            panel._rb_force_full.setChecked(True)
            panel._generate_btn.click()
            qtbot.waitUntil(
                lambda: http.generate_signals.called, timeout=2000,
            )
            kwargs = http.generate_signals.call_args.kwargs
            assert kwargs["force_full"] is True
        finally:
            _teardown_panel(panel)


# ============================================================
# Force-full confirmation: cancel path
# ============================================================


class TestForceFullConfirmation:

    def test_cancel_does_not_post(self, qtbot, cancel_force_full_dialog):
        """If the operator clicks Cancel on the force-full confirm
        dialog, the POST is NOT fired. The button re-enables (it was
        only disabled by the click handler if we got past the
        confirm; cancel returns before that)."""
        panel, http = _build_panel(prompts=cancel_force_full_dialog)
        _add(qtbot, panel)
        try:
            _populate_picker(panel, ["NSE"])
            panel._rb_force_full.setChecked(True)
            panel._generate_btn.click()
            # Give the event loop a moment in case anything queued
            qtbot.wait(50)
            # The confirmation dialog returned Cancel; no POST.
            assert http.generate_signals.called is False
            # Button still enabled (cancel returns before the
            # button-disable line in _on_generate_clicked).
            assert panel._generate_btn.isEnabled() is True
        finally:
            _teardown_panel(panel)


# ============================================================
# 409 conflict path: friendly modal, button re-enables
# ============================================================


class TestConflictHandling:

    def test_409_triggers_friendly_warning_and_re_enables_button(
        self, qtbot, silent_modals,
    ):
        """When the server returns 409 (concurrent-run guard), the
        worker fires `failed` with the error message. The panel
        recognises it and shows the friendly warning modal (stubbed
        to a no-op by silent_modals), then re-enables the button."""
        panel, http = _build_panel(prompts=silent_modals)
        # Make generate_signals raise the 409
        http.generate_signals.side_effect = ApiError(
            409,
            "A signal_backtest run is already in flight for "
            "exchange='NSE' (run_id=15). Wait for completion.",
        )
        _add(qtbot, panel)
        try:
            _populate_picker(panel, ["NSE"])
            panel._generate_btn.click()
            qtbot.waitUntil(
                lambda: http.generate_signals.called, timeout=2000,
            )
            # Worker delivered the failure; button should be re-enabled
            qtbot.waitUntil(
                lambda: panel._generate_btn.isEnabled() is True,
                timeout=2000,
            )
        finally:
            _teardown_panel(panel)


# ============================================================
# Last-generation-per-exchange grid
# ============================================================


class TestLastGenerationPanel:

    def test_rows_appear_after_picker_populates(self, qtbot):
        """Once the picker reports loaded, the panel builds one row
        per configured exchange."""
        panel, http = _build_panel()
        _add(qtbot, panel)
        try:
            assert panel._metadata_labels == {}
            _populate_picker(panel, ["NSE", "NYSE"])
            # Rows now exist
            assert "NSE" in panel._metadata_labels
            assert "NYSE" in panel._metadata_labels
        finally:
            _teardown_panel(panel)

    def test_row_renders_metadata_when_file_exists(self, qtbot):
        panel, http = _build_panel()
        _add(qtbot, panel)
        try:
            _populate_picker(panel, ["NSE"])
            _seed_one_metadata_response(panel, "NSE", {
                "exchange": "NSE", "exists": True,
                "filename": "NSE.json",
                "size_bytes": 14_680_064,
                "mtime": "2026-05-21T10:00:00+00:00",
                "counts": {"signals_total": 105684},
            })
            text = panel._metadata_labels["NSE"].text()
            assert "MB" in text
            assert "105,684" in text
        finally:
            _teardown_panel(panel)

    def test_row_says_no_file_yet_when_exists_false(self, qtbot):
        panel, http = _build_panel()
        _add(qtbot, panel)
        try:
            _populate_picker(panel, ["NSE"])
            _seed_one_metadata_response(panel, "NSE", {
                "exchange": "NSE", "exists": False,
            })
            assert "no file yet" in panel._metadata_labels["NSE"].text().lower()
        finally:
            _teardown_panel(panel)

    def test_row_shows_error_inline_when_fetch_fails(self, qtbot):
        """A failed metadata worker shouldn't pop a modal — that
        would be obnoxious on a 30s polling timer. Inline error in
        the row instead."""
        panel, http = _build_panel()
        _add(qtbot, panel)
        try:
            _populate_picker(panel, ["NSE"])
            panel._on_metadata_failed("NSE", "HTTP 500: backend down")
            text = panel._metadata_labels["NSE"].text()
            assert "fetch failed" in text.lower()
            assert "backend down" in text.lower()
        finally:
            _teardown_panel(panel)


# ============================================================
# Lifecycle
# ============================================================


class TestPanelLifecycle:

    def test_on_close_stops_polling_timer(self, qtbot):
        """The panel polls every 30s by default. on_close() should
        stop the timer so it doesn't keep firing after the panel
        (and any consuming view) is torn down."""
        panel, http = _build_panel(start_polling_on_init=True)
        _add(qtbot, panel)
        # Sanity: timer is running
        assert panel._poll_timer.isActive() is True
        panel.on_close()
        assert panel._poll_timer.isActive() is False
        _teardown_panel(panel)
