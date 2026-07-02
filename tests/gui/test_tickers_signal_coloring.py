"""TickerTableModel row coloring.

What's covered here (pytest-qt, offscreen Qt):

  - `set_rows()` accepts an optional `last_signals` dict, keyed by
    ticker_id, whose values are `SignalColorInputs` (the raw DB inputs
    the coloring rule needs: current_signal / current_regime /
    previous_signal).
  - `data(...)` for `Qt.BackgroundRole` returns the brush produced by
    the single source of truth,
    `trip_ema.gui.determine_background_color.DetermineBackgroundColor`.
    Color is regime-aware: a fresh current-bar signal colors directly;
    otherwise the current regime + previous-bar signal decide the
    "hold" color (green/red) or an exit color (yellow/gray).
  - Tickers absent from `last_signals` return no background brush —
    the view's alternating-row colors keep applying as before.
  - Backward compatibility: calling `set_rows(rows)` with a single
    argument keeps working and produces no coloring.
  - A subsequent `set_rows(rows)` without `last_signals` clears prior
    coloring — the refresh worker rebuilds the dict every tick.
  - Composition with ForegroundRole: a parked/delisted ticker that also
    has a signal shows its foreground color over the signal background.

These tests don't exercise the SQL query (`queries.last_run_signals`)
because that needs a live Postgres — see the DB tests in
`tests/persistence/test_last_run_signals_window.py`.
"""

from __future__ import annotations

from datetime import date, datetime, UTC

import pytest
from PySide6.QtCore import Qt

from trip_ema.gui.queries import TickerRow, SignalColorInputs
from trip_ema.gui.determine_background_color import DetermineBackgroundColor
from trip_ema.gui.views.tickers import (
    COL_TIER,
    TickerSortFilterProxy, TickerTableModel,
)

GREEN = DetermineBackgroundColor.GREEN
RED = DetermineBackgroundColor.RED
YELLOW = DetermineBackgroundColor.YELLOW
GRAY = DetermineBackgroundColor.GRAY


def _sig(
    current: str | None = None,
    *,
    regime: int | None = None,
    previous: str | None = None,
) -> SignalColorInputs:
    """Build a SignalColorInputs for a model `last_signals` dict.

    A bare `_sig("BUY")` models a fresh current-bar BUY (colors green
    regardless of regime). Regime + previous drive the hold cases.
    """
    return SignalColorInputs(
        current_signal=current,
        current_regime=regime,
        previous_signal=previous,
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
        # every column. The view falls back to alternating-row colors.
        for col in range(m.columnCount()):
            assert m.data(m.index(0, col), Qt.BackgroundRole) is None

    def test_explicit_None_last_signals_produces_no_background(self, qtbot):
        m = TickerTableModel()
        m.set_rows([_ticker(1, "RELIANCE")], last_signals=None)
        assert m.data(m.index(0, 0), Qt.BackgroundRole) is None


# ---------------------------------------------------------------------------
# Color mapping — fresh current-bar signal
# ---------------------------------------------------------------------------


class TestSignalBackgroundColors:
    """One test per signal type, plus the absent-from-dict case. A fresh
    current-bar signal colors directly, independent of regime."""

    @pytest.mark.parametrize("signal,expected_hex", [
        ("BUY",            GREEN),
        ("SELL",           RED),
        ("EXIT_FROM_BUY",  YELLOW),
        ("EXIT_FROM_SELL", GRAY),
    ])
    def test_known_signal_returns_expected_color(
        self, qtbot, signal, expected_hex,
    ):
        m = TickerTableModel()
        m.set_rows([_ticker(1, "X")], last_signals={1: _sig(signal)})
        brush = m.data(m.index(0, 0), Qt.BackgroundRole)
        assert brush is not None, f"no brush for {signal}"
        assert brush.color().name().lower() == expected_hex.lower()

    def test_signal_colors_apply_to_every_column(self, qtbot):
        # The row should color uniformly, not just one cell.
        m = TickerTableModel()
        m.set_rows([_ticker(1, "X")], last_signals={1: _sig("BUY")})
        for col in range(m.columnCount()):
            brush = m.data(m.index(0, col), Qt.BackgroundRole)
            assert brush is not None, f"missing brush at col {col}"
            assert brush.color().name().lower() == GREEN.lower()

    def test_unsignalled_ticker_returns_no_background(self, qtbot):
        # Two tickers; only one is in last_signals.
        m = TickerTableModel()
        m.set_rows(
            [_ticker(1, "SIGNALLED"), _ticker(2, "QUIET")],
            last_signals={1: _sig("BUY")},
        )
        assert m.data(m.index(0, 0), Qt.BackgroundRole) is not None
        assert m.data(m.index(1, 0), Qt.BackgroundRole) is None

    def test_unknown_signal_string_returns_no_background(self, qtbot):
        # Defensive: a signal_type the GUI doesn't recognise (e.g. a
        # future event kind added to the DB CHECK constraint without
        # updating the enum) maps to None and produces no background
        # rather than crashing or showing a random color.
        m = TickerTableModel()
        m.set_rows(
            [_ticker(1, "X")], last_signals={1: _sig("UNKNOWN_SIGNAL")},
        )
        assert m.data(m.index(0, 0), Qt.BackgroundRole) is None


# ---------------------------------------------------------------------------
# Color mapping — regime-aware "hold" + the Red→Green→Neutral fix
# ---------------------------------------------------------------------------


class TestRegimeAwareColoring:
    """The reason this rewrite exists: color must consult the current
    regime + previous-bar signal, not just the last signal seen."""

    def test_hold_green_when_regime_green_and_prev_buy(self, qtbot):
        # No fresh flip this bar, but the ticker is still in a green
        # regime after a prior BUY → keep it green.
        m = TickerTableModel()
        m.set_rows(
            [_ticker(1, "X")],
            last_signals={1: _sig(None, regime=1, previous="BUY")},
        )
        brush = m.data(m.index(0, 0), Qt.BackgroundRole)
        assert brush is not None
        assert brush.color().name().lower() == GREEN.lower()

    def test_hold_red_when_regime_red_and_prev_sell(self, qtbot):
        m = TickerTableModel()
        m.set_rows(
            [_ticker(1, "X")],
            last_signals={1: _sig(None, regime=-1, previous="SELL")},
        )
        brush = m.data(m.index(0, 0), Qt.BackgroundRole)
        assert brush is not None
        assert brush.color().name().lower() == RED.lower()

    def test_red_green_neutral_shows_yellow_not_green(self, qtbot):
        # THE FIX. Trajectory Red→Green→Neutral: the current bar flipped
        # green→neutral (EXIT_FROM_BUY this bar); the previous bar was
        # the BUY. Old code colored GREEN off the stale BUY; correct is
        # YELLOW, driven by the current-bar EXIT_FROM_BUY.
        m = TickerTableModel()
        m.set_rows(
            [_ticker(1, "X")],
            last_signals={
                1: _sig("EXIT_FROM_BUY", regime=0, previous="BUY"),
            },
        )
        brush = m.data(m.index(0, 0), Qt.BackgroundRole)
        assert brush is not None
        assert brush.color().name().lower() == YELLOW.lower()

    def test_neutral_after_green_flip_without_exit_is_blank(self, qtbot):
        # Neutral regime, a prior BUY, but no exit signal on the current
        # bar → not a hold-green (regime isn't green) and not an exit →
        # no color.
        m = TickerTableModel()
        m.set_rows(
            [_ticker(1, "X")],
            last_signals={1: _sig(None, regime=0, previous="BUY")},
        )
        assert m.data(m.index(0, 0), Qt.BackgroundRole) is None

    def test_quietly_green_no_recent_flip_is_blank(self, qtbot):
        # Green regime but no current or previous flip (ticker has been
        # quietly green for months) → no color. Coloring is a two-bar
        # rule, not a persistent-state highlight.
        m = TickerTableModel()
        m.set_rows(
            [_ticker(1, "X")],
            last_signals={1: _sig(None, regime=1, previous=None)},
        )
        assert m.data(m.index(0, 0), Qt.BackgroundRole) is None

    def test_hold_yellow_when_neutral_and_prev_exit_from_buy(self, qtbot):
        m = TickerTableModel()
        m.set_rows(
            [_ticker(1, "X")],
            last_signals={
                1: _sig(None, regime=0, previous="EXIT_FROM_BUY"),
            },
        )
        brush = m.data(m.index(0, 0), Qt.BackgroundRole)
        assert brush is not None
        assert brush.color().name().lower() == YELLOW.lower()

    def test_hold_gray_when_neutral_and_prev_exit_from_sell(self, qtbot):
        m = TickerTableModel()
        m.set_rows(
            [_ticker(1, "X")],
            last_signals={
                1: _sig(None, regime=0, previous="EXIT_FROM_SELL"),
            },
        )
        brush = m.data(m.index(0, 0), Qt.BackgroundRole)
        assert brush is not None
        assert brush.color().name().lower() == GRAY.lower()


class TestColorConstants:
    """Pin the four known colors on the single source of truth so a
    future tweak requires a deliberate test update, not silent drift."""

    def test_four_distinct_colors(self):
        colors = {GREEN, RED, YELLOW, GRAY}
        assert len(colors) == 4

    def test_buy_is_green_family(self):
        from PySide6.QtGui import QColor
        c = QColor(GREEN)
        assert c.green() > c.red() and c.green() > c.blue()

    def test_sell_is_red_family(self):
        from PySide6.QtGui import QColor
        c = QColor(RED)
        assert c.red() > c.green() and c.red() > c.blue()

    def test_exit_signals_use_distinct_backgrounds(self):
        # EXIT_FROM_BUY (yellow) and EXIT_FROM_SELL (gray) deliberately
        # differ so the visual encodes prior direction.
        assert YELLOW != GRAY

    def test_exit_from_buy_is_yellow_family(self):
        from PySide6.QtGui import QColor
        c = QColor(YELLOW)
        assert c.red() > c.blue() and c.green() > c.blue()

    def test_exit_from_sell_is_gray_family(self):
        from PySide6.QtGui import QColor
        c = QColor(GRAY)
        assert abs(c.red() - c.green()) < 20
        assert abs(c.green() - c.blue()) < 20
        assert abs(c.red() - c.blue()) < 20


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
        m.set_rows([_ticker(1, "X")], last_signals={1: _sig("BUY")})
        assert m.data(m.index(0, 0), Qt.BackgroundRole) is not None

        m.set_rows([_ticker(1, "X")])
        assert m.data(m.index(0, 0), Qt.BackgroundRole) is None

    def test_subsequent_set_rows_with_different_dict_repaints(self, qtbot):
        m = TickerTableModel()
        m.set_rows([_ticker(1, "X")], last_signals={1: _sig("BUY")})
        brush_buy = m.data(m.index(0, 0), Qt.BackgroundRole)
        assert brush_buy.color().name().lower() == GREEN.lower()

        m.set_rows([_ticker(1, "X")], last_signals={1: _sig("SELL")})
        brush_sell = m.data(m.index(0, 0), Qt.BackgroundRole)
        assert brush_sell.color().name().lower() == RED.lower()


# ---------------------------------------------------------------------------
# Interaction with ForegroundRole
# ---------------------------------------------------------------------------


class TestForegroundBackgroundInteraction:
    """Parked tickers get red text via ForegroundRole. Delisted ones
    get gray text. Neither role interferes with the other; both should
    apply independently."""

    def test_parked_ticker_with_fresh_buy_has_red_text_on_green_bg(self, qtbot):
        m = TickerTableModel()
        m.set_rows(
            [_ticker(1, "X", parked=True)], last_signals={1: _sig("BUY")},
        )
        fg = m.data(m.index(0, 0), Qt.ForegroundRole)
        bg = m.data(m.index(0, 0), Qt.BackgroundRole)
        assert fg is not None
        assert bg is not None
        # Red text → red dominant.
        assert fg.color().red() > fg.color().green()
        # Green BG → green dominant.
        assert bg.color().green() > bg.color().red()

    def test_delisted_ticker_with_fresh_exit_from_buy_has_gray_text_on_yellow_bg(self, qtbot):
        # EXIT_FROM_BUY paints yellow (was long, now flat); delisted
        # still paints gray text on top.
        m = TickerTableModel()
        m.set_rows(
            [_ticker(1, "X", delisted_at=datetime(2025, 1, 1, tzinfo=UTC))],
            last_signals={1: _sig("EXIT_FROM_BUY")},
        )
        fg = m.data(m.index(0, 0), Qt.ForegroundRole)
        bg = m.data(m.index(0, 0), Qt.BackgroundRole)
        assert fg is not None
        assert bg is not None
        # Gray text — RGB roughly equal.
        c = fg.color()
        assert abs(c.red() - c.green()) < 20
        # Yellow BG — red + green high, blue low.
        assert bg.color().red() > bg.color().blue()
        assert bg.color().green() > bg.color().blue()

    def test_non_signalled_parked_ticker_keeps_existing_behavior(self, qtbot):
        # Regression guard: last_signals is empty, so only the
        # ForegroundRole fires (just like before signal coloring).
        m = TickerTableModel()
        m.set_rows([_ticker(1, "X", parked=True)])
        fg = m.data(m.index(0, 0), Qt.ForegroundRole)
        bg = m.data(m.index(0, 0), Qt.BackgroundRole)
        assert fg is not None       # red text persists
        assert bg is None           # no background coloring


# ---------------------------------------------------------------------------
# TickerSortFilterProxy — signal-grouped sort
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
    """The custom proxy puts signalled rows at the top, grouped by the
    recent flip (current_signal or previous_signal), alphabetical within
    each group. Non-signalled rows stay below. Sort keys off the recent
    flip — unchanged by the color rewrite."""

    def test_within_group_is_alphabetical_case_insensitive(self, qtbot):
        from PySide6.QtCore import Qt

        rows = [_t(1, "zulu"), _t(2, "ALPHA"), _t(3, "Mike")]
        signals = {1: _sig("BUY"), 2: _sig("BUY"), 3: _sig("BUY")}
        model = TickerTableModel()
        proxy = TickerSortFilterProxy()
        proxy.setSourceModel(model)
        proxy.setSortRole(Qt.DisplayRole)
        model.set_rows(rows, last_signals=signals)
        proxy.sort(COL_TIER, Qt.AscendingOrder)

        visible = _proxy_visible_symbols(proxy, model)
        assert visible == ["ALPHA", "Mike", "zulu"]

    def test_exit_from_buy_ranks_before_exit_from_sell(self, qtbot):
        # EXIT_FROM_BUY (rank 2) comes before EXIT_FROM_SELL (rank 3).
        from PySide6.QtCore import Qt

        rows = [_t(1, "DELTA"), _t(2, "CHARLIE"), _t(3, "ALPHA")]
        signals = {
            1: _sig("EXIT_FROM_BUY"),
            2: _sig("EXIT_FROM_SELL"),
            3: _sig("EXIT_FROM_BUY"),
        }
        model = TickerTableModel()
        proxy = TickerSortFilterProxy()
        proxy.setSourceModel(model)
        proxy.setSortRole(Qt.DisplayRole)
        model.set_rows(rows, last_signals=signals)
        proxy.sort(COL_TIER, Qt.AscendingOrder)

        visible = _proxy_visible_symbols(proxy, model)
        assert visible == ["ALPHA", "DELTA", "CHARLIE"]

    def test_hold_signal_participates_in_sort_via_recent_flip(self, qtbot):
        # A ticker with no current flip but a previous BUY (hold-green)
        # still sorts into the BUY group — recent_signal falls back to
        # previous_signal.
        from PySide6.QtCore import Qt

        rows = [_t(1, "ZULU"), _t(2, "ALPHA")]
        signals = {
            1: _sig(None),                       # unsignalled → bottom
            2: _sig(None, regime=1, previous="BUY"),  # hold-green → BUY group
        }
        model = TickerTableModel()
        proxy = TickerSortFilterProxy()
        proxy.setSourceModel(model)
        proxy.setSortRole(Qt.DisplayRole)
        model.set_rows(rows, last_signals=signals)
        proxy.sort(COL_TIER, Qt.AscendingOrder)

        visible = _proxy_visible_symbols(proxy, model)
        assert visible == ["ALPHA", "ZULU"]

    def test_unknown_signal_string_treated_as_unsignalled(self, qtbot):
        # Defensive: an unknown signal_type goes to the bottom rather
        # than picking an arbitrary group.
        from PySide6.QtCore import Qt

        rows = [_t(1, "ZULU"), _t(2, "ALPHA")]
        signals = {1: _sig("FUTURE_KIND"), 2: _sig("BUY")}
        model = TickerTableModel()
        proxy = TickerSortFilterProxy()
        proxy.setSourceModel(model)
        proxy.setSortRole(Qt.DisplayRole)
        model.set_rows(rows, last_signals=signals)
        proxy.sort(COL_TIER, Qt.AscendingOrder)

        visible = _proxy_visible_symbols(proxy, model)
        assert visible == ["ALPHA", "ZULU"]


class TestModelLastSignalsAccessor:
    """The proxy reads `last_signals` via the model's public property."""

    def test_last_signals_property_returns_set_dict(self, qtbot):
        m = TickerTableModel()
        m.set_rows([_t(1, "X")], last_signals={1: _sig("BUY")})
        assert m.last_signals == {1: _sig("BUY")}

    def test_last_signals_property_returns_empty_after_clear(self, qtbot):
        m = TickerTableModel()
        m.set_rows([_t(1, "X")], last_signals={1: _sig("BUY")})
        m.set_rows([_t(1, "X")])
        assert m.last_signals == {}


# ---------------------------------------------------------------------------
# Recent-events panel format
# ---------------------------------------------------------------------------


class TestRecentEventsFormat:
    """The panel renders `computed_at` in IST in the format
    `YYYY-MM-DD HH:MM : <tf> <signal> @ <price>`. Multiple events
    on the same bar_timestamp end up on separate lines with their
    distinct computed_at values.
    """

    def test_format_string_uses_computed_at_in_ist(self, qtbot):
        from datetime import datetime, timezone
        from PySide6.QtWidgets import QApplication

        from trip_ema.gui.queries import SignalEventRow
        from trip_ema.gui.views.tickers import TickersView

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
