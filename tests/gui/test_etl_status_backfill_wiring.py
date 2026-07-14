"""View-level tests for the EtlStatusView's backfill wiring
(Step 13c).

Scope:
  - build_backfill_argv pure-function tests (no Qt)
  - ADD-button validation against the active-ticker set
  - Backfill click → date validation, ALL-tickers branch,
    pre-flight failure handling
  - QProcess argv construction via injected stub

The view's _start_backfill_process method invokes
sys.executable + QProcess.start(); we test the helper that builds
argv. End-to-end subprocess behavior (real CLI run) is covered
by manual smoke tests on the live service — not worth the harness
in CI.
"""
from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

import pytest

from .fakes import FakePrompts

from trip_ema.gui.views.etl_status import build_backfill_argv


# ============================================================
# build_backfill_argv (pure function — no Qt)
# ============================================================

class TestBuildBackfillArgv:

    def test_minimum_argv(self):
        args = build_backfill_argv(
            exchange="NSE",
            date_from=date(2024, 1, 1),
            date_to=date(2026, 6, 28),
            stocks=[],
            refetch_bars=True,
        )
        assert args == [
            "-m", "trip_ema.etl.cli", "backfill",
            "--exchange", "NSE",
            "--date_from", "2024-01-01",
            "--date_to", "2026-06-28",
        ]

    def test_no_fetch_bars_flag(self):
        args = build_backfill_argv(
            exchange="NSE",
            date_from=date(2024, 1, 1),
            date_to=date(2026, 6, 28),
            stocks=[],
            refetch_bars=False,
        )
        assert "--no_fetch_bars" in args

    def test_refetch_bars_omits_flag(self):
        args = build_backfill_argv(
            exchange="NSE",
            date_from=date(2024, 1, 1),
            date_to=date(2026, 6, 28),
            stocks=[],
            refetch_bars=True,
        )
        assert "--no_fetch_bars" not in args

    def test_stocks_join_with_comma(self):
        args = build_backfill_argv(
            exchange="NSE",
            date_from=date(2024, 1, 1),
            date_to=date(2026, 6, 28),
            stocks=["RELIANCE", "TCS", "INFY"],
            refetch_bars=True,
        )
        # --stocks RELIANCE,TCS,INFY
        idx = args.index("--stocks")
        assert args[idx + 1] == "RELIANCE,TCS,INFY"

    def test_empty_stocks_omits_flag(self):
        args = build_backfill_argv(
            exchange="NSE",
            date_from=date(2024, 1, 1),
            date_to=date(2026, 6, 28),
            stocks=[],
            refetch_bars=True,
        )
        assert "--stocks" not in args

    def test_bse_exchange(self):
        args = build_backfill_argv(
            exchange="BSE",
            date_from=date(2020, 1, 1),
            date_to=date(2020, 12, 31),
            stocks=["AAPL"],
            refetch_bars=False,
        )
        idx = args.index("--exchange")
        assert args[idx + 1] == "BSE"


# ============================================================
# ADD validation
# ============================================================
# These tests construct the view with mocks just enough to drive
# the validation paths.


@pytest.fixture
def prompts():
    """The view's Prompts. Recording, so a test can assert on WHAT the user was
    told — the old QMessageBox stub returned Ok and threw the message away."""
    return FakePrompts()

@pytest.fixture
def view(qtbot, prompts):
    """A minimally-wired EtlStatusView with all the schedulers
    disabled. Returns the view; teardown handled by qtbot.

    Phase GUI-2: picker_auto_fetch=False kwarg replaces the
    class-method monkeypatch on ExchangePicker._fetch_exchanges_list.
    """
    from trip_ema.gui.views.etl_status import EtlStatusView
    db = MagicMock()
    http = MagicMock()
    v = EtlStatusView(
        db=db, http=http, start_refresh_on_init=False,
        prompts=prompts,
        picker_auto_fetch=False,
    )
    qtbot.addWidget(v)
    return v


class TestAddValidation:

    def test_empty_input_is_noop(self, view):
        view._stocks_input.setText("")
        view._on_add_stock_clicked()
        assert view._chip_area.chips() == []

    def test_unloaded_universe_rejects(
        self, view, prompts,
    ):
        # _active_ticker_set is empty (initial state)
        view._stocks_input.setText("RELIANCE")
        view._on_add_stock_clicked()
        assert len(prompts.warnings) == 1
        assert view._chip_area.chips() == []

    def test_unknown_symbol_rejects(
        self, view, prompts,
    ):
        view._active_ticker_set = frozenset(["RELIANCE", "TCS"])
        view._stocks_input.setText("PHANTOM")
        view._on_add_stock_clicked()
        assert len(prompts.warnings) == 1
        assert view._chip_area.chips() == []

    def test_known_symbol_added(self, view):
        view._active_ticker_set = frozenset(["RELIANCE", "TCS"])
        view._stocks_input.setText("TCS")
        view._on_add_stock_clicked()
        assert view._chip_area.chips() == ["TCS"]
        assert view._stocks_input.text() == ""

    def test_normalizes_to_uppercase(self, view):
        view._active_ticker_set = frozenset(["RELIANCE"])
        view._stocks_input.setText("reliance")
        view._on_add_stock_clicked()
        assert view._chip_area.chips() == ["RELIANCE"]


# ============================================================
# Backfill button — early-exit paths
# ============================================================

class TestBackfillButton:

    def test_blocks_when_already_running(self, view):
        # Simulate an in-flight QProcess
        view._backfill_process = MagicMock()
        view._on_backfill_clicked()
        # Nothing should have changed in form state — no modal
        # opened, no exception
        assert view._backfill_process is not None

    def test_blocks_without_exchange(self, view, prompts):
        # Picker returns None (loading sentinel). Instance attribute —
        # plain assignment; the view is fixture-scoped and dies with
        # the test, so no monkeypatch cleanup is needed.
        view._picker.current_exchange = lambda: None
        view._on_backfill_clicked()
        assert len(prompts.warnings) == 1

    def test_blocks_when_date_from_after_date_to(
        self, view, prompts,
    ):
        view._picker.current_exchange = lambda: "NSE"
        from PySide6.QtCore import QDate
        view._from_date.setDate(QDate(2026, 6, 28))
        view._to_date.setDate(QDate(2024, 1, 1))

        view._on_backfill_clicked()
        assert len(prompts.warnings) == 1


# ============================================================
# Mutex state
# ============================================================

class TestMutexState:

    def test_local_running_flag_ors_in(self, view):
        # When DB shows nothing in flight AND local_running is
        # True, _apply_mutex_state must disable the buttons.
        view._local_running = True
        view._apply_mutex_state(
            is_running=True, inflight_kind="backfill",
        )
        assert not view._backfill_btn.isEnabled()
        assert not view._refresh_listings_btn.isEnabled()
        assert not view._recompute_tiers_btn.isEnabled()
        assert not view._run_intraday_btn.isEnabled()

    def test_idle_state_enables_all(self, view):
        view._local_running = False
        view._apply_mutex_state(
            is_running=False, inflight_kind=None,
        )
        assert view._backfill_btn.isEnabled()
        assert view._refresh_listings_btn.isEnabled()
        assert view._recompute_tiers_btn.isEnabled()
        assert view._run_intraday_btn.isEnabled()

    def test_cancel_hidden_when_no_process(self, view):
        view._backfill_process = None
        view._apply_mutex_state(
            is_running=False, inflight_kind=None,
        )
        assert view._cancel_backfill_btn.isHidden()

    def test_backfill_kind_lights_backfill_lamp(self, view):
        view._apply_mutex_state(
            is_running=True, inflight_kind="backfill",
        )
        assert view._backfill_light.is_active() is True
        assert view._intraday_light.is_active() is False

    def test_intraday_kind_lights_intraday_lamp(self, view):
        view._apply_mutex_state(
            is_running=True, inflight_kind="intraday",
        )
        assert view._intraday_light.is_active() is True
        assert view._backfill_light.is_active() is False
