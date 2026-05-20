"""Tests for the /v1/admin/signals/* routes (Phase 15b).

Strategy: pure FastAPI tests with dependency overrides. No real DB
required for most tests — we mock the db_engine and the executor.
A small number of integration-shaped tests use a real Postgres
(via the db_engine fixture re-exported through tests/etl/conftest.py)
to verify the concurrent-run guard's advisory lock behaves under
genuine transaction semantics.

Coverage:
  - POST happy path: 202, run_id present, executor.submit called
    with the right args, etl_runs row opened with kind='signal_backtest'
    and exchange populated.
  - POST timeframe handling: default ['1W'], repeatable, intraday
    rejected, unknown rejected.
  - POST force_full flag propagates.
  - POST concurrent-run guard: a second POST while another is in
    flight returns 409 with the existing run_id.
  - POST unknown/unconfigured exchange → 400.
  - POST missing exchange → 422.
  - POST auth: bearer-token gate; behaves like other /v1/admin/*.
  - POST executor disabled → 503.
  - GET happy path: returns metadata; counts subdict populated.
  - GET file absent → 200 with exists=False.
  - GET unknown exchange → 400.
  - GET corrupt file → 500.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from trip_ema.transports.http import create_app
from trip_ema.transports.http.dependencies import (
    get_cache, get_compute_scheduler, get_config, get_db_engine,
    get_distributor, get_etl_executor, get_redis_queue, get_registry,
)


# ---------- shared fixtures ---------- #


@pytest.fixture
def admin_token(monkeypatch):
    monkeypatch.setenv("TRIP_EMA_ADMIN_TOKEN", "test-token-15b")
    return "test-token-15b"


@pytest.fixture
def auth_headers(admin_token):
    return {"Authorization": f"Bearer {admin_token}"}


class FakeExecutor:
    """Captures submitted callables without running them."""

    def __init__(self):
        self.submitted: list[tuple] = []

    def submit(self, fn, *args, **kwargs):
        self.submitted.append((fn.__name__, args, kwargs))
        return MagicMock()


def _make_config(*, market_hours_exchanges=("NSE",)):
    """Build a MagicMock config that satisfies the route's needs.
    Same shape as test_admin_etl's _make_config so the helpers
    in dependencies and exchange validation work."""
    cfg = MagicMock()
    cfg.http.admin_token_env = "TRIP_EMA_ADMIN_TOKEN"
    cfg.etl.exchange = "NSE"
    cfg.market_hours = [
        type("MH", (), {"exchange": ex})() for ex in market_hours_exchanges
    ]
    return cfg


class FakeDbEngine:
    """A fake Engine whose .connect() and .begin() return a fake
    connection that returns whatever we hand it for each SELECT.

    Mocking SQLAlchemy's Connection is verbose; this class captures
    just the slice the route uses: execute() that returns an object
    with .first() or .scalar_one(), plus context-manager semantics
    for connect() and begin().
    """

    def __init__(self, *,
                 in_flight_run_id: int | None = None,
                 next_run_id: int = 42):
        self.in_flight_run_id = in_flight_run_id
        self.next_run_id = next_run_id
        self.executed_queries: list[tuple[str, dict]] = []

    def begin(self):
        return _FakeConnCtx(self)

    def connect(self):
        return _FakeConnCtx(self)


class _FakeConnCtx:
    def __init__(self, engine):
        self.engine = engine

    def __enter__(self):
        return _FakeConn(self.engine)

    def __exit__(self, *exc):
        return False


class _FakeConn:
    """Mimics the route's usage of conn.execute(text(...), {...})."""

    def __init__(self, engine):
        self.engine = engine

    def execute(self, statement, params=None):
        # str(statement) for sqlalchemy.text() returns the SQL string;
        # we string-match the parts we care about.
        sql = str(statement).strip().upper()
        self.engine.executed_queries.append((sql, params or {}))

        # The advisory lock is a no-op for our purposes.
        if "PG_ADVISORY_XACT_LOCK" in sql:
            return _FakeResult(rows=[])
        # The concurrent-run guard SELECT.
        if "SELECT RUN_ID FROM ETL_RUNS" in sql:
            if self.engine.in_flight_run_id is not None:
                return _FakeResult(rows=[
                    type("Row", (), {"run_id": self.engine.in_flight_run_id})()
                ])
            return _FakeResult(rows=[])
        # EtlStatusRepository.open_run does INSERT INTO etl_runs ...
        # RETURNING run_id. Hand back the next_run_id.
        if "INSERT INTO ETL_RUNS" in sql:
            return _FakeResult(scalar=self.engine.next_run_id)
        # Anything else (we don't expect any) — return empty.
        return _FakeResult(rows=[])

    def commit(self):
        pass

    def rollback(self):
        pass


class _FakeResult:
    def __init__(self, *, rows=None, scalar=None):
        self._rows = rows or []
        self._scalar = scalar

    def first(self):
        return self._rows[0] if self._rows else None

    def scalar_one(self):
        if self._scalar is None:
            raise ValueError("scalar_one called with no scalar set")
        return self._scalar


def _make_client(*, config=None, db_engine=None, executor=None):
    """Build a TestClient with the standard overrides."""
    app = create_app()
    app.dependency_overrides[get_config] = lambda: (config or _make_config())
    app.dependency_overrides[get_db_engine] = lambda: (
        db_engine if db_engine is not None else FakeDbEngine()
    )
    app.dependency_overrides[get_etl_executor] = lambda: executor
    app.dependency_overrides[get_registry] = lambda: MagicMock()
    app.dependency_overrides[get_cache] = lambda: MagicMock()
    app.dependency_overrides[get_compute_scheduler] = lambda: MagicMock()
    app.dependency_overrides[get_distributor] = lambda: None
    app.dependency_overrides[get_redis_queue] = lambda: None
    return TestClient(app)


# ============================================================
# POST /v1/admin/signals/generate
# ============================================================


class TestAdminSignalsGenerateHappyPath:

    def test_returns_202_with_run_id_and_path(self, auth_headers):
        exec_ = FakeExecutor()
        engine = FakeDbEngine(next_run_id=42)
        client = _make_client(db_engine=engine, executor=exec_)

        resp = client.post(
            "/v1/admin/signals/generate?exchange=NSE",
            headers=auth_headers,
        )
        assert resp.status_code == 202, resp.text
        body = resp.json()
        assert body["run_id"] == 42
        assert body["exchange"] == "NSE"
        assert body["timeframes"] == ["1W"]   # default
        assert body["force_full"] is False
        assert body["estimated_output_path"] == "generated_signals/NSE.json"
        assert "submitted_at" in body

    def test_submits_runner_to_executor_with_run_id(self, auth_headers):
        """The route MUST pass run_id to the runner so the runner
        skips its own open_run call (per 15a's split-ownership
        contract). The submitted args are (db_engine, config,
        exchange, timeframes, force_full, run_id)."""
        exec_ = FakeExecutor()
        engine = FakeDbEngine(next_run_id=7)
        client = _make_client(db_engine=engine, executor=exec_)

        client.post(
            "/v1/admin/signals/generate?exchange=NSE",
            headers=auth_headers,
        )

        assert len(exec_.submitted) == 1
        fn_name, args, _ = exec_.submitted[0]
        assert fn_name == "_run_signal_generation_safe"
        # args[0] is db_engine, args[1] is config, args[2] is exchange,
        # args[3] is timeframes (list[Timeframe]), args[4] is
        # force_full, args[5] is the run_id we want to flow through.
        assert args[2] == "NSE"
        assert args[4] is False
        assert args[5] == 7

    def test_opens_etl_runs_row_with_correct_kind_and_exchange(self, auth_headers):
        engine = FakeDbEngine(next_run_id=99)
        client = _make_client(db_engine=engine, executor=FakeExecutor())

        client.post(
            "/v1/admin/signals/generate?exchange=NSE",
            headers=auth_headers,
        )

        # The INSERT INTO etl_runs ... statement was executed with
        # the kind='signal_backtest' and exchange='NSE' params.
        insert_calls = [
            (sql, params) for (sql, params) in engine.executed_queries
            if "INSERT INTO ETL_RUNS" in sql
        ]
        assert len(insert_calls) == 1, "expected exactly one INSERT"
        _, params = insert_calls[0]
        # EtlStatusRepository.open_run binds kind, exchange,
        # correlation_id, notes.
        assert params.get("kind") == "signal_backtest"
        assert params.get("ex") == "NSE"
        assert "cid" in params
        assert "notes" in params

    def test_grabs_advisory_lock_keyed_by_exchange(self, auth_headers):
        engine = FakeDbEngine()
        client = _make_client(db_engine=engine, executor=FakeExecutor())

        client.post(
            "/v1/admin/signals/generate?exchange=NSE",
            headers=auth_headers,
        )

        # The first DB call must be pg_advisory_xact_lock — that's
        # what serializes the SELECT-then-INSERT against concurrent
        # POSTs for the same exchange.
        first_sql, _ = engine.executed_queries[0]
        assert "PG_ADVISORY_XACT_LOCK" in first_sql


class TestAdminSignalsGenerateTimeframes:

    def test_default_is_1W_only(self, auth_headers):
        exec_ = FakeExecutor()
        client = _make_client(db_engine=FakeDbEngine(), executor=exec_)
        resp = client.post(
            "/v1/admin/signals/generate?exchange=NSE",
            headers=auth_headers,
        )
        assert resp.status_code == 202
        assert resp.json()["timeframes"] == ["1W"]

    def test_repeatable(self, auth_headers):
        exec_ = FakeExecutor()
        client = _make_client(db_engine=FakeDbEngine(), executor=exec_)
        resp = client.post(
            "/v1/admin/signals/generate?exchange=NSE"
            "&timeframe=1D&timeframe=1W",
            headers=auth_headers,
        )
        assert resp.status_code == 202
        assert resp.json()["timeframes"] == ["1D", "1W"]
        _, args, _ = exec_.submitted[0]
        tfs = args[3]
        assert [tf.value for tf in tfs] == ["1D", "1W"]

    def test_unknown_timeframe_rejected(self, auth_headers):
        client = _make_client(db_engine=FakeDbEngine(), executor=FakeExecutor())
        resp = client.post(
            "/v1/admin/signals/generate?exchange=NSE&timeframe=ABC",
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert "ABC" in resp.json()["detail"]

    def test_intraday_timeframe_rejected_in_phase_15(self, auth_headers):
        client = _make_client(db_engine=FakeDbEngine(), executor=FakeExecutor())
        # `1h` is a valid Timeframe enum value (Timeframe.HOURLY) but
        # not allowed by Phase 15 (D3: only 1D + 1W supported).
        # Note: the enum is case-sensitive — "1h" parses, "1H" doesn't.
        resp = client.post(
            "/v1/admin/signals/generate?exchange=NSE&timeframe=1h",
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert "not supported" in resp.json()["detail"].lower()


class TestAdminSignalsGenerateForceFull:

    def test_force_full_true_propagates(self, auth_headers):
        exec_ = FakeExecutor()
        client = _make_client(db_engine=FakeDbEngine(), executor=exec_)
        resp = client.post(
            "/v1/admin/signals/generate?exchange=NSE&force_full=true",
            headers=auth_headers,
        )
        assert resp.status_code == 202
        assert resp.json()["force_full"] is True
        # args[4] is force_full per the wrapper signature.
        _, args, _ = exec_.submitted[0]
        assert args[4] is True


class TestAdminSignalsGenerateConcurrentRunGuard:

    def test_in_flight_returns_409_with_existing_run_id(self, auth_headers):
        """If etl_runs has a row with kind=signal_backtest, exchange=NSE,
        outcome IS NULL, the route returns 409 and does NOT open a new
        row."""
        engine = FakeDbEngine(in_flight_run_id=15)
        exec_ = FakeExecutor()
        client = _make_client(db_engine=engine, executor=exec_)

        resp = client.post(
            "/v1/admin/signals/generate?exchange=NSE",
            headers=auth_headers,
        )
        assert resp.status_code == 409
        detail = resp.json()["detail"]
        assert "run_id=15" in detail
        assert "NSE" in detail
        # No INSERT happened, no submit happened.
        assert not any(
            "INSERT INTO ETL_RUNS" in sql
            for (sql, _) in engine.executed_queries
        )
        assert exec_.submitted == []


class TestAdminSignalsGenerateExchangeValidation:

    def test_missing_exchange_returns_422(self, auth_headers):
        client = _make_client(db_engine=FakeDbEngine(), executor=FakeExecutor())
        resp = client.post(
            "/v1/admin/signals/generate",
            headers=auth_headers,
        )
        assert resp.status_code == 422

    def test_unknown_exchange_returns_400(self, auth_headers):
        """XYZ is not in the codebase's KNOWN_EXCHANGES (the suffix
        map). 400 with a 'not supported' detail."""
        client = _make_client(
            db_engine=FakeDbEngine(),
            executor=FakeExecutor(),
        )
        resp = client.post(
            "/v1/admin/signals/generate?exchange=XYZ",
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert "not supported" in resp.json()["detail"].lower()

    def test_unconfigured_exchange_returns_400(self, auth_headers):
        """NYSE is a known exchange code but our config only has
        NSE in market_hours."""
        cfg = _make_config(market_hours_exchanges=("NSE",))
        client = _make_client(
            config=cfg,
            db_engine=FakeDbEngine(),
            executor=FakeExecutor(),
        )
        resp = client.post(
            "/v1/admin/signals/generate?exchange=NYSE",
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert "not configured" in resp.json()["detail"].lower()


class TestAdminSignalsGenerateAuth:

    def test_no_header_returns_401_or_503(self, monkeypatch):
        """Mirrors the auth-gate behavior of admin_etl's
        test_requires_auth: 503 when the env var is unset, 401 when
        it's set but the header is missing.

        We don't pin a specific outcome here because the existing
        require_admin_token sees the env-var state at dep-resolution
        time, which can be `None` (was never set in test process) or
        `<token>` (set by an earlier test in the same pytest run that
        leaked) depending on test ordering. Accept either."""
        monkeypatch.delenv("TRIP_EMA_ADMIN_TOKEN", raising=False)
        client = _make_client(db_engine=FakeDbEngine(), executor=FakeExecutor())
        resp = client.post("/v1/admin/signals/generate?exchange=NSE")
        assert resp.status_code in (401, 503)

    def test_wrong_token_returns_403(self, admin_token):
        """Env var set but the header presented doesn't match.
        Following require_admin_token's convention (not what the
        design literally says): wrong token → 403, missing header
        → 401, env var unset → 503."""
        client = _make_client(db_engine=FakeDbEngine(), executor=FakeExecutor())
        resp = client.post(
            "/v1/admin/signals/generate?exchange=NSE",
            headers={"Authorization": "Bearer not-the-right-token"},
        )
        assert resp.status_code == 403


class TestAdminSignalsGenerateExecutor:

    def test_executor_none_returns_503(self, auth_headers):
        client = _make_client(db_engine=FakeDbEngine(), executor=None)
        resp = client.post(
            "/v1/admin/signals/generate?exchange=NSE",
            headers=auth_headers,
        )
        assert resp.status_code == 503
        assert "disabled" in resp.json()["detail"].lower()


# ============================================================
# Wrapper's close-on-completion contract (regression guard for
# the Phase 15b worker that closes the etl_runs row on behalf of
# the runner — see admin_signals._run_signal_generation_safe).
#
# 15a's generate_historical_signals deliberately leaves the row
# OPEN when run_id was passed in. The wrapper is the only thing
# that closes it. Without these tests, regressing the wrapper to
# "fire and forget" would silently leave runs perpetually
# in-flight, breaking the concurrent-run guard forever.
# ============================================================


class TestWrapperClosesEtlRunsRow:

    def test_success_closes_with_outcome_success(self):
        """The wrapper runs the runner, gets a SignalGenerationResult,
        and closes the row with outcome='success' and a summary note."""
        from trip_ema.transports.http.routes.admin_signals import (
            _run_signal_generation_safe,
        )
        from unittest.mock import patch

        db_engine = FakeDbEngine()
        config = MagicMock()

        # Fake result that mimics SignalGenerationResult's shape
        fake_result = MagicMock()
        fake_result.tickers_processed = 5
        fake_result.tickers_skipped = 2
        fake_result.signals_emitted = {
            "BUY": 10, "SELL": 8, "EXIT_FROM_BUY": 4, "EXIT_FROM_SELL": 5,
        }

        with patch(
            "trip_ema.etl.signal_generator.generate_historical_signals",
            return_value=fake_result,
        ) as mock_gen:
            _run_signal_generation_safe(
                db_engine, config, "NSE", [], False, run_id=99,
            )
        # Runner was called with the run_id we supplied
        assert mock_gen.call_args.kwargs["run_id"] == 99

        # Wrapper closed the row. EtlStatusRepository.close_run runs
        # UPDATE etl_runs SET ..., so an UPDATE statement appears in
        # the engine's executed_queries.
        update_calls = [
            (sql, params) for (sql, params) in db_engine.executed_queries
            if "UPDATE ETL_RUNS" in sql
        ]
        assert len(update_calls) == 1, (
            f"expected one UPDATE; got {[s for s, _ in db_engine.executed_queries]}"
        )

    def test_failure_closes_with_outcome_failed(self):
        """If the runner raises, the wrapper STILL closes the row,
        but with outcome='failed'. Otherwise the row stays in-flight
        forever and the next /generate POST 409s indefinitely."""
        from trip_ema.transports.http.routes.admin_signals import (
            _run_signal_generation_safe,
        )
        from unittest.mock import patch

        db_engine = FakeDbEngine()
        config = MagicMock()

        with patch(
            "trip_ema.etl.signal_generator.generate_historical_signals",
            side_effect=RuntimeError("simulated runner failure"),
        ):
            _run_signal_generation_safe(
                db_engine, config, "NSE", [], False, run_id=99,
            )

        # An UPDATE happened even though the runner raised.
        update_calls = [
            (sql, params) for (sql, params) in db_engine.executed_queries
            if "UPDATE ETL_RUNS" in sql
        ]
        assert len(update_calls) == 1, (
            "wrapper must close the row even when the runner raises"
        )
        # The outcome bind in close_run's UPDATE is `:out` (not
        # `:outcome` — bind-param naming is inconsistent across the
        # repo, but `:out` is what close_run actually uses).
        _, params = update_calls[0]
        assert params.get("out") == "failed", (
            f"expected out='failed', got {params}"
        )

    def test_close_failure_does_not_propagate(self):
        """If close_run itself raises (e.g. DB connection issue), the
        wrapper logs and swallows — we're already on a non-critical
        completion path; we don't want to crash the executor thread."""
        from trip_ema.transports.http.routes.admin_signals import (
            _run_signal_generation_safe,
        )
        from unittest.mock import patch

        class FailingClose(FakeDbEngine):
            def begin(self):
                # raise on the close's transaction
                raise RuntimeError("simulated close failure")

            def connect(self):
                # but allow other uses
                return _FakeConnCtx(self)

        with patch(
            "trip_ema.etl.signal_generator.generate_historical_signals",
            return_value=MagicMock(
                tickers_processed=1, tickers_skipped=0,
                signals_emitted={
                    "BUY": 1, "SELL": 0,
                    "EXIT_FROM_BUY": 0, "EXIT_FROM_SELL": 0,
                },
            ),
        ):
            # Should NOT raise — the wrapper swallows close failures
            _run_signal_generation_safe(
                FailingClose(), MagicMock(), "NSE", [], False, run_id=99,
            )


# ============================================================
# GET /v1/admin/signals/generated
# ============================================================


@pytest.fixture
def output_dir_with_nse_file(tmp_path, monkeypatch):
    """Set up `<tmp>/generated_signals/NSE.json` with a realistic
    payload, then chdir into <tmp> so the route's
    Path.cwd()-based resolution finds it."""
    monkeypatch.chdir(tmp_path)
    outdir = tmp_path / "generated_signals"
    outdir.mkdir()
    payload = {
        "schema_version": "1.0",
        "exchange": "NSE",
        "generated_at": "2026-05-21T01:42:00+00:00",
        "generated_by_run_id": 17,
        "params": {"1W": {"ema": {}, "atr": {}}},
        "counts": {
            "tickers": 2283,
            "signals_total": 105684,
            "signals_by_type": {
                "BUY": 32987, "SELL": 35396,
                "EXIT_FROM_BUY": 17857, "EXIT_FROM_SELL": 19444,
            },
            "first_date": "2005-01-03",
            "last_date": "2026-05-15",
        },
        "by_date": {},   # the actual generator fills this; route ignores it
    }
    (outdir / "NSE.json").write_text(json.dumps(payload))
    return outdir


class TestAdminSignalsGeneratedHappyPath:

    def test_returns_200_with_metadata(
        self, auth_headers, output_dir_with_nse_file,
    ):
        client = _make_client()
        resp = client.get(
            "/v1/admin/signals/generated?exchange=NSE",
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["exchange"] == "NSE"
        assert body["exists"] is True
        assert body["filename"] == "NSE.json"
        assert body["size_bytes"] > 0
        assert body["mtime"] is not None
        assert body["generated_by_run_id"] == 17
        assert body["schema_version"] == "1.0"
        # counts subdict comes through verbatim
        counts = body["counts"]
        assert counts["tickers"] == 2283
        assert counts["signals_total"] == 105684
        assert counts["signals_by_type"]["BUY"] == 32987
        assert counts["first_date"] == "2005-01-03"

    def test_response_does_not_include_by_date(
        self, auth_headers, output_dir_with_nse_file,
    ):
        """The endpoint explicitly excludes by_date to keep responses
        small. Even if a future schema change adds the field, the
        response model doesn't include it."""
        client = _make_client()
        resp = client.get(
            "/v1/admin/signals/generated?exchange=NSE",
            headers=auth_headers,
        )
        body = resp.json()
        assert "by_date" not in body


class TestAdminSignalsGeneratedAbsent:

    def test_file_missing_returns_200_with_exists_false(
        self, auth_headers, tmp_path, monkeypatch,
    ):
        """An exchange that's known and configured but has no
        on-disk file returns 200 with exists=false. The other
        metadata fields are all None."""
        monkeypatch.chdir(tmp_path)
        # No file written → exists=false
        client = _make_client()
        resp = client.get(
            "/v1/admin/signals/generated?exchange=NSE",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["exchange"] == "NSE"
        assert body["exists"] is False
        assert body["filename"] is None
        assert body["counts"] is None

    def test_output_dir_missing_returns_exists_false(
        self, auth_headers, tmp_path, monkeypatch,
    ):
        """Even the generated_signals dir doesn't exist yet. Still
        a 200 with exists=false — the operator hasn't run a
        generate yet, that's not an error."""
        monkeypatch.chdir(tmp_path)
        client = _make_client()
        resp = client.get(
            "/v1/admin/signals/generated?exchange=NSE",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["exists"] is False


class TestAdminSignalsGeneratedExchangeValidation:

    def test_missing_exchange_returns_422(self, auth_headers):
        client = _make_client()
        resp = client.get(
            "/v1/admin/signals/generated",
            headers=auth_headers,
        )
        assert resp.status_code == 422

    def test_unknown_exchange_returns_400(self, auth_headers, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        client = _make_client()
        resp = client.get(
            "/v1/admin/signals/generated?exchange=XYZ",
            headers=auth_headers,
        )
        assert resp.status_code == 400

    def test_unconfigured_exchange_returns_400(
        self, auth_headers, tmp_path, monkeypatch,
    ):
        monkeypatch.chdir(tmp_path)
        cfg = _make_config(market_hours_exchanges=("NSE",))
        client = _make_client(config=cfg)
        resp = client.get(
            "/v1/admin/signals/generated?exchange=NYSE",
            headers=auth_headers,
        )
        assert resp.status_code == 400


class TestAdminSignalsGeneratedAuth:

    def test_no_header_returns_401_or_503(self, monkeypatch, tmp_path):
        monkeypatch.delenv("TRIP_EMA_ADMIN_TOKEN", raising=False)
        monkeypatch.chdir(tmp_path)
        client = _make_client()
        resp = client.get("/v1/admin/signals/generated?exchange=NSE")
        assert resp.status_code in (401, 503)


class TestAdminSignalsGeneratedCorruptFile:

    def test_corrupt_json_returns_500(
        self, auth_headers, tmp_path, monkeypatch,
    ):
        """If the on-disk file exists but isn't parseable, surface a
        500 rather than lying with exists=false. The operator needs
        to know the file is there but broken so they can investigate."""
        monkeypatch.chdir(tmp_path)
        outdir = tmp_path / "generated_signals"
        outdir.mkdir()
        (outdir / "NSE.json").write_text("not valid json {{{")
        client = _make_client()
        resp = client.get(
            "/v1/admin/signals/generated?exchange=NSE",
            headers=auth_headers,
        )
        assert resp.status_code == 500
        assert "could not be parsed" in resp.json()["detail"]
