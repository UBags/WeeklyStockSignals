"""Clean up ruff warnings introduced by commits 8bfa0d9 + 6896097.

Skipped on purpose: UP017 in test_last_run_signals_window.py (datetime.UTC
is 3.11+; your pytest runs on system Python 3.10, so `timezone.utc`
stays). The pre-existing UP035 / UP045 / SIM* warnings across queries.py,
tickers.py, signals.py are codebase-wide noise — left alone.
"""
from __future__ import annotations
import pathlib

REPO = pathlib.Path.home() / "PycharmProjects" / "WeeklyStockSignals"

def patch(rel, old, new, *, label):
    p = REPO / rel
    text = p.read_text()
    n = text.count(old)
    assert n == 1, f"[{label}] expected 1, found {n} in {rel}"
    p.write_text(text.replace(old, new))
    print(f"  ✓ {label}")

# signals_helpers.py — new function signature
patch(
    "trip_ema/gui/signals_helpers.py",
    label="signals_helpers.sort_key.signature",
    old="def signal_industry_mcap_sort_key(\n    row, signal_type: Optional[str],\n) -> tuple:",
    new="def signal_industry_mcap_sort_key(\n    row, signal_type: str | None,\n) -> tuple:",
)

# new test file — three Optional sites + one Yoda
patch(
    "tests/gui_pure/test_signal_industry_mcap_sort_key.py",
    label="test.imports+_Row",
    old=(
        "from decimal import Decimal\n"
        "from typing import Optional\n"
        "\n"
        "from trip_ema.gui.signals_helpers import (\n"
        "    SIGNAL_GROUP_RANK,\n"
        "    UNSIGNALLED_RANK,\n"
        "    signal_industry_mcap_sort_key,\n"
        ")\n"
        "\n"
        "\n"
        "@dataclass\n"
        "class _Row:\n"
        "    symbol: str\n"
        "    industry: Optional[str] = None\n"
        "    market_cap: Optional[Decimal] = None\n"
        "\n"
        "\n"
        "def _sorted_symbols(items: list[tuple[_Row, Optional[str]]]) -> list[str]:\n"
    ),
    new=(
        "from decimal import Decimal\n"
        "\n"
        "from trip_ema.gui.signals_helpers import (\n"
        "    SIGNAL_GROUP_RANK,\n"
        "    UNSIGNALLED_RANK,\n"
        "    signal_industry_mcap_sort_key,\n"
        ")\n"
        "\n"
        "\n"
        "@dataclass\n"
        "class _Row:\n"
        "    symbol: str\n"
        "    industry: str | None = None\n"
        "    market_cap: Decimal | None = None\n"
        "\n"
        "\n"
        "def _sorted_symbols(items: list[tuple[_Row, str | None]]) -> list[str]:\n"
    ),
)

patch(
    "tests/gui_pure/test_signal_industry_mcap_sort_key.py",
    label="test.unyoda",
    old="    assert UNSIGNALLED_RANK > max(SIGNAL_GROUP_RANK.values())\n",
    new="    assert max(SIGNAL_GROUP_RANK.values()) < UNSIGNALLED_RANK\n",
)

print("Done.")