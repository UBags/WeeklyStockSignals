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
def patched_market_hours(monkeypatch):
    """Patch MarketHoursService at the import site inside cmd_cycle.

    cmd_cycle imports MarketHoursService inside its function body
    via `from ..scheduler.market_hours import MarketHoursService`.
    To intercept this, we patch the module's attribute.
    """
    import trip_ema.scheduler.market_hours as mh_mod

    state = {"is_open": True, "raise_keyerror": False}

    class FakeMarketHours:
        def __init__(self, hours_config):
            pass

        def is_open(self, exchange, when):
            if state["raise_keyerror"]:
                raise KeyError(exchange)
            return state["is_open"]

    monkeypatch.setattr(
        mh_mod, "MarketHoursService", FakeMarketHours,
    )
    return state


@pytest.fixture
def patched_compute_runs(monkeypatch):
    """Patch ComputeRunsRepository.start()/finish()."""
    import trip_ema.persistence.compute_runs_repo as cr_mod

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

    monkeypatch.setattr(
        cr_mod, "ComputeRunsRepository", FakeRepo,
    )
    return captured


@pytest.fixture
def patched_transaction(monkeypatch):
    """Patch trip_ema.persistence.transaction to a no-op context
    manager."""
    import trip_ema.persistence as p_mod
    from contextlib import contextmanager

    @contextmanager
    def fake_tx(engine):
        yield MagicMock()

    monkeypatch.setattr(p_mod, "transaction", fake_tx)


class Stages:
    """The three stage functions cmd_cycle runs, as collaborators rather than
    module globals. `**stages.kwargs()` hands them to the command.

    This is what the importlib dance at the top of this module was working
    around: `trip_ema.orchestrator` re-exports the function names, which shadow
    the submodules, so a string-path monkeypatch resolved to the function and
    failed. Injection has no such problem — there is no path to resolve."""

    def __init__(self, captured):
        self.captured = captured

    def kwargs(self, **overrides):
        d = {
            "fetch_fn": self.fetch,
            "recompute_fn": self.recompute,
            "notify_fn": self.notify,
        }
        d.update(overrides)
        return d


@pytest.fixture
def stages():
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

    st = Stages(captured)
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
        patched_transaction, stages, monkeypatch, capsys,
    ):
        import trip_ema.config.brevo_secrets as brevo_mod

        fake_config.transports.brevo = SimpleNamespace(
            secrets_file="secrets/brevo.yaml",
            sender_email="from@example.com",
            sender_name="Test",
        )
        monkeypatch.setattr(
            brevo_mod, "load_brevo_api_key",
            lambda path: "xkeysib-test-key",
        )

        cmd_cycle(
            SimpleNamespace(exchange="NSE"), ctx, **stages.kwargs(),
        )
        notify = stages.captured["notify_kwargs"]
        assert notify is not None
        assert notify["brevo_api_key"] == "xkeysib-test-key"
        assert notify["brevo_from_address"] == "from@example.com"
