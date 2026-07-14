"""Tests for `trip-ema-etl cycle` CLI handler (Step 13d-1).

cmd_cycle integrates: fetch + recompute_market + notify_subscribers
with a compute_runs row lifecycle. Tests verify:
  - kind determination via MarketHoursService
  - compute_runs.start() called before fetch, finish() in finally
  - progress_cb wires "[stage] msg" to stdout
  - failure paths set failed_stage + error_message + return 1
  - notify stage skipped cleanly when brevo not configured

The engine and the three stage functions are INJECTED: cmd_cycle takes an
AppContext and keyword-only fetch_fn / recompute_fn / notify_fn. MarketHoursService,
ComputeRunsRepository and `transaction` are still patched — those are repository
seams and belong to a later phase.

The importlib dance this module used to open with is gone with the stage
patching that needed it: `trip_ema.orchestrator.__init__` re-exports the
function names, which shadow the submodules, so a string-path monkeypatch
resolved to the function rather than the module and failed. There is no path to
resolve when you simply pass the function in.
"""
from __future__ import annotations

from datetime import datetime, UTC
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from trip_ema.app_context import AppContext
from trip_ema.etl.cli import cmd_cycle


# ============================================================
# Fixtures
# ============================================================

@pytest.fixture
def fake_config():
    cfg = SimpleNamespace()
    cfg.market_hours = []
    cfg.transports = SimpleNamespace(brevo=None)
    return cfg


@pytest.fixture
def ctx(fake_config):
    """cmd_cycle's collaborators, handed to it. The engine was never a global —
    the command just built its own, so a test had no way in except to patch the
    factory."""
    engine = MagicMock()
    engine.dispose = MagicMock()
    return AppContext(config=fake_config, db_engine=engine)


@pytest.fixture
def patched_market_hours():
    """Fake MarketHoursService factory injected into cmd_cycle.

    Phase ETL: replaces the setattr on mh_mod with a
    factory the test hands to cmd_cycle via market_hours_factory=.
    Fixture returns the mutable state dict; the FakeMarketHours
    class is stashed on state["factory"].
    """
    state = {"is_open": True, "raise_keyerror": False}

    class FakeMarketHours:
        def __init__(self, hours_config):
            pass

        def is_open(self, exchange, when):
            if state["raise_keyerror"]:
                raise KeyError(exchange)
            return state["is_open"]

    state["factory"] = FakeMarketHours
    return state


@pytest.fixture
def patched_compute_runs():
    """Fake ComputeRunsRepository factory + captured start/finish
    call args. Phase ETL: injected via compute_runs_repo_factory=."""
    captured = {"start": None, "finish": None}

    class FakeRepo:
        def __init__(self, conn):
            pass

        def start(self, *, kind, exchange, notes=None):
            captured["start"] = {
                "kind": kind, "exchange": exchange, "notes": notes,
            }
            return 9999

        def finish(self, run_id, **kwargs):
            captured["finish"] = {
                "run_id": run_id, **kwargs,
            }

        def set_current_stage(self, run_id, stage):
            # No-op — the real repo publishes to a live compute_runs
            # row, but the tests don't assert on the intermediate
            # stage transitions.
            pass

    captured["factory"] = FakeRepo
    return captured


@pytest.fixture
def patched_transaction():
    """No-op transaction context manager. Phase ETL: injected via
    tx_fn= kwarg on cmd_cycle rather than monkeypatched on the
    persistence module."""
    from contextlib import contextmanager

    @contextmanager
    def fake_tx(engine):
        yield MagicMock()

    return fake_tx


class Stages:
    """The three stage functions cmd_cycle runs, plus the four injected
    persistence/service collaborators (Phase ETL: market_hours,
    compute_runs, tx, brevo). `**stages.kwargs()` hands them all to
    cmd_cycle."""

    def __init__(self, captured, mh, cr, tx, brevo_loader=None):
        self.captured = captured
        self._mh = mh
        self._cr = cr
        self._tx = tx
        self._brevo = brevo_loader

    def kwargs(self, **overrides):
        d = {
            "fetch_fn": self.fetch,
            "recompute_fn": self.recompute,
            "notify_fn": self.notify,
            "market_hours_factory": self._mh["factory"],
            "compute_runs_repo_factory": self._cr["factory"],
            "tx_fn": self._tx,
        }
        if self._brevo is not None:
            d["brevo_key_loader"] = self._brevo
        d.update(overrides)
        return d


def _deps(mh, cr, tx, *, brevo=None):
    """Kwargs the injected-collaborator tests pass to cmd_cycle
    when they don't use the `stages` fixture (they wire fetch_fn
    /recompute_fn/notify_fn explicitly with test-specific stubs)."""
    d = {
        "market_hours_factory": mh["factory"],
        "compute_runs_repo_factory": cr["factory"],
        "tx_fn": tx,
    }
    if brevo is not None:
        d["brevo_key_loader"] = brevo
    return d


@pytest.fixture
def stages(patched_market_hours, patched_compute_runs, patched_transaction):
    captured = {
        "fetch_kwargs": None,
        "recompute_kwargs": None,
        "notify_kwargs": None,
    }

    def fake_fetch(*args, **kwargs):
        captured["fetch_kwargs"] = kwargs
        cb = kwargs.get("progress_cb")
        if cb is not None:
            cb("fetch_bars", "running")
            cb("fetch_bars", "done")
        return 100

    def fake_recompute(db_engine, **kwargs):
        captured["recompute_kwargs"] = kwargs
        cb = kwargs.get("progress_cb")
        if cb is not None:
            cb("stock_state", "running")
            cb("stock_state", "done")
        return SimpleNamespace(
            stage_errors=[],
            stock_state_succeeded=10,
            stock_state_failed=0,
            stock_state_skipped=0,
        )

    def fake_notify(db_engine, **kwargs):
        captured["notify_kwargs"] = kwargs
        cb = kwargs.get("progress_cb")
        if cb is not None:
            cb("notify", "running")
            cb("notify", "done")

    st = Stages(
        captured, patched_market_hours, patched_compute_runs,
        patched_transaction,
    )
    st.fetch, st.recompute, st.notify = fake_fetch, fake_recompute, fake_notify
    return st


# ============================================================
# Tests
# ============================================================

class TestKindDetermination:

    def test_market_open_yields_intraday_kind(
        self, fake_config, ctx,
        patched_market_hours, patched_compute_runs,
        patched_transaction, stages, capsys,
    ):
        patched_market_hours["is_open"] = True
        args = SimpleNamespace(exchange="NSE")
        rc = cmd_cycle(args, ctx, **stages.kwargs())
        assert rc == 0
        assert patched_compute_runs["start"]["kind"] == "intraday"

    def test_market_closed_yields_eod_kind(
        self, fake_config, ctx,
        patched_market_hours, patched_compute_runs,
        patched_transaction, stages, capsys,
    ):
        patched_market_hours["is_open"] = False
        args = SimpleNamespace(exchange="NSE")
        rc = cmd_cycle(args, ctx, **stages.kwargs())
        assert rc == 0
        assert patched_compute_runs["start"]["kind"] == "eod"

    def test_unknown_exchange_returns_2(
        self, fake_config, ctx,
        patched_market_hours, patched_compute_runs,
        patched_transaction, stages, capsys,
    ):
        patched_market_hours["raise_keyerror"] = True
        args = SimpleNamespace(exchange="MARS")
        rc = cmd_cycle(args, ctx, **stages.kwargs())
        assert rc == 2
        assert patched_compute_runs["start"] is None


class TestComputeRunsLifecycle:

    def test_finish_called_with_run_id(
        self, fake_config, ctx,
        patched_market_hours, patched_compute_runs,
        patched_transaction, stages, capsys,
    ):
        cmd_cycle(
            SimpleNamespace(exchange="NSE"), ctx, **stages.kwargs(),
        )
        assert patched_compute_runs["finish"] is not None
        assert patched_compute_runs["finish"]["run_id"] == 9999

    def test_finish_called_even_on_stage_failure(
        self, fake_config, ctx,
        patched_market_hours, patched_compute_runs,
        patched_transaction, capsys,
    ):
        def boom_fetch(*a, **k):
            raise RuntimeError("yfinance dead")
        _fetch_fn = boom_fetch
        _recompute_fn = lambda *a, **k: SimpleNamespace(
                            stage_errors=[], stock_state_succeeded=0,
                            stock_state_failed=0, stock_state_skipped=0,
                        )
        _notify_fn = lambda *a, **k: None

        rc = cmd_cycle(
            SimpleNamespace(exchange="NSE"), ctx, fetch_fn=_fetch_fn, recompute_fn=_recompute_fn, notify_fn=_notify_fn,
            **_deps(patched_market_hours, patched_compute_runs, patched_transaction),
        )
        assert rc == 1
        assert patched_compute_runs["finish"] is not None
        assert "yfinance dead" in (
            patched_compute_runs["finish"]
            .get("error_message", "") or ""
        )

    def test_recompute_stage_error_sets_failed_stage(
        self, fake_config, ctx,
        patched_market_hours, patched_compute_runs,
        patched_transaction, capsys,
    ):
        _fetch_fn = lambda *a, **k: 0
        _recompute_fn = lambda *a, **k: SimpleNamespace(
                            stage_errors=[("iiv", "IIV exploded")],
                            stock_state_succeeded=10,
                            stock_state_failed=0,
                            stock_state_skipped=0,
                        )
        _notify_fn = lambda *a, **k: None

        rc = cmd_cycle(
            SimpleNamespace(exchange="NSE"), ctx, fetch_fn=_fetch_fn, recompute_fn=_recompute_fn, notify_fn=_notify_fn,
            **_deps(patched_market_hours, patched_compute_runs, patched_transaction),
        )
        assert rc == 1
        finish = patched_compute_runs["finish"]
        assert finish["failed_stage"] == "iiv"
        assert "IIV exploded" in finish["error_message"]


class TestStdoutProgress:

    def test_progress_cb_writes_stage_markers_to_stdout(
        self, fake_config, ctx,
        patched_market_hours, patched_compute_runs,
        patched_transaction, stages, capsys,
    ):
        cmd_cycle(
            SimpleNamespace(exchange="NSE"), ctx, **stages.kwargs(),
        )
        out = capsys.readouterr().out
        assert "[fetch_bars] running" in out
        assert "[fetch_bars] done" in out
        assert "[stock_state] running" in out
        assert "[stock_state] done" in out

    def test_failure_summary_emitted_to_stderr(
        self, fake_config, ctx,
        patched_market_hours, patched_compute_runs,
        patched_transaction, capsys,
    ):
        _fetch_fn = lambda *a, **k: 0
        _recompute_fn = lambda *a, **k: SimpleNamespace(
                            stage_errors=[("derive", "no signals")],
                            stock_state_succeeded=0,
                            stock_state_failed=0,
                            stock_state_skipped=0,
                        )
        _notify_fn = lambda *a, **k: None
        rc = cmd_cycle(
            SimpleNamespace(exchange="NSE"), ctx, fetch_fn=_fetch_fn, recompute_fn=_recompute_fn, notify_fn=_notify_fn,
            **_deps(patched_market_hours, patched_compute_runs, patched_transaction),
        )
        assert rc == 1
        err = capsys.readouterr().err
        assert "cycle FAILED in derive:" in err
        assert "no signals" in err


class TestNotifyConfig:

    def test_no_brevo_config_skips_notify(
        self, fake_config, ctx,
        patched_market_hours, patched_compute_runs,
        patched_transaction, stages, capsys,
    ):
        rc = cmd_cycle(
            SimpleNamespace(exchange="NSE"), ctx, **stages.kwargs(),
        )
        assert rc == 0
        assert stages.captured["notify_kwargs"] is None
        out = capsys.readouterr().out
        assert "notify" in out

    def test_brevo_config_present_invokes_notify(
        self, fake_config, ctx,
        patched_market_hours, patched_compute_runs,
        patched_transaction, stages, capsys,
    ):
        fake_config.transports.brevo = SimpleNamespace(
            secrets_file="secrets/brevo.yaml",
            sender_email="from@example.com",
            sender_name="Test",
        )

        cmd_cycle(
            SimpleNamespace(exchange="NSE"), ctx,
            **stages.kwargs(
                brevo_key_loader=lambda path: "xkeysib-test-key",
            ),
        )
        notify = stages.captured["notify_kwargs"]
        assert notify is not None
        assert notify["brevo_api_key"] == "xkeysib-test-key"
        assert notify["brevo_from_address"] == "from@example.com"
