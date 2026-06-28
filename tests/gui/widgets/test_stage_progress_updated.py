"""Tests for the updated LIVE_CYCLE_STAGES (Step 13d-2).

The 13b version had 7 stages in the wrong order. 13d-2 updates
to the 8-stage canonical list that matches cmd_cycle's actual
emission order.
"""
from __future__ import annotations

from trip_ema.gui.views.widgets.stage_progress import (
    LIVE_CYCLE_STAGES,
)
from trip_ema.gui.views.widgets.cycle_stdout_parser import (
    LIVE_CYCLE_LABEL_ORDER,
)


class TestLiveCycleStagesUpdated:

    def test_has_8_stages(self):
        assert len(LIVE_CYCLE_STAGES) == 8

    def test_order_matches_cmd_cycle_emission(self):
        """Order locked by L2 — must match cmd_cycle's actual
        stage emission order so the order-based "previous stages
        done" trick works in the GUI's stdout parser."""
        assert LIVE_CYCLE_STAGES == (
            "Fetch Stock OHLCV",
            "Compute Stock Indicators",
            "Compute Stock Enrichment",
            "Compute Signals",
            "Refresh Industry Index Values",
            "Compute Industry Indicators",
            "Compute Industry Enrichment",
            "Notify Subscribers",
        )

    def test_notify_subscribers_added(self):
        """13b list ended at 'Compute Signals' (wrong). New list
        explicitly includes the notify stage as stage 8."""
        assert "Notify Subscribers" in LIVE_CYCLE_STAGES
        assert LIVE_CYCLE_STAGES[-1] == "Notify Subscribers"

    def test_refresh_industry_index_values_renamed(self):
        """'Compute Industry Indices' (old) → 'Refresh Industry
        Index Values' (per F3 lock)."""
        assert "Compute Industry Indices" not in LIVE_CYCLE_STAGES
        assert (
            "Refresh Industry Index Values" in LIVE_CYCLE_STAGES
        )

    def test_compute_signals_repositioned(self):
        """13b had 'Compute Signals' as the LAST stage. New order
        puts it as stage 4 (after stock_enrichment, before iiv)."""
        compute_signals_idx = LIVE_CYCLE_STAGES.index(
            "Compute Signals",
        )
        assert compute_signals_idx == 3  # 0-indexed → stage 4

    def test_matches_canonical_order_in_parser(self):
        """LIVE_CYCLE_STAGES must equal LIVE_CYCLE_LABEL_ORDER
        from cycle_stdout_parser. Both modules export the same
        canonical list; the parser's labels_before() relies on
        it."""
        assert tuple(LIVE_CYCLE_LABEL_ORDER) == LIVE_CYCLE_STAGES
