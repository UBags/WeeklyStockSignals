"""Tests for the progress_cb hook in recompute_market (Step 13d-1).

Verifies the hook fires at every stage boundary with the right
shape: (stage_name, message). Uses monkeypatching to stub each
_stage_* helper so the test runs in isolation (no DB).

NOTE on imports: `trip_ema.orchestrator.__init__.py` re-exports
`recompute_market` (the function name), which SHADOWS the
submodule of the same name at the package-attribute level. So we
use `importlib.import_module()` to get the actual module
unambiguously.
"""
from __future__ import annotations

import importlib
from datetime import datetime, UTC
from unittest.mock import MagicMock

import pytest

# `from trip_ema.orchestrator import recompute_market as rm_mod` would
# yield the FUNCTION (due to __init__ re-export). importlib gives the
# module.
rm_mod = importlib.import_module(
    "trip_ema.orchestrator.recompute_market",
)
recompute_market = rm_mod.recompute_market
_emit_progress = rm_mod._emit_progress


# Stage names emitted by recompute_market (mirror the order in the
# function body). The CLI's progress_cb mapping in cmd_cycle relies
# on this exact set.
EXPECTED_STAGES = [
    "stock_state",
    "stock_enrichment",
    "signals",
    "cache_rebuild",
    "iiv",
    "industry_state",
    "industry_enrichment",
]


@pytest.fixture
def stub_stages(monkeypatch):
    """Stub all 8 _stage_* helpers to no-op returns so recompute_market
    runs to completion without touching the DB."""
    monkeypatch.setattr(
        rm_mod, "_stage_stock_state",
        lambda *a, **k: (10, 0, 0),
    )
    monkeypatch.setattr(
        rm_mod, "_stage_stock_enrichment",
        lambda *a, **k: (10, 0, 0),
    )
    monkeypatch.setattr(
        rm_mod, "_stage_rotate_signals",
        lambda *a, **k: 5,
    )
    monkeypatch.setattr(
        rm_mod, "_stage_derive_and_insert",
        lambda *a, **k: 3,
    )
    monkeypatch.setattr(
        rm_mod, "_stage_rebuild_cache",
        lambda *a, **k: True,
    )
    monkeypatch.setattr(
        rm_mod, "_stage_iiv_refresh",
        lambda *a, **k: (50, 0, 0),
    )
    monkeypatch.setattr(
        rm_mod, "_stage_industry_state",
        lambda *a, **k: (50, 0, 0),
    )
    monkeypatch.setattr(
        rm_mod, "_stage_industry_enrichment",
        lambda *a, **k: (50, 0, 0),
    )


# ============================================================
# _emit_progress helper
# ============================================================

class TestEmitProgress:

    def test_none_callback_is_noop(self):
        _emit_progress(None, "stage_x", "running")

    def test_calls_callback_with_args(self):
        calls = []
        _emit_progress(
            lambda s, m: calls.append((s, m)),
            "stage_x", "running",
        )
        assert calls == [("stage_x", "running")]

    def test_swallows_callback_exception(self):
        def bad_cb(stage, msg):
            raise RuntimeError("intentional")
        _emit_progress(bad_cb, "stage_x", "running")


# ============================================================
# progress_cb wiring across the 8 stages
# ============================================================

class TestProgressCbWiring:

    def test_fires_running_for_every_stage(self, stub_stages):
        calls = []
        cache = MagicMock()
        cache.signal_count = lambda: 0
        recompute_market(
            db_engine=MagicMock(),
            as_of=datetime(2026, 6, 28, 12, 0, tzinfo=UTC),
            cache=cache,
            progress_cb=lambda s, m: calls.append((s, m)),
        )
        running_emits = [c for c in calls if c[1] == "running"]
        running_stages = [c[0] for c in running_emits]
        assert running_stages == EXPECTED_STAGES, (
            f"Expected running emits in order {EXPECTED_STAGES}, "
            f"got {running_stages}"
        )

    def test_fires_done_for_every_stage(self, stub_stages):
        calls = []
        cache = MagicMock()
        cache.signal_count = lambda: 0
        recompute_market(
            db_engine=MagicMock(),
            cache=cache,
            progress_cb=lambda s, m: calls.append((s, m)),
        )
        done_stages = [c[0] for c in calls if c[1] == "done"]
        assert done_stages == EXPECTED_STAGES

    def test_pairs_running_done_in_order(self, stub_stages):
        calls = []
        cache = MagicMock()
        cache.signal_count = lambda: 0
        recompute_market(
            db_engine=MagicMock(), cache=cache,
            progress_cb=lambda s, m: calls.append((s, m)),
        )
        for i, stage in enumerate(EXPECTED_STAGES):
            assert calls[i * 2] == (stage, "running")
            assert calls[i * 2 + 1] == (stage, "done")

    def test_cache_rebuild_skipped_when_cache_is_none(
        self, stub_stages,
    ):
        calls = []
        recompute_market(
            db_engine=MagicMock(),
            cache=None,
            progress_cb=lambda s, m: calls.append((s, m)),
        )
        cache_emits = [
            c for c in calls if c[0] == "cache_rebuild"
        ]
        assert cache_emits == []

    def test_no_progress_cb_is_safe(self, stub_stages):
        cache = MagicMock()
        cache.signal_count = lambda: 0
        result = recompute_market(
            db_engine=MagicMock(), cache=cache,
        )
        assert result.stage_errors == []


# ============================================================
# Failure path: stage halt fires "failed:" emit + stops downstream
# ============================================================

class TestFailurePath:

    def test_stock_state_halt_emits_failed(self, monkeypatch):
        def boom(*a, **k):
            raise ValueError("synthetic stock_state failure")
        monkeypatch.setattr(
            rm_mod, "_stage_stock_state", boom,
        )
        for name in (
            "_stage_stock_enrichment", "_stage_rotate_signals",
            "_stage_derive_and_insert", "_stage_rebuild_cache",
            "_stage_iiv_refresh", "_stage_industry_state",
            "_stage_industry_enrichment",
        ):
            monkeypatch.setattr(
                rm_mod, name, lambda *a, **k: (0, 0, 0),
            )

        calls = []
        result = recompute_market(
            db_engine=MagicMock(),
            progress_cb=lambda s, m: calls.append((s, m)),
        )
        assert calls[0] == ("stock_state", "running")
        assert calls[1][0] == "stock_state"
        assert calls[1][1].startswith("failed:")
        assert "synthetic stock_state failure" in calls[1][1]
        for c in calls[2:]:
            assert c[0] == "stock_state"
        assert result.stage_errors == [
            ("stock_state", "synthetic stock_state failure"),
        ]

    def test_iiv_halt_emits_failed_skips_industry_stages(
        self, monkeypatch,
    ):
        monkeypatch.setattr(
            rm_mod, "_stage_stock_state",
            lambda *a, **k: (10, 0, 0),
        )
        monkeypatch.setattr(
            rm_mod, "_stage_stock_enrichment",
            lambda *a, **k: (10, 0, 0),
        )
        monkeypatch.setattr(
            rm_mod, "_stage_rotate_signals",
            lambda *a, **k: 5,
        )
        monkeypatch.setattr(
            rm_mod, "_stage_derive_and_insert",
            lambda *a, **k: 3,
        )
        monkeypatch.setattr(
            rm_mod, "_stage_rebuild_cache",
            lambda *a, **k: True,
        )

        def boom(*a, **k):
            raise RuntimeError("iiv boom")
        monkeypatch.setattr(rm_mod, "_stage_iiv_refresh", boom)

        industry_stages_called = []
        def watch_industry_state(*a, **k):
            industry_stages_called.append("industry_state")
            return (0, 0, 0)
        def watch_industry_enrich(*a, **k):
            industry_stages_called.append("industry_enrichment")
            return (0, 0, 0)
        monkeypatch.setattr(
            rm_mod, "_stage_industry_state",
            watch_industry_state,
        )
        monkeypatch.setattr(
            rm_mod, "_stage_industry_enrichment",
            watch_industry_enrich,
        )

        calls = []
        cache = MagicMock()
        cache.signal_count = lambda: 0
        recompute_market(
            db_engine=MagicMock(), cache=cache,
            progress_cb=lambda s, m: calls.append((s, m)),
        )
        assert industry_stages_called == []
        iiv_failed = [
            c for c in calls
            if c[0] == "iiv" and c[1].startswith("failed:")
        ]
        assert len(iiv_failed) == 1
        assert "iiv boom" in iiv_failed[0][1]
