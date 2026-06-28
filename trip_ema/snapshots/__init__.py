"""trip_ema/snapshots/ — Step 13a.

Aggregate-row snapshot machinery for the ETL Status GUI.

Currently contains:
  - MarketOverviewSnapshotter: writes one row to
    market_overview_daily per (snapshot_date, exchange), powering
    the GUI's Overall Market Overview footer + the 90-day trend
    dialogs.

Future additions (post-13a):
  - Backfill stage 10 will use a walking variant of this
    snapshotter to compute historical daily snapshots by
    re-aggregating daily bars and re-running the indicator
    pipeline as-of each historical day.
"""
from .market_overview import (
    MarketOverviewCounts,
    MarketOverviewSnapshotter,
)

__all__ = [
    "MarketOverviewCounts",
    "MarketOverviewSnapshotter",
]
