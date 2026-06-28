"""MarketTrendDialog — 90-day trend chart for market_overview_daily.

Two variants chosen via TrendKind:
  - REGIMES: three lines (green / red / neutral regime counts)
  - EMA_ENVELOPE: three lines (above_highest / below_lowest /
    between counts)

Backed by pyqtgraph (already in the stack). Single-pane plot with
a legend, time on the x-axis (days), counts on the y-axis.

Behavior on empty data:
  Shows a single "No snapshot history yet" label centered in the
  chart area. Live EoD accretion fills it over ~13 weeks. The
  deferred backfill stage 10 will populate retroactively when it
  lands.
"""
from __future__ import annotations

from datetime import date, timedelta
from enum import Enum

import pyqtgraph as pg
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QLabel, QVBoxLayout,
)

from ..db import GuiDbSession


# 90-day window per Step 13a/13b spec.
TREND_WINDOW_DAYS = 90


class TrendKind(Enum):
    REGIMES = "regimes"
    EMA_ENVELOPE = "ema_envelope"
    SIGNALS = "signals"  # === STEP 13D3: TrendKind.SIGNALS ===


_TITLES = {
    TrendKind.REGIMES: "Regime Counts — last 90 days",
    TrendKind.EMA_ENVELOPE: (
        "EMA Envelope Position — last 90 days"
    ),
    TrendKind.SIGNALS: "Signal Counts — last 90 days",
}

# Line color mapping. Aligned with the rest of the GUI's regime
# palette where applicable.
_PEN_KW = {
    "green":   {"color": "#2ecc71", "width": 2},
    "red":     {"color": "#e74c3c", "width": 2},
    "neutral": {"color": "#95a5a6", "width": 2},
    "above":   {"color": "#2ecc71", "width": 2},
    "below":   {"color": "#e74c3c", "width": 2},
    "between": {"color": "#3498db", "width": 2},
    # Step 13d-3 — bar fills for SIGNALS trend
    "sig_buys":       "#66bb6a",
    "sig_sells":      "#ef5350",
    "sig_exit_buys":  "#1976d2",
    "sig_exit_sells": "#9e9e9e",
}


class MarketTrendDialog(QDialog):
    """Modal dialog with a pyqtgraph line plot."""

    def __init__(
        self,
        *,
        db: GuiDbSession,
        exchange: str,
        kind: TrendKind,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._db = db
        self._exchange = exchange
        self._kind = kind

        self.setWindowTitle(_TITLES[kind])
        self.setModal(True)
        self.resize(900, 500)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)
        outer.setSpacing(8)

        self._header_label = QLabel()
        self._header_label.setStyleSheet(
            "font-weight: 600; padding-bottom: 4px;"
        )
        outer.addWidget(self._header_label)

        self._plot = pg.PlotWidget()
        self._plot.setBackground("#1e1e1e")
        self._plot.showGrid(x=True, y=True, alpha=0.2)
        self._plot.addLegend(offset=(-10, 10))
        self._plot.getAxis("left").setPen(pg.mkPen("#888"))
        self._plot.getAxis("bottom").setPen(pg.mkPen("#888"))
        self._plot.getAxis("left").setTextPen(pg.mkPen("#ccc"))
        self._plot.getAxis("bottom").setTextPen(pg.mkPen("#ccc"))
        outer.addWidget(self._plot, 1)

        self._empty_label = QLabel(
            "No snapshot history yet for this exchange."
        )
        self._empty_label.setAlignment(Qt.AlignCenter)
        self._empty_label.setStyleSheet(
            "color: #888; font-style: italic; padding: 20px;"
        )
        self._empty_label.hide()
        outer.addWidget(self._empty_label)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        outer.addWidget(buttons)

        self._load_and_render()

    # ============================================================
    # Data + render
    # ============================================================

    def _load_and_render(self) -> None:
        try:
            from ...persistence.market_overview_repo import (
                MarketOverviewRepository,
            )
            with self._db.connect() as conn:
                rows = MarketOverviewRepository(conn).range(
                    exchange=self._exchange,
                    n_days=TREND_WINDOW_DAYS,
                )
        except Exception as e:
            self._header_label.setText(
                f"Failed to load snapshot history: {e}"
            )
            return

        if not rows:
            self._empty_label.show()
            self._plot.hide()
            self._header_label.setText(
                f"Exchange: {self._exchange} — no data"
            )
            return

        # Header summary
        if len(rows) == 1:
            range_str = rows[0].snapshot_date.isoformat()
        else:
            range_str = (
                f"{rows[0].snapshot_date.isoformat()} "
                f"to {rows[-1].snapshot_date.isoformat()}"
            )
        self._header_label.setText(
            f"Exchange: {self._exchange} — "
            f"{len(rows)} snapshot(s) — {range_str}"
        )

        # X-axis: days since the earliest snapshot
        x0 = rows[0].snapshot_date
        xs = [(r.snapshot_date - x0).days for r in rows]

        if self._kind == TrendKind.SIGNALS:
            self._render_signals_stacked_bars(xs, rows)
        elif self._kind == TrendKind.REGIMES:
            self._plot.plot(
                xs, [r.n_green_regime for r in rows],
                name="Green", pen=pg.mkPen(**_PEN_KW["green"]),
            )
            self._plot.plot(
                xs, [r.n_red_regime for r in rows],
                name="Red", pen=pg.mkPen(**_PEN_KW["red"]),
            )
            self._plot.plot(
                xs, [r.n_neutral_regime for r in rows],
                name="Neutral",
                pen=pg.mkPen(**_PEN_KW["neutral"]),
            )
            self._plot.setLabel("left", "Ticker count")
        else:
            self._plot.plot(
                xs, [r.n_above_highest_ema for r in rows],
                name="Above highest EMA",
                pen=pg.mkPen(**_PEN_KW["above"]),
            )
            self._plot.plot(
                xs, [r.n_below_lowest_ema for r in rows],
                name="Below lowest EMA",
                pen=pg.mkPen(**_PEN_KW["below"]),
            )
            self._plot.plot(
                xs, [r.n_between_emas for r in rows],
                name="Between EMAs",
                pen=pg.mkPen(**_PEN_KW["between"]),
            )
            self._plot.setLabel("left", "Ticker count")

        # X-axis label: "days since {x0}"
        self._plot.setLabel(
            "bottom", f"Days since {x0.isoformat()}",
        )

    def _render_signals_stacked_bars(
        self, xs: list, rows: list,
    ) -> None:
        """Render 4 stacked bars per snapshot date for SIGNALS kind.

        Q7 lock — bars stacked: Buys, Sells, Exit Buys, Exit Sells.
        Colors from the 13d-3 palette (sig_buys / sig_sells /
        sig_exit_buys / sig_exit_sells).
        """
        n_buys = [r.n_buys for r in rows]
        n_sells = [r.n_sells for r in rows]
        n_exit_buys = [r.n_exit_buys for r in rows]
        n_exit_sells = [r.n_exit_sells for r in rows]

        # Stacking offsets: y0 = 0, y0 + buys, y0 + buys + sells, ...
        zero = [0] * len(xs)
        off_sells = list(n_buys)
        off_exit_buys = [
            a + b for a, b in zip(n_buys, n_sells)
        ]
        off_exit_sells = [
            a + b + c
            for a, b, c in zip(n_buys, n_sells, n_exit_buys)
        ]

        width = 0.8

        # Buys (bottom)
        bg_buys = pg.BarGraphItem(
            x=xs, height=n_buys, y0=zero, width=width,
            brush=_PEN_KW["sig_buys"], pen=_PEN_KW["sig_buys"],
            name="Buys",
        )
        self._plot.addItem(bg_buys)

        bg_sells = pg.BarGraphItem(
            x=xs, height=n_sells, y0=off_sells, width=width,
            brush=_PEN_KW["sig_sells"], pen=_PEN_KW["sig_sells"],
            name="Sells",
        )
        self._plot.addItem(bg_sells)

        bg_exit_buys = pg.BarGraphItem(
            x=xs, height=n_exit_buys, y0=off_exit_buys, width=width,
            brush=_PEN_KW["sig_exit_buys"],
            pen=_PEN_KW["sig_exit_buys"],
            name="Exit Buys",
        )
        self._plot.addItem(bg_exit_buys)

        bg_exit_sells = pg.BarGraphItem(
            x=xs, height=n_exit_sells, y0=off_exit_sells,
            width=width,
            brush=_PEN_KW["sig_exit_sells"],
            pen=_PEN_KW["sig_exit_sells"],
            name="Exit Sells",
        )
        self._plot.addItem(bg_exit_sells)

        # pyqtgraph BarGraphItems don't auto-register with the
        # built-in addLegend() — re-add manually so the user can
        # tell the colors apart.
        legend = self._plot.getPlotItem().legend
        if legend is not None:
            legend.addItem(bg_buys, "Buys")
            legend.addItem(bg_sells, "Sells")
            legend.addItem(bg_exit_buys, "Exit Buys")
            legend.addItem(bg_exit_sells, "Exit Sells")

        self._plot.setLabel("left", "Signal count")

