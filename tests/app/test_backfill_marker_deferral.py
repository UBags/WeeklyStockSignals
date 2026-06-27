"""Tests for the V1 cutover Step 11a deferral layer in trip_ema.app.

Covers:
  - _make_deferring_compute: invokes base when marker is clear;
    spawns poller + returns None when marker is set
  - _make_deferring_distribute: invokes base when marker is clear;
    returns None when marker is set; does NOT spawn poller
  - _spawn_backfill_poller: dedup (one thread at a time); polls
    until marker clears; runs ONE catch-up tick (compute +
    distribute); resets active flag on exit

The poller is exercised at a tiny poll interval (~50ms) so tests
run in well under a second. Production cadence (15 min) is the
default.
"""
from __future__ import annotations

import os
import threading
import time
from datetime import date
from unittest.mock import MagicMock, patch

import pytest

from trip_ema.persistence import transaction
from trip_ema.persistence.backfill_runs_repo import (
    BackfillRunsRepository,
)


# ============================================================
# Helpers — manipulate the marker directly
# ============================================================

def _set_marker_active(db_engine) -> int:
    with transaction(db_engine) as conn:
        return BackfillRunsRepository(conn).start(
            pid=os.getpid(),
            exchange="NSE",
            date_from=date(2024, 1, 1),
            date_to=date(2024, 12, 31),
            refetch_bars=True,
            ticker_scope="ALL",
        )


def _clear_marker(db_engine, run_id: int) -> None:
    with transaction(db_engine) as conn:
        BackfillRunsRepository(conn).finish(run_id)


def _wait_for(condition, timeout_s: float = 2.0,
              poll_s: float = 0.01) -> bool:
    """Spin-wait for `condition` to return truthy. Returns whether
    it succeeded within the timeout."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(poll_s)
    return False


# ============================================================
# _make_deferring_compute
# ============================================================

class TestDeferringCompute:

    def test_invokes_base_when_marker_clear(self, db_engine):
        from trip_ema.app import (
            _BackfillPollerState,
            _make_deferring_compute,
        )

        base = MagicMock(return_value="base_result")
        state = _BackfillPollerState()
        wrapped = _make_deferring_compute(
            base_callable=base,
            db_engine=db_engine,
            poller_state=state,
            catch_up_distribute=MagicMock(),
        )

        result = wrapped()
        assert result == "base_result"
        base.assert_called_once_with()
        assert state.active is False   # no poller spawned

    def test_returns_none_when_marker_set(self, db_engine):
        from trip_ema.app import (
            _BackfillPollerState,
            _make_deferring_compute,
        )

        base = MagicMock()
        catch_up_distribute = MagicMock()
        state = _BackfillPollerState()
        wrapped = _make_deferring_compute(
            base_callable=base,
            db_engine=db_engine,
            poller_state=state,
            catch_up_distribute=catch_up_distribute,
        )

        rid = _set_marker_active(db_engine)
        try:
            # Patch _spawn_backfill_poller to capture invocation
            # without actually spawning a real thread.
            with patch(
                "trip_ema.app._spawn_backfill_poller",
            ) as spawn:
                result = wrapped()
            assert result is None
            base.assert_not_called()
            spawn.assert_called_once()
            kwargs = spawn.call_args.kwargs
            assert kwargs["db_engine"] is db_engine
            assert kwargs["state"] is state
            assert kwargs["catch_up_compute"] is base
            assert (
                kwargs["catch_up_distribute"]
                is catch_up_distribute
            )
        finally:
            _clear_marker(db_engine, rid)


# ============================================================
# _make_deferring_distribute
# ============================================================

class TestDeferringDistribute:

    def test_invokes_base_when_marker_clear(self, db_engine):
        from trip_ema.app import _make_deferring_distribute

        base = MagicMock(return_value="base_result")
        wrapped = _make_deferring_distribute(
            base_callable=base, db_engine=db_engine,
        )

        result = wrapped()
        assert result == "base_result"
        base.assert_called_once_with()

    def test_returns_none_when_marker_set(self, db_engine):
        from trip_ema.app import _make_deferring_distribute

        base = MagicMock()
        wrapped = _make_deferring_distribute(
            base_callable=base, db_engine=db_engine,
        )

        rid = _set_marker_active(db_engine)
        try:
            result = wrapped()
            assert result is None
            base.assert_not_called()
        finally:
            _clear_marker(db_engine, rid)

    def test_distribute_does_NOT_spawn_poller(self, db_engine):
        """Only the compute wrapper spawns the poller. Distribute
        just no-ops on deferral."""
        from trip_ema.app import _make_deferring_distribute

        wrapped = _make_deferring_distribute(
            base_callable=MagicMock(), db_engine=db_engine,
        )
        rid = _set_marker_active(db_engine)
        try:
            with patch(
                "trip_ema.app._spawn_backfill_poller",
            ) as spawn:
                wrapped()
            spawn.assert_not_called()
        finally:
            _clear_marker(db_engine, rid)


# ============================================================
# _spawn_backfill_poller — the threaded path
# ============================================================

class TestSpawnPoller:

    def test_runs_catch_up_after_marker_clears(self, db_engine):
        """The full lifecycle: marker set → spawn → poll loop →
        marker cleared externally → poller runs ONE catch-up
        compute + distribute → poller exits."""
        from trip_ema.app import (
            _BackfillPollerState,
            _spawn_backfill_poller,
        )

        compute_done = threading.Event()
        distribute_done = threading.Event()

        def _compute():
            compute_done.set()

        def _distribute():
            distribute_done.set()

        state = _BackfillPollerState()
        rid = _set_marker_active(db_engine)
        try:
            thread = _spawn_backfill_poller(
                db_engine=db_engine,
                state=state,
                catch_up_compute=_compute,
                catch_up_distribute=_distribute,
                poll_interval_seconds=0.05,
            )
            assert thread is not None
            assert state.active is True

            # Marker still set; poller should NOT have run anything
            time.sleep(0.2)
            assert not compute_done.is_set()
            assert not distribute_done.is_set()

            # Clear the marker; poller should detect within ~1
            # poll interval and run catch-up
            _clear_marker(db_engine, rid)

            assert _wait_for(compute_done.is_set, timeout_s=2.0)
            assert _wait_for(distribute_done.is_set, timeout_s=1.0)

            # Wait for thread to exit and state.active to flip
            thread.join(timeout=2.0)
            assert not thread.is_alive()
            assert state.active is False
        finally:
            # In case the test failed and marker is still set
            with transaction(db_engine) as conn:
                BackfillRunsRepository(conn).release_locks()

    def test_dedup_returns_none_when_already_active(
        self, db_engine,
    ):
        from trip_ema.app import (
            _BackfillPollerState,
            _spawn_backfill_poller,
        )

        state = _BackfillPollerState()
        rid = _set_marker_active(db_engine)
        try:
            t1 = _spawn_backfill_poller(
                db_engine=db_engine,
                state=state,
                catch_up_compute=MagicMock(),
                catch_up_distribute=MagicMock(),
                poll_interval_seconds=0.05,
            )
            assert t1 is not None
            assert state.active is True

            # Second spawn while first is active returns None
            t2 = _spawn_backfill_poller(
                db_engine=db_engine,
                state=state,
                catch_up_compute=MagicMock(),
                catch_up_distribute=MagicMock(),
                poll_interval_seconds=0.05,
            )
            assert t2 is None

            _clear_marker(db_engine, rid)
            t1.join(timeout=2.0)
            assert state.active is False
        finally:
            with transaction(db_engine) as conn:
                BackfillRunsRepository(conn).release_locks()

    def test_state_active_resets_even_on_catch_up_exception(
        self, db_engine,
    ):
        """If the catch-up compute raises, the poller still exits
        cleanly and resets state.active. The orchestrator's next
        tick should be free to fire normally."""
        from trip_ema.app import (
            _BackfillPollerState,
            _spawn_backfill_poller,
        )

        def _boom():
            raise RuntimeError("simulated catch-up failure")

        state = _BackfillPollerState()
        rid = _set_marker_active(db_engine)
        try:
            thread = _spawn_backfill_poller(
                db_engine=db_engine,
                state=state,
                catch_up_compute=_boom,
                catch_up_distribute=MagicMock(),
                poll_interval_seconds=0.05,
            )
            _clear_marker(db_engine, rid)
            thread.join(timeout=2.0)
            assert not thread.is_alive()
            assert state.active is False
        finally:
            with transaction(db_engine) as conn:
                BackfillRunsRepository(conn).release_locks()

    def test_compute_exception_does_not_block_distribute(
        self, db_engine,
    ):
        """Catch-up compute failing should NOT skip the catch-up
        distribute step — both are independent try/except blocks
        in the poller."""
        from trip_ema.app import (
            _BackfillPollerState,
            _spawn_backfill_poller,
        )

        distribute_done = threading.Event()

        def _boom():
            raise RuntimeError("simulated catch-up compute failure")

        def _distribute():
            distribute_done.set()

        state = _BackfillPollerState()
        rid = _set_marker_active(db_engine)
        try:
            thread = _spawn_backfill_poller(
                db_engine=db_engine,
                state=state,
                catch_up_compute=_boom,
                catch_up_distribute=_distribute,
                poll_interval_seconds=0.05,
            )
            _clear_marker(db_engine, rid)
            assert _wait_for(
                distribute_done.is_set, timeout_s=2.0,
            )
            thread.join(timeout=1.0)
        finally:
            with transaction(db_engine) as conn:
                BackfillRunsRepository(conn).release_locks()


# ============================================================
# Integration — deferring compute fires poller end-to-end
# ============================================================

class TestComputeWrapperEndToEnd:

    def test_deferred_compute_spawns_poller_that_runs_catch_up(
        self, db_engine,
    ):
        """The full Step 11a story: marker set → orchestrator tick
        fires deferring compute closure → poller spawns → marker
        cleared → catch-up runs."""
        from trip_ema.app import (
            _BackfillPollerState,
            _make_deferring_compute,
        )

        catch_up_compute_done = threading.Event()
        catch_up_distribute_done = threading.Event()

        # The base callable IS the catch-up callable (the poller
        # invokes it as catch_up_compute, and the wrapper invokes
        # it directly when the marker is clear).
        def _base():
            catch_up_compute_done.set()

        def _distribute():
            catch_up_distribute_done.set()

        state = _BackfillPollerState()

        # We need a tiny poll interval for the test. Monkeypatch
        # the module constant via the function default by passing
        # poll_interval_seconds through the spawn call. But the
        # deferring wrapper doesn't expose that knob, so patch
        # the spawn function itself to use a small interval.
        rid = _set_marker_active(db_engine)
        try:
            from trip_ema.app import _spawn_backfill_poller

            def _fast_spawn(**kwargs):
                kwargs["poll_interval_seconds"] = 0.05
                return _spawn_backfill_poller(**kwargs)

            with patch(
                "trip_ema.app._spawn_backfill_poller",
                side_effect=_fast_spawn,
            ):
                wrapped = _make_deferring_compute(
                    base_callable=_base,
                    db_engine=db_engine,
                    poller_state=state,
                    catch_up_distribute=_distribute,
                )
                # Tick fires; marker is set, so base NOT called yet
                # and a poller is spawned.
                result = wrapped()
                assert result is None
                assert not catch_up_compute_done.is_set()
                assert state.active is True

                # Clear marker — poller should detect + run catch-up
                _clear_marker(db_engine, rid)
                assert _wait_for(
                    catch_up_compute_done.is_set, timeout_s=2.0,
                )
                assert _wait_for(
                    catch_up_distribute_done.is_set,
                    timeout_s=1.0,
                )

                # Wait for poller to exit
                assert _wait_for(
                    lambda: state.active is False, timeout_s=1.0,
                )
        finally:
            with transaction(db_engine) as conn:
                BackfillRunsRepository(conn).release_locks()
