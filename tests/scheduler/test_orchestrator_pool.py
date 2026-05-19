"""Tests for OrchestratorPool.

Covers:
  * Construction: rejects duplicate exchanges
  * Lookup: get, exchanges, all, __contains__, __len__
  * Lifecycle: start_all / stop_all delegates to each orchestrator
  * Lifecycle: a failed start on one orchestrator does NOT abort others
  * Pulse hook: orchestrator_started / orchestrator_stopped events fire
  * Health: any_unhealthy aggregates per-orchestrator status
"""

from __future__ import annotations

from unittest.mock import MagicMock, call

import pytest

from trip_ema.scheduler.orchestrator_pool import OrchestratorPool


def _mock_orch(exchange: str, *, unhealthy: bool = False) -> MagicMock:
    """A MagicMock that quacks like a MarketOrchestrator."""
    m = MagicMock()
    m.exchange = exchange
    m.is_unhealthy.return_value = unhealthy
    return m


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_empty_pool_ok(self):
        pool = OrchestratorPool([])
        assert len(pool) == 0
        assert pool.exchanges() == []
        assert pool.all() == []

    def test_single_orchestrator(self):
        orch = _mock_orch("NSE")
        pool = OrchestratorPool([orch])
        assert len(pool) == 1
        assert pool.exchanges() == ["NSE"]

    def test_multiple_orchestrators(self):
        pool = OrchestratorPool([
            _mock_orch("NSE"), _mock_orch("BSE"), _mock_orch("NYSE"),
        ])
        assert pool.exchanges() == ["BSE", "NSE", "NYSE"]  # sorted

    def test_duplicate_exchange_rejected(self):
        with pytest.raises(ValueError, match="Duplicate exchange"):
            OrchestratorPool([_mock_orch("NSE"), _mock_orch("NSE")])

    def test_duplicate_error_mentions_exchange(self):
        # Operator should see WHICH exchange was duplicated.
        try:
            OrchestratorPool([_mock_orch("BSE"), _mock_orch("BSE")])
        except ValueError as e:
            assert "BSE" in str(e)


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------


class TestLookup:
    @pytest.fixture
    def pool(self) -> OrchestratorPool:
        return OrchestratorPool([
            _mock_orch("NSE"), _mock_orch("BSE"), _mock_orch("NYSE"),
        ])

    def test_get_existing(self, pool):
        result = pool.get("NSE")
        assert result is not None
        assert result.exchange == "NSE"

    def test_get_unknown_returns_none(self, pool):
        assert pool.get("XYZ") is None

    def test_case_sensitive(self, pool):
        # "nse" != "NSE"
        assert pool.get("nse") is None

    def test_contains(self, pool):
        assert "NSE" in pool
        assert "XYZ" not in pool

    def test_len(self, pool):
        assert len(pool) == 3

    def test_all_sorted(self, pool):
        # all() returns orchestrators sorted by exchange code.
        result = pool.all()
        assert [o.exchange for o in result] == ["BSE", "NSE", "NYSE"]


# ---------------------------------------------------------------------------
# Lifecycle: start_all / stop_all
# ---------------------------------------------------------------------------


class TestLifecycle:
    def test_start_all_calls_each(self):
        a, b, c = _mock_orch("A"), _mock_orch("B"), _mock_orch("C")
        pool = OrchestratorPool([a, b, c])
        pool.start_all()
        a.start.assert_called_once()
        b.start.assert_called_once()
        c.start.assert_called_once()

    def test_stop_all_calls_each_with_timeout(self):
        a, b, c = _mock_orch("A"), _mock_orch("B"), _mock_orch("C")
        pool = OrchestratorPool([a, b, c])
        pool.stop_all(timeout_per_orchestrator=5.0)
        a.stop.assert_called_once_with(timeout=5.0)
        b.stop.assert_called_once_with(timeout=5.0)
        c.stop.assert_called_once_with(timeout=5.0)

    def test_start_all_continues_if_one_orchestrator_raises(self):
        a, b, c = _mock_orch("A"), _mock_orch("B"), _mock_orch("C")
        b.start.side_effect = RuntimeError("can't start B")
        pool = OrchestratorPool([a, b, c])
        # Must NOT raise — the pool should continue past B's failure.
        pool.start_all()
        # A and C still got started.
        a.start.assert_called_once()
        c.start.assert_called_once()

    def test_stop_all_continues_if_one_orchestrator_raises(self):
        a, b, c = _mock_orch("A"), _mock_orch("B"), _mock_orch("C")
        b.stop.side_effect = RuntimeError("B is wedged")
        pool = OrchestratorPool([a, b, c])
        pool.stop_all()
        a.stop.assert_called_once()
        c.stop.assert_called_once()


# ---------------------------------------------------------------------------
# Pulse hook integration
# ---------------------------------------------------------------------------


class TestPulseHook:
    def test_orchestrator_started_event_fired_per_orchestrator(self):
        hook = MagicMock()
        a, b = _mock_orch("A"), _mock_orch("B")
        pool = OrchestratorPool([a, b], pulse_hook=hook)
        pool.start_all()
        # Both fired exactly once each.
        hook.publish_orchestrator_started.assert_has_calls([
            call("A"), call("B"),
        ], any_order=True)
        assert hook.publish_orchestrator_started.call_count == 2

    def test_orchestrator_stopped_event_fired_per_orchestrator(self):
        hook = MagicMock()
        a, b = _mock_orch("A"), _mock_orch("B")
        pool = OrchestratorPool([a, b], pulse_hook=hook)
        pool.stop_all()
        hook.publish_orchestrator_stopped.assert_has_calls([
            call("A", reason="normal"),
            call("B", reason="normal"),
        ], any_order=True)

    def test_orchestrator_started_event_NOT_fired_if_start_raises(self):
        # If start() raises, the pulse event should NOT claim
        # "orchestrator started" — that'd be misleading.
        hook = MagicMock()
        a = _mock_orch("A")
        a.start.side_effect = RuntimeError("nope")
        pool = OrchestratorPool([a], pulse_hook=hook)
        pool.start_all()
        hook.publish_orchestrator_started.assert_not_called()

    def test_no_pulse_hook_is_fine(self):
        pool = OrchestratorPool([_mock_orch("A")], pulse_hook=None)
        # Doesn't raise.
        pool.start_all()
        pool.stop_all()


# ---------------------------------------------------------------------------
# Health surface
# ---------------------------------------------------------------------------


class TestHealthSurface:
    def test_any_unhealthy_false_when_all_healthy(self):
        pool = OrchestratorPool([
            _mock_orch("A", unhealthy=False),
            _mock_orch("B", unhealthy=False),
        ])
        assert pool.any_unhealthy() is False

    def test_any_unhealthy_true_when_one_unhealthy(self):
        pool = OrchestratorPool([
            _mock_orch("A", unhealthy=False),
            _mock_orch("B", unhealthy=True),
        ])
        assert pool.any_unhealthy() is True

    def test_any_unhealthy_false_for_empty_pool(self):
        # Empty pool isn't "unhealthy" — it's just empty.
        pool = OrchestratorPool([])
        assert pool.any_unhealthy() is False
