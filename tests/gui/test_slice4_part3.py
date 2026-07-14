"""Tests for the Slice 4 Part 3 GUI components — System Health,
Settings, and About dialog.

Coverage:
  - HealthCardWidget renders status/glyph/headline/detail correctly
  - SystemHealthView builds cards, updates them on successive polls,
    handles unreachable backend gracefully
  - QSettings round-trip: load → save → load gets the same values
  - SettingsView populates form from QSettings, marks dirty on edit,
    saves correctly, reset-to-defaults works
  - Settings config viewer renders file contents
  - Backup-config writes to chosen path (via an injected FakePrompts)
  - AboutDialog constructs and shows version info
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from PySide6.QtCore import QCoreApplication, QSettings, Qt
from PySide6.QtWidgets import QApplication

from .fakes import FakePrompts

from trip_ema.gui.about_dialog import AboutDialog
from trip_ema.gui.views.settings import (
    DEFAULT_SETTINGS, KEY_CHART_DEFAULT_YEARS, KEY_ETL_REFRESH,
    KEY_HEALTH_REFRESH, KEY_NOTIFICATIONS_REFRESH, KEY_TICKERS_REFRESH,
    SettingsView, load_gui_settings, save_gui_settings,
)
from trip_ema.gui.views.system_health import (
    HealthCardWidget, STATUS_GLYPHS, SystemHealthView,
)


# ============================================================
# Test-isolation: each test gets its own QSettings org/app
# ============================================================


@pytest.fixture(autouse=True)
def isolated_qsettings(monkeypatch, tmp_path, request):
    """QSettings persists across tests, which means save_gui_settings
    in one test would bleed into the next. We pin org/app to a unique
    name per test so we don't have cross-test contamination."""
    # Use the test name to make settings unique per test
    org = f"trip_ema_test_{request.node.name}"
    QCoreApplication.setOrganizationName(org)
    QCoreApplication.setApplicationName(org)
    yield
    # Cleanup: clear our settings group so nothing lingers
    s = QSettings()
    s.clear()
    s.sync()


# ============================================================
# HealthCardWidget
# ============================================================


class TestHealthCardWidget:

    def test_constructs_with_unknown_status(self, qtbot):
        w = HealthCardWidget()
        qtbot.addWidget(w)
        # Initial state populated in __init__
        assert "Loading" in w._headline_label.text()

    def test_set_card_populates_fields(self, qtbot):
        w = HealthCardWidget()
        qtbot.addWidget(w)
        w.set_card({
            "name": "Ticker universe",
            "status": "ok",
            "headline": "2300 active tickers",
            "detail": "5 delisted, 7 parked",
        })
        assert w._name_label.text() == "Ticker universe"
        assert w._headline_label.text() == "2300 active tickers"
        assert w._detail_label.text() == "5 delisted, 7 parked"
        # ok-status glyph
        assert w._glyph_label.text() == STATUS_GLYPHS["ok"]

    def test_set_card_hides_detail_when_none(self, qtbot):
        w = HealthCardWidget()
        qtbot.addWidget(w)
        w.set_card({
            "name": "X", "status": "ok", "headline": "Hi", "detail": None,
        })
        assert not w._detail_label.isVisible() or w._detail_label.text() == ""

    def test_status_changes_background_color(self, qtbot):
        w = HealthCardWidget()
        qtbot.addWidget(w)
        w.set_card({"name": "X", "status": "crit", "headline": "Bad"})
        crit_style = w.styleSheet()
        w.set_card({"name": "X", "status": "ok", "headline": "Good"})
        ok_style = w.styleSheet()
        assert crit_style != ok_style

    def test_unknown_status_doesnt_crash(self, qtbot):
        w = HealthCardWidget()
        qtbot.addWidget(w)
        # Make sure an unknown status string falls back gracefully
        w.set_card({"name": "X", "status": "nonsense", "headline": "?"})
        # Glyph defaults to '?'
        assert w._glyph_label.text() in ("?", "—")


# ============================================================
# SystemHealthView
# ============================================================


def _make_health_view():
    db = MagicMock()
    http = MagicMock()
    return SystemHealthView(
        db=db, http=http, start_refresh_on_init=False,
    )


class TestSystemHealthView:

    def test_constructs(self, qtbot):
        v = _make_health_view()
        qtbot.addWidget(v)
        # No cards before any snapshot received
        assert len(v._cards) == 0
        # Overall label has initial placeholder
        assert "Overall" in v._overall_label.text()

    def test_render_cards_creates_widgets(self, qtbot):
        v = _make_health_view()
        qtbot.addWidget(v)
        v._on_snapshot_received({
            "overall_status": "ok",
            "cards": [
                {"name": "A", "status": "ok", "headline": "A ok"},
                {"name": "B", "status": "warn", "headline": "B warn"},
                {"name": "C", "status": "ok", "headline": "C ok"},
            ],
        })
        assert len(v._cards) == 3
        # First card populated
        assert v._cards[0]._name_label.text() == "A"

    def test_render_cards_reuses_widgets_across_refresh(self, qtbot):
        """On successive polls, the widget instances should be reused
        rather than rebuilt — verify by checking object identity."""
        v = _make_health_view()
        qtbot.addWidget(v)
        v._on_snapshot_received({
            "overall_status": "ok",
            "cards": [
                {"name": "A", "status": "ok", "headline": "h1"},
                {"name": "B", "status": "ok", "headline": "h2"},
            ],
        })
        first_a = v._cards[0]
        first_b = v._cards[1]
        # Second poll with same number of cards
        v._on_snapshot_received({
            "overall_status": "warn",
            "cards": [
                {"name": "A", "status": "warn", "headline": "h1-changed"},
                {"name": "B", "status": "ok", "headline": "h2"},
            ],
        })
        # Same widgets, updated contents
        assert v._cards[0] is first_a
        assert v._cards[1] is first_b
        assert "h1-changed" in v._cards[0]._headline_label.text()

    def test_fewer_cards_in_next_poll_hides_extras(self, qtbot):
        v = _make_health_view()
        qtbot.addWidget(v)
        v._on_snapshot_received({
            "overall_status": "ok",
            "cards": [
                {"name": "A", "status": "ok", "headline": "h"},
                {"name": "B", "status": "ok", "headline": "h"},
                {"name": "C", "status": "ok", "headline": "h"},
            ],
        })
        assert len(v._cards) == 3
        # Next poll has only 2 cards
        v._on_snapshot_received({
            "overall_status": "ok",
            "cards": [
                {"name": "A", "status": "ok", "headline": "h"},
                {"name": "B", "status": "ok", "headline": "h"},
            ],
        })
        # Widget count stays at 3 but third is hidden
        assert len(v._cards) == 3
        assert v._cards[2].isHidden()

    def test_failure_shows_error_card(self, qtbot):
        v = _make_health_view()
        qtbot.addWidget(v)
        v._on_snapshot_failed("network error: connection refused")
        # One card showing the failure
        assert len(v._cards) >= 1
        # And the overall label reflects the failure
        assert "UNREACHABLE" in v._overall_label.text()

    def test_overall_status_reflects_payload(self, qtbot):
        v = _make_health_view()
        qtbot.addWidget(v)
        v._on_snapshot_received({
            "overall_status": "crit",
            "cards": [{"name": "X", "status": "crit", "headline": "down"}],
        })
        assert "CRIT" in v._overall_label.text()


# ============================================================
# Settings QSettings round-trip
# ============================================================


class TestSettingsPersistence:

    def test_load_returns_defaults_when_empty(self, qtbot):
        # Fresh QSettings (isolated by fixture) — should give defaults
        prefs = load_gui_settings()
        for key, default in DEFAULT_SETTINGS.items():
            assert prefs[key] == default

    def test_save_then_load_roundtrips(self, qtbot):
        save_gui_settings({
            KEY_ETL_REFRESH: 12,
            KEY_TICKERS_REFRESH: 60,
            KEY_NOTIFICATIONS_REFRESH: 120,
            KEY_HEALTH_REFRESH: 30,
            KEY_CHART_DEFAULT_YEARS: 10,
        })
        prefs = load_gui_settings()
        assert prefs[KEY_ETL_REFRESH] == 12
        assert prefs[KEY_TICKERS_REFRESH] == 60
        assert prefs[KEY_NOTIFICATIONS_REFRESH] == 120
        assert prefs[KEY_HEALTH_REFRESH] == 30
        assert prefs[KEY_CHART_DEFAULT_YEARS] == 10

    def test_save_partial_preserves_others(self, qtbot):
        save_gui_settings({KEY_ETL_REFRESH: 7})
        prefs = load_gui_settings()
        assert prefs[KEY_ETL_REFRESH] == 7
        # Others stay at defaults
        assert prefs[KEY_TICKERS_REFRESH] == DEFAULT_SETTINGS[KEY_TICKERS_REFRESH]


# ============================================================
# SettingsView
# ============================================================


def _make_settings_view(config_path=None, prompts=None):
    return SettingsView(
        config_path=config_path,
        prompts=prompts if prompts is not None else FakePrompts(),
    )


class TestSettingsViewBasics:

    def test_constructs_with_no_config_path(self, qtbot):
        v = _make_settings_view(config_path=None)
        qtbot.addWidget(v)
        # No path = label says so, backup button disabled
        assert "no config" in v._path_label.text().lower()
        assert not v._backup_button.isEnabled()
        # Save button is disabled because nothing dirty
        assert not v._save_prefs_button.isEnabled()

    def test_constructs_with_valid_config(self, qtbot, tmp_path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text("database:\n  host: localhost\n")
        v = _make_settings_view(config_path=cfg)
        qtbot.addWidget(v)
        assert "config.yaml" in v._path_label.text()
        # Viewer has the file content
        assert "localhost" in v._config_viewer.toPlainText()
        assert v._backup_button.isEnabled()


class TestSettingsViewForm:

    def test_form_starts_with_current_prefs(self, qtbot):
        save_gui_settings({KEY_ETL_REFRESH: 33})
        v = _make_settings_view()
        qtbot.addWidget(v)
        assert v._etl_spin.value() == 33

    def test_form_starts_with_defaults_when_unset(self, qtbot):
        v = _make_settings_view()
        qtbot.addWidget(v)
        assert v._etl_spin.value() == DEFAULT_SETTINGS[KEY_ETL_REFRESH]

    def test_editing_marks_dirty(self, qtbot):
        v = _make_settings_view()
        qtbot.addWidget(v)
        assert not v._save_prefs_button.isEnabled()
        v._etl_spin.setValue(42)
        assert v._save_prefs_button.isEnabled()
        assert v._dirty_prefs

    def test_save_persists_to_qsettings(self, qtbot):
        prompts = FakePrompts()
        v = _make_settings_view(prompts=prompts)
        qtbot.addWidget(v)
        v._etl_spin.setValue(77)
        v._tickers_spin.setValue(88)
        v._on_save_prefs_clicked()
        # Now load_gui_settings should see those values
        prefs = load_gui_settings()
        assert prefs[KEY_ETL_REFRESH] == 77
        assert prefs[KEY_TICKERS_REFRESH] == 88
        # Save button disabled after save
        assert not v._save_prefs_button.isEnabled()
        # The success dialog fired. The old stub threw this away.
        assert len(prompts.informations) == 1

    def test_reset_to_defaults_marks_dirty(self, qtbot):
        save_gui_settings({KEY_ETL_REFRESH: 99})
        prompts = FakePrompts(confirm_answer=True)   # user clicks Yes
        v = _make_settings_view(prompts=prompts)
        qtbot.addWidget(v)
        # Loaded from QSettings as 99
        assert v._etl_spin.value() == 99
        v._on_reset_prefs_clicked()
        # Now should be at default 5
        assert v._etl_spin.value() == DEFAULT_SETTINGS[KEY_ETL_REFRESH]
        # And marked dirty so user has to click Save
        assert v._save_prefs_button.isEnabled()
        # Reset is destructive: the dialog must NOT default to Yes.
        assert prompts.confirm_defaults == [False]


class TestSettingsViewConfigViewer:

    def test_reload_re_reads_file(self, qtbot, tmp_path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text("a: 1\n")
        v = _make_settings_view(config_path=cfg)
        qtbot.addWidget(v)
        assert "a: 1" in v._config_viewer.toPlainText()
        # Modify the file then reload
        cfg.write_text("a: 999\n")
        v._on_reload_clicked()
        assert "a: 999" in v._config_viewer.toPlainText()

    def test_backup_copies_file(self, qtbot, tmp_path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text("hello: world\n")
        target = tmp_path / "backup.yaml"
        prompts = FakePrompts(save_path=str(target))
        v = _make_settings_view(config_path=cfg, prompts=prompts)
        qtbot.addWidget(v)
        v._on_backup_clicked()
        assert target.exists()
        assert target.read_text() == "hello: world\n"

    def test_backup_cancel_does_nothing(self, qtbot, tmp_path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text("x\n")
        prompts = FakePrompts(save_path=None)        # user cancels
        v = _make_settings_view(config_path=cfg, prompts=prompts)
        qtbot.addWidget(v)
        v._on_backup_clicked()
        # No copy made, no exception

    def test_backup_disabled_when_no_config(self, qtbot):
        v = _make_settings_view(config_path=None)
        qtbot.addWidget(v)
        assert not v._backup_button.isEnabled()


# ============================================================
# About dialog
# ============================================================


class TestAboutDialog:

    def test_constructs(self, qtbot):
        dlg = AboutDialog()
        qtbot.addWidget(dlg)
        assert dlg.windowTitle() == "About trip_ema"

    def test_shows_runtime_versions(self, qtbot):
        dlg = AboutDialog()
        qtbot.addWidget(dlg)
        # Spelunk the QLabel children for the runtime info
        from PySide6.QtWidgets import QLabel
        all_text = " ".join(
            l.text() for l in dlg.findChildren(QLabel)
        )
        # We should see Python and Qt versions
        assert "Python" in all_text
        assert "Qt" in all_text or "PySide6" in all_text
