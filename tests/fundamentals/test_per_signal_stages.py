"""C6b — the shared per-signal sequencer.

Two things to prove without a database:

1. ORDER + WRAPPING. The sequencer calls the three runners in the fixed order
   enrichment -> labels -> ensemble, each through the injected run_phase, with
   the phase names and progress messages the hand-written backfill blocks
   used.

2. SCOPE ROUTING. Each stage receives its OWN scope. In particular the
   backfill wiring passes ensemble_scope=None while enrichment/labels get the
   ticker list — the asymmetry a careless extraction would have flattened.

The runners are INJECTED (the _enrichment_runner / _labels_runner /
_ensemble_runner parameters), not monkeypatched: this codebase retired ~450
test patches in favour of seams, and adding new monkeypatch here would both
trip the patch budget and betray that effort. DI is the same seam pattern.
"""
from __future__ import annotations

from trip_ema.fundamentals.per_signal_stages import (
    PerSignalStageResult,
    run_per_signal_stages,
    _passthrough_run_phase,
)


class _Recorder:
    def __init__(self):
        self.runner_calls = []
        self.phase_calls = []
        self.progress = []

    def runner(self, name):
        def _fn(db, exchange, *, scoped_tickers=None, progress_cb=None):
            self.runner_calls.append((name, scoped_tickers))
            return f"{name}-result"
        return _fn

    def run_phase(self, db, ex, phase, fn, *, backfill_run_id=None,
                  force=False):
        self.phase_calls.append((phase, backfill_run_id, force))
        return fn()          # actually invoke, so runner_calls populates

    def progress_cb(self, stage, message):
        self.progress.append((stage, message))

    def as_runners(self):
        return dict(
            _enrichment_runner=self.runner("enrichment"),
            _labels_runner=self.runner("labels"),
            _ensemble_runner=self.runner("ensemble"),
        )


class TestOrderAndWrapping:

    def test_the_three_stages_run_in_order(self):
        rec = _Recorder()
        run_per_signal_stages(
            None, "NSE",
            enrichment_scope=[1], labels_scope=[1], ensemble_scope=None,
            run_phase=rec.run_phase, **rec.as_runners(),
        )
        assert [n for n, _ in rec.runner_calls] == [
            "enrichment", "labels", "ensemble",
        ]

    def test_phase_names_match_the_original_backfill_blocks(self):
        rec = _Recorder()
        run_per_signal_stages(
            None, "NSE",
            enrichment_scope=None, labels_scope=None, ensemble_scope=None,
            run_phase=rec.run_phase, **rec.as_runners(),
        )
        assert [p for p, _r, _f in rec.phase_calls] == [
            "signal_enrichment", "signal_labels", "signal_ensemble_features",
        ]

    def test_progress_messages_match(self):
        rec = _Recorder()
        run_per_signal_stages(
            None, "NSE",
            enrichment_scope=None, labels_scope=None, ensemble_scope=None,
            run_phase=rec.run_phase, progress_cb=rec.progress_cb,
            **rec.as_runners(),
        )
        assert rec.progress == [
            ("stage_8b_signal_enrichment", "starting"),
            ("stage_8b_signal_enrichment", "done"),
            ("stage_8c_signal_labels", "starting"),
            ("stage_8c_signal_labels", "done"),
            ("stage_8d_signal_ensemble", "starting"),
            ("stage_8d_signal_ensemble", "done"),
        ]

    def test_run_id_and_force_are_threaded_to_every_phase(self):
        rec = _Recorder()
        run_per_signal_stages(
            None, "NSE",
            enrichment_scope=None, labels_scope=None, ensemble_scope=None,
            run_phase=rec.run_phase, backfill_run_id=77, force=True,
            **rec.as_runners(),
        )
        for _p, rid, force in rec.phase_calls:
            assert rid == 77 and force is True


class TestScopeRouting:

    def test_each_stage_gets_its_own_scope(self):
        rec = _Recorder()
        run_per_signal_stages(
            None, "NSE",
            enrichment_scope=[10, 11], labels_scope=[99],
            ensemble_scope=None, run_phase=rec.run_phase, **rec.as_runners(),
        )
        by_name = dict(rec.runner_calls)
        assert by_name["enrichment"] == [10, 11]
        assert by_name["labels"] == [99]
        assert by_name["ensemble"] is None

    def test_backfill_asymmetry_ensemble_none_others_scoped(self):
        """The exact backfill wiring: enrichment+labels scoped to the run's
        tickers, ensemble a full sweep (None). This is what a careless
        extraction would have flattened into one shared scope."""
        rec = _Recorder()
        run_per_signal_stages(
            None, "NSE",
            enrichment_scope=[5], labels_scope=[5], ensemble_scope=None,
            run_phase=rec.run_phase, **rec.as_runners(),
        )
        by_name = dict(rec.runner_calls)
        assert by_name["enrichment"] == [5]
        assert by_name["labels"] == [5]
        assert by_name["ensemble"] is None      # NOT [5]


class TestSkipReporting:

    def test_a_skipped_phase_reports_skipped(self):
        """When run_phase returns None (ledger skip on resume), the progress
        message is 'skipped (resume)' and the result field is None."""
        rec = _Recorder()

        def _skipping_run_phase(db, ex, phase, fn, *, backfill_run_id=None,
                                force=False):
            return None      # simulate COMPLETE-and-skipped

        res = run_per_signal_stages(
            None, "NSE",
            enrichment_scope=None, labels_scope=None, ensemble_scope=None,
            run_phase=_skipping_run_phase, progress_cb=rec.progress_cb,
            **rec.as_runners(),
        )
        assert res == PerSignalStageResult(None, None, None)
        assert ("stage_8b_signal_enrichment", "skipped (resume)") in rec.progress


class TestPassthrough:

    def test_default_run_phase_just_runs_the_stage(self):
        """The EOD default: no ledger. _passthrough_run_phase calls fn() and
        returns its result, so every stage runs and nothing is skipped."""
        rec = _Recorder()
        res = run_per_signal_stages(
            None, "NSE",
            enrichment_scope=[1], labels_scope=[2], ensemble_scope=[3],
            **rec.as_runners(),
            # run_phase defaults to _passthrough_run_phase
        )
        assert res.enrichment == "enrichment-result"
        assert res.labels == "labels-result"
        assert res.ensemble == "ensemble-result"
        assert [n for n, _ in rec.runner_calls] == [
            "enrichment", "labels", "ensemble",
        ]

    def test_passthrough_signature_matches_run_phase(self):
        """It must be substitutable for phase_ledger.run_phase — same shape,
        so the sequencer body drives both callers unchanged."""
        called = {}
        _passthrough_run_phase(
            "db", "NSE", "some_phase",
            lambda: called.setdefault("ran", True),
            backfill_run_id=5, force=True,
        )
        assert called.get("ran") is True
