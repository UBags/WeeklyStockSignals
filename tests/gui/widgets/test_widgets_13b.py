"""Step 13b widget unit tests.

Pure-Qt tests for the small widgets introduced by the ETL Status
view rewrite. No DB, no HTTP — each test constructs the widget,
exercises its API, and verifies state transitions.
"""
from __future__ import annotations

import pytest

from trip_ema.gui.views.widgets.flashing_light import FlashingLight
from trip_ema.gui.views.widgets.stage_progress import (
    LIVE_CYCLE_STAGES, StageProgressList, StageRow, StageStatus,
)
from trip_ema.gui.views.widgets.ticker_chip import (
    TickerChip, TickerChipArea,
)


# ============================================================
# FlashingLight
# ============================================================

class TestFlashingLight:

    def test_starts_inactive(self, qtbot):
        light = FlashingLight()
        qtbot.addWidget(light)
        assert light.is_active() is False

    def test_set_active_true_starts_pulse(self, qtbot):
        light = FlashingLight()
        qtbot.addWidget(light)
        light.set_active(True)
        assert light.is_active() is True

    def test_set_active_false_stops_pulse(self, qtbot):
        light = FlashingLight()
        qtbot.addWidget(light)
        light.set_active(True)
        light.set_active(False)
        assert light.is_active() is False

    def test_set_active_same_state_is_noop(self, qtbot):
        light = FlashingLight()
        qtbot.addWidget(light)
        light.set_active(False)  # already false
        assert light.is_active() is False
        light.set_active(True)
        light.set_active(True)   # already true
        assert light.is_active() is True


# ============================================================
# TickerChip + TickerChipArea
# ============================================================

class TestTickerChip:

    def test_symbol_accessor(self, qtbot):
        chip = TickerChip("RELIANCE")
        qtbot.addWidget(chip)
        assert chip.symbol() == "RELIANCE"

    def test_remove_signal_emitted(self, qtbot):
        chip = TickerChip("RELIANCE")
        qtbot.addWidget(chip)
        with qtbot.waitSignal(
            chip.remove_requested, timeout=500,
        ) as blocker:
            # Find the ✕ button and click it
            from PySide6.QtWidgets import QPushButton
            for child in chip.findChildren(QPushButton):
                if child.text() == "✕":
                    child.click()
                    break
        assert blocker.args == ["RELIANCE"]


class TestTickerChipArea:

    def test_starts_empty(self, qtbot):
        area = TickerChipArea()
        qtbot.addWidget(area)
        assert area.is_empty()
        assert area.chips() == []

    def test_add_chip_appends(self, qtbot):
        area = TickerChipArea()
        qtbot.addWidget(area)
        assert area.add_chip("RELIANCE") is True
        assert area.chips() == ["RELIANCE"]

    def test_add_chip_normalizes_uppercase(self, qtbot):
        area = TickerChipArea()
        qtbot.addWidget(area)
        area.add_chip("reliance")
        assert area.chips() == ["RELIANCE"]

    def test_add_duplicate_is_noop(self, qtbot):
        area = TickerChipArea()
        qtbot.addWidget(area)
        area.add_chip("RELIANCE")
        assert area.add_chip("RELIANCE") is False
        assert area.chips() == ["RELIANCE"]

    def test_add_empty_string_rejected(self, qtbot):
        area = TickerChipArea()
        qtbot.addWidget(area)
        assert area.add_chip("") is False
        assert area.add_chip("   ") is False
        assert area.chips() == []

    def test_remove_chip(self, qtbot):
        area = TickerChipArea()
        qtbot.addWidget(area)
        area.add_chip("REL")
        area.add_chip("TML")
        area.add_chip("ACC")
        area.remove_chip("TML")
        assert area.chips() == ["REL", "ACC"]

    def test_clear(self, qtbot):
        area = TickerChipArea()
        qtbot.addWidget(area)
        area.add_chip("REL")
        area.add_chip("TML")
        area.clear()
        assert area.is_empty()
        assert area.chips() == []

    def test_chips_changed_signal_fires(self, qtbot):
        area = TickerChipArea()
        qtbot.addWidget(area)
        with qtbot.waitSignal(
            area.chips_changed, timeout=500,
        ):
            area.add_chip("REL")


# ============================================================
# StageRow + StageProgressList
# ============================================================

class TestStageRow:

    def test_default_status_is_queued(self, qtbot):
        row = StageRow("Compute Signals")
        qtbot.addWidget(row)
        assert row.status() == StageStatus.QUEUED

    def test_set_status_changes_status(self, qtbot):
        row = StageRow("Compute Signals")
        qtbot.addWidget(row)
        row.set_status(StageStatus.IN_PROGRESS)
        assert row.status() == StageStatus.IN_PROGRESS

    def test_name_accessor(self, qtbot):
        row = StageRow("Fetch Stock OHLCV")
        qtbot.addWidget(row)
        assert row.name() == "Fetch Stock OHLCV"


class TestStageProgressList:

    def test_initial_state_all_queued(self, qtbot):
        lst = StageProgressList(LIVE_CYCLE_STAGES)
        qtbot.addWidget(lst)
        assert len(lst.rows()) == len(LIVE_CYCLE_STAGES)
        for row in lst.rows():
            assert row.status() == StageStatus.QUEUED

    def test_set_one_stage(self, qtbot):
        lst = StageProgressList(LIVE_CYCLE_STAGES)
        qtbot.addWidget(lst)
        assert lst.set_stage_status(
            "Compute Signals", StageStatus.DONE,
        ) is True
        for row in lst.rows():
            if row.name() == "Compute Signals":
                assert row.status() == StageStatus.DONE
            else:
                assert row.status() == StageStatus.QUEUED

    def test_set_unknown_stage_returns_false(self, qtbot):
        lst = StageProgressList(LIVE_CYCLE_STAGES)
        qtbot.addWidget(lst)
        assert lst.set_stage_status(
            "Nonexistent Stage", StageStatus.DONE,
        ) is False

    def test_all_done_predicate(self, qtbot):
        lst = StageProgressList(LIVE_CYCLE_STAGES)
        qtbot.addWidget(lst)
        assert lst.all_done() is False
        for row in lst.rows():
            row.set_status(StageStatus.DONE)
        assert lst.all_done() is True

    def test_reset_all_returns_to_queued(self, qtbot):
        lst = StageProgressList(LIVE_CYCLE_STAGES)
        qtbot.addWidget(lst)
        for row in lst.rows():
            row.set_status(StageStatus.DONE)
        lst.reset_all()
        for row in lst.rows():
            assert row.status() == StageStatus.QUEUED
