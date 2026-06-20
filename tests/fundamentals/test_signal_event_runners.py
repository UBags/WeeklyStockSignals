"""Tests for Phase 6-1 signal event runners."""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pytest

from trip_ema.fundamentals import IndicatorState
from trip_ema.fundamentals.runners import (
    run_signal_events_for_all_industries,
    run_signal_events_for_industry,
)
from trip_ema.persistence import transaction
from trip_ema.persistence.fundamentals_repo import (
    FundamentalsRunRepository,
    IndicatorStateRepository,
    IndustryIndexRepository,
    IndustryIndexValueRepository,
    SignalEventRepository,
)


# ---------- helpers ---------- #


def _setup_industry_with_indicator_states(
    db_engine, name: str = "IT - Services",
    display_sequence: list[int] = None,
):
    """Create an industry index, populate index_value AND
    indicator_state rows with a hand-crafted display_regime sequence.

    The display_sequence is the list of display_regime values to
    write, one per consecutive weekly bar starting from 2025-10-01.
    """
    if display_sequence is None:
        display_sequence = [0, 1, 1, 0, -1, -1, 0]

    base_date = date(2025, 10, 1)
    with transaction(db_engine) as conn:
        IndustryIndexRepository(conn).upsert(
            name, sector_name="Technology", base_date=base_date,
        )
        value_repo = IndustryIndexValueRepository(conn)
        state_repo = IndicatorStateRepository(conn)
        for i, display in enumerate(display_sequence):
            td = base_date + timedelta(days=i * 7)
            close_value = 1000.0 + i * 5.0
            value_repo.upsert_value(
                name, trade_date=td, value=close_value,
                constituents_used=10, constituents_missing=0,
                renormalized=False,
                effective_rebalance_date=base_date,
            )
            state_repo.upsert_for_industry(IndicatorState(
                ticker_id=None, timeframe=None, industry_name=name,
                trade_date=td,
                med_ema=close_value, long_ema=close_value,
                longest_ema=close_value,
                atr=10.0,
                upper_inner_band=close_value + 2.0,
                lower_inner_band=close_value - 2.0,
                upper_mid_band=close_value + 20.0,
                lower_mid_band=close_value - 20.0,
                upper_outer_band=close_value + 45.0,
                lower_outer_band=close_value - 45.0,
                core_regime=display, display_regime=display,
                pierces_upper_mid=False, pierces_lower_mid=False,
                dist_to_med_in_hl_units=None,
                dist_to_long_in_hl_units=None,
                dist_to_longest_in_hl_units=None,
                mean_dist_to_med_in_hl_units=None,
                mean_dist_to_long_in_hl_units=None,
                mean_dist_to_longest_in_hl_units=None,
                bars_since_med_turn=None, bars_since_long_turn=None,
                bars_since_longest_turn=None,
                pct_change_since_med_turn=None,
                pct_change_since_long_turn=None,
                pct_change_since_longest_turn=None,
                med_pattern_label="NA", long_pattern_label="NA",
                longest_pattern_label="NA",
                med_turn_buy=False, med_turn_sell=False,
                long_turn_buy=False, long_turn_sell=False,
                longest_turn_buy=False, longest_turn_sell=False,
                computed_at=datetime.now(timezone.utc),
            ))


# ---------- run_signal_events_for_industry ---------- #


class TestRunSignalEventsForIndustry:

    def test_writes_events_for_known_sequence(self, db_engine):
        """display_sequence: 0,1,1,0,-1,-1,0 should fire:
          bar 1: BUY (0→1)
          bar 3: EXIT_FROM_BUY (1→0)
          bar 4: SELL (0→-1)
          bar 6: EXIT_FROM_SELL (-1→0)
        4 events total."""
        _setup_industry_with_indicator_states(
            db_engine, "IT - Services",
            display_sequence=[0, 1, 1, 0, -1, -1, 0],
        )
        run_id = run_signal_events_for_industry(
            db_engine, "IT - Services",
        )
        with db_engine.connect() as conn:
            r = FundamentalsRunRepository(conn).get(run_id)
            events = SignalEventRepository(conn).get_for_industry(
                "IT - Services",
            )
        assert r.status == "SUCCESS"
        assert r.succeeded == 4
        # 4 events with the expected kinds.
        kinds = sorted(e.event_kind for e in events)
        assert kinds == ["BUY", "EXIT_FROM_BUY", "EXIT_FROM_SELL", "SELL"]

    def test_close_at_event_reflects_index_value(self, db_engine):
        """The close_at_event field should match the
        industry_index_value at the event date, not the EMA value."""
        # bar 1 has display=+1, value=1005.0 (1000 + 1*5).
        _setup_industry_with_indicator_states(
            db_engine, "IT - Services",
            display_sequence=[0, 1, 0],
        )
        run_signal_events_for_industry(db_engine, "IT - Services")
        with db_engine.connect() as conn:
            events = SignalEventRepository(conn).get_for_industry(
                "IT - Services",
            )
        buy_events = [e for e in events if e.event_kind == "BUY"]
        assert len(buy_events) == 1
        # bar index 1 → value = 1000 + 1*5 = 1005.
        assert buy_events[0].close_at_event == 1005.0

    def test_idempotent_via_skip_if_recent(self, db_engine):
        """First call writes events. Second call hits skip-if-recent
        and writes nothing new.

        Skip-if-recent is based on event date age (must be < 80 days).
        So we need recent dates for this test to exercise the guard.
        Override the default base_date in the helper."""
        # Build with dates ending today.
        name = "IT - Services"
        today = date.today()
        base_date = today - timedelta(days=14)
        with transaction(db_engine) as conn:
            IndustryIndexRepository(conn).upsert(
                name, sector_name="Technology", base_date=base_date,
            )
            value_repo = IndustryIndexValueRepository(conn)
            state_repo = IndicatorStateRepository(conn)
            for i, display in enumerate([0, 1, 0]):
                td = base_date + timedelta(days=i * 7)
                close_value = 1000.0 + i * 5.0
                value_repo.upsert_value(
                    name, trade_date=td, value=close_value,
                    constituents_used=10, constituents_missing=0,
                    renormalized=False,
                    effective_rebalance_date=base_date,
                )
                state_repo.upsert_for_industry(IndicatorState(
                    ticker_id=None, timeframe=None, industry_name=name,
                    trade_date=td,
                    med_ema=close_value, long_ema=close_value,
                    longest_ema=close_value,
                    atr=10.0,
                    upper_inner_band=close_value + 2.0,
                    lower_inner_band=close_value - 2.0,
                    upper_mid_band=close_value + 20.0,
                    lower_mid_band=close_value - 20.0,
                    upper_outer_band=close_value + 45.0,
                    lower_outer_band=close_value - 45.0,
                    core_regime=display, display_regime=display,
                    pierces_upper_mid=False, pierces_lower_mid=False,
                    dist_to_med_in_hl_units=None,
                    dist_to_long_in_hl_units=None,
                    dist_to_longest_in_hl_units=None,
                    mean_dist_to_med_in_hl_units=None,
                    mean_dist_to_long_in_hl_units=None,
                    mean_dist_to_longest_in_hl_units=None,
                    bars_since_med_turn=None, bars_since_long_turn=None,
                    bars_since_longest_turn=None,
                    pct_change_since_med_turn=None,
                    pct_change_since_long_turn=None,
                    pct_change_since_longest_turn=None,
                    med_pattern_label="NA", long_pattern_label="NA",
                    longest_pattern_label="NA",
                    med_turn_buy=False, med_turn_sell=False,
                    long_turn_buy=False, long_turn_sell=False,
                    longest_turn_buy=False, longest_turn_sell=False,
                    computed_at=datetime.now(timezone.utc),
                ))
        run_id_1 = run_signal_events_for_industry(db_engine, name)
        run_id_2 = run_signal_events_for_industry(db_engine, name)
        with db_engine.connect() as conn:
            r1 = FundamentalsRunRepository(conn).get(run_id_1)
            r2 = FundamentalsRunRepository(conn).get(run_id_2)
        # First wrote, second skipped.
        assert r1.succeeded == 2
        assert r2.succeeded == 0
        assert r2.skipped == 1

    def test_force_re_runs(self, db_engine):
        """With force=True, skip-if-recent is bypassed."""
        _setup_industry_with_indicator_states(
            db_engine, "IT - Services",
            display_sequence=[0, 1, 0],
        )
        run_signal_events_for_industry(db_engine, "IT - Services")
        run_id_2 = run_signal_events_for_industry(
            db_engine, "IT - Services", force=True,
        )
        with db_engine.connect() as conn:
            r2 = FundamentalsRunRepository(conn).get(run_id_2)
        assert r2.succeeded == 2

    def test_delete_then_insert_clears_stale_rows(self, db_engine):
        """If the indicator_state changes and a re-compute is forced,
        the old signal_events rows should be deleted, not just
        upserted-on-top. Otherwise stale rows linger.

        Scenario: first compute with display [0,1,0] → 2 events.
        Then mutate display to [0,-1,0] (the row at bar 1 changes
        from +1 to -1) → recompute should now have SELL+EXIT_FROM_SELL,
        and NO BUY/EXIT_FROM_BUY left from the prior run."""
        _setup_industry_with_indicator_states(
            db_engine, "IT - Services",
            display_sequence=[0, 1, 0],
        )
        run_signal_events_for_industry(db_engine, "IT - Services")
        # Mutate the indicator_state to change the signals.
        # (Re-running _setup overwrites the indicator_state rows.)
        _setup_industry_with_indicator_states(
            db_engine, "IT - Services",
            display_sequence=[0, -1, 0],
        )
        # Force re-compute.
        run_signal_events_for_industry(
            db_engine, "IT - Services", force=True,
        )
        with db_engine.connect() as conn:
            events = SignalEventRepository(conn).get_for_industry(
                "IT - Services",
            )
        kinds = sorted(e.event_kind for e in events)
        # Should be just SELL and EXIT_FROM_SELL now; no BUY or EXIT_FROM_BUY.
        assert kinds == ["EXIT_FROM_SELL", "SELL"]

    def test_raises_if_industry_does_not_exist(self, db_engine):
        with pytest.raises(ValueError, match="does not exist"):
            run_signal_events_for_industry(
                db_engine, "Nonexistent Industry",
            )

    def test_raises_if_no_indicator_state(self, db_engine):
        """Industry exists but no indicator_state rows → operator
        needs to run compute-indicator-state first."""
        with transaction(db_engine) as conn:
            IndustryIndexRepository(conn).upsert(
                "Empty Industry", sector_name="Other",
                base_date=date(2025, 10, 1),
            )
        with pytest.raises(ValueError, match="no indicator_state"):
            run_signal_events_for_industry(
                db_engine, "Empty Industry",
            )

    def test_no_events_when_display_always_zero(self, db_engine):
        """A series stuck at display=0 throughout produces no events."""
        _setup_industry_with_indicator_states(
            db_engine, "IT - Services",
            display_sequence=[0, 0, 0, 0, 0],
        )
        run_id = run_signal_events_for_industry(
            db_engine, "IT - Services",
        )
        with db_engine.connect() as conn:
            r = FundamentalsRunRepository(conn).get(run_id)
            events = SignalEventRepository(conn).get_for_industry(
                "IT - Services",
            )
        assert r.succeeded == 0
        assert events == []


# ---------- run_signal_events_for_all_industries ---------- #


class TestRunSignalEventsForAllIndustries:

    def test_succeeds_for_all_industries(self, db_engine):
        _setup_industry_with_indicator_states(
            db_engine, "Industry A",
            display_sequence=[0, 1, 0],
        )
        _setup_industry_with_indicator_states(
            db_engine, "Industry B",
            display_sequence=[0, -1, 0],
        )
        report = run_signal_events_for_all_industries(db_engine)
        names_succeeded = {name for name, _ in report["succeeded"]}
        assert names_succeeded == {"Industry A", "Industry B"}
        assert report["failed"] == []

    def test_skips_industries_with_no_indicator_state(self, db_engine):
        _setup_industry_with_indicator_states(
            db_engine, "Industry A",
            display_sequence=[0, 1, 0],
        )
        with transaction(db_engine) as conn:
            IndustryIndexRepository(conn).upsert(
                "Industry Empty", sector_name="Other",
                base_date=date(2025, 10, 1),
            )
        report = run_signal_events_for_all_industries(db_engine)
        names_succeeded = {name for name, _ in report["succeeded"]}
        names_skipped = {name for name, _ in report["skipped"]}
        assert names_succeeded == {"Industry A"}
        assert names_skipped == {"Industry Empty"}
        for _, reason in report["skipped"]:
            assert "no indicator_state" in reason

    def test_continues_after_one_fails(self, db_engine, monkeypatch):
        _setup_industry_with_indicator_states(
            db_engine, "Industry Good",
            display_sequence=[0, 1, 0],
        )
        _setup_industry_with_indicator_states(
            db_engine, "Industry Bad",
            display_sequence=[0, 1, 0],
        )
        from trip_ema.fundamentals import runners as runners_mod
        original = runners_mod.run_signal_events_for_industry

        def patched(engine, name, **kw):
            if name == "Industry Bad":
                raise RuntimeError("simulated failure")
            return original(engine, name, **kw)

        monkeypatch.setattr(
            runners_mod, "run_signal_events_for_industry", patched,
        )
        report = run_signal_events_for_all_industries(db_engine)
        names_succeeded = {name for name, _ in report["succeeded"]}
        names_failed = {name for name, _ in report["failed"]}
        assert names_succeeded == {"Industry Good"}
        assert names_failed == {"Industry Bad"}

    def test_empty_when_no_indices_exist(self, db_engine):
        report = run_signal_events_for_all_industries(db_engine)
        assert report["succeeded"] == []
        assert report["skipped"] == []
        assert report["failed"] == []
