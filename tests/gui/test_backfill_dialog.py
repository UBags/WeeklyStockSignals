"""Phase 15d GUI tests: BackfillDialog.

The dialog is a small pure widget — no workers, no http calls — so
the tests are accordingly simple. Coverage:

  - Defaults: start date is today minus 30 days; tier checkbox is
    off; spinbox is disabled.
  - Tier checkbox toggles the spinbox enable state.
  - `values()` returns (date, None) when checkbox is off, even if
    the spinbox shows a number.
  - `values()` returns (date, int) when checkbox is on.
  - Date range constraints: minimum 1990-01-01, maximum today.
  - The window title includes the exchange name.

The dialog isn't shown via exec() in these tests — that would
spin a modal loop. Instead we instantiate, drive the widget state
directly, and call `values()`. The Accepted/Rejected branch in
EtlStatusView's _on_backfill_clicked is exercised separately
(integration concern, not a dialog concern).
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from trip_ema.gui.views.widgets.backfill_dialog import BackfillDialog


def _add(qtbot, widget):
    qtbot.addWidget(widget)
    return widget


# ============================================================
# Defaults
# ============================================================


class TestDefaults:

    def test_default_start_is_today_minus_30_days(self, qtbot):
        dialog = BackfillDialog(exchange="NSE")
        _add(qtbot, dialog)
        start, tier = dialog.values()
        expected = date.today() - timedelta(days=30)
        assert start == expected
        assert tier is None

    def test_default_tier_checkbox_off(self, qtbot):
        dialog = BackfillDialog(exchange="NSE")
        _add(qtbot, dialog)
        assert dialog._tier_checkbox.isChecked() is False

    def test_default_tier_spinbox_disabled(self, qtbot):
        """Off until the checkbox enables it. Operators shouldn't be
        able to set a tier value without the deliberate act of
        ticking the checkbox."""
        dialog = BackfillDialog(exchange="NSE")
        _add(qtbot, dialog)
        assert dialog._tier_spinbox.isEnabled() is False

    def test_default_tier_spinbox_value_is_1(self, qtbot):
        """Cosmetic default. T1 is the smallest set and the most
        common choice when an operator does restrict."""
        dialog = BackfillDialog(exchange="NSE")
        _add(qtbot, dialog)
        assert dialog._tier_spinbox.value() == 1

    def test_title_includes_exchange(self, qtbot):
        """Operators may have multiple GUI windows open. The title
        bar disambiguates."""
        dialog = BackfillDialog(exchange="NYSE")
        _add(qtbot, dialog)
        assert "NYSE" in dialog.windowTitle()


# ============================================================
# Tier checkbox / spinbox interaction
# ============================================================


class TestTierToggle:

    def test_checkbox_on_enables_spinbox(self, qtbot):
        dialog = BackfillDialog(exchange="NSE")
        _add(qtbot, dialog)
        dialog._tier_checkbox.setChecked(True)
        assert dialog._tier_spinbox.isEnabled() is True

    def test_checkbox_off_disables_spinbox(self, qtbot):
        dialog = BackfillDialog(exchange="NSE")
        _add(qtbot, dialog)
        dialog._tier_checkbox.setChecked(True)
        dialog._tier_checkbox.setChecked(False)
        assert dialog._tier_spinbox.isEnabled() is False

    def test_values_returns_none_tier_when_checkbox_off(self, qtbot):
        """Even if the spinbox displays a value (it always does —
        QSpinBox always has a current value), `values()` should
        report None when the checkbox is off."""
        dialog = BackfillDialog(exchange="NSE")
        _add(qtbot, dialog)
        # Set spinbox to 2 but leave checkbox off
        dialog._tier_spinbox.setValue(2)
        _, tier = dialog.values()
        assert tier is None

    def test_values_returns_int_tier_when_checkbox_on(self, qtbot):
        dialog = BackfillDialog(exchange="NSE")
        _add(qtbot, dialog)
        dialog._tier_checkbox.setChecked(True)
        dialog._tier_spinbox.setValue(3)
        _, tier = dialog.values()
        assert tier == 3


# ============================================================
# Date constraints
# ============================================================


class TestDateConstraints:

    def test_minimum_date_is_1990(self, qtbot):
        """1990-01-01 is the floor. Operators trying to backfill
        further back are almost certainly typos."""
        dialog = BackfillDialog(exchange="NSE")
        _add(qtbot, dialog)
        min_qd = dialog._date_edit.minimumDate()
        assert min_qd.year() == 1990
        assert min_qd.month() == 1
        assert min_qd.day() == 1

    def test_maximum_date_is_today(self, qtbot):
        """Future start_dates are nonsense — they'd produce an
        empty backfill window."""
        dialog = BackfillDialog(exchange="NSE")
        _add(qtbot, dialog)
        max_qd = dialog._date_edit.maximumDate()
        today = date.today()
        assert max_qd.year() == today.year
        assert max_qd.month() == today.month
        assert max_qd.day() == today.day

    def test_can_override_default_start(self, qtbot):
        """Passing default_start lets the caller (or future
        integrations) preset the date — e.g. derive it from the last
        successful run."""
        custom = date(2024, 1, 15)
        dialog = BackfillDialog(exchange="NSE", default_start=custom)
        _add(qtbot, dialog)
        start, _ = dialog.values()
        assert start == custom


# ============================================================
# Full round-trip via values()
# ============================================================


class TestValuesRoundTrip:

    def test_picks_up_user_changes_to_date(self, qtbot):
        from PySide6.QtCore import QDate
        dialog = BackfillDialog(exchange="NSE")
        _add(qtbot, dialog)
        dialog._date_edit.setDate(QDate(2023, 6, 1))
        start, _ = dialog.values()
        assert start == date(2023, 6, 1)

    def test_combined_date_and_tier(self, qtbot):
        from PySide6.QtCore import QDate
        dialog = BackfillDialog(exchange="NSE")
        _add(qtbot, dialog)
        dialog._date_edit.setDate(QDate(2022, 11, 30))
        dialog._tier_checkbox.setChecked(True)
        dialog._tier_spinbox.setValue(2)
        start, tier = dialog.values()
        assert start == date(2022, 11, 30)
        assert tier == 2
