"""Settings view (Slice 4).

Two sections in one tab:

  Top — GUI preferences:
    - ETL refresh cadence (seconds)
    - Tickers refresh cadence (seconds)
    - Notifications refresh cadence (seconds)
    - Health refresh cadence (seconds)
    - Default chart time range
  These persist to QSettings; they take effect on next launch.
  A note explains "changes apply on restart" so the user isn't
  surprised that the live tab's cadence didn't change.

  Bottom — Service config (read-only):
    - Path to the loaded config.yaml
    - Full YAML dump in a monospace text area
    - [Backup config] button — copies the YAML to a user-chosen path
    - [Reload from disk] button — re-reads the current config file
      into the viewer (the running service is unaffected)

The Settings tab is intentionally read-mostly. We don't try to edit
config.yaml from the GUI — that's a sharp knife better wielded with
an editor and a restart.
"""

from __future__ import annotations

import logging
import shutil
from datetime import (
    UTC,
    datetime,
)
from pathlib import Path

from PySide6.QtCore import QSettings
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QComboBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout,
    QLabel, QMessageBox, QPushButton, QSpinBox, QTextEdit,
    QVBoxLayout, QWidget,
)

logger = logging.getLogger(__name__)


# Bridge between QSettings keys and field defaults. Keep names stable
# — changing them orphans the user's stored values.
SETTINGS_GROUP = "trip_ema/gui"

KEY_ETL_REFRESH = "etl_refresh_seconds"
KEY_TICKERS_REFRESH = "tickers_refresh_seconds"
KEY_NOTIFICATIONS_REFRESH = "notifications_refresh_seconds"
KEY_HEALTH_REFRESH = "health_refresh_seconds"
KEY_CHART_DEFAULT_YEARS = "chart_default_years"

# Phase 14.6c: the last exchange the operator targeted in the ETL
# Status tab's picker. String, not int — handled by the dedicated
# `load_last_selected_exchange` / `save_last_selected_exchange`
# helpers below rather than the int-coercing `load_gui_settings`.
KEY_LAST_SELECTED_EXCHANGE = "last_selected_exchange"


DEFAULT_SETTINGS = {
    KEY_ETL_REFRESH: 5,
    KEY_TICKERS_REFRESH: 30,
    KEY_NOTIFICATIONS_REFRESH: 60,
    KEY_HEALTH_REFRESH: 10,
    KEY_CHART_DEFAULT_YEARS: 3,
}


def load_gui_settings() -> dict:
    """Read all GUI prefs from QSettings, falling back to defaults.
    Other parts of the GUI can call this at view construction time
    to pick up the user's cadence preferences."""
    s = QSettings()
    s.beginGroup(SETTINGS_GROUP)
    out: dict = {}
    for key, default in DEFAULT_SETTINGS.items():
        value = s.value(key, default)
        # QSettings round-trips strings; coerce ints.
        try:
            out[key] = int(value)
        except (TypeError, ValueError):
            out[key] = default
    s.endGroup()
    return out


def save_gui_settings(values: dict) -> None:
    """Persist GUI prefs. Caller passes a dict matching the
    DEFAULT_SETTINGS keys."""
    s = QSettings()
    s.beginGroup(SETTINGS_GROUP)
    for key, value in values.items():
        s.setValue(key, int(value))
    s.endGroup()
    s.sync()


# Phase 14.6c — string-typed pref helpers for the last-selected
# exchange. Separate from load_gui_settings/save_gui_settings
# because those round-trip everything through int(). Keeps the
# existing int-only API stable; this is a parallel surface for
# string values that may grow in future (e.g. last-selected
# symbol on the Signals tab, last-opened tab name).


def load_last_selected_exchange() -> str | None:
    """Return the operator's last-picked exchange in the ETL Status
    tab, or None if nothing's been saved yet (first launch).

    Returns a string (never an int) — the QSettings round-trip
    naturally preserves strings if save_last_selected_exchange was
    used. Returns None for both 'never saved' and 'saved but empty'."""
    s = QSettings()
    s.beginGroup(SETTINGS_GROUP)
    value = s.value(KEY_LAST_SELECTED_EXCHANGE, None)
    s.endGroup()
    if value is None or value == "":
        return None
    return str(value)


def save_last_selected_exchange(exchange: str) -> None:
    """Persist the operator's exchange selection for next launch.
    Caller is responsible for validating that `exchange` is a real
    exchange code; this just writes whatever it's given."""
    s = QSettings()
    s.beginGroup(SETTINGS_GROUP)
    s.setValue(KEY_LAST_SELECTED_EXCHANGE, str(exchange))
    s.endGroup()
    s.sync()


class SettingsView(QWidget):
    """GUI prefs + read-only config viewer + backup action."""

    def __init__(
        self,
        *,
        config_path: Path | None = None,
        parent: QWidget | None = None,
    ):
        """Args:
          config_path: Path to the loaded config.yaml. Used by the
            viewer + backup action. None → viewer shows '(no config
            path provided)'."""
        super().__init__(parent)
        self._config_path = config_path
        self._dirty_prefs = False

        self._build_ui()
        self._load_current_prefs_into_form()
        self._load_config_into_viewer()

    # ---------- UI ---------- #

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)
        outer.setSpacing(12)

        outer.addWidget(self._build_prefs_section())
        outer.addWidget(self._build_config_section(), 1)

    def _build_prefs_section(self) -> QWidget:
        box = QGroupBox("GUI preferences")
        outer = QVBoxLayout(box)

        form = QFormLayout()
        # All four refresh cadences are SpinBoxes with sensible ranges
        self._etl_spin = self._make_seconds_spin(min_=1, max_=600)
        form.addRow("ETL Status refresh (s):", self._etl_spin)

        self._tickers_spin = self._make_seconds_spin(min_=5, max_=600)
        form.addRow("Tickers refresh (s):", self._tickers_spin)

        self._notifications_spin = self._make_seconds_spin(min_=5, max_=600)
        form.addRow("Notifications refresh (s):", self._notifications_spin)

        self._health_spin = self._make_seconds_spin(min_=2, max_=600)
        form.addRow("Health refresh (s):", self._health_spin)

        # Default chart years dropdown
        self._chart_years_combo = QComboBox()
        for y, label in [(1, "1Y"), (3, "3Y"), (5, "5Y"), (10, "10Y"), (20, "All")]:
            self._chart_years_combo.addItem(label, y)
        self._chart_years_combo.currentIndexChanged.connect(self._mark_dirty)
        form.addRow("Default chart range:", self._chart_years_combo)

        outer.addLayout(form)

        note = QLabel(
            "Refresh cadences apply on next launch. The Settings panel "
            "doesn't restart the running views — close and re-open "
            "trip-ema-console to pick up changes."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: #888; font-style: italic; padding: 4px 0;")
        outer.addWidget(note)

        button_row = QHBoxLayout()
        self._save_prefs_button = QPushButton("Save preferences")
        self._save_prefs_button.clicked.connect(self._on_save_prefs_clicked)
        self._save_prefs_button.setEnabled(False)
        button_row.addWidget(self._save_prefs_button)
        self._reset_prefs_button = QPushButton("Reset to defaults")
        self._reset_prefs_button.clicked.connect(self._on_reset_prefs_clicked)
        button_row.addWidget(self._reset_prefs_button)
        button_row.addStretch(1)
        outer.addLayout(button_row)

        return box

    def _make_seconds_spin(self, *, min_: int, max_: int) -> QSpinBox:
        sp = QSpinBox()
        sp.setRange(min_, max_)
        sp.setSuffix(" s")
        sp.valueChanged.connect(self._mark_dirty)
        return sp

    def _build_config_section(self) -> QWidget:
        box = QGroupBox("Service config (read-only)")
        outer = QVBoxLayout(box)

        # Path display
        path_row = QHBoxLayout()
        path_row.addWidget(QLabel("Loaded from:"))
        self._path_label = QLabel()
        self._path_label.setStyleSheet("font-family: monospace; color: #aaa;")
        path_row.addWidget(self._path_label, 1)
        outer.addLayout(path_row)

        # Config YAML viewer
        self._config_viewer = QTextEdit()
        self._config_viewer.setReadOnly(True)
        viewer_font = QFont("monospace")
        viewer_font.setPointSize(11)
        self._config_viewer.setFont(viewer_font)
        self._config_viewer.setStyleSheet(
            "background-color: #1a1a1a; color: #ddd; padding: 6px;"
        )
        outer.addWidget(self._config_viewer, 1)

        # Action buttons
        button_row = QHBoxLayout()
        self._reload_button = QPushButton("Reload from disk")
        self._reload_button.clicked.connect(self._on_reload_clicked)
        button_row.addWidget(self._reload_button)

        self._backup_button = QPushButton("Backup config…")
        self._backup_button.clicked.connect(self._on_backup_clicked)
        button_row.addWidget(self._backup_button)

        button_row.addStretch(1)
        self._config_status_label = QLabel("")
        self._config_status_label.setStyleSheet(
            "color: #888; font-style: italic;"
        )
        button_row.addWidget(self._config_status_label)
        outer.addLayout(button_row)

        return box

    # ---------- prefs handlers ---------- #

    def _load_current_prefs_into_form(self) -> None:
        prefs = load_gui_settings()
        # Block signals so loading doesn't set dirty
        self._with_signals_blocked(lambda: self._set_form_values(prefs))
        self._dirty_prefs = False
        self._save_prefs_button.setEnabled(False)

    def _set_form_values(self, prefs: dict) -> None:
        self._etl_spin.setValue(prefs[KEY_ETL_REFRESH])
        self._tickers_spin.setValue(prefs[KEY_TICKERS_REFRESH])
        self._notifications_spin.setValue(prefs[KEY_NOTIFICATIONS_REFRESH])
        self._health_spin.setValue(prefs[KEY_HEALTH_REFRESH])
        # chart_default_years: find combo entry by userData
        target = prefs[KEY_CHART_DEFAULT_YEARS]
        for i in range(self._chart_years_combo.count()):
            if self._chart_years_combo.itemData(i) == target:
                self._chart_years_combo.setCurrentIndex(i)
                break

    def _with_signals_blocked(self, fn) -> None:
        widgets = [
            self._etl_spin, self._tickers_spin, self._notifications_spin,
            self._health_spin, self._chart_years_combo,
        ]
        for w in widgets:
            w.blockSignals(True)
        try:
            fn()
        finally:
            for w in widgets:
                w.blockSignals(False)

    def _mark_dirty(self, *_args) -> None:
        self._dirty_prefs = True
        self._save_prefs_button.setEnabled(True)

    def _on_save_prefs_clicked(self) -> None:
        save_gui_settings({
            KEY_ETL_REFRESH: self._etl_spin.value(),
            KEY_TICKERS_REFRESH: self._tickers_spin.value(),
            KEY_NOTIFICATIONS_REFRESH: self._notifications_spin.value(),
            KEY_HEALTH_REFRESH: self._health_spin.value(),
            KEY_CHART_DEFAULT_YEARS: self._chart_years_combo.currentData(),
        })
        self._dirty_prefs = False
        self._save_prefs_button.setEnabled(False)
        QMessageBox.information(
            self, "Preferences saved",
            "GUI preferences saved. Close and reopen trip-ema-console "
            "to pick them up.",
        )

    def _on_reset_prefs_clicked(self) -> None:
        confirm = QMessageBox.question(
            self, "Reset preferences?",
            "Restore all GUI preferences to their default values?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return
        self._with_signals_blocked(lambda: self._set_form_values(DEFAULT_SETTINGS))
        self._dirty_prefs = True
        self._save_prefs_button.setEnabled(True)

    # ---------- config viewer handlers ---------- #

    def _load_config_into_viewer(self) -> None:
        """Read config.yaml from disk into the text area. We display the
        raw file rather than the parsed Pydantic model so the user sees
        their config exactly as it lives on disk (comments preserved)."""
        if self._config_path is None or not self._config_path.exists():
            self._path_label.setText("(no config path provided)")
            self._config_viewer.setPlainText("")
            self._backup_button.setEnabled(False)
            return
        self._path_label.setText(str(self._config_path))
        try:
            text = self._config_path.read_text(encoding="utf-8")
            self._config_viewer.setPlainText(text)
            self._config_status_label.setText(
                f"Loaded {len(text):,} bytes."
            )
            self._backup_button.setEnabled(True)
        except Exception as e:
            self._config_viewer.setPlainText(f"# Error reading config:\n# {e}")
            self._backup_button.setEnabled(False)

    def _on_reload_clicked(self) -> None:
        self._load_config_into_viewer()

    def _on_backup_clicked(self) -> None:
        """Copy config.yaml to a user-chosen location. Default
        filename is timestamped so successive backups don't collide."""
        if self._config_path is None or not self._config_path.exists():
            QMessageBox.warning(
                self, "No config to back up",
                "The current config path is not set or does not exist.",
            )
            return
        # L8-NO-MACHINE-CLOCK: was datetime.now() — naive, and the machine's clock.
        # A config file belongs to no exchange, so there is no market_now() to reach
        # for; UTC is the honest answer, it sorts correctly, and the Z says so out
        # loud instead of leaving the reader to guess whose midnight this was.
        ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%SZ")
        suggested = str(
            Path.cwd() / f"{self._config_path.stem}_{ts}.yaml"
        )
        path_str, _ = QFileDialog.getSaveFileName(
            self, "Backup config", suggested,
            "YAML files (*.yaml *.yml);;All files (*)",
        )
        if not path_str:
            return
        try:
            shutil.copy2(self._config_path, path_str)
            self._config_status_label.setText(
                f"Backed up to {path_str}"
            )
        except Exception as e:
            QMessageBox.warning(
                self, "Backup failed",
                f"Could not copy config to {path_str}:\n\n{e}",
            )
            return

    # ---------- lifecycle ---------- #

    def on_close(self) -> None:
        """Nothing to clean up — no refresh timer, no workers."""
        if self._dirty_prefs:
            # Could prompt the user; for simplicity we just lose the
            # unsaved edits. The Save button being enabled is a clear
            # visual cue.
            logger.debug("Settings view closed with unsaved pref changes")
