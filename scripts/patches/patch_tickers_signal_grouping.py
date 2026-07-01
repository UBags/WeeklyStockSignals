#!/usr/bin/env python3
"""
Patcher: Tickers tab — signal-type-primary grouping with industry sub-groups.

Delivers the operator spec:
  a) BUYs (green bg) grouped by industry, desc by rank
  b) SELLs (red bg) grouped by industry, desc by rank
  c) EXIT_FROM_BUY (yellow bg) grouped by industry, desc by rank
  d) EXIT_FROM_SELL (gray bg) grouped by industry, desc by rank
  e) All others: grouped by industry, alphabetical by symbol

Files touched:
  1. trip_ema/gui/signals_helpers.py               (add new sort key)
  2. trip_ema/gui/views/tickers.py                 (color map + proxy rewire)
  3. tests/gui/test_tickers_signal_coloring.py     (updated assertions)

Idempotent: re-running is a no-op after successful first run (sentinel checks).

Run from repo root:
    python scripts/patches/patch_tickers_signal_grouping.py
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
HELPERS = REPO / "trip_ema/gui/signals_helpers.py"
TICKERS = REPO / "trip_ema/gui/views/tickers.py"
TESTS   = REPO / "tests/gui/test_tickers_signal_coloring.py"


# ============================================================
# Patch 1: signals_helpers.py — add new sort key
# ============================================================
HELPERS_SENTINEL = "signal_group_industry_rank_sort_key"

HELPERS_ANCHOR = "def partition_ticker_rows(\n"

HELPERS_INSERT = '''# Step 12c: signal-type-primary sort for the Tickers tab.
# Contrasts with `industry_rank_alpha_sort_key` above which groups by
# industry first. This one groups by SIGNAL TYPE first — the Tickers
# page spec (per operator) wants all BUYs first, then SELLs, then
# EXIT_FROM_BUY, then EXIT_FROM_SELL, then the rest. Within each
# signaled group: industry ASC, then rank_score DESC.
TICKER_TAB_SIGNAL_GROUP_RANK: dict[str, int] = {
    "BUY": 0,
    "SELL": 1,
    "EXIT_FROM_BUY": 2,
    "EXIT_FROM_SELL": 3,
}


def signal_group_industry_rank_sort_key(
    row, signal_type: str | None,
    rank_score: float | None = None,
) -> tuple:
    """Sort key for the Tickers tab (Step 12c spec).

    Ordering, in priority:
      1. Signal-type group:
           BUY < SELL < EXIT_FROM_BUY < EXIT_FROM_SELL < unsignaled.
      2. Within a signaled group: industry ASC (NULL last), then
         rank_score DESC (missing = 0.0), then symbol ASC.
      3. Within unsignaled: industry ASC (NULL last), then symbol ASC.

    Contrast with `industry_rank_alpha_sort_key` which groups by
    industry FIRST. That helper stays in place for the Signals-tab
    dropdown, which wants a different visual grouping.

    `row` must expose `.industry` and `.symbol`. Missing attributes
    treated as None / empty.
    """
    sig_rank = TICKER_TAB_SIGNAL_GROUP_RANK.get(
        signal_type, UNSIGNALLED_RANK,
    ) if signal_type is not None else UNSIGNALLED_RANK

    industry = getattr(row, 'industry', None)
    industry_is_null = industry is None
    industry_key = (industry or '').lower()

    is_signaled = sig_rank < UNSIGNALLED_RANK
    rank = rank_score if rank_score is not None else 0.0
    # Negate so larger rank_score sorts first among signaled.
    # For unsignaled rows the rank is forced to 0 so the tuple stays
    # well-typed; symbol drives the final ordering.
    rank_key = -rank if is_signaled else 0.0

    return (
        sig_rank,
        industry_is_null,
        industry_key,
        rank_key,
        row.symbol.lower(),
    )


'''


# ============================================================
# Patch 2: tickers.py — colors + import + lessThan
# ============================================================

# 2a: color map
TICKERS_COLOR_OLD = '''SIGNAL_BACKGROUND_COLORS = {
    "BUY":            "#c8e6c9",   # light green
    "SELL":           "#ffcdd2",   # light red
    "EXIT_FROM_BUY":  "#bbdefb",   # light blue (change to neutral from BUY)
    "EXIT_FROM_SELL": "#bbdefb",   # light blue (change to neutral from SELL)
}'''

TICKERS_COLOR_NEW = '''# Step 12c: EXIT_FROM_BUY and EXIT_FROM_SELL now use distinct
# backgrounds so operators can tell "was long, now flat" from
# "was short, now flat" at a glance without reading the signal column.
SIGNAL_BACKGROUND_COLORS = {
    "BUY":            "#c8e6c9",   # light green
    "SELL":           "#ffcdd2",   # light red
    "EXIT_FROM_BUY":  "#fff9c4",   # light yellow (was long, now flat)
    "EXIT_FROM_SELL": "#e0e0e0",   # light gray   (was short, now flat)
}'''

# 2b: import — add signal_group_industry_rank_sort_key
TICKERS_IMPORT_OLD = '''from ..signals_helpers import (
    industry_rank_alpha_sort_key,
    signal_industry_mcap_sort_key,
)'''

TICKERS_IMPORT_NEW = '''from ..signals_helpers import (
    industry_rank_alpha_sort_key,
    signal_group_industry_rank_sort_key,  # Step 12c — Tickers tab sort
    signal_industry_mcap_sort_key,
)'''

# 2c: lessThan body — swap the sort-key call
TICKERS_LESSTHAN_OLD = '''        # Step 12b: single industry-grouped sort key for ALL rows
        # (signaled + unsignaled). Industry primary, then within
        # industry: signaled-rank-DESC then unsignaled-symbol-ASC.
        # See industry_rank_alpha_sort_key for the full ordering
        # contract. The shared helper means the Tickers tab and the
        # Signals dropdown order rows identically.
        left_row = src.row_at(left.row())
        right_row = src.row_at(right.row())
        if left_row is None or right_row is None:
            return super().lessThan(left, right)

        signals = src.last_signals
        ranks = src.rank_scores
        left_sig = signals.get(left_row.ticker_id)
        right_sig = signals.get(right_row.ticker_id)
        left_rank = ranks.get(left_row.ticker_id)
        right_rank = ranks.get(right_row.ticker_id)
        return industry_rank_alpha_sort_key(
            left_row, left_sig, left_rank,
        ) < industry_rank_alpha_sort_key(
            right_row, right_sig, right_rank,
        )'''

TICKERS_LESSTHAN_NEW = '''        # Step 12c: signal-type-primary sort. BUY block first, then
        # SELL, then EXIT_FROM_BUY, then EXIT_FROM_SELL, then the
        # unsignaled remainder. Within each signaled block: industry
        # ASC, rank_score DESC, symbol ASC. Within the unsignaled
        # block: industry ASC, symbol ASC. See
        # signal_group_industry_rank_sort_key for the ordering contract.
        # The Signals-tab dropdown still uses industry_rank_alpha_sort_key
        # (industry-primary) — the two views deliberately differ.
        left_row = src.row_at(left.row())
        right_row = src.row_at(right.row())
        if left_row is None or right_row is None:
            return super().lessThan(left, right)

        signals = src.last_signals
        ranks = src.rank_scores
        left_sig = signals.get(left_row.ticker_id)
        right_sig = signals.get(right_row.ticker_id)
        left_rank = ranks.get(left_row.ticker_id)
        right_rank = ranks.get(right_row.ticker_id)
        return signal_group_industry_rank_sort_key(
            left_row, left_sig, left_rank,
        ) < signal_group_industry_rank_sort_key(
            right_row, right_sig, right_rank,
        )'''


# ============================================================
# Patch 3: tests/gui/test_tickers_signal_coloring.py
# ============================================================

# 3a: `test_exit_signals_share_the_same_blue` → invert
TESTS_EXIT_SHARE_OLD = '''    def test_exit_signals_share_the_same_blue(self):
        # Both EXIT kinds use the same "change to neutral" color.
        assert (
            SIGNAL_BACKGROUND_COLORS["EXIT_FROM_BUY"]
            == SIGNAL_BACKGROUND_COLORS["EXIT_FROM_SELL"]
        )'''

TESTS_EXIT_SHARE_NEW = '''    def test_exit_signals_use_distinct_backgrounds(self):
        # Step 12c: EXIT_FROM_BUY (yellow) and EXIT_FROM_SELL (gray)
        # deliberately differ so the visual encodes prior direction.
        assert (
            SIGNAL_BACKGROUND_COLORS["EXIT_FROM_BUY"]
            != SIGNAL_BACKGROUND_COLORS["EXIT_FROM_SELL"]
        )'''

# 3b: `test_exit_is_blue_family` → split into yellow + gray families
TESTS_EXIT_BLUE_OLD = '''    def test_exit_is_blue_family(self):
        from PySide6.QtGui import QColor
        c = QColor(SIGNAL_BACKGROUND_COLORS["EXIT_FROM_BUY"])
        assert c.blue() > c.red() and c.blue() > c.green()'''

TESTS_EXIT_BLUE_NEW = '''    def test_exit_from_buy_is_yellow_family(self):
        # Step 12c: yellow = R and G high, B low.
        from PySide6.QtGui import QColor
        c = QColor(SIGNAL_BACKGROUND_COLORS["EXIT_FROM_BUY"])
        assert c.red() > c.blue() and c.green() > c.blue()

    def test_exit_from_sell_is_gray_family(self):
        # Step 12c: gray = R ≈ G ≈ B (all channels roughly equal).
        from PySide6.QtGui import QColor
        c = QColor(SIGNAL_BACKGROUND_COLORS["EXIT_FROM_SELL"])
        assert abs(c.red() - c.green()) < 20
        assert abs(c.green() - c.blue()) < 20
        assert abs(c.red() - c.blue()) < 20'''

# 3c: delisted+exit interaction — rewrite around EXIT_FROM_BUY (yellow)
# because gray-text-on-gray-bg (delisted + EXIT_FROM_SELL) is unreadable.
TESTS_DELISTED_OLD = '''    def test_delisted_ticker_with_fresh_exit_signal_has_gray_text_on_blue_bg(self, qtbot):
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
        assert bg.color().blue() > bg.color().red()'''

TESTS_DELISTED_NEW = '''    def test_delisted_ticker_with_fresh_exit_from_buy_has_gray_text_on_yellow_bg(self, qtbot):
        # Step 12c: EXIT_FROM_BUY paints yellow (was long, now flat);
        # delisted still paints gray text on top. Yellow bg + gray text
        # stays readable (contrast is fine).
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
        # Yellow BG — red + green high, blue low.
        assert bg.color().red() > bg.color().blue()
        assert bg.color().green() > bg.color().blue()'''

# 3d: proxy test — invert `share_a_rank` to `ranks_before`
TESTS_PROXY_SHARE_OLD = '''    def test_exit_from_buy_and_exit_from_sell_share_a_rank(self, qtbot):
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
        assert visible == ["ALPHA", "CHARLIE", "DELTA"]'''

TESTS_PROXY_SHARE_NEW = '''    def test_exit_from_buy_ranks_before_exit_from_sell(self, qtbot):
        # Step 12c: EXIT_FROM_BUY (yellow, rank 2) comes before
        # EXIT_FROM_SELL (gray, rank 3). Within each group, no industry
        # is set on the stubs so symbol drives the tiebreak.
        from PySide6.QtCore import Qt

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
        # ALPHA + DELTA (EXIT_FROM_BUY block, symbol ASC) then
        # CHARLIE (EXIT_FROM_SELL block).
        assert visible == ["ALPHA", "DELTA", "CHARLIE"]'''


# ============================================================
# Patch application
# ============================================================

def _apply_replace(path: Path, old: str, new: str, label: str) -> str:
    """Return one of: 'applied', 'idempotent', or raises AssertionError."""
    text = path.read_text()
    if new in text:
        return "idempotent"
    assert old in text, (
        f"[{label}] anchor not found in {path}. "
        f"File may have drifted from expected content. "
        f"Search for:\n---\n{old[:200]}\n---"
    )
    assert text.count(old) == 1, (
        f"[{label}] anchor appears {text.count(old)} times in {path}; "
        f"must be unique for safe replacement."
    )
    path.write_text(text.replace(old, new, 1))
    return "applied"


def _apply_insert(
    path: Path, anchor: str, insert: str, sentinel: str, label: str,
) -> str:
    """Insert `insert` immediately before `anchor` line. Idempotent
    via sentinel presence check (sentinel must be a substring of
    `insert`)."""
    assert sentinel in insert, (
        f"[{label}] sentinel '{sentinel}' not a substring of insert text — "
        "would break idempotency check."
    )
    text = path.read_text()
    if sentinel in text:
        return "idempotent"
    assert anchor in text, f"[{label}] anchor not found in {path}"
    assert text.count(anchor) == 1, (
        f"[{label}] anchor appears {text.count(anchor)} times; must be unique."
    )
    path.write_text(text.replace(anchor, insert + anchor, 1))
    return "applied"


def main() -> int:
    for p in (HELPERS, TICKERS, TESTS):
        if not p.exists():
            print(f"FATAL: expected file missing: {p}", file=sys.stderr)
            return 2

    results: list[tuple[str, str]] = []

    # Patch 1
    results.append((
        "signals_helpers.py: signal_group_industry_rank_sort_key",
        _apply_insert(
            HELPERS, HELPERS_ANCHOR, HELPERS_INSERT,
            HELPERS_SENTINEL, "helpers-1",
        ),
    ))

    # Patch 2a, 2b, 2c
    results.append((
        "tickers.py: SIGNAL_BACKGROUND_COLORS (yellow+gray)",
        _apply_replace(TICKERS, TICKERS_COLOR_OLD, TICKERS_COLOR_NEW, "tickers-2a"),
    ))
    results.append((
        "tickers.py: import signal_group_industry_rank_sort_key",
        _apply_replace(TICKERS, TICKERS_IMPORT_OLD, TICKERS_IMPORT_NEW, "tickers-2b"),
    ))
    results.append((
        "tickers.py: lessThan → signal-type-primary sort",
        _apply_replace(TICKERS, TICKERS_LESSTHAN_OLD, TICKERS_LESSTHAN_NEW, "tickers-2c"),
    ))

    # Patch 3a, 3b, 3c, 3d
    results.append((
        "tests: exit_signals_use_distinct_backgrounds",
        _apply_replace(TESTS, TESTS_EXIT_SHARE_OLD, TESTS_EXIT_SHARE_NEW, "tests-3a"),
    ))
    results.append((
        "tests: split exit family (yellow + gray)",
        _apply_replace(TESTS, TESTS_EXIT_BLUE_OLD, TESTS_EXIT_BLUE_NEW, "tests-3b"),
    ))
    results.append((
        "tests: delisted + EXIT_FROM_BUY on yellow bg",
        _apply_replace(TESTS, TESTS_DELISTED_OLD, TESTS_DELISTED_NEW, "tests-3c"),
    ))
    results.append((
        "tests: exit_from_buy_ranks_before_exit_from_sell",
        _apply_replace(TESTS, TESTS_PROXY_SHARE_OLD, TESTS_PROXY_SHARE_NEW, "tests-3d"),
    ))

    # Report
    print()
    print("=" * 60)
    print(" Patch application results")
    print("=" * 60)
    for label, status in results:
        tag = "✓ applied  " if status == "applied" else "· idempotent"
        print(f"  {tag}  {label}")
    print()

    applied = sum(1 for _, s in results if s == "applied")
    idempotent = sum(1 for _, s in results if s == "idempotent")
    print(f"Summary: {applied} applied, {idempotent} idempotent.")
    if applied == 0:
        print("No changes made — patcher was previously applied.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
