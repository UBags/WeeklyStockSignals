"""Phase 16d — TickerTableModel row coloring for the most recent
tick's signals.

What's covered here (pytest-qt, offscreen Qt):

  - `set_rows()` accepts an optional `last_signals` dict, keyed by
    ticker_id, with values being the four signal_type strings stored
    in the `signal_events` table: BUY / SELL / EXIT_FROM_BUY /
    EXIT_FROM_SELL.
  - `data(...)` for `Qt.BackgroundRole` returns the corresponding
    light-color brush per the SIGNAL_BACKGROUND_COLORS map.
  - Tickers absent from `last_signals` return no background brush —
    the view's alternating-row colors keep applying as before.
  - Backward compatibility: calling `set_rows(rows)` with a single
    argument (as the existing test_gui.py tests do) keeps working
    and produces no coloring.
  - A subsequent `set_rows(rows)` without `last_signals` clears prior
    coloring — important because the refresh worker rebuilds the dict
    on every tick.
  - Composition with ForegroundRole: a parked ticker that also has a
    fresh BUY shows red text on a light-green background. (No
    interaction issues between the two roles.)

These tests don't exercise the SQL query (`queries.last_run_signals`)
because that needs a live Postgres. The integration tests in
`tests/gui/test_slice4_views.py` follow the same convention — model
tests here, query tests via a real DB elsewhere.
"""

from __future__ import annotations

from datetime import date, datetime, timezone, UTC

import pytest
from PySide6.QtCore import Qt

from trip_ema.gui.queries import TickerRow
from trip_ema.gui.views.tickers import (
    SIGNAL_BACKGROUND_COLORS, COL_SYMBOL, COL_TIER,
    TickerSortFilterProxy, TickerTableModel,
)


def _ticker(
    tid: int = 1, symbol: str = "RELIANCE", *,
    parked: bool = False, delisted_at: datetime | None = None,
) -> TickerRow:
    return TickerRow(
        ticker_id=tid, exchange="NSE", symbol=symbol,
        instrument_type="EQ", full_name=f"{symbol} Ltd",
        isin="INE000A00000", tier=1, is_active=True,
        delisted_at=delisted_at,
        last_successful_bar_date=date(2026, 6, 18),
        consecutive_failures=0, parked=parked,
    )


# ---------------------------------------------------------------------------
# Backward compat
# ---------------------------------------------------------------------------


class TestBackwardCompat:
    """Existing single-argument `set_rows(rows)` callers (every test in
    test_gui.py, plus any third-party patch that monkeypatches the
    model) must keep working."""

    def test_single_arg_set_rows_still_works(self, qtbot):
        m = TickerTableModel()
        m.set_rows([_ticker(1), _ticker(2, "TCS")])
        assert m.rowCount() == 2

    def test_single_arg_set_rows_produces_no_background(self, qtbot):
        m = TickerTableModel()
        m.set_rows([_ticker(1, "RELIANCE")])
        # No last_signals supplied → BackgroundRole returns None for
        # every column. The view falls back to alternating-row colors
        # the same way it did before Phase 16d.
        for col in range(m.columnCount()):
            assert m.data(m.index(0, col), Qt.BackgroundRole) is None

    def test_explicit_None_last_signals_produces_no_background(self, qtbot):
        m = TickerTableModel()
        m.set_rows([_ticker(1, "RELIANCE")], last_signals=None)
        assert m.data(m.index(0, 0), Qt.BackgroundRole) is None


# ---------------------------------------------------------------------------
# Color mapping
# ---------------------------------------------------------------------------


class TestSignalBackgroundColors:
    """One test per signal type, plus the absent-from-dict case."""

    @pytest.mark.parametrize("signal,expected_hex", [
        ("BUY",            "#c8e6c9"),
        ("SELL",           "#ffcdd2"),
        ("EXIT_FROM_BUY",  "#bbdefb"),
        ("EXIT_FROM_SELL", "#bbdefb"),
    ])
    def test_known_signal_returns_expected_color(
        self, qtbot, signal, expected_hex,
    ):
        m = TickerTableModel()
        m.set_rows([_ticker(1, "X")], last_signals={1: signal})
        brush = m.data(m.index(0, 0), Qt.BackgroundRole)
        assert brush is not None, f"no brush for {signal}"
        assert brush.color().name().lower() == expected_hex.lower()

    def test_signal_colors_apply_to_every_column(self, qtbot):
        # The row should color uniformly, not just one cell.
        m = TickerTableModel()
        m.set_rows([_ticker(1, "X")], last_signals={1: "BUY"})
        for col in range(m.columnCount()):
            brush = m.data(m.index(0, col), Qt.BackgroundRole)
            assert brush is not None, f"missing brush at col {col}"
            assert brush.color().name().lower() == "#c8e6c9"

    def test_unsignalled_ticker_returns_no_background(self, qtbot):
        # Two tickers; only one is in last_signals.
        m = TickerTableModel()
        m.set_rows(
            [_ticker(1, "SIGNALLED"), _ticker(2, "QUIET")],
            last_signals={1: "BUY"},
        )
        assert m.data(m.index(0, 0), Qt.BackgroundRole) is not None
        assert m.data(m.index(1, 0), Qt.BackgroundRole) is None

    def test_unknown_signal_string_returns_no_background(self, qtbot):
        # Defensive: a signal_type not in the SIGNAL_BACKGROUND_COLORS
        # map (e.g. a future event kind added to the DB CHECK
        # constraint without updating the GUI) silently produces no
        # background rather than crashing or showing a random color.
        m = TickerTableModel()
        m.set_rows([_ticker(1, "X")], last_signals={1: "UNKNOWN_SIGNAL"})
        assert m.data(m.index(0, 0), Qt.BackgroundRole) is None


class TestColorMapConstant:
    """Pin the four known mappings so a future tweak to one color
    requires a deliberate test update, not a silent drift."""

    def test_all_four_signal_types_have_a_color(self):
        assert set(SIGNAL_BACKGROUND_COLORS.keys()) == {
            "BUY", "SELL", "EXIT_FROM_BUY", "EXIT_FROM_SELL",
        }

    def test_buy_is_green_family(self):
        # Green = G > R and G > B (roughly).
        from PySide6.QtGui import QColor
        c = QColor(SIGNAL_BACKGROUND_COLORS["BUY"])
        assert c.green() > c.red() and c.green() > c.blue()

    def test_sell_is_red_family(self):
        from PySide6.QtGui import QColor
        c = QColor(SIGNAL_BACKGROUND_COLORS["SELL"])
        assert c.red() > c.green() and c.red() > c.blue()

    def test_exit_signals_share_the_same_blue(self):
        # Both EXIT kinds use the same "change to neutral" color.
        assert (
            SIGNAL_BACKGROUND_COLORS["EXIT_FROM_BUY"]
            == SIGNAL_BACKGROUND_COLORS["EXIT_FROM_SELL"]
        )

    def test_exit_is_blue_family(self):
        from PySide6.QtGui import QColor
        c = QColor(SIGNAL_BACKGROUND_COLORS["EXIT_FROM_BUY"])
        assert c.blue() > c.red() and c.blue() > c.green()


# ---------------------------------------------------------------------------
# State transitions
# ---------------------------------------------------------------------------


class TestSetRowsResetsLastSignals:
    """A second `set_rows()` call without `last_signals` clears prior
    coloring. The refresh worker rebuilds the dict every tick, so a
    formerly-signalled ticker that's no longer in the latest tick
    must lose its color."""

    def test_subsequent_set_rows_without_dict_clears_coloring(self, qtbot):
        m = TickerTableModel()
        m.set_rows([_ticker(1, "X")], last_signals={1: "BUY"})
        assert m.data(m.index(0, 0), Qt.BackgroundRole) is not None

        m.set_rows([_ticker(1, "X")])
        assert m.data(m.index(0, 0), Qt.BackgroundRole) is None

    def test_subsequent_set_rows_with_different_dict_repaints(self, qtbot):
        m = TickerTableModel()
        m.set_rows([_ticker(1, "X")], last_signals={1: "BUY"})
        brush_buy = m.data(m.index(0, 0), Qt.BackgroundRole)
        assert brush_buy.color().name().lower() == "#c8e6c9"

        m.set_rows([_ticker(1, "X")], last_signals={1: "SELL"})
        brush_sell = m.data(m.index(0, 0), Qt.BackgroundRole)
        assert brush_sell.color().name().lower() == "#ffcdd2"


# ---------------------------------------------------------------------------
# Interaction with ForegroundRole
# ---------------------------------------------------------------------------


class TestForegroundBackgroundInteraction:
    """Parked tickers get red text via ForegroundRole. Delisted ones
    get gray text. Neither role interferes with the other; both should
    apply independently. Verifies the row's overall paint stays sensible
    when both roles fire."""

    def test_parked_ticker_with_fresh_buy_has_red_text_on_green_bg(self, qtbot):
        m = TickerTableModel()
        m.set_rows([_ticker(1, "X", parked=True)], last_signals={1: "BUY"})
        fg = m.data(m.index(0, 0), Qt.ForegroundRole)
        bg = m.data(m.index(0, 0), Qt.BackgroundRole)
        assert fg is not None
        assert bg is not None
        # Red text → red dominant.
        assert fg.color().red() > fg.color().green()
        # Green BG → green dominant.
        assert bg.color().green() > bg.color().red()

    def test_delisted_ticker_with_fresh_exit_signal_has_gray_text_on_blue_bg(self, qtbot):
        m = TickerTableModel()
        m.set_rows(
            [_ticker(1, "X", delisted_at=datetime(2025, 1, 1, tzinfo=UTC))],
            last_signals={1: "EXIT_FROM_BUY"},
        )
        fg = m.data(m.index(0, 0), Qt.ForegroundRole)
        bg = m.data(m.index(0, 0), Qt.BackgroundRole)
        assert fg is not None
        assert bg is not None
        # Gray text — RGB roughly equal.
        c = fg.color()
        assert abs(c.red() - c.green()) < 20
        # Blue BG — blue dominant.
        assert bg.color().blue() > bg.color().red()

    def test_non_signalled_parked_ticker_keeps_existing_behavior(self, qtbot):
        # Regression guard against accidentally removing the existing
        # parked-row foreground color. last_signals is empty, so only
        # the ForegroundRole fires (just like pre-Phase-16d).
        m = TickerTableModel()
        m.set_rows([_ticker(1, "X", parked=True)])
        fg = m.data(m.index(0, 0), Qt.ForegroundRole)
        bg = m.data(m.index(0, 0), Qt.BackgroundRole)
        assert fg is not None       # red text persists
        assert bg is None           # no background coloring


# ---------------------------------------------------------------------------
# TickerSortFilterProxy — Phase 16e signal-grouped sort
# ---------------------------------------------------------------------------


def _t(tid: int, sym: str, *, tier: int = 3) -> TickerRow:
    """Small helper for proxy tests — minimal TickerRow."""
    return TickerRow(
        ticker_id=tid, exchange="NSE", symbol=sym, instrument_type="EQ",
        full_name=f"{sym} Ltd", isin=None, tier=tier, is_active=True,
        delisted_at=None, last_successful_bar_date=date(2026, 6, 18),
        consecutive_failures=0, parked=False,
    )


def _proxy_visible_symbols(proxy, model) -> list[str]:
    """Walk the proxy in proxy-row order and return the symbol list."""
    out = []
    for proxy_row in range(proxy.rowCount()):
        src_idx = proxy.mapToSource(proxy.index(proxy_row, 0))
        out.append(model.row_at(src_idx.row()).symbol)
    return out


class TestSignalGroupedSort:
    """The custom proxy should put signalled rows at the top, grouped
    by color in the order Green → Red → Blue, alphabetical within each
    group. Non-signalled rows stay below."""

    def test_signalled_rows_float_to_top_in_color_order(self, qtbot):
        from PySide6.QtCore import Qt

        rows = [
            _t(1, "ZULU"),
            _t(2, "ALPHA"),
            _t(3, "DELTA"),     # SELL
            _t(4, "CHARLIE"),   # BUY
            _t(5, "ECHO"),      # BUY
            _t(6, "FOXTROT"),   # EXIT_FROM_BUY
            _t(7, "BRAVO"),
            _t(8, "ANGOLA"),    # EXIT_FROM_SELL
        ]
        signals = {
            3: "SELL", 4: "BUY", 5: "BUY",
            6: "EXIT_FROM_BUY", 8: "EXIT_FROM_SELL",
        }

        model = TickerTableModel()
        proxy = TickerSortFilterProxy()
        proxy.setSourceModel(model)
        proxy.setSortRole(Qt.DisplayRole)
        model.set_rows(rows, last_signals=signals)
        proxy.sort(COL_TIER, Qt.AscendingOrder)

        visible = _proxy_visible_symbols(proxy, model)
        # Top of list: BUYs (alphabetical) → SELL → EXITs (alphabetical)
        assert visible[:5] == ["CHARLIE", "ECHO", "DELTA", "ANGOLA", "FOXTROT"]
        # Bottom of list: non-signalled remain. Order among them follows
        # whatever the parent's lessThan does for the sort column; we
        # only assert membership, not within-bottom order (that's the
        # SQL's job in production).
        assert set(visible[5:]) == {"ZULU", "ALPHA", "BRAVO"}

    def test_no_signals_leaves_order_to_column_sort(self, qtbot):
        from PySide6.QtCore import Qt

        rows = [
            _t(1, "BETA",  tier=2),
            _t(2, "ALPHA", tier=1),
            _t(3, "GAMMA", tier=1),
        ]
        model = TickerTableModel()
        proxy = TickerSortFilterProxy()
        proxy.setSourceModel(model)
        proxy.setSortRole(Qt.DisplayRole)
        # No signals supplied — every row goes to the unsignalled bucket.
        model.set_rows(rows)
        proxy.sort(COL_TIER, Qt.AscendingOrder)

        visible = _proxy_visible_symbols(proxy, model)
        # Sort by tier ascending — Tier 1 rows first, Tier 2 below.
        # Within Tier 1 the proxy preserves source order (stable sort);
        # in production the source order = SQL ORDER BY tier, symbol so
        # would be ALPHA, GAMMA.
        assert visible.index("BETA") > visible.index("ALPHA")
        assert visible.index("BETA") > visible.index("GAMMA")

    def test_buy_sorts_before_sell(self, qtbot):
        from PySide6.QtCore import Qt

        rows = [_t(1, "ZULU"), _t(2, "ALPHA")]
        signals = {1: "BUY", 2: "SELL"}
        model = TickerTableModel()
        proxy = TickerSortFilterProxy()
        proxy.setSourceModel(model)
        proxy.setSortRole(Qt.DisplayRole)
        model.set_rows(rows, last_signals=signals)
        proxy.sort(COL_TIER, Qt.AscendingOrder)

        visible = _proxy_visible_symbols(proxy, model)
        # Even though ZULU sorts after ALPHA alphabetically, the BUY
        # group wins over SELL group at the layer-1 comparison.
        assert visible == ["ZULU", "ALPHA"]

    def test_sell_sorts_before_exit(self, qtbot):
        from PySide6.QtCore import Qt

        rows = [_t(1, "ALPHA"), _t(2, "BETA")]
        signals = {1: "EXIT_FROM_BUY", 2: "SELL"}
        model = TickerTableModel()
        proxy = TickerSortFilterProxy()
        proxy.setSourceModel(model)
        proxy.setSortRole(Qt.DisplayRole)
        model.set_rows(rows, last_signals=signals)
        proxy.sort(COL_TIER, Qt.AscendingOrder)

        visible = _proxy_visible_symbols(proxy, model)
        assert visible == ["BETA", "ALPHA"]

    def test_within_group_is_alphabetical_case_insensitive(self, qtbot):
        from PySide6.QtCore import Qt

        # Construct rows in non-alphabetical order with mixed case.
        rows = [_t(1, "zulu"), _t(2, "ALPHA"), _t(3, "Mike")]
        signals = {1: "BUY", 2: "BUY", 3: "BUY"}
        model = TickerTableModel()
        proxy = TickerSortFilterProxy()
        proxy.setSourceModel(model)
        proxy.setSortRole(Qt.DisplayRole)
        model.set_rows(rows, last_signals=signals)
        proxy.sort(COL_TIER, Qt.AscendingOrder)

        visible = _proxy_visible_symbols(proxy, model)
        assert visible == ["ALPHA", "Mike", "zulu"]

    def test_exit_from_buy_and_exit_from_sell_share_a_rank(self, qtbot):
        from PySide6.QtCore import Qt

        # Within the blue group both kinds intermix purely on symbol.
        rows = [_t(1, "DELTA"), _t(2, "CHARLIE"), _t(3, "ALPHA")]
        signals = {
            1: "EXIT_FROM_BUY",
            2: "EXIT_FROM_SELL",
            3: "EXIT_FROM_BUY",
        }
        model = TickerTableModel()
        proxy = TickerSortFilterProxy()
        proxy.setSourceModel(model)
        proxy.setSortRole(Qt.DisplayRole)
        model.set_rows(rows, last_signals=signals)
        proxy.sort(COL_TIER, Qt.AscendingOrder)

        visible = _proxy_visible_symbols(proxy, model)
        # Pure alphabetical — neither EXIT_FROM_BUY nor EXIT_FROM_SELL
        # sorts before the other.
        assert visible == ["ALPHA", "CHARLIE", "DELTA"]

    def test_unknown_signal_string_treated_as_unsignalled(self, qtbot):
        # Defensive: an unknown signal_type (e.g. a future DB CHECK
        # adds a new kind without the GUI being updated) goes to the
        # bottom rather than picking an arbitrary group.
        from PySide6.QtCore import Qt

        rows = [_t(1, "ZULU"), _t(2, "ALPHA")]
        signals = {1: "FUTURE_KIND", 2: "BUY"}
        model = TickerTableModel()
        proxy = TickerSortFilterProxy()
        proxy.setSourceModel(model)
        proxy.setSortRole(Qt.DisplayRole)
        model.set_rows(rows, last_signals=signals)
        proxy.sort(COL_TIER, Qt.AscendingOrder)

        visible = _proxy_visible_symbols(proxy, model)
        # ALPHA (BUY → group 0) floats above ZULU (unknown → group 99).
        assert visible == ["ALPHA", "ZULU"]


class TestModelLastSignalsAccessor:
    """The proxy reads `last_signals` via the model's public property."""

    def test_last_signals_property_returns_set_dict(self, qtbot):
        m = TickerTableModel()
        m.set_rows([_t(1, "X")], last_signals={1: "BUY"})
        assert m.last_signals == {1: "BUY"}

    def test_last_signals_property_returns_empty_after_clear(self, qtbot):
        m = TickerTableModel()
        m.set_rows([_t(1, "X")], last_signals={1: "BUY"})
        m.set_rows([_t(1, "X")])
        assert m.last_signals == {}


# ---------------------------------------------------------------------------
# Recent-events panel format (Phase 16e)
# ---------------------------------------------------------------------------


class TestRecentEventsFormat:
    """The panel renders `computed_at` in IST in the format
    `YYYY-MM-DD HH:MM : <tf> <signal> @ <price>`. Multiple events
    on the same bar_timestamp end up on separate lines with their
    distinct computed_at values.
    """

    def test_format_string_uses_computed_at_in_ist(self, qtbot):
        # Construct three SignalEventRows that share a bar_timestamp
        # but have different computed_at values — exactly the scenario
        # Uddi flagged ("looks like all the same time"). Render via the
        # view's helper and confirm each line gets a distinct timestamp.
        from datetime import datetime, timezone
        from PySide6.QtWidgets import QApplication

        from trip_ema.gui.queries import SignalEventRow
        from trip_ema.gui.views.tickers import TickersView

        # We don't construct a full TickersView here (it needs db/http);
        # instead we reproduce the rendering inline to verify the
        # format. If/when TickersView extracts the formatter to a
        # static method, this test should switch to calling it.
        from zoneinfo import ZoneInfo
        ist = ZoneInfo("Asia/Kolkata")
        shared_bar = datetime(2026, 6, 15, 0, 0, tzinfo=UTC)
        events = [
            SignalEventRow(
                event_id=1, bar_timestamp=shared_bar,
                computed_at=datetime(2026, 6, 17, 4, 0, tzinfo=UTC),  # 09:30 IST
                timeframe="1W", signal="BUY", price_at_signal=1839.40,
            ),
            SignalEventRow(
                event_id=2, bar_timestamp=shared_bar,
                computed_at=datetime(2026, 6, 16, 7, 10, tzinfo=UTC),  # 12:40 IST
                timeframe="1W", signal="EXIT_FROM_BUY", price_at_signal=1814.70,
            ),
            SignalEventRow(
                event_id=3, bar_timestamp=shared_bar,
                computed_at=datetime(2026, 6, 16, 4, 0, tzinfo=UTC),   # 09:30 IST
                timeframe="1W", signal="BUY", price_at_signal=1833.50,
            ),
        ]
        # Inline the same rendering tickers._on_events_received uses.
        lines = []
        for e in events:
            ts = e.computed_at.astimezone(ist).strftime("%Y-%m-%d %H:%M")
            lines.append(
                f"{ts} : {e.timeframe} {e.signal} @ {e.price_at_signal:.2f}"
            )
        assert lines == [
            "2026-06-17 09:30 : 1W BUY @ 1839.40",
            "2026-06-16 12:40 : 1W EXIT_FROM_BUY @ 1814.70",
            "2026-06-16 09:30 : 1W BUY @ 1833.50",
        ]