"""About dialog for the trip_ema operator console (Slice 4).

Modal dialog reached via Help → About on the MainWindow's menu bar.
Shows the trip_ema version, Python/Qt versions, and a short blurb.
"""

from __future__ import annotations

import sys

from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QLabel, QVBoxLayout, QWidget,
)


def show_about_dialog(parent: QWidget | None = None) -> None:
    """Construct and exec_() the dialog. Called from a menu action."""
    dlg = AboutDialog(parent)
    dlg.exec()


class AboutDialog(QDialog):

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("About trip_ema")
        self.setModal(True)
        self.setMinimumWidth(420)
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(20, 20, 20, 20)

        # Title
        title = QLabel("trip_ema operator console")
        title_font = QFont()
        title_font.setPointSize(15)
        title_font.setBold(True)
        title.setFont(title_font)
        layout.addWidget(title)

        # Version line
        version_text = f"Version {_version()}"
        version = QLabel(version_text)
        layout.addWidget(version)

        # Description
        desc = QLabel(
            "Operator console for trip_ema — Python port of the "
            "Pine Script Triple-EMA+ATR indicator. Provides ETL "
            "control, ticker management, chart viewing, subscriber "
            "admin, and system-health monitoring for the trip_ema "
            "headless service."
        )
        desc.setWordWrap(True)
        desc.setStyleSheet("color: #aaa; padding-top: 6px;")
        layout.addWidget(desc)

        # Runtime info
        runtime = QLabel(
            f"<small>"
            f"Python {sys.version.split()[0]}<br>"
            f"Qt {_qt_version()} / PySide6 {_pyside_version()}<br>"
            f"pyqtgraph {_pyqtgraph_version()}"
            f"</small>"
        )
        runtime.setStyleSheet("color: #888; padding-top: 6px;")
        layout.addWidget(runtime)

        # Close button
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)


# ---------- helpers ---------- #


def _version() -> str:
    """trip_ema's own version. Falls back gracefully if not exposed."""
    try:
        from trip_ema import __version__
        return __version__
    except ImportError:
        return "(unknown)"


def _qt_version() -> str:
    try:
        from PySide6.QtCore import qVersion
        return qVersion()
    except Exception:
        return "(unknown)"


def _pyside_version() -> str:
    try:
        import PySide6
        return PySide6.__version__
    except Exception:
        return "(unknown)"


def _pyqtgraph_version() -> str:
    try:
        import pyqtgraph
        return pyqtgraph.__version__
    except Exception:
        return "(not installed)"
