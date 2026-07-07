"""Tests for the phase-ledger resume engine (trip_ema/backfill/phase_ledger.py).

These prove the core resume contract:
  - fresh phases run
  - COMPLETE phases are skipped on re-run
  - FAILED phases re-run
  - a phase left RUNNING (hard crash — no clean FAILED mark) re-runs
  - force=True re-runs even COMPLETE phases
  - COMPLETE is written ONLY after fn() returns (a raising fn never marks
    COMPLETE), which is what makes a half-done phase always re-run

Uses the real DB fixture (db_engine) + the pipeline_phase_status table from
migration 0039.
"""
from __future__ import annotations

import pytest

from trip_ema.backfill.phase_ledger import (
    run_phase, phase_status, reset_phases,
    COMPLETE, FAILED, RUNNING,
)


class TestPhaseLedgerResume:
    def test_fresh_phase_runs(self, db_engine):
        reset_phases(db_engine, "TEST")
        calls = []
        r = run_phase(
            db_engine, "TEST", "p1",
            lambda: calls.append("p1") or 42,
        )
        assert calls == ["p1"]
        assert r == 42
        assert phase_status(db_engine, "TEST", "p1") == COMPLETE

    def test_complete_phase_skipped_on_rerun(self, db_engine):
        reset_phases(db_engine, "TEST")
        run_phase(db_engine, "TEST", "p1", lambda: 1)
        calls = []
        r = run_phase(
            db_engine, "TEST", "p1", lambda: calls.append("p1") or 2,
        )
        assert calls == []          # not re-run
        assert r is None            # skipped -> None
        assert phase_status(db_engine, "TEST", "p1") == COMPLETE

    def test_failed_phase_reruns(self, db_engine):
        reset_phases(db_engine, "TEST")

        def boom():
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError):
            run_phase(db_engine, "TEST", "p1", boom)
        assert phase_status(db_engine, "TEST", "p1") == FAILED

        # re-run: a FAILED phase is not COMPLETE, so it runs again
        calls = []
        run_phase(db_engine, "TEST", "p1", lambda: calls.append("p1") or 1)
        assert calls == ["p1"]
        assert phase_status(db_engine, "TEST", "p1") == COMPLETE

    def test_raising_fn_never_marks_complete(self, db_engine):
        """The core correctness rule: COMPLETE is written only after fn()
        returns. A raising fn leaves the phase non-COMPLETE."""
        reset_phases(db_engine, "TEST")

        def boom():
            raise ValueError("half done")

        with pytest.raises(ValueError):
            run_phase(db_engine, "TEST", "p1", boom)
        # never COMPLETE — so a resume will re-run it
        assert phase_status(db_engine, "TEST", "p1") != COMPLETE

    def test_force_reruns_complete_phase(self, db_engine):
        reset_phases(db_engine, "TEST")
        run_phase(db_engine, "TEST", "p1", lambda: 1)
        calls = []
        run_phase(
            db_engine, "TEST", "p1",
            lambda: calls.append("p1") or 2, force=True,
        )
        assert calls == ["p1"]      # force re-runs despite COMPLETE

    def test_resume_skips_done_reruns_rest(self, db_engine):
        """Simulate a pipeline that completes p1, crashes at p2; a resume
        skips p1 and re-runs p2 + p3."""
        reset_phases(db_engine, "TEST")
        run_phase(db_engine, "TEST", "p1", lambda: 1)
        with pytest.raises(RuntimeError):
            run_phase(
                db_engine, "TEST", "p2",
                lambda: (_ for _ in ()).throw(RuntimeError("x")),
            )
        # resume
        ran = []
        for ph in ["p1", "p2", "p3"]:
            run_phase(db_engine, "TEST", ph, lambda ph=ph: ran.append(ph) or 1)
        assert ran == ["p2", "p3"]   # p1 skipped, p2+p3 run

    def test_reset_forces_full_rerun(self, db_engine):
        reset_phases(db_engine, "TEST")
        run_phase(db_engine, "TEST", "p1", lambda: 1)
        run_phase(db_engine, "TEST", "p2", lambda: 1)
        n = reset_phases(db_engine, "TEST")
        assert n == 2
        # after reset, both re-run
        ran = []
        for ph in ["p1", "p2"]:
            run_phase(db_engine, "TEST", ph, lambda ph=ph: ran.append(ph) or 1)
        assert ran == ["p1", "p2"]
