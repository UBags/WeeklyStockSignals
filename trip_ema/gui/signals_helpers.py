"""Pure helpers for the Signals tab.

Qt-free so the rendering / formatting / partitioning logic is
unit-testable under `tests/gui_pure/`. The Qt-aware view
(`trip_ema/gui/views/signals.py`) imports these and wires them into
the QComboBox model and the crosshair label text.

Three responsibilities:

1. `format_price_label` — TradingView-style right-axis price tag text.
   1 decimal, comma-grouped. Stays narrow so the pinned tab doesn't
   eat the chart area.

2. `format_date_label` — TradingView-style bottom-axis date tag text.
   `DD-Mon-YY` for both 1W and 1D. The bar_timestamp on weekly bars
   is already Monday-anchored IST, so no snapping is needed at
   format time.

3. `partition_ticker_rows` — splits an ordered list of
   `(has_recent_signal, ...)`-bearing rows into two lists: those
   with recent signals (top of the dropdown) and the rest. The SQL
   layer ordered them already; this helper just finds the boundary.

If a future feature wants Indian-style lakh grouping on the price
tag (relevant for index values like Sensex >= 1,00,000), swap
`format_price_label` to delegate to the existing
`notifications/batcher._format_indian_crores`-style helper. Keeping
the formatting choice isolated here means the view itself stays
agnostic.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Protocol
from collections.abc import Iterable


class _HasRecentFlag(Protocol):
    """Structural type for any row carrying a `has_recent_signal`
    boolean. Lets `partition_ticker_rows` work with either the SQL
    layer's `TickerComboRow` dataclass or a test stub without an
    explicit base class."""
    has_recent_signal: bool


def format_price_label(value: float) -> str:
    """Right-axis price tag text. e.g. 2955.10 -> '2,955.1'.

    1 decimal, western comma grouping. Indian lakh-style grouping
    isn't used here — most equity prices sit well below ₹1,00,000
    where the two styles agree.
    """
    return f"{value:,.1f}"


def format_date_label(dt: datetime, *, timeframe: str = "1W") -> str:
    """Bottom-axis date tag text. e.g. 2026-06-15 -> '15-Jun-26'.

    Both 1W and 1D render as `DD-Mon-YY`. The 1W timestamps stored on
    weekly bars are already Monday-anchored IST (see Pine-style
    bucketing in the project memory), so the caller can pass the bar
    timestamp directly without snapping. The `timeframe` kwarg is
    accepted for symmetry with future timeframes that might want a
    different format (e.g., '1H' -> 'HH:MM'); it's a no-op today.
    """
    return dt.strftime("%d-%b-%y")


# Signal-group rank — lower sorts first. Matches the four signal_type
# values that signal_events carries today. EXIT_FROM_BUY and
# EXIT_FROM_SELL share a rank because they share the same blue badge
# in the Tickers tab and the same blue background in the dropdown.
SIGNAL_GROUP_RANK: dict[str, int] = {
    "BUY": 0,
    "SELL": 1,
    "EXIT_FROM_BUY": 2,
    "EXIT_FROM_SELL": 2,
}
UNSIGNALLED_RANK: int = 99


def signal_industry_mcap_sort_key(
    row, signal_type: str | None,
) -> tuple:
    """Sort key used by both the Tickers tab proxy and the Signals
    tab dropdown for signalled rows.

    Ordering, in priority:
      1. Signal group: BUY < SELL < EXIT_* < unknown.
      2. Industry name ASC, with industry IS NULL last.
      3. Market cap DESC, with market_cap IS NULL last (within an
         industry, a row without a tagged mcap can't be ranked, so
         it sinks to the bottom of its industry sub-group).
      4. Symbol ASC (case-insensitive) as a deterministic tiebreak.

    `row` must expose `.industry`, `.market_cap`, `.symbol`. Missing
    attributes are treated as None so the helper works on either
    `TickerRow` (Tickers tab) or `TickerComboRow` (Signals dropdown).
    `signal_type` is the four-string signal_events value or None.
    """
    sig_rank = SIGNAL_GROUP_RANK.get(signal_type, UNSIGNALLED_RANK) \
        if signal_type is not None else UNSIGNALLED_RANK

    industry = getattr(row, 'industry', None)
    industry_is_null = industry is None
    industry_key = (industry or '').lower()

    mcap = getattr(row, 'market_cap', None)
    mcap_is_null = mcap is None
    # Negate so larger mcap sorts ascending-first.
    # When mcap is None the bool sentinel above already pushes the
    # row to the bottom; the Decimal(0) placeholder is just a
    # comparable sentinel so the tuple stays well-typed.
    mcap_key: Decimal = -mcap if mcap is not None else Decimal(0)

    return (
        sig_rank,
        industry_is_null,
        industry_key,
        mcap_is_null,
        mcap_key,
        row.symbol.lower(),
    )


def partition_ticker_rows(
    rows: Iterable[_HasRecentFlag],
) -> tuple[list, list]:
    """Split a pre-sorted iterable into (recent, other) lists.

    Input contract: the source iterable must be sorted such that all
    `has_recent_signal=True` rows precede all `has_recent_signal=False`
    rows. The SQL query that feeds this (queries.ticker_universe_-
    with_recent_signals) does that ordering server-side.

    Either output list may be empty:
      - empty `recent` when no signals fired in the last 2 weeks
        (quiet market, or initial deploy)
      - empty `other` is unusual — every ticker fired in the last
        2 weeks — but not erroneous

    Doesn't validate the ordering. A misordered input produces a
    partition where the second list contains some `True` rows; the
    view would then put them in the grey-bg section incorrectly. We
    trust the SQL layer's ORDER BY rather than paying for a sort here.
    """
    recent: list = []
    other: list = []
    for row in rows:
        if row.has_recent_signal:
            recent.append(row)
        else:
            other.append(row)
    return recent, other