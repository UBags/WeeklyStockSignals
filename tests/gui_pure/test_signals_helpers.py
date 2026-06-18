"""Pure tests for trip_ema/gui/signals_helpers.py.

No Qt, no DB. The Qt-aware view calls these from its mouse handler
and combo-population callback; this file pins down the wire-level
behavior they depend on so regressions in formatting or partitioning
get caught before the GUI ever opens.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import pytest

from trip_ema.gui.signals_helpers import (
    format_date_label,
    format_price_label,
    partition_ticker_rows,
)


# ---------- format_price_label ---------- #


class TestFormatPriceLabel:

    def test_typical_equity_price_one_decimal_western_grouping(self):
        # The user's RELIANCE example from the spec
        assert format_price_label(2955.10) == "2,955.1"

    def test_sub_thousand_price_no_comma(self):
        assert format_price_label(987.6) == "987.6"

    def test_lakh_boundary_western_grouping(self):
        # Indian-style would give "1,00,000.0"; we use western
        # because individual equity prices rarely reach this band
        # and the consistency matters more than the locale style.
        assert format_price_label(100000.0) == "100,000.0"

    def test_rounds_to_one_decimal(self):
        assert format_price_label(2955.16) == "2,955.2"
        assert format_price_label(2955.14) == "2,955.1"

    def test_zero(self):
        assert format_price_label(0.0) == "0.0"

    def test_negative(self):
        # Negative prices shouldn't occur on equity charts, but the
        # formatter must not crash on them — defensive against any
        # caller that passes raw mouse-y values without clamping.
        assert format_price_label(-150.5) == "-150.5"

    def test_very_large(self):
        # Sensex-style index value — far above any individual share
        assert format_price_label(82_456.78) == "82,456.8"


# ---------- format_date_label ---------- #


class TestFormatDateLabel:

    def test_weekly_monday_anchored(self):
        # Mon 15 Jun 2026 is the Monday-of-week anchor for the week
        # containing the user's sample 18 Jun
        dt = datetime(2026, 6, 15)
        assert format_date_label(dt, timeframe="1W") == "15-Jun-26"

    def test_daily(self):
        dt = datetime(2026, 6, 18)
        assert format_date_label(dt, timeframe="1D") == "18-Jun-26"

    def test_default_timeframe_is_1W(self):
        # Same format for both right now, but we want the default to
        # match the documented value
        dt = datetime(2026, 6, 15)
        assert format_date_label(dt) == format_date_label(dt, timeframe="1W")

    def test_january_pads_month_name(self):
        dt = datetime(2026, 1, 5)
        assert format_date_label(dt) == "05-Jan-26"

    def test_year_2000_two_digit(self):
        dt = datetime(2000, 3, 14)
        assert format_date_label(dt) == "14-Mar-00"


# ---------- partition_ticker_rows ---------- #


@dataclass(frozen=True)
class _Row:
    """Minimal stand-in for queries.TickerComboRow — only the field
    that partition_ticker_rows actually reads."""
    symbol: str
    has_recent_signal: bool


class TestPartitionTickerRows:

    def test_typical_mixed_partition(self):
        rows = [
            _Row("ABB", True),
            _Row("HDFCBANK", True),
            _Row("RELIANCE", True),
            _Row("AARTIIND", False),
            _Row("YESBANK", False),
        ]
        recent, other = partition_ticker_rows(rows)
        assert [r.symbol for r in recent] == ["ABB", "HDFCBANK", "RELIANCE"]
        assert [r.symbol for r in other] == ["AARTIIND", "YESBANK"]

    def test_all_recent(self):
        rows = [_Row("A", True), _Row("B", True)]
        recent, other = partition_ticker_rows(rows)
        assert [r.symbol for r in recent] == ["A", "B"]
        assert other == []

    def test_none_recent(self):
        rows = [_Row("A", False), _Row("B", False)]
        recent, other = partition_ticker_rows(rows)
        assert recent == []
        assert [r.symbol for r in other] == ["A", "B"]

    def test_empty_input(self):
        recent, other = partition_ticker_rows([])
        assert recent == [] and other == []

    def test_accepts_generator(self):
        # The contract takes an Iterable, not a list — a generator
        # from a SQLAlchemy result should work without materialization.
        def gen():
            yield _Row("A", True)
            yield _Row("B", False)
        recent, other = partition_ticker_rows(gen())
        assert [r.symbol for r in recent] == ["A"]
        assert [r.symbol for r in other] == ["B"]

    def test_misordered_input_does_not_resort(self):
        """Documentation contract: input must already be ordered with
        all True rows before all False rows. If a caller violates this,
        the result will have a misclassified row — we don't pay for
        a sort to compensate. This test pins the cheap-and-fast
        behavior so a future 'fix' that adds sorting is intentional."""
        rows = [
            _Row("ZEEL", False),
            _Row("AARTI", True),
        ]
        recent, other = partition_ticker_rows(rows)
        # Both partitions reflect the (wrong) input order
        assert [r.symbol for r in recent] == ["AARTI"]
        assert [r.symbol for r in other] == ["ZEEL"]