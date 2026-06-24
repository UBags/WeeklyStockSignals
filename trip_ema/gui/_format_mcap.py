"""Format a market-cap value (INR) for GUI labels.

Operator spec (input value is INR; mcap_cr = inr / 1e7):
    mcap_cr >= 100,000        -> "1.XX L cr"   (1 lakh crore = 100,000 cr)
    10,000 <= mcap_cr < 100k  -> "XX.X K cr"
    1,000  <= mcap_cr < 10k   -> "X.X K cr"
    mcap_cr < 1,000           -> "XXX cr"

Used by:
  * trip_ema/gui/industry_panel.py — mini grid titles
  * trip_ema/gui/signals_view.py    — Bar detail Regime line
"""
from __future__ import annotations
from decimal import Decimal
from typing import Optional, Union


def format_mcap_cr(mcap_inr: Optional[Union[Decimal, float, int]]) -> str:
    """Render an INR market cap as a short crore-denominated label.

    Returns an empty string for None / non-positive inputs so callers
    can compose titles without conditionals: an empty mcap simply
    contributes nothing to the title.
    """
    if mcap_inr is None:
        return ""
    try:
        val = float(mcap_inr)
    except (TypeError, ValueError):
        return ""
    if val <= 0:
        return ""

    cr = val / 1e7  # INR -> crore (1 cr = 10,000,000 INR)

    if cr >= 100_000:
        return f"{cr/100_000:.2f} L cr"
    if cr >= 1_000:
        return f"{cr/1000:.1f} K cr"
    # < 1,000 cr -> integer rupees-in-crore
    return f"{cr:.0f} cr"
