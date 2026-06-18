"""Left panel for the Signals tab.

Holds:
  - Industry chart at top (~30% vertical) — ChartWidget in INDUSTRY
    mode, fetched via /v1/signals/industry/{name}/history.
  - 4×3 grid of mini ChartWidgets below (~70% vertical), one per
    top-by-mcap ticker in the same industry as the selected ticker.
    Up to 12; fewer if the industry has <12 active tickers. Empty
    slots stay blank rather than reshaping the grid.

Flow when SignalsView selects a new ticker:
  load_for_ticker(exchange, symbol, instrument):
    1. Look up the ticker's industry name (instrument_industry).
    2. If industry found:
         a. Fire industry history fetch → render top chart.
         b. Fire top-12-by-mcap query → for each ticker returned,
            fire a 5Y history fetch → render that mini.
    3. If no industry found:
         Render a NIFTY 50 tracking ETF (see _NIFTY50_FALLBACK)
         in the top chart slot as a broad-market reference.
         Mini grid stays empty per spec (no peer industry to
         draw the top-12 from).

All fetches go through the worker pool — no blocking on the GUI
thread. Each mini receives its result via its own callback, so
the grid populates progressively rather than blocking until all
13 fetches complete.
"""

from __future__ import annotations

import logging
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QGridLayout, QLabel, QSplitter, QVBoxLayout, QWidget,
)
from sqlalchemy import text

from ..db import GuiDbSession
from ..http_client import ApiError, TripEmaHttpClient
from ..queries import top_n_tickers_in_industry_by_mcap
from ..workers import submit
from ..chart_widget import ChartMode, ChartWidget

logger = logging.getLogger(__name__)


_GRID_ROWS = 4
_GRID_COLS = 3
_N_MINIS = _GRID_ROWS * _GRID_COLS   # 12

# Range for the industry chart follows the SignalsView's top-bar
# range selector. Minis are fixed at 5Y per spec.
_MINI_YEARS = 5

# When a ticker has no instrument_industry classification, fall back
# to a NIFTY 50 tracking ETF in the industry chart slot as a broad-
# market reference. NIFTY 50 itself isn't stored as a ticker and
# isn't in industry_index — the major tracking ETFs (NIFTYBEES,
# HDFCNIFTY, AXISNIFTY) all track the same underlying, so the chart
# shape is identical regardless of which one we pick. NIFTYBEES
# chosen for history depth: ~4300 bars from 2009 vs HDFCNIFTY's
# ~725 from 2023 (verified 2026-06-18). Change this tuple to switch.
_NIFTY50_FALLBACK: tuple[str, str, str] = ("NSE", "NIFTYBEES", "ETF")


class IndustryPanel(QWidget):

    def __init__(
        self,
        db: GuiDbSession,
        http: TripEmaHttpClient,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self._db = db
        self._http = http

        # Track the current ticker so late-arriving fetches for a
        # previous selection get dropped instead of overwriting the
        # current view.
        self._current_request_id = 0

        # Track the range for the industry chart — fixed at 10 years
        # per spec (as opposed to 5Y for the minis below).
        self._industry_years = 10

        # True when the industry chart slot is showing the NIFTY 50
        # fallback (selected ticker had no industry classification).
        # Used so range changes know to re-fetch the fallback rather
        # than the (None) industry name.
        self._in_fallback_mode = False

        self._build_ui()

    # ---------- UI ---------- #

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(4)

        # Vertical splitter: industry chart on top, mini grid below
        self._inner_splitter = QSplitter(Qt.Vertical)

        # Industry chart slot
        self._industry_chart = ChartWidget(
            ChartMode.INDUSTRY,
            title="(no ticker selected)",
            parent=self,
        )
        self._inner_splitter.addWidget(self._industry_chart)

        # Mini grid container
        grid_container = QWidget()
        grid = QGridLayout(grid_container)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(4)
        self._mini_charts: list[ChartWidget] = []
        for slot in range(_N_MINIS):
            mini = ChartWidget(
                ChartMode.MINI, title="—", parent=grid_container,
            )
            mini.setMinimumHeight(120)
            row, col = divmod(slot, _GRID_COLS)
            grid.addWidget(mini, row, col)
            self._mini_charts.append(mini)
        # Even row stretch so the grid distributes vertical space
        for r in range(_GRID_ROWS):
            grid.setRowStretch(r, 1)
        for c in range(_GRID_COLS):
            grid.setColumnStretch(c, 1)
        self._inner_splitter.addWidget(grid_container)

        # 30 / 70 vertical split per spec
        self._inner_splitter.setStretchFactor(0, 30)
        self._inner_splitter.setStretchFactor(1, 70)
        self._inner_splitter.setSizes([300, 700])

        outer.addWidget(self._inner_splitter)

    # ---------- Public API ---------- #

    def set_industry_chart_years(self, years: int) -> None:
        """Called by SignalsView when its top-bar Range button changes.
        No-op: the industry chart is fixed at 10 years per spec.
        Minis are also unaffected (fixed at 5Y)."""
        pass

    def load_for_ticker(
        self,
        exchange: str,
        symbol: str,
        instrument: str = "EQ",
    ) -> None:
        """Re-populate the left panel for the given ticker. Resolves
        the ticker's industry, then fires the industry chart fetch
        and the top-12-by-mcap fetch in parallel.
        """
        self._current_request_id += 1
        request_id = self._current_request_id

        # Clear the existing display so the user sees the panel
        # responding immediately (even before async fetches return).
        self._industry_chart.render(None)
        self._industry_chart.set_title(f"{symbol}: resolving industry…")
        for mini in self._mini_charts:
            mini.render(None)
            mini.set_title("—")

        def fetch_industry():
            with self._db.connect() as conn:
                row = conn.execute(text("""
                    SELECT ii.industry
                    FROM instrument_industry ii
                    JOIN tickers t ON t.ticker_id = ii.ticker_id
                    WHERE t.exchange = :ex
                      AND t.symbol = :sym
                      AND t.instrument = :inst
                """), {
                    "ex": exchange, "sym": symbol, "inst": instrument,
                }).first()
                return row.industry if row else None

        submit(
            fetch_industry,
            on_finished=lambda name: self._on_industry_resolved(name, request_id),
            on_failed=lambda msg: self._on_industry_resolve_failed(msg, request_id),
        )

    # ---------- Industry resolution ---------- #

    def _on_industry_resolved(
        self, industry_name: Optional[str], request_id: int,
    ) -> None:
        if request_id != self._current_request_id:
            return  # stale — a newer ticker has been selected
        if not industry_name:
            # No classification → fall back to a NIFTY 50 tracking ETF
            # in the industry chart slot. Mini grid stays empty (no
            # peer industry for the top-12 query); load_for_ticker
            # already cleared the minis at the start of the flow.
            self._current_industry_name = None
            self._in_fallback_mode = True
            ex, sym, _inst = _NIFTY50_FALLBACK
            self._industry_chart.set_title(
                f"NIFTY 50 (broad market, via {ex}:{sym})"
            )
            self._fetch_nifty50_fallback(request_id)
            return

        self._current_industry_name = industry_name
        self._in_fallback_mode = False
        self._industry_chart.set_title(industry_name)
        self._fetch_industry_chart(industry_name, request_id)
        self._fetch_top_minis(industry_name, request_id)

    def _on_industry_resolve_failed(self, message: str, request_id: int) -> None:
        if request_id != self._current_request_id:
            return
        logger.warning("Industry resolution failed: %s", message)
        self._industry_chart.set_title(
            f"(industry lookup failed: {message[:60]})"
        )

    # ---------- Industry chart fetch ---------- #

    def _fetch_nifty50_fallback(self, request_id: int) -> None:
        """Fetch the NIFTY 50 tracking ETF history and render it in
        the industry chart slot. Used when the selected ticker has
        no instrument_industry classification.

        The ETF is treated as a regular ticker — the history endpoint
        replays the engine over its bars to materialize EMAs / bands
        / regime, so the resulting chart looks the same as any other
        Triple-EMA chart. Mini grid stays empty (load_for_ticker
        already cleared it).
        """
        ex, sym, inst = _NIFTY50_FALLBACK
        def fetch():
            return self._http.get_signal_history(
                ex, sym, instrument=inst, years=self._industry_years,
            )
        submit(
            fetch,
            on_finished=lambda payload: self._on_industry_chart_received(
                payload, request_id,
            ),
            on_failed=lambda msg: self._on_industry_chart_failed(
                msg, request_id,
            ),
        )

    def _fetch_industry_chart(
        self, industry_name: str, request_id: int,
    ) -> None:
        def fetch():
            return self._http.get_industry_history(
                industry_name, years=self._industry_years,
            )

        submit(
            fetch,
            on_finished=lambda payload: self._on_industry_chart_received(
                payload, request_id,
            ),
            on_failed=lambda msg: self._on_industry_chart_failed(
                msg, request_id,
            ),
        )

    def _on_industry_chart_received(
        self, payload: dict, request_id: int,
    ) -> None:
        if request_id != self._current_request_id:
            return
        self._industry_chart.render(payload)

    def _on_industry_chart_failed(
        self, message: str, request_id: int,
    ) -> None:
        if request_id != self._current_request_id:
            return
        logger.warning("Industry chart fetch failed: %s", message)
        self._industry_chart.set_title(
            f"{self._current_industry_name} (fetch failed)"
        )

    # ---------- Mini grid fetch ---------- #

    def _fetch_top_minis(self, industry_name: str, request_id: int) -> None:
        """Step 1: get the top-12 ticker list. Step 2: fan out 12
        parallel history fetches, one per ticker, each rendering its
        own mini when it arrives."""
        def fetch_list():
            with self._db.connect() as conn:
                return top_n_tickers_in_industry_by_mcap(
                    conn, industry_name, n=_N_MINIS,
                )
        submit(
            fetch_list,
            on_finished=lambda rows: self._on_top_minis_list(rows, request_id),
            on_failed=lambda msg: logger.warning(
                "Top-mcap query failed: %s", msg,
            ),
        )

    def _on_top_minis_list(self, rows: list, request_id: int) -> None:
        """`rows` is a list of TickerRef-ish records from the query.
        Spec: empty slots stay blank (we don't reshape the grid)."""
        if request_id != self._current_request_id:
            return
        for slot in range(_N_MINIS):
            if slot >= len(rows):
                self._mini_charts[slot].render(None)
                self._mini_charts[slot].set_title("—")
                continue
            row = rows[slot]
            mini = self._mini_charts[slot]
            mini.set_title(row.symbol)
            self._fetch_mini_history(
                slot, row.exchange, row.symbol, row.instrument_type,
                request_id,
            )

    def _fetch_mini_history(
        self, slot: int, exchange: str, symbol: str,
        instrument: str, request_id: int,
    ) -> None:
        def fetch():
            return self._http.get_signal_history(
                exchange, symbol, instrument=instrument, years=_MINI_YEARS,
            )
        submit(
            fetch,
            on_finished=lambda payload: self._on_mini_history(
                slot, payload, request_id,
            ),
            on_failed=lambda msg: self._on_mini_failed(slot, msg, request_id),
        )

    def _on_mini_history(
        self, slot: int, payload: dict, request_id: int,
    ) -> None:
        if request_id != self._current_request_id:
            return
        self._mini_charts[slot].render(payload)

    def _on_mini_failed(
        self, slot: int, message: str, request_id: int,
    ) -> None:
        if request_id != self._current_request_id:
            return
        logger.debug("Mini fetch failed for slot %d: %s", slot, message)
        self._mini_charts[slot].set_title(
            self._mini_charts[slot]._title_label.text() + " (failed)"
            if self._mini_charts[slot]._title_label else "(failed)"
        )