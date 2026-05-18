"""Main window: sidebar (left) + QStackedWidget (right).

Slice 4 final layout (7 working views):

  1. ETL Status & Control
  2. Tickers
  3. Signals (chart)
  4. Subscribers
  5. Notifications (log)
  6. System Health
  7. Settings (GUI prefs + read-only config viewer)

Plus a Help menu with About → AboutDialog.

User-configurable refresh cadences (from QSettings) are read once at
construction time and passed to each view. Changing them in Settings
takes effect on next launch; the views don't reload their schedulers
mid-session.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QMainWindow, QMenuBar, QStackedWidget, QStatusBar,
    QVBoxLayout, QWidget,
)

from .db import GuiDbSession
from .http_client import TripEmaHttpClient
from .views.etl_status import EtlStatusView
from .views.notifications import NotificationLogView
from .views.settings import (
    KEY_CHART_DEFAULT_YEARS, KEY_ETL_REFRESH, KEY_HEALTH_REFRESH,
    KEY_NOTIFICATIONS_REFRESH, KEY_TICKERS_REFRESH, SettingsView,
    load_gui_settings,
)
from .views.signals import SignalsView
from .views.subscribers import SubscribersView
from .views.system_health import SystemHealthView
from .views.tickers import TickersView

logger = logging.getLogger(__name__)


class MainWindow(QMainWindow):
    """Top-level window. Constructs the sidebar, the stack, and the
    individual views, wiring them to the DB session and HTTP client.

    Designed for ~1400×900. QSettings persists size between launches
    so the user's resize sticks.
    """

    # Cross-tab navigation: emitted by any view that wants to send the
    # user to the Signals tab pre-loaded with a specific ticker. The
    # Tickers tab emits it from its "View chart" button.
    request_chart_for_ticker = Signal(str, str, str)  # exchange, symbol, instrument

    def __init__(
        self,
        db: GuiDbSession,
        http: TripEmaHttpClient,
        *,
        parent: Optional[QWidget] = None,
        config_path: Optional[Path] = None,
        populate_signals_combo: bool = True,
        populate_subscribers_lookup: bool = True,
        start_refresh_on_init: bool = True,
    ):
        super().__init__(parent)
        self._db = db
        self._http = http
        self._config_path = config_path
        self._populate_signals_combo = populate_signals_combo
        self._populate_subscribers_lookup = populate_subscribers_lookup
        self._start_refresh_on_init = start_refresh_on_init

        # Load user prefs once; passed to each view at construction.
        # Changes via the Settings tab apply on next launch (not live).
        self._gui_prefs = load_gui_settings()

        self.setWindowTitle("trip_ema — operator console")
        self.resize(1400, 900)

        self._build_layout()
        self._build_views()
        self._build_menu_bar()
        self._build_status_bar()

        # Land on the first view by default.
        self._sidebar.setCurrentRow(0)

    # ---------- construction ---------- #

    def _build_layout(self) -> None:
        central = QWidget()
        outer = QHBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # Left: sidebar
        self._sidebar = QListWidget()
        self._sidebar.setFixedWidth(220)
        self._sidebar.setObjectName("sidebar")
        self._sidebar.setIconSize(QSize(20, 20))
        self._sidebar.setSpacing(2)
        # Restrained palette so the sidebar reads as a chrome element,
        # not a content panel. The grays match Qt's default frame.
        self._sidebar.setStyleSheet("""
            QListWidget#sidebar {
                background-color: #2b2b2b;
                color: #d9d9d9;
                border: 0;
                font-size: 13px;
                padding: 8px 0;
            }
            QListWidget#sidebar::item {
                padding: 10px 14px;
                border: 0;
            }
            QListWidget#sidebar::item:selected {
                background-color: #3d6fc0;
                color: white;
            }
            QListWidget#sidebar::item:hover:!selected {
                background-color: #3a3a3a;
            }
        """)

        # Right: stack
        self._stack = QStackedWidget()

        outer.addWidget(self._sidebar)
        outer.addWidget(self._stack, 1)

        # Sidebar selection switches the stack page
        self._sidebar.currentRowChanged.connect(self._stack.setCurrentIndex)

        self.setCentralWidget(central)

    def _build_views(self) -> None:
        """Build all 7 views in their final order. Each view is added
        to BOTH the sidebar (as a labeled item) and the stack (as a
        widget). The currentRowChanged signal keeps them in sync."""

        # View 1: ETL Status & Control
        etl_view = EtlStatusView(
            db=self._db, http=self._http,
            start_refresh_on_init=self._start_refresh_on_init,
        )
        self._apply_cadence_pref(etl_view, KEY_ETL_REFRESH)
        self._add_view("ETL Status", "⚙", etl_view)

        # View 2: Tickers
        tickers_view = TickersView(
            db=self._db, http=self._http,
            start_refresh_on_init=self._start_refresh_on_init,
        )
        self._apply_cadence_pref(tickers_view, KEY_TICKERS_REFRESH)
        # Tickers tab emits a "view chart" request on the MainWindow's
        # cross-tab signal bus.
        tickers_view.request_chart.connect(
            self.request_chart_for_ticker.emit
        )
        self._add_view("Tickers", "📊", tickers_view)

        # View 3: Signals/Charts
        self._signals_view = SignalsView(
            db=self._db, http=self._http,
            populate_combo_on_init=self._populate_signals_combo,
        )
        # Apply user's default chart range pref
        default_years = self._gui_prefs.get(KEY_CHART_DEFAULT_YEARS, 3)
        try:
            self._signals_view._current_years = default_years
            # Tick the matching range button so the UI reflects it
            for btn in self._signals_view._range_buttons.buttons():
                btn.setChecked(btn.property("years") == default_years)
        except Exception:
            logger.debug("Could not apply chart_default_years pref")
        self._signals_stack_index = self._stack.count()
        self._add_view("Signals", "📈", self._signals_view)

        # Cross-tab signal: when any view wants the chart for a ticker,
        # switch to the Signals tab and load it.
        self.request_chart_for_ticker.connect(self._on_chart_requested)

        # View 4: Subscribers
        subscribers_view = SubscribersView(
            db=self._db, http=self._http,
            start_refresh_on_init=self._start_refresh_on_init,
            populate_ticker_lookup_on_init=self._populate_subscribers_lookup,
        )
        # Subscribers uses the Tickers cadence by default; a separate
        # pref would be over-engineered for a tab that rarely changes.
        self._apply_cadence_pref(subscribers_view, KEY_TICKERS_REFRESH)
        self._add_view("Subscribers", "👥", subscribers_view)

        # View 5: Notifications (log)
        notifications_view = NotificationLogView(
            db=self._db, http=self._http,
            start_refresh_on_init=self._start_refresh_on_init,
        )
        self._apply_cadence_pref(notifications_view, KEY_NOTIFICATIONS_REFRESH)
        self._add_view("Notifications", "🔔", notifications_view)

        # View 6: System Health
        system_health_view = SystemHealthView(
            db=self._db, http=self._http,
            start_refresh_on_init=self._start_refresh_on_init,
        )
        self._apply_cadence_pref(system_health_view, KEY_HEALTH_REFRESH)
        self._add_view("System Health", "❤", system_health_view)

        # View 7: Settings (no DB/HTTP; just reads config + QSettings)
        settings_view = SettingsView(config_path=self._config_path)
        self._add_view("Settings", "⚙", settings_view)

    def _apply_cadence_pref(self, view: QWidget, pref_key: str) -> None:
        """Overwrite the view's REFRESH_CADENCE_SECONDS to the user's
        saved pref. Best-effort: silently logs if the view's scheduler
        layout doesn't accept the change. Most views run their first
        tick immediately, so this affects the SECOND tick onward."""
        value = self._gui_prefs.get(pref_key)
        if value is None:
            return
        try:
            view.REFRESH_CADENCE_SECONDS = float(value)
            sched = getattr(view, "_scheduler", None)
            if sched is None:
                return
            if hasattr(sched, "set_interval"):
                sched.set_interval(float(value))
            elif hasattr(sched, "_interval_seconds"):
                sched._interval_seconds = float(value)
        except Exception:
            logger.debug(
                "Could not apply cadence pref %s on %s",
                pref_key, type(view).__name__,
            )

    def _on_chart_requested(self, exchange: str, symbol: str, instrument: str) -> None:
        """Switch to the Signals tab and ask it to load this ticker."""
        self._sidebar.setCurrentRow(self._signals_stack_index)
        self._signals_view.load_ticker(exchange, symbol, instrument)

    def _add_view(self, label: str, icon_text: str, widget: QWidget) -> None:
        """Add a working view to both the sidebar and the stack."""
        item = QListWidgetItem(f"{icon_text}   {label}")
        item.setData(Qt.UserRole, label)
        self._sidebar.addItem(item)
        self._stack.addWidget(widget)

    def _build_menu_bar(self) -> None:
        """Help menu with About action. Kept minimal — no edit/view
        menus, no file menu. The Settings tab handles all preferences."""
        menu_bar = self.menuBar()
        help_menu = menu_bar.addMenu("&Help")

        about_action = QAction("&About trip_ema...", self)
        about_action.triggered.connect(self._on_about_clicked)
        help_menu.addAction(about_action)

    def _on_about_clicked(self) -> None:
        from .about_dialog import show_about_dialog
        show_about_dialog(self)

    def _build_status_bar(self) -> None:
        bar = QStatusBar()
        # Initial state: tell the user whether the action buttons work
        if not self._http.token_configured:
            bar.showMessage(
                "TRIP_EMA_ADMIN_TOKEN not set — action buttons disabled "
                "(read-only mode)"
            )
        else:
            bar.showMessage("Ready")
        self.setStatusBar(bar)

    # ---------- lifecycle ---------- #

    def closeEvent(self, event) -> None:
        """Clean up timers and DB pool before the window goes away."""
        for i in range(self._stack.count()):
            widget = self._stack.widget(i)
            if hasattr(widget, "on_close"):
                try:
                    widget.on_close()
                except Exception:
                    logger.exception("Error closing view %d", i)
        try:
            self._db.dispose()
        except Exception:
            logger.exception("Error disposing GUI DB session")
        super().closeEvent(event)
