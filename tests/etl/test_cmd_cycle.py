"""Tests for `trip-ema-etl cycle` CLI handler (Step 13d-1).

cmd_cycle integrates: fetch + recompute_market + notify_subscribers
with a compute_runs row lifecycle. Tests verify:
  - kind determination via MarketHoursService
  - compute_runs.start() called before fetch, finish() in finally
  - progress_cb wires "[stage] msg" to stdout
  - failure paths set failed_stage + error_message + return 1
  - notify stage skipped cleanly when brevo not configured

All external dependencies (DB engine, MarketHoursService, the
three stage functions, ComputeRunsRepository) are monkeypatched.

NOTE on monkeypatching: `trip_ema.orchestrator.__init__.py`
re-exports `recompute_market` and `notify_subscribers` function
names, which shadow the submodules at the package-attribute
level. pytest's `monkeypatch.setattr("trip_ema.orchestrator.X.X",
...)` does getattr traversal and fails because the path resolves
to the function, not the module. Workaround: use
`importlib.import_module()` to get module objects directly, then
monkeypatch.setattr with the module object (not a string path).
"""
from __future__ import annotations

import importlib
from datetime import datetime, UTC
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from trip_ema.etl import cli as cli_mod
from trip_ema.etl.cli import cmd_cycle

# Module objects for monkeypatching (bypass the orchestrator
# package-attribute shadow)
recompute_mod = importlib.import_module(
    "trip_ema.orchestrator.recompute_market",
)
notify_mod = importlib.import_module(
    "trip_ema.orchestrator.notify_subscribers",
)
runner_mod = importlib.import_module("trip_ema.etl.runner")


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
def patched_engine(monkeypatch):
    """Stub create_engine_from_config to return a MagicMock engine."""
    engine = MagicMock()
    engine.dispose = MagicMock()
    monkeypatch.setattr(
        cli_mod, "create_engine_from_config", lambda c: engine,
    )
    return engine


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


@pytest.fixture
def patched_stages(monkeypatch):
    """Patch _run_fetch_pass + recompute_market + notify_subscribers
    via module-object setattr (avoids the orchestrator-package shadow
    issue)."""
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

    monkeypatch.setattr(runner_mod, "_run_fetch_pass", fake_fetch)
    monkeypatch.setattr(
        recompute_mod, "recompute_market", fake_recompute,
    )
    monkeypatch.setattr(
        notify_mod, "notify_subscribers", fake_notify,
    )
    return captured


# ============================================================
# Tests
# ============================================================

class TestKindDetermination:

    def test_market_open_yields_intraday_kind(
        self, fake_config, patched_engine,
        patched_market_hours, patched_compute_runs,
        patched_transaction, patched_stages, capsys,
    ):
        patched_market_hours["is_open"] = True
        args = SimpleNamespace(exchange="NSE")
        rc = cmd_cycle(args, fake_config)
        assert rc == 0
        assert patched_compute_runs["start"]["kind"] == "intraday"

    def test_market_closed_yields_eod_kind(
        self, fake_config, patched_engine,
        patched_market_hours, patched_compute_runs,
        patched_transaction, patched_stages, capsys,
    ):
        patched_market_hours["is_open"] = False
        args = SimpleNamespace(exchange="NSE")
        rc = cmd_cycle(args, fake_config)
        assert rc == 0
        assert patched_compute_runs["start"]["kind"] == "eod"

    def test_unknown_exchange_returns_2(
        self, fake_config, patched_engine,
        patched_market_hours, patched_compute_runs,
        patched_transaction, patched_stages, capsys,
    ):
        patched_market_hours["raise_keyerror"] = True
        args = SimpleNamespace(exchange="MARS")
        rc = cmd_cycle(args, fake_config)
        assert rc == 2
        assert patched_compute_runs["start"] is None


class TestComputeRunsLifecycle:

    def test_finish_called_with_run_id(
        self, fake_config, patched_engine,
        patched_market_hours, patched_compute_runs,
        patched_transaction, patched_stages, capsys,
    ):
        cmd_cycle(
            SimpleNamespace(exchange="NSE"), fake_config,
        )
        assert patched_compute_runs["finish"] is not None
        assert patched_compute_runs["finish"]["run_id"] == 9999

    def test_finish_called_even_on_stage_failure(
        self, fake_config, patched_engine,
        patched_market_hours, patched_compute_runs,
        patched_transaction, monkeypatch, capsys,
    ):
        def boom_fetch(*a, **k):
            raise RuntimeError("yfinance dead")
        monkeypatch.setattr(
            runner_mod, "_run_fetch_pass", boom_fetch,
        )
        monkeypatch.setattr(
            recompute_mod, "recompute_market",
            lambda *a, **k: SimpleNamespace(
                stage_errors=[], stock_state_succeeded=0,
                stock_state_failed=0, stock_state_skipped=0,
            ),
        )
        monkeypatch.setattr(
            notify_mod, "notify_subscribers",
            lambda *a, **k: None,
        )

        rc = cmd_cycle(
            SimpleNamespace(exchange="NSE"), fake_config,
        )
        assert rc == 1
        assert patched_compute_runs["finish"] is not None
        assert "yfinance dead" in (
            patched_compute_runs["finish"]
            .get("error_message", "") or ""
        )

    def test_recompute_stage_error_sets_failed_stage(
        self, fake_config, patched_engine,
        patched_market_hours, patched_compute_runs,
        patched_transaction, monkeypatch, capsys,
    ):
        monkeypatch.setattr(
            runner_mod, "_run_fetch_pass",
            lambda *a, **k: 0,
        )
        monkeypatch.setattr(
            recompute_mod, "recompute_market",
            lambda *a, **k: SimpleNamespace(
                stage_errors=[("iiv", "IIV exploded")],
                stock_state_succeeded=10,
                stock_state_failed=0,
                stock_state_skipped=0,
            ),
        )
        monkeypatch.setattr(
            notify_mod, "notify_subscribers",
            lambda *a, **k: None,
        )

        rc = cmd_cycle(
            SimpleNamespace(exchange="NSE"), fake_config,
        )
        assert rc == 1
        finish = patched_compute_runs["finish"]
        assert finish["failed_stage"] == "iiv"
        assert "IIV exploded" in finish["error_message"]


class TestStdoutProgress:

    def test_progress_cb_writes_stage_markers_to_stdout(
        self, fake_config, patched_engine,
        patched_market_hours, patched_compute_runs,
        patched_transaction, patched_stages, capsys,
    ):
        cmd_cycle(
            SimpleNamespace(exchange="NSE"), fake_config,
        )
        out = capsys.readouterr().out
        assert "[fetch_bars] running" in out
        assert "[fetch_bars] done" in out
        assert "[stock_state] running" in out
        assert "[stock_state] done" in out

    def test_failure_summary_emitted_to_stderr(
        self, fake_config, patched_engine,
        patched_market_hours, patched_compute_runs,
        patched_transaction, monkeypatch, capsys,
    ):
        monkeypatch.setattr(
            runner_mod, "_run_fetch_pass",
            lambda *a, **k: 0,
        )
        monkeypatch.setattr(
            recompute_mod, "recompute_market",
            lambda *a, **k: SimpleNamespace(
                stage_errors=[("derive", "no signals")],
                stock_state_succeeded=0,
                stock_state_failed=0,
                stock_state_skipped=0,
            ),
        )
        monkeypatch.setattr(
            notify_mod, "notify_subscribers",
            lambda *a, **k: None,
        )
        rc = cmd_cycle(
            SimpleNamespace(exchange="NSE"), fake_config,
        )
        assert rc == 1
        err = capsys.readouterr().err
        assert "cycle FAILED in derive:" in err
        assert "no signals" in err


class TestNotifyConfig:

    def test_no_brevo_config_skips_notify(
        self, fake_config, patched_engine,
        patched_market_hours, patched_compute_runs,
        patched_transaction, patched_stages, capsys,
    ):
        rc = cmd_cycle(
            SimpleNamespace(exchange="NSE"), fake_config,
        )
        assert rc == 0
        assert patched_stages["notify_kwargs"] is None
        out = capsys.readouterr().out
        assert "notify" in out

    def test_brevo_config_present_invokes_notify(
        self, fake_config, patched_engine,
        patched_market_hours, patched_compute_runs,
        patched_transaction, patched_stages, monkeypatch, capsys,
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
            SimpleNamespace(exchange="NSE"), fake_config,
        )
        notify = patched_stages["notify_kwargs"]
        assert notify is not None
        assert notify["brevo_api_key"] == "xkeysib-test-key"
        assert notify["brevo_from_address"] == "from@example.com"
